#!/usr/bin/env python3
"""SessionStart hook — records an event and prints an agent-visible preamble."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Ensure `swarm` package importable
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from swarmd.lib.paths import ensure_session_dirs  # noqa: E402
from swarmd.specialists.event_scribe import emit_event  # noqa: E402


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    session_id = payload.get("session_id") or os.environ.get("SESSION_ID", "")
    if not session_id:
        # Fall through silently — we have no session context
        return 0

    ensure_session_dirs(session_id)
    emit_event(session_id=session_id, hook="SessionStart")

    preamble = (
        "⚠️ This session is being observed by the swarm harness. "
        "You are running under a coordination layer that: "
        "(1) injects corrective guidance when it detects drift, thrashing, "
        "or fabricated progress; "
        "(2) verifies the mission's success criteria independently; "
        "(3) NEVER allows you to stop for input — only `mission_complete` "
        "verdicts from the completion_judge permit a natural stop. "
        "The human is not present. Keep working until the mission is "
        "verifiably complete."
    )
    # additionalContext is one standard way Claude Code accepts SessionStart output.
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": preamble,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
