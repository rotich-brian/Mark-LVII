"""
core/waylandinput.py - typing and key presses on Wayland, with the user's permission.

WHY
    pyautogui sends X11 events. Under Wayland those reach only Xwayland windows
    (and not even those without X authorisation), so typing into a native
    Wayland app does nothing, while the tool reports success.

    The sanctioned route is the desktop portal org.freedesktop.portal.RemoteDesktop.
    GNOME shows a permission dialog the first time. The approval is remembered
    (a restore token, kept in config/wayland_input.json, mode 600) so later
    launches are silent.

SCOPE
    Keyboard only: text, single keys, key chords. Mouse input needs a second
    portal (a screen stream for absolute positions) and is not done here.

SAFETY
    Nothing happens until the first key is sent, so the dialog never appears
    unprompted at startup. Everything fails soft: a refusal, a timeout or a
    missing library returns an explanation and callers fall back to what they
    did before. The permission can be revoked in GNOME Settings -> Privacy.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
_TOKEN_FILE = BASE_DIR / "config" / "wayland_input.json"
_PORTAL = dict(bus_name="org.freedesktop.portal.Desktop",
               object_path="/org/freedesktop/portal/desktop")
_RD = "org.freedesktop.portal.RemoteDesktop"

_lock = threading.RLock()
_conn = None
_session: Optional[str] = None
_failed_until = 0.0
_last_reason = ""


def is_wayland() -> bool:
    return (os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
            or bool(os.environ.get("WAYLAND_DISPLAY")))


def last_reason() -> str:
    return _last_reason


# ── key names -> X keysyms ────────────────────────────────────────────────────
_NAMED = {
    "enter": 0xff0d, "return": 0xff0d, "tab": 0xff09, "esc": 0xff1b, "escape": 0xff1b,
    "backspace": 0xff08, "delete": 0xffff, "del": 0xffff, "space": 0x20,
    "home": 0xff50, "left": 0xff51, "up": 0xff52, "right": 0xff53, "down": 0xff54,
    "pageup": 0xff55, "pgup": 0xff55, "pagedown": 0xff56, "pgdn": 0xff56,
    "end": 0xff57, "insert": 0xff63, "capslock": 0xffe5,
    "ctrl": 0xffe3, "ctrlleft": 0xffe3, "control": 0xffe3, "shift": 0xffe1,
    "shiftleft": 0xffe1, "alt": 0xffe9, "altleft": 0xffe9, "option": 0xffe9,
    "win": 0xffeb, "winleft": 0xffeb, "command": 0xffeb, "super": 0xffeb,
}
for _n in range(1, 13):
    _NAMED[f"f{_n}"] = 0xffbd + _n          # F1 = 0xffbe


def keysym_for(key: str) -> Optional[int]:
    """The X keysym for a pyautogui-style key name or a single character."""
    k = str(key)
    if k.lower() in _NAMED:
        return _NAMED[k.lower()]
    if len(k) == 1:
        c = ord(k)
        if c == 10:
            return 0xff0d
        if c == 9:
            return 0xff09
        if 0x20 <= c <= 0x7e or 0xa0 <= c <= 0xff:
            return c
        if c >= 0x100:
            return 0x01000000 + c            # Unicode keysym
    return None


# ── portal plumbing ───────────────────────────────────────────────────────────
def _load_token() -> str:
    try:
        return str(json.loads(_TOKEN_FILE.read_text()).get("restore_token") or "")
    except Exception:
        return ""


def _save_token(token: str) -> None:
    try:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(json.dumps({"restore_token": token}))
        os.chmod(_TOKEN_FILE, 0o600)
    except Exception:
        pass


def _request(conn, method: str, signature: str, args: tuple, options: dict,
             timeout: float):
    """Call a portal method that answers through a Response signal.
    Returns (code, results) or raises."""
    from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
    from jeepney.io.blocking import Proxy

    token = "jarvis_" + secrets.token_hex(6)
    sender = conn.unique_name.lstrip(":").replace(".", "_")
    req_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request",
                     member="Response", path=req_path)
    Proxy(message_bus, conn).AddMatch(rule)
    opts = {"handle_token": ("s", token), **options}
    addr = DBusAddress(_PORTAL["object_path"], bus_name=_PORTAL["bus_name"], interface=_RD)
    conn.send_and_get_reply(new_method_call(addr, method, signature, (*args, opts)), timeout=10)
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(f"{method}: no answer (was the permission dialog ignored?)")
        sig = conn.receive(timeout=left)
        if sig.header.fields.get(1) == req_path:
            return sig.body


def _open_session(timeout: float) -> tuple[bool, str]:
    global _conn, _session
    try:
        from jeepney.io.blocking import open_dbus_connection
    except Exception:
        return False, "the jeepney library is not installed"
    try:
        conn = open_dbus_connection(bus="SESSION")
    except Exception as e:
        return False, f"cannot reach the desktop bus ({e})"
    try:
        code, res = _request(conn, "CreateSession", "a{sv}", (), {
            "session_handle_token": ("s", "jarvis_" + secrets.token_hex(4))}, 10)
        if code != 0:
            return False, "the desktop refused to create an input session"
        session = res["session_handle"][1]

        sel = {"types": ("u", 1), "persist_mode": ("u", 2)}      # 1 = keyboard
        tok = _load_token()
        if tok:
            sel["restore_token"] = ("s", tok)
        code, _ = _request(conn, "SelectDevices", "oa{sv}", (session,), sel, 10)
        if code != 0:
            return False, "the desktop refused the keyboard device request"

        code, res = _request(conn, "Start", "osa{sv}", (session, ""), {}, timeout)
        if code != 0:
            return False, ("permission was declined" if code == 1
                           else "permission was not granted")
        new_tok = (res.get("restore_token") or ("s", ""))[1]
        if new_tok:
            _save_token(new_tok)
        _conn, _session = conn, session
        return True, ""
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return False, str(e)


def ensure(timeout: float = 60.0) -> bool:
    """Make sure an input session is open. May show GNOME's permission dialog the
    first time, so only call it when a key is about to be sent."""
    global _failed_until, _last_reason
    if not is_wayland():
        _last_reason = "not a Wayland session"
        return False
    with _lock:
        if _session is not None:
            return True
        if time.monotonic() < _failed_until:
            return False
        ok, why = _open_session(timeout)
        _last_reason = "" if ok else why
        if not ok:
            _failed_until = time.monotonic() + 120     # do not nag
        return ok


def _notify_key(keysym: int, down: bool) -> None:
    from jeepney import DBusAddress, new_method_call
    addr = DBusAddress(_PORTAL["object_path"], bus_name=_PORTAL["bus_name"], interface=_RD)
    msg = new_method_call(addr, "NotifyKeyboardKeysym", "oa{sv}iu",
                          (_session, {}, int(keysym), 1 if down else 0))
    _conn.send_and_get_reply(msg, timeout=5)


def _drop_session() -> None:
    global _conn, _session
    try:
        if _conn is not None:
            _conn.close()
    except Exception:
        pass
    _conn = _session = None


def _send(keysyms_and_state: list[tuple[int, bool]], gap: float = 0.0) -> bool:
    global _failed_until
    if not ensure():
        return False
    with _lock:
        try:
            for sym, down in keysyms_and_state:
                _notify_key(sym, down)
                if gap:
                    time.sleep(gap)
            return True
        except Exception as e:                      # session died: retry once
            _drop_session()
            if not ensure():
                return False
            try:
                for sym, down in keysyms_and_state:
                    _notify_key(sym, down)
                return True
            except Exception:
                _drop_session()
                _failed_until = time.monotonic() + 60
                return False


# ── public API ────────────────────────────────────────────────────────────────
def _typeable(ch: str) -> bool:
    """Characters sent as key symbols. Only what the active keyboard layout has
    can be sent that way (verified: accents and CJK were silently dropped), so
    only plain ASCII, newline and tab qualify."""
    return ch in "\n\t" or 0x20 <= ord(ch) <= 0x7e


def _paste(text: str) -> bool:
    """Put text on the clipboard and press Ctrl+V, then restore the clipboard.
    The way to enter characters the keyboard layout does not have."""
    import shutil
    import subprocess
    if not (shutil.which("wl-copy") and shutil.which("wl-paste")):
        return False
    old = None
    try:
        r = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, timeout=2)
        if r.returncode == 0:
            old = r.stdout
    except Exception:
        pass
    try:
        subprocess.run(["wl-copy", "--", text], timeout=3, check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        time.sleep(0.08)
        ok = hotkey("ctrl", "v")
        time.sleep(0.3)                   # let the app read it before we restore
        return ok
    except Exception:
        return False
    finally:
        if old is not None:
            try:
                subprocess.run(["wl-copy"], input=old, timeout=3,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass


def type_text(text: str, interval: float = 0.006) -> bool:
    """Type text into whatever has keyboard focus."""
    text = str(text)
    ok = True
    i = 0
    while i < len(text):
        j = i
        direct = _typeable(text[i])
        while j < len(text) and _typeable(text[j]) == direct:
            j += 1
        run = text[i:j]
        if direct:
            seq: list[tuple[int, bool]] = []
            for ch in run:
                sym = keysym_for(ch)
                if sym is not None:
                    seq += [(sym, True), (sym, False)]
            ok = (_send(seq, gap=interval) if seq else True) and ok
        else:
            ok = _paste(run) and ok
        i = j
    return ok


def press(key: str) -> bool:
    sym = keysym_for(key)
    if sym is None:
        return False
    return _send([(sym, True), (sym, False)], gap=0.01)


def key_down(key: str) -> bool:
    sym = keysym_for(key)
    return False if sym is None else _send([(sym, True)])


def key_up(key: str) -> bool:
    sym = keysym_for(key)
    return False if sym is None else _send([(sym, False)])


def hotkey(*keys: str) -> bool:
    syms = [keysym_for(k) for k in keys]
    if not syms or any(s is None for s in syms):
        return False
    seq = [(s, True) for s in syms] + [(s, False) for s in reversed(syms)]
    return _send(seq, gap=0.01)


# ── pyautogui bridge ──────────────────────────────────────────────────────────
_installed = False


def install() -> bool:
    """Route pyautogui's keyboard functions through the portal on Wayland.

    The originals stay as the fallback, so a refusal or a missing library leaves
    behaviour exactly as it was. Mouse functions are untouched."""
    global _installed
    if _installed or not is_wayland():
        return False
    try:
        import pyautogui
    except Exception:
        return False                      # no X authorisation: nothing to patch
    orig = {n: getattr(pyautogui, n) for n in
            ("write", "typewrite", "press", "hotkey", "keyDown", "keyUp")}

    def _write(message, interval=0.0, *a, **k):
        if type_text(str(message), interval=max(0.004, float(interval or 0))):
            return None
        return orig["write"](message, interval, *a, **k)

    def _press(keys, presses=1, interval=0.0, *a, **k):
        names = [keys] if isinstance(keys, str) else list(keys)
        ok = True
        for _ in range(int(presses)):
            for n in names:
                ok = press(n) and ok
            if interval:
                time.sleep(interval)
        return None if ok else orig["press"](keys, presses, interval, *a, **k)

    def _hotkey(*keys, **k):
        if hotkey(*keys):
            return None
        return orig["hotkey"](*keys, **k)

    def _down(key, *a, **k):
        return None if key_down(key) else orig["keyDown"](key, *a, **k)

    def _up(key, *a, **k):
        return None if key_up(key) else orig["keyUp"](key, *a, **k)

    pyautogui.write = pyautogui.typewrite = _write
    pyautogui.press, pyautogui.hotkey = _press, _hotkey
    pyautogui.keyDown, pyautogui.keyUp = _down, _up
    _installed = True
    return True
