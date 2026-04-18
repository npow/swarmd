"""Swarm statusline — one-liner formatter + CLI entry point.

Invoked by Claude Code's statusLine hook on every refresh. Must exit
quickly (<500ms); no network, no subprocess, stdlib file I/O only.
"""

from __future__ import annotations

import dataclasses
import json
import math
import sys

from swarm.lib.status import SessionSnapshot


def _sanitize_for_json(obj):
    """Replace math.inf/-inf with None so json.dumps stays RFC 8259 compliant."""
    if isinstance(obj, float) and math.isinf(obj):
        return None
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    return obj


_MAX_LINE = 200
_MAX_SID_DISPLAY = 8  # first 8 chars of the session_id


def _hms(sec: float) -> str:
    sec = int(sec)
    m, s = divmod(sec, 60)
    return f"{m}:{s:02d}"


def _duration_compact(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    m = sec // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    return f"{h}h{m % 60}m"


def format_line(snap: SessionSnapshot) -> str:
    """Render a one-line status string for a snapshot.

    Returns an empty string for a truly empty session so Claude Code's
    statusline goes blank rather than showing zeros.
    """
    # Empty-session short-circuit
    if (
        not snap.mission_title
        and not snap.criteria
        and snap.findings_total == 0
        and snap.interventions_total == 0
        and not snap.launcher_alive
    ):
        return ""

    parts: list[str] = []
    if not snap.launcher_alive:
        parts.append("[ended]")

    sid = snap.session_id[:_MAX_SID_DISPLAY]
    parts.append(f"swarm {sid}:")

    n_pass = sum(1 for c in snap.criteria if c.status == "pass")
    n_total = len(snap.criteria)
    if snap.all_pass and snap.hold_sec >= snap.hold_target_sec and snap.hold_target_sec > 0:
        parts.append("MISSION ✓")
    else:
        parts.append(f"{n_pass}/{n_total}")

    if snap.all_pass and not (snap.hold_sec >= snap.hold_target_sec):
        parts.append(f"hold {_hms(snap.hold_sec)}")

    if snap.iter_count > 0:
        parts.append(f"iter {snap.iter_count}")

    if snap.interventions_pending_ack > 0:
        parts.append(f"· {snap.interventions_pending_ack} pending ack")

    if snap.duration_sec > 0:
        parts.append(f"· {_duration_compact(snap.duration_sec)}")

    line = " ".join(parts)
    if len(line) > _MAX_LINE:
        line = line[: _MAX_LINE - 1] + "…"
    return line


def main(argv: list[str]) -> int:
    """CLI entry.
      swarm-statusline <session_id>    → format for that session
      swarm-statusline --auto          → pick most-recent session
      swarm-statusline --auto --json   → emit full SessionSnapshot as JSON
    """
    wants_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]

    sid: str | None = None
    if argv and argv[0] == "--auto":
        sid = SessionSnapshot.find_most_recent()
    elif len(argv) >= 2 and argv[0] == "--session":
        sid = argv[1]
    elif argv and argv[0] != "--session":
        sid = argv[0]

    if sid is None:
        # No session to report. Print empty line (statusline goes blank).
        print("")
        return 0

    try:
        snap = SessionSnapshot.load(sid)
    except Exception:
        # Catches ValueError (bad sid), PermissionError, and any future
        # exception from status.py — hook must never spew tracebacks.
        print("")
        return 0

    if wants_json:
        # Convert to dict for JSON; dataclass values must be JSON-safe
        def _default(o):
            # Finding / Intervention are pydantic — model_dump
            if hasattr(o, "model_dump"):
                return o.model_dump(mode="json")
            raise TypeError(f"unserializable: {type(o)}")

        d = dataclasses.asdict(snap)
        print(json.dumps(_sanitize_for_json(d), default=_default, allow_nan=False))
        return 0

    print(format_line(snap))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
