"""
Spotify control — search Spotify and play it in the LOCAL desktop app.

HOW IT WORKS
    * Search:   Spotify Web API, "client credentials" flow. That needs only a free
                developer app's Client ID + Secret (no user login, no OAuth redirect).
                It can look music up; it cannot control playback by itself.
    * Playback: the desktop Spotify app's MPRIS interface, driven with `playerctl`.
                `playerctl -p spotify open spotify:track:<id>` starts that track.
    * Launch:   `flatpak run com.spotify.Client` if Spotify is not running yet.

SECURITY
    * No shell anywhere: every external command is an argument list.
    * A URI is only ever passed to playerctl after matching a strict pattern.
    * Credentials live in config/api_keys.json (plugin_config.spotify_control), set
      from the app's plugin settings. Nothing is hardcoded.
    * Names returned by Spotify are data, not instructions; they are length-limited
      before being spoken.

NEEDS
    sudo dnf install playerctl           (the app is Flatpak: com.spotify.Client)
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time

NAMESPACE = "spotify_control"
PLAYER = "spotify"
FLATPAK_ID = "com.spotify.Client"

_URI_RE = re.compile(r"^spotify:(track|album|playlist|artist):[A-Za-z0-9]{10,32}$")
_KINDS = ("track", "album", "playlist")

PLUGIN = {
    "name": "spotify_control",
    "description": (
        "Play music on the user's Spotify desktop app and control it. Use for "
        "'play <song/artist/album/playlist> on Spotify', 'play some <artist>', "
        "'pause/resume the music', 'next/previous song', 'skip', 'volume 40', "
        "'what's playing'. For a request like 'play Burna Boy' use action=play "
        "with query='Burna Boy' and kind=track (an artist name finds that "
        "artist's popular tracks). Use kind=album or kind=playlist only when the "
        "user says album or playlist. Do NOT use for YouTube or local video "
        "(use video_player / youtube_video) and do NOT use open_app just to start "
        "music; this tool launches Spotify itself."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "play | pause | resume | next | previous | volume | now_playing | open",
            },
            "query": {"type": "STRING", "description": "What to search for (play only)."},
            "kind": {"type": "STRING", "description": "track (default) | album | playlist"},
            "volume": {"type": "INTEGER", "description": "0-100 (volume only)."},
        },
        "required": ["action"],
    },
    "tier": "extra",
    "brief": "Play and control music in the Spotify desktop app.",
}

PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "Spotify",
    "fields": [
        {"key": "client_id", "label": "Client ID", "type": "text",
         "placeholder": "from developer.spotify.com/dashboard"},
        {"key": "client_secret", "label": "Client secret", "type": "password",
         "placeholder": "from the same Spotify app"},
    ],
}

_token: dict = {"value": "", "expires": 0.0}


# ── helpers ───────────────────────────────────────────────────────────────────
def _clip(text, n: int = 80) -> str:
    return " ".join(str(text or "").split())[:n]


def _playerctl(*args: str, timeout: int = 8) -> subprocess.CompletedProcess:
    return subprocess.run(["playerctl", "-p", PLAYER, *args],
                          capture_output=True, text=True, timeout=timeout)


def _spotify_running() -> bool:
    try:
        r = subprocess.run(["playerctl", "-l"], capture_output=True, text=True, timeout=5)
        return any(line.strip().startswith(PLAYER) for line in r.stdout.splitlines())
    except Exception:
        return False


def _ensure_spotify() -> str | None:
    """Make sure Spotify is running. Returns an error sentence, or None when ready."""
    if _spotify_running():
        return None
    if shutil.which("flatpak") is None and shutil.which("spotify") is None:
        return "Spotify does not seem to be installed."
    cmd = ["flatpak", "run", FLATPAK_ID] if shutil.which("flatpak") else ["spotify"]
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return f"I could not start Spotify: {_clip(e)}"
    for _ in range(40):                      # up to ~20 s for the window and MPRIS
        time.sleep(0.5)
        if _spotify_running():
            time.sleep(1.5)                  # let it finish loading before commands
            return None
    return "I started Spotify but it never became ready. Is it signed in?"


def _credentials() -> tuple[str, str]:
    from memory.config_manager import get_plugin_config
    cfg = get_plugin_config(NAMESPACE)
    return str(cfg.get("client_id") or "").strip(), str(cfg.get("client_secret") or "").strip()


def _get_token() -> str:
    import requests
    if _token["value"] and time.time() < _token["expires"] - 30:
        return _token["value"]
    cid, secret = _credentials()
    if not cid or not secret:
        raise RuntimeError("no_credentials")
    r = requests.post("https://accounts.spotify.com/api/token",
                      data={"grant_type": "client_credentials"},
                      auth=(cid, secret), timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"token_{r.status_code}")
    data = r.json()
    _token["value"] = data["access_token"]
    _token["expires"] = time.time() + float(data.get("expires_in", 3600))
    return _token["value"]


def _search(query: str, kind: str) -> tuple[str, str] | None:
    """-> (uri, spoken name) of the best match, or None."""
    import requests
    r = requests.get("https://api.spotify.com/v1/search",
                     params={"q": query[:200], "type": kind, "limit": 1},
                     headers={"Authorization": f"Bearer {_get_token()}"}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"search_{r.status_code}")
    items = (r.json().get(f"{kind}s") or {}).get("items") or []
    items = [i for i in items if isinstance(i, dict)]
    if not items:
        return None
    it = items[0]
    uri = str(it.get("uri") or "")
    if not _URI_RE.match(uri):
        return None
    name = _clip(it.get("name"))
    if kind == "track":
        artists = ", ".join(_clip(a.get("name"), 40) for a in (it.get("artists") or [])[:2]
                            if isinstance(a, dict))
        if artists:
            name = f"{name} by {artists}"
    return uri, name


# ── entry point ───────────────────────────────────────────────────────────────
def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        result = _run(parameters or {})
    except Exception as e:                   # never raise into the voice loop
        result = f"Spotify failed: {_clip(e)}"
    if player:
        try:
            player.write_log(f"JARVIS: {result}")
        except Exception:
            pass
    return result


def _run(p: dict) -> str:
    action = str(p.get("action") or "").strip().lower()
    if shutil.which("playerctl") is None:
        return ("playerctl is not installed, so I cannot control Spotify. "
                "Run: sudo dnf install playerctl")

    if action in ("open", "launch", "start"):
        err = _ensure_spotify()
        return err or "Spotify is open."

    if action in ("pause", "resume", "next", "previous", "volume", "now_playing"):
        if not _spotify_running():
            return "Spotify is not running."
        if action == "pause":
            _playerctl("pause");  return "Paused."
        if action == "resume":
            _playerctl("play");   return "Resumed."
        if action == "next":
            _playerctl("next");   return "Skipped to the next track."
        if action == "previous":
            _playerctl("previous"); return "Going back a track."
        if action == "volume":
            try:
                v = max(0, min(100, int(p.get("volume"))))
            except (TypeError, ValueError):
                return "What volume, from 0 to 100?"
            _playerctl("volume", f"{v / 100:.2f}")
            return f"Spotify volume set to {v} percent."
        r = _playerctl("metadata", "--format", "{{artist}} - {{title}}")
        text = _clip(r.stdout, 120)
        return f"Now playing: {text}." if text and r.returncode == 0 else "Nothing is playing."

    if action == "play":
        query = _clip(p.get("query"), 200)
        if not query:
            _playerctl("play")               # no query: just resume
            return "Resumed."
        kind = str(p.get("kind") or "track").strip().lower()
        if kind not in _KINDS:
            kind = "track"
        try:
            found = _search(query, kind)
        except RuntimeError as e:
            msg = str(e)
            if msg == "no_credentials":
                return ("Spotify search is not set up. Add your Spotify Client ID and "
                        "secret in the plugin settings.")
            if msg.startswith("token_"):
                return "Spotify rejected the Client ID and secret. Please check them."
            return f"Spotify search failed ({msg})."
        except Exception as e:
            return f"I could not reach Spotify search: {_clip(e)}"
        if not found:
            return f"I could not find {kind} '{query}' on Spotify."
        uri, name = found
        err = _ensure_spotify()
        if err:
            return err
        r = _playerctl("open", uri)
        if r.returncode != 0:
            return f"Spotify would not play it: {_clip(r.stderr) or 'unknown error'}"
        return f"Playing {name}."

    return "I did not understand that Spotify request."
