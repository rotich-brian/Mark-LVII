"""
Sports scores - live, recent and upcoming football (soccer) results.

WHY A PLUGIN, NOT A WEB SEARCH
    The assistant's web_search needs Gemini quota and a search engine that will
    talk to it; both failed when asked for a score. A scores API answers in
    milliseconds with structured data and needs neither.

SOURCES, IN ORDER
    1. Your own endpoint, if you set one (plugin settings -> Scores -> Endpoint).
       GET <endpoint> with the query appended as ?q=<text>. It may answer JSON
       with a "summary" string (spoken as is) or any plain text.
    2. ESPN's public scoreboard (no key): scanned across the main competitions
       (current window, then single days around today), all in parallel.
    3. TheSportsDB (the free "123" test key): a search by "A vs B", or a team's
       last results.

SECURITY
    The endpoint URL comes only from the user's own settings; it must be http(s).
    Replies are size-limited and are treated as DATA - spoken, never obeyed.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

NAMESPACE = "sports_scores"
_UA = {"User-Agent": "Mozilla/5.0 (JARVIS scores plugin)"}
_MAX_BYTES = 400_000
_ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"
_TSDB = "https://www.thesportsdb.com/api/v1/json/123/"

# Competitions scanned for a score. Not exhaustive; a slug ESPN does not know
# just returns nothing and is skipped.
_SLUGS = (
    "caf.nations_qual", "caf.nations", "fifa.worldq.caf", "fifa.friendly",
    "fifa.world", "fifa.worldq.uefa", "fifa.worldq.conmebol",
    "fifa.worldq.concacaf", "uefa.nations", "uefa.euro", "uefa.euroq",
    "conmebol.america", "concacaf.gold", "caf.champions", "caf.confed",
    "uefa.champions", "uefa.europa", "uefa.europa.conf", "conmebol.libertadores",
    "eng.1", "eng.2", "eng.fa", "eng.league_cup", "esp.1", "esp.copa_del_rey",
    "ger.1", "ita.1", "fra.1", "ned.1", "por.1", "sco.1", "tur.1", "bra.1",
    "arg.1", "usa.1", "mex.1", "rsa.1", "nga.1", "ken.1", "egy.1", "mar.1",
)

_LEAGUE_NAMES = {
    "eng.1": "Premier League", "eng.2": "Championship", "eng.fa": "FA Cup",
    "eng.league_cup": "League Cup", "esp.1": "La Liga", "ger.1": "Bundesliga",
    "ita.1": "Serie A", "fra.1": "Ligue 1", "ned.1": "Eredivisie",
    "por.1": "Primeira Liga", "sco.1": "Scottish Premiership", "tur.1": "Super Lig",
    "bra.1": "Brasileirao", "arg.1": "Argentine Primera", "usa.1": "MLS",
    "mex.1": "Liga MX", "rsa.1": "South African Premiership",
    "nga.1": "Nigerian Premier League", "ken.1": "Kenyan Premier League",
    "egy.1": "Egyptian Premier League", "mar.1": "Botola Pro",
    "uefa.champions": "Champions League", "uefa.europa": "Europa League",
    "uefa.europa.conf": "Conference League", "caf.champions": "CAF Champions League",
    "caf.confed": "CAF Confederation Cup", "conmebol.libertadores": "Copa Libertadores",
    "esp.copa_del_rey": "Copa del Rey",
}

PLUGIN = {
    "name": "sports_scores",
    "description": (
        "Football (soccer) scores: live, final and upcoming. Use for 'what is "
        "the score of Nigeria vs Tanzania', 'did Arsenal win', 'who is playing "
        "today', 'South Africa result'. Pass the team name(s) in 'query'. This "
        "is the FIRST choice for any sports score - do not use web_search or "
        "the browser for scores. Reports what the data says, including whether "
        "the match is finished, live or not yet played."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {"type": "STRING",
                      "description": "Team(s) or match, e.g. 'Nigeria vs Tanzania' or 'Arsenal'."},
        },
        "required": ["query"],
    },
    "tier": "extra",
    "brief": "Look up football scores: live, final, upcoming.",
}

PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "Scores",
    "fields": [
        {"key": "endpoint", "label": "Your scores endpoint (optional)", "type": "text",
         "placeholder": "https://example.com/scores  (called with ?q=<query>)"},
        {"key": "endpoint_key", "label": "Endpoint API key (optional)", "type": "password",
         "placeholder": "sent as the X-API-Key header"},
    ],
}


# ── helpers ───────────────────────────────────────────────────────────────────
def _get_json(url: str, headers: dict | None = None, timeout: float = 12.0):
    req = urllib.request.Request(url, headers={**_UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError("reply too large")
    return json.loads(raw.decode("utf-8", "replace"))


def _clip(text, n: int = 300) -> str:
    return " ".join(str(text or "").split())[:n]


def _norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", str(s).lower())
    s = re.sub(r"\b(fc|cf|afc|sc|the|national team|men|mens)\b", " ", s)
    return " ".join(s.split())


def _teams_from_query(q: str) -> list[str]:
    parts = re.split(r"\s+(?:vs\.?|v\.?|versus|against|and|-)\s+", q.strip(), flags=re.I)
    return [t for t in (_norm(p) for p in parts) if t]


def _matches(team_norm: str, name: str) -> bool:
    n = _norm(name)
    return bool(team_norm) and (team_norm == n or team_norm in n or n in team_norm)


def _when(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return d.strftime("%a %d %b, %H:%M")
    except Exception:
        return ""


# ── ESPN ──────────────────────────────────────────────────────────────────────
def _espn_events(slug: str, dates: str | None) -> list[dict]:
    try:
        d = _get_json(_ESPN.format(slug=slug) + (f"?dates={dates}" if dates else ""), timeout=10)
    except Exception:
        return []
    league = ((d.get("leagues") or [{}])[0].get("name")) or slug
    out = []
    for e in d.get("events") or []:
        try:
            c = e["competitions"][0]
            comps = c["competitors"]
            home = next(x for x in comps if x.get("homeAway") == "home")
            away = next(x for x in comps if x.get("homeAway") == "away")
            st = e["status"]["type"]
            out.append({
                "league": league, "date": e.get("date", ""),
                "home": home["team"]["displayName"], "away": away["team"]["displayName"],
                "hs": home.get("score"), "as": away.get("score"),
                "state": st.get("state"), "detail": st.get("shortDetail") or st.get("description"),
                "clock": e["status"].get("displayClock"),
            })
        except Exception:
            continue
    return out


def _describe(ev: dict) -> str:
    h, a = ev["home"], ev["away"]
    state = ev.get("state")
    if state == "pre":
        return f"{h} v {a} has not started yet: kick-off {_when(ev['date'])} ({ev['league']})."
    score = f"{h} {ev['hs']}, {a} {ev['as']}"
    if state == "in":
        return f"LIVE: {score}, {ev.get('detail') or ev.get('clock') or 'in play'} ({ev['league']})."
    return f"{score}, full time ({ev['league']}, {_when(ev['date'])[:10]})."


def _espn_scan(teams: list[str], dates: str | None) -> list[dict]:
    with ThreadPoolExecutor(max_workers=12) as pool:
        batches = list(pool.map(lambda sl: _espn_events(sl, dates), _SLUGS))
    hits = []
    for evs in batches:
        for ev in evs:
            names = (ev["home"], ev["away"])
            if not teams or all(any(_matches(t, n) for n in names) for t in teams):
                hits.append(ev)
    return hits


def _espn_lookup(teams: list[str]) -> list[dict]:
    # ESPN rejects date RANGES (HTTP 400) but takes one day. First pass: each
    # competition's default scoreboard, which already covers the current window.
    # Only if that has nothing for these teams, try single days around today.
    hits = _espn_scan(teams, None)
    if not hits:
        today = datetime.now(timezone.utc).date()
        for off in (-1, 1, 2, -2):
            hits = _espn_scan(teams, f"{today + timedelta(days=off):%Y%m%d}")
            if hits:
                break
    order = {"in": 0, "post": 1, "pre": 2}
    hits.sort(key=lambda e: (order.get(e.get("state"), 3), e.get("date", "")))
    return hits


# ── TheSportsDB ───────────────────────────────────────────────────────────────
def _tsdb_lookup(query: str, teams: list[str]) -> str | None:
    try:
        if len(teams) >= 2:
            q = urllib.parse.quote(f"{teams[0]} vs {teams[1]}".replace(" ", "_"))
            evs = (_get_json(_TSDB + f"searchevents.php?e={q}").get("event") or [])
        else:
            tid = ((_get_json(_TSDB + "searchteams.php?t=" + urllib.parse.quote(query))
                    .get("teams") or [{}])[0].get("idTeam"))
            evs = (_get_json(_TSDB + f"eventslast.php?id={tid}").get("results") or []) if tid else []
    except Exception:
        return None
    evs = [e for e in evs if e.get("strSport") == "Soccer"]
    evs.sort(key=lambda e: e.get("dateEvent") or "", reverse=True)
    if not evs:
        return None
    e = evs[0]
    hs, as_ = e.get("intHomeScore"), e.get("intAwayScore")
    day = e.get("dateEvent") or ""
    if hs is None or as_ is None:
        return f"{_clip(e.get('strEvent'))} is listed for {day} with no score yet."
    return (f"{_clip(e.get('strHomeTeam'))} {hs}, {_clip(e.get('strAwayTeam'))} {as_}, "
            f"{day} ({_clip(e.get('strLeague'), 60)}).")


# ── custom endpoint ───────────────────────────────────────────────────────────
def _custom(query: str) -> str | None:
    try:
        from memory.config_manager import get_plugin_config
        cfg = get_plugin_config(NAMESPACE)
    except Exception:
        return None
    url = str(cfg.get("endpoint") or "").strip()
    if not url:
        return None
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return "Your scores endpoint must start with http or https."
    key = str(cfg.get("endpoint_key") or "").strip()
    sep = "&" if "?" in url else "?"
    try:
        data = _get_json(url + sep + "q=" + urllib.parse.quote(query),
                         headers={"X-API-Key": key} if key else None)
    except Exception as e:
        return f"__endpoint_failed__ {_clip(e, 80)}"
    if isinstance(data, dict):
        for k in ("summary", "text", "result", "message"):
            if data.get(k):
                return _clip(data[k], 400)
    return _clip(json.dumps(data), 400) if data else None


# ── entry point ───────────────────────────────────────────────────────────────
def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        result = _run(_clip((parameters or {}).get("query"), 120))
    except Exception as e:
        result = f"Scores lookup failed: {_clip(e, 100)}"
    if player:
        try:
            player.write_log(f"JARVIS: {result}")
        except Exception:
            pass
    return result


_NEXT_RE = re.compile(r"\b(next|upcoming|fixtures?|schedule|when|plays?|playing)\b", re.I)
_NOISE = re.compile(r"\b(next|upcoming|fixtures?|schedule|when|is|are|does|do|plays?|playing|"
                    r"match(es)?|game(s)?|the|their|his|her|for|of)\b", re.I)


def _team_in_league(slug: str, team_norm: str):
    try:
        d = _get_json(_ESPN.replace("/scoreboard", "/teams").format(slug=slug), timeout=10)
        for t in d["sports"][0]["leagues"][0]["teams"]:
            t = t["team"]
            if _matches(team_norm, t.get("displayName", "")):
                return slug, t["id"], t["displayName"]
    except Exception:
        pass
    return None


def _upcoming(team_norm: str) -> list[dict]:
    """The team's next fixtures: ESPN's per-team schedule in its own league, plus
    anything upcoming in the current scoreboards (cups, Europe)."""
    out = [e for e in _espn_scan([team_norm], None) if e.get("state") == "pre"]
    with ThreadPoolExecutor(max_workers=12) as pool:
        found = [r for r in pool.map(lambda sl: _team_in_league(sl, team_norm), _SLUGS) if r]
    for slug, tid, _name in found[:5]:
        try:
            d = _get_json(_ESPN.replace("/scoreboard", f"/teams/{tid}/schedule").format(slug=slug)
                          + "?fixture=true", timeout=10)
        except Exception:
            continue
        league = _LEAGUE_NAMES.get(slug) or ((d.get("league") or {}).get("name")) or slug
        for e in d.get("events") or []:
            try:
                c = e["competitions"][0]
                if c["status"]["type"]["state"] != "pre":
                    continue
                comps = c["competitors"]
                home = next(x for x in comps if x.get("homeAway") == "home")
                away = next(x for x in comps if x.get("homeAway") == "away")
                out.append({"league": league, "date": e.get("date", ""), "state": "pre",
                            "home": home["team"]["displayName"], "away": away["team"]["displayName"]})
            except Exception:
                continue
    seen, uniq = set(), []
    for e in sorted(out, key=lambda e: e.get("date", "")):
        key = (e["date"][:10], e["home"], e["away"])
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return uniq


def _run(query: str) -> str:
    if not query:
        return "Which team or match?"
    if _NEXT_RE.search(query):
        names = _teams_from_query(_NOISE.sub(" ", query))
        if len(names) == 1 and names[0]:
            nxt = _upcoming(names[0])
            if nxt:
                lines = [f"{e['home']} v {e['away']}, {_when(e['date'])} ({e['league']})."
                         for e in nxt[:3]]
                return "Next fixtures: " + " ".join(lines)
    teams = _teams_from_query(query)

    mine = _custom(query)
    if mine and not mine.startswith("__endpoint_failed__"):
        return mine

    hits = _espn_lookup(teams)
    if hits:
        lines = [_describe(e) for e in hits[:4]]
        return " ".join(lines)

    other = _tsdb_lookup(query, teams)
    if other:
        return other
    return (f"I found no recent or upcoming match for '{query}' in the scores data "
            f"(checked today's window and the days either side). Try the full team name.")
