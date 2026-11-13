"""
core/waylandshot.py - screen capture that works on Wayland.

WHY
    mss and pyautogui grab the screen the X11 way. Under Wayland that returns a
    frame of pure black (measured: every pixel 0), so the assistant "looks at the
    screen" and truthfully reports that it can see nothing. GNOME also refuses
    direct calls to its own screenshot service ("Screenshot is not allowed").

    The sanctioned route is the desktop portal, org.freedesktop.portal.Screenshot.
    GNOME asks the user once to allow it; after that it captures without asking.

HOW
    Subscribe to the request's Response signal, call Screenshot(), wait for the
    answer, read the PNG the portal wrote, then delete it - the portal saves into
    ~/Pictures, and an assistant that quietly piles up screenshots of the
    desktop there is a privacy leak of its own.

    Only used when the session is Wayland. Everything fails soft: any problem
    returns None and callers fall back to what they did before.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

_lock = threading.Lock()
_denied_until = 0.0          # after a refusal, do not nag for a while
_DENY_BACKOFF = 120.0


def is_wayland() -> bool:
    return (os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
            or bool(os.environ.get("WAYLAND_DISPLAY")))


def grab_png(timeout: float = 30.0) -> Optional[bytes]:
    """The whole desktop as PNG bytes, or None when it cannot be had."""
    global _denied_until
    if not is_wayland() or time.monotonic() < _denied_until:
        return None
    try:
        from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
        from jeepney.io.blocking import Proxy, open_dbus_connection
    except Exception:
        return None

    with _lock:                                   # one portal request at a time
        try:
            conn = open_dbus_connection(bus="SESSION")
        except Exception:
            return None
        try:
            token = "jarvis_" + secrets.token_hex(6)
            # The request object's path is predictable, so subscribe BEFORE the
            # call: a fast answer cannot slip past.
            sender = conn.unique_name.lstrip(":").replace(".", "_")
            req_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
            rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request",
                             member="Response", path=req_path)
            Proxy(message_bus, conn).AddMatch(rule)

            portal = DBusAddress("/org/freedesktop/portal/desktop",
                                 bus_name="org.freedesktop.portal.Desktop",
                                 interface="org.freedesktop.portal.Screenshot")
            msg = new_method_call(portal, "Screenshot", "sa{sv}", (
                "", {"handle_token": ("s", token), "interactive": ("b", False)}))
            conn.send_and_get_reply(msg, timeout=10)

            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                try:
                    sig = conn.receive(timeout=left)
                except Exception:
                    return None
                if sig.header.fields.get(1) != req_path:   # 1 = PATH
                    continue
                code, results = sig.body
                break
            if code != 0:                          # 1 = cancelled, 2 = denied
                _denied_until = time.monotonic() + _DENY_BACKOFF
                return None
            uri = results.get("uri", ("s", ""))[1]
            path = Path(unquote(urlparse(uri).path))
            if not path.is_file():
                return None
            data = path.read_bytes()
            try:
                path.unlink()                      # do not leave desktop shots behind
            except OSError:
                pass
            return data
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass


def grab_image():
    """The whole desktop as a PIL image, or None."""
    data = grab_png()
    if not data:
        return None
    try:
        import io

        from PIL import Image
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None
