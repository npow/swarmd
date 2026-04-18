"""OS desktop notification delivery with four fallbacks.

Injection-safety: ALL user-controlled strings flow through subprocess argv,
never through a shell or an AppleScript `-e` source string. See the osascript
fallback below for the specific pattern.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

_TITLE_MAX = 100
_BODY_MAX = 400
_SUBPROCESS_TIMEOUT_SEC = 5

_OSASCRIPT_TEMPLATE = (
    "on run argv\n"
    "    set t to item 1 of argv\n"
    "    set b to item 2 of argv\n"
    "    display notification b with title t\n"
    "end run\n"
)


def _truncate(s: str, limit: int) -> str:
    if s is None:
        return ""
    s = str(s)
    return s[:limit] if len(s) > limit else s


def _which(name: str) -> str | None:
    return shutil.which(name)


def _try_terminal_notifier(title: str, body: str) -> bool:
    path = _which("terminal-notifier")
    if not path:
        return False
    try:
        subprocess.run(
            [path, "-title", title, "-message", body],
            timeout=_SUBPROCESS_TIMEOUT_SEC,
            check=False,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _try_osascript(title: str, body: str) -> bool:
    """Safe osascript pattern: script via stdin, title/body via argv.
    The `-e` flag is NEVER used with user-controlled strings."""
    path = _which("osascript")
    if not path:
        return False
    try:
        subprocess.run(
            [path, "-", title, body],
            input=_OSASCRIPT_TEMPLATE,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SEC,
            check=False,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _try_notify_send(title: str, body: str) -> bool:
    path = _which("notify-send")
    if not path:
        return False
    try:
        subprocess.run(
            [path, title, body],
            timeout=_SUBPROCESS_TIMEOUT_SEC,
            check=False,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _stderr_fallback(title: str, body: str) -> bool:
    sys.stderr.write(f"[swarm-notify] {title}: {body}\n")
    sys.stderr.flush()
    return True


def notify(title: str, body: str) -> bool:
    """Send a desktop notification. Truncates title/body, then tries
    backends in order: terminal-notifier → osascript → notify-send → stderr.
    Returns True on any successful delivery (including stderr fallback)."""
    title = _truncate(title, _TITLE_MAX)
    body = _truncate(body, _BODY_MAX)
    for backend in (_try_terminal_notifier, _try_osascript, _try_notify_send):
        if backend(title, body):
            return True
    return _stderr_fallback(title, body)
