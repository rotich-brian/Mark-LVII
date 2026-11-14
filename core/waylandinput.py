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
    Keyboard: text, single keys, key chords (its own session).
    Mouse: moving, clicking, double/right/middle click, scrolling (a second
    session, opened on first use). The portal's absolute positioning needs a
    screen stream that this GNOME does not return, so the pointer is placed with
    relative moves: homed to a known point by clamping against the monitor
    edges, then moved a measured distance. Measured on this machine: no pointer
    acceleration, and landing error 0.0 px across both monitors.

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
def _load_token(key: str = "restore_token") -> str:
    try:
        return str(json.loads(_TOKEN_FILE.read_text()).get(key) or "")
    except Exception:
        return ""


def _save_token(token: str, key: str = "restore_token") -> None:
    try:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(_TOKEN_FILE.read_text())
        except Exception:
            data = {}
        data[key] = token
        _TOKEN_FILE.write_text(json.dumps(data))
        os.chmod(_TOKEN_FILE, 0o600)
    except Exception:
        pass


_SC = "org.freedesktop.portal.ScreenCast"


def _request(conn, method: str, signature: str, args: tuple, options: dict,
             timeout: float, iface: str = _RD):
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
    addr = DBusAddress(_PORTAL["object_path"], bus_name=_PORTAL["bus_name"], interface=iface)
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


# ── mouse ─────────────────────────────────────────────────────────────────────
# The portal's absolute positioning needs a screen stream, and on this GNOME the
# RemoteDesktop reply carried none (verified: both call orders). So positioning
# is done with RELATIVE motion: send the pointer to the desktop's top-left
# corner, then move it a measured distance. That relies on the compositor
# applying no pointer acceleration to these virtual moves - measured, see
# calibrate(). A bonus: no screen stream means no "screen is shared" indicator.
_pconn = None
_psession: Optional[str] = None
_pfailed_until = 0.0
_pos: Optional[tuple] = None            # where we believe the pointer is (logical px)
_BTN = {"left": 272, "right": 273, "middle": 274}
_SLAM = 40000.0


def _open_pointer_session(timeout: float) -> tuple[bool, str]:
    global _pconn, _psession
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
            return False, "the desktop refused to create a pointer session"
        session = res["session_handle"][1]
        sel = {"types": ("u", 2), "persist_mode": ("u", 2)}       # 2 = pointer
        tok = _load_token("pointer_restore_token")
        if tok:
            sel["restore_token"] = ("s", tok)
        code, _ = _request(conn, "SelectDevices", "oa{sv}", (session,), sel, 10)
        if code != 0:
            return False, "the desktop refused the pointer device request"
        code, res = _request(conn, "Start", "osa{sv}", (session, ""), {}, timeout)
        if code != 0:
            return False, ("permission was declined" if code == 1
                           else "permission was not granted")
        new_tok = (res.get("restore_token") or ("s", ""))[1]
        if new_tok:
            _save_token(new_tok, "pointer_restore_token")
        _pconn, _psession = conn, session
        return True, ""
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return False, str(e)


def ensure_pointer(timeout: float = 90.0) -> bool:
    global _pfailed_until, _last_reason
    if not is_wayland():
        _last_reason = "not a Wayland session"
        return False
    with _lock:
        if _psession is not None:
            return True
        if time.monotonic() < _pfailed_until:
            return False
        ok, why = _open_pointer_session(timeout)
        _last_reason = "" if ok else why
        if not ok:
            _pfailed_until = time.monotonic() + 120
        return ok


def _drop_pointer() -> None:
    global _pconn, _psession, _pos
    try:
        if _pconn is not None:
            _pconn.close()
    except Exception:
        pass
    _pconn = _psession = _pos = None


def _pcall(method: str, sig: str, args: tuple) -> None:
    from jeepney import DBusAddress, new_method_call
    addr = DBusAddress(_PORTAL["object_path"], bus_name=_PORTAL["bus_name"], interface=_RD)
    # The session path leads every call, so the signature starts with "o".
    _pconn.send_and_get_reply(new_method_call(addr, method, "o" + sig, (_psession, {}, *args)), timeout=5)


def _rel(dx: float, dy: float) -> None:
    _pcall("NotifyPointerMotion", "a{sv}dd", (float(dx), float(dy)))


def _mouse(fn) -> bool:
    """Run fn() against the pointer session; reopen once if it died."""
    global _pfailed_until
    if not ensure_pointer():
        return False
    with _lock:
        for attempt in (0, 1):
            try:
                fn()
                return True
            except Exception:
                _drop_pointer()
                if attempt or not ensure_pointer():
                    _pfailed_until = time.monotonic() + 60
                    return False
    return False


# Layout-aware positioning. The pointer is confined to the monitors, and monitors
# need not line up (here: one starts at y=351, the other at x=1920, so the
# desktop's corner (0,0) is on neither). We model that confinement, find a
# sequence of axis-aligned slams that ends at the SAME point from any start
# ("homing"), then plan a path from there to the target, checking it in the
# model. Only axis-aligned moves are used, which clamp predictably.
_STEP = 250.0


def _screen_rects() -> list:
    """Monitor rectangles (x, y, w, h) in logical pixels."""
    try:
        from PyQt6.QtGui import QGuiApplication
        if QGuiApplication.instance() is not None:
            r = [(g.x(), g.y(), g.width(), g.height())
                 for g in (sc.geometry() for sc in QGuiApplication.screens())]
            if r:
                return r
    except Exception:
        pass
    try:
        import pyautogui
        w, h = pyautogui.size()
        return [(0, 0, int(w), int(h))]
    except Exception:
        return [(0, 0, 1920, 1080)]


def _inside(rects, x: int, y: int) -> bool:
    return any(rx <= x < rx + rw and ry <= y < ry + rh for rx, ry, rw, rh in rects)


def _sim_axis(rects, pos, dx: float, dy: float):
    """Where an axis-aligned move ends, stopping at the edge of the monitors."""
    x, y = int(pos[0]), int(pos[1])
    if dx:
        stepdir = 1 if dx > 0 else -1
        for _ in range(int(min(abs(dx), 20000))):
            if not _inside(rects, x + stepdir, y):
                break
            x += stepdir
    if dy:
        stepdir = 1 if dy > 0 else -1
        for _ in range(int(min(abs(dy), 20000))):
            if not _inside(rects, x, y + stepdir):
                break
            y += stepdir
    return (x, y)


def _run_sim(rects, pos, moves):
    for dx, dy in moves:
        pos = _sim_axis(rects, pos, dx, dy)
    return pos


def _homing(rects):
    """(moves, home_point): a slam sequence that ends at one point from any start."""
    big = _SLAM
    span = max((ry + rh for _x, ry, _w, rh in rects), default=1080)
    kick = float(span) + 100
    cands = [
        [(-big, 0), (0, -big)],
        [(-big, 0), (0, -big), (0, kick), (-big, 0), (0, -big)],
        [(0, -big), (-big, 0), (0, kick), (-big, 0), (0, -big)],
        [(-big, 0), (0, -big), (0, kick), (-big, 0), (0, -big), (kick, 0), (-big, 0), (0, -big)],
    ]
    starts = [(rx + fx * (rw - 1), ry + fy * (rh - 1))
              for rx, ry, rw, rh in rects for fx in (0, 0.5, 1) for fy in (0, 0.5, 1)]
    for seq in cands:
        ends = {_run_sim(rects, st, seq) for st in starts}
        if len(ends) == 1:
            return seq, ends.pop()
    return cands[0], _run_sim(rects, starts[0], cands[0])


def _plan(rects, home, target):
    """Axis-aligned moves from home to target that the model says arrive."""
    tx, ty = int(round(target[0])), int(round(target[1]))
    dx, dy = tx - home[0], ty - home[1]
    options = [[(dx, 0), (0, dy)], [(0, dy), (dx, 0)]]
    # Via each monitor's own corner, for layouts where neither L-shape connects.
    for rx, ry, rw, rh in rects:
        for cx, cy in ((rx + 2, ry + 2), (rx + rw - 3, ry + 2), (rx + 2, ry + rh - 3)):
            options.append([(cx - home[0], 0), (0, cy - home[1]),
                            (tx - cx, 0), (0, ty - cy)])
            options.append([(0, cy - home[1]), (cx - home[0], 0),
                            (0, ty - cy), (tx - cx, 0)])
    for mv in options:
        if _run_sim(rects, home, mv) == (tx, ty):
            return mv
    return None


def _send_moves(moves) -> None:
    for dx, dy in moves:
        left = abs(dx) if abs(dx) < _SLAM else _SLAM
        stepdir = 1 if (dx or dy) and (dx > 0 or dy > 0) else -1
        if abs(dx) >= _SLAM or abs(dy) >= _SLAM:
            _rel(dx, dy)
            time.sleep(0.02)
            continue
        total = abs(dx) or abs(dy)
        sent = 0.0
        while sent < total:
            d = min(_STEP, total - sent)
            _rel(stepdir * d if dx else 0, stepdir * d if dy else 0)
            sent += d


def move_to(x: float, y: float) -> bool:
    """Put the pointer at logical desktop position (x, y)."""
    rects = _screen_rects()
    if not _inside(rects, int(round(x)), int(round(y))):
        # Clamp to the nearest monitor, as a real pointer would be.
        best = None
        for rx, ry, rw, rh in rects:
            cx, cy = min(max(x, rx), rx + rw - 1), min(max(y, ry), ry + rh - 1)
            d = (cx - x) ** 2 + (cy - y) ** 2
            if best is None or d < best[0]:
                best = (d, cx, cy)
        x, y = best[1], best[2]
    seq, home = _homing(rects)
    path = _plan(rects, home, (x, y))
    if path is None:
        return False
    def go():
        global _pos
        _send_moves(seq)
        time.sleep(0.03)
        _send_moves(path)
        time.sleep(0.03)
        _pos = (float(x), float(y))
    return _mouse(go)


def _button(button: str, down: bool) -> None:
    _pcall("NotifyPointerButton", "a{sv}iu", (_BTN.get(button, 272), 1 if down else 0))


def mouse_down(button: str = "left") -> bool:
    return _mouse(lambda: _button(button, True))


def mouse_up(button: str = "left") -> bool:
    return _mouse(lambda: _button(button, False))


def click(x=None, y=None, clicks: int = 1, button: str = "left") -> bool:
    if x is not None and y is not None:
        if not move_to(x, y):
            return False
        time.sleep(0.12)                    # let the pointer arrive and hover settle
    elif _pos is None:
        return False                         # nowhere known to click
    for i in range(max(1, int(clicks))):
        if not (mouse_down(button) and (time.sleep(0.04) or True) and mouse_up(button)):
            return False
        if i + 1 < clicks:
            time.sleep(0.08)
    return True


def scroll(amount: int) -> bool:
    """Positive scrolls up, negative down (pyautogui's convention)."""
    steps = -int(amount)                    # the portal: positive = down
    return _mouse(lambda: _pcall("NotifyPointerAxisDiscrete", "a{sv}ui", (0, steps)))


# ── pyautogui bridge ──────────────────────────────────────────────────────────
_installed = False


def install() -> bool:
    """Route pyautogui's keyboard functions through the portal on Wayland.

    The originals stay as the fallback, so a refusal or a missing library leaves
    behaviour exactly as it was. The mouse session only opens on the first mouse
    call, so keyboard-only use never shows the screen-share indicator."""
    global _installed
    if _installed or not is_wayland():
        return False
    try:
        import pyautogui
    except Exception:
        return False                      # no X authorisation: nothing to patch
    orig = {n: getattr(pyautogui, n) for n in
            ("write", "typewrite", "press", "hotkey", "keyDown", "keyUp",
             "moveTo", "click", "doubleClick", "rightClick", "middleClick",
             "mouseDown", "mouseUp", "scroll")}

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

    def _xy(x, y):
        if isinstance(x, (tuple, list)) and len(x) == 2 and y is None:
            return x[0], x[1]
        return x, y

    def _moveTo(x=None, y=None, *a, **k):
        x, y = _xy(x, y)
        if x is not None and y is not None and move_to(x, y):
            return None
        return orig["moveTo"](x, y, *a, **k)

    def _click(x=None, y=None, clicks=1, interval=0.0, button="left", *a, **k):
        x, y = _xy(x, y)
        if click(x, y, clicks, str(button)):
            return None
        return orig["click"](x, y, clicks, interval, button, *a, **k)

    def _double(x=None, y=None, *a, **k):
        x, y = _xy(x, y)
        return None if click(x, y, 2, "left") else orig["doubleClick"](x, y, *a, **k)

    def _right(x=None, y=None, *a, **k):
        x, y = _xy(x, y)
        return None if click(x, y, 1, "right") else orig["rightClick"](x, y, *a, **k)

    def _middle(x=None, y=None, *a, **k):
        x, y = _xy(x, y)
        return None if click(x, y, 1, "middle") else orig["middleClick"](x, y, *a, **k)

    def _mdown(x=None, y=None, button="left", *a, **k):
        x, y = _xy(x, y)
        if (x is None or move_to(x, y)) and mouse_down(str(button)):
            return None
        return orig["mouseDown"](x, y, button, *a, **k)

    def _mup(x=None, y=None, button="left", *a, **k):
        x, y = _xy(x, y)
        if (x is None or move_to(x, y)) and mouse_up(str(button)):
            return None
        return orig["mouseUp"](x, y, button, *a, **k)

    def _scroll(clicks, x=None, y=None, *a, **k):
        x, y = _xy(x, y)
        if (x is None or move_to(x, y)) and scroll(int(clicks)):
            return None
        return orig["scroll"](clicks, x, y, *a, **k)

    pyautogui.moveTo, pyautogui.click = _moveTo, _click
    pyautogui.doubleClick, pyautogui.rightClick = _double, _right
    pyautogui.middleClick, pyautogui.mouseDown = _middle, _mdown
    pyautogui.mouseUp, pyautogui.scroll = _mup, _scroll
    pyautogui.write = pyautogui.typewrite = _write
    pyautogui.press, pyautogui.hotkey = _press, _hotkey
    pyautogui.keyDown, pyautogui.keyUp = _down, _up
    _installed = True
    return True
