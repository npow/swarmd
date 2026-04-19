#!/usr/bin/env python3
"""Stop hook — the heart of 'never stop until done'.

- If a `mission_complete` intervention is pending: ack it and let the stop proceed.
- Otherwise: drain pending interventions, add a peer-consult verdict, and block.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from swarmd.lib.interventions import ack_all, read_pending  # noqa: E402
from swarmd.lib.paths import ensure_session_dirs, mission_yaml_path  # noqa: E402
from swarmd.specialists.event_scribe import emit_event  # noqa: E402


def _mission_prose(session_id: str) -> str:
    """Return just the `mission:` prose string, not the raw YAML."""
    p = mission_yaml_path(session_id)
    if not p.exists():
        return ""
    try:
        import yaml as _yaml

        data = _yaml.safe_load(p.read_text()) or {}
        return str(data.get("mission", ""))
    except Exception:
        return ""


def _peer_consult(mission_text: str, session_id: str) -> str:
    """Call `claude -p --bare --model opus` with the mission and transcript; return verdict text."""
    # Conservative: if PEER_CONSULT_DISABLED env set, skip — used in tests.
    if os.environ.get("PEER_CONSULT_DISABLED"):
        return "PEER_DISABLED"
    prompt = (
        "You are an independent peer consulted on whether an agent should stop. "
        "The mission is:\n"
        "<<<MISSION>>>\n" + mission_text + "\n<<<END_MISSION>>>\n\n"
        "Given the agent's current state, are all success criteria verifiably "
        "met NOW? If yes, reply exactly: DONE\n"
        "If no, name the first unmet criterion and give ONE concrete next action. "
        "Never rubber-stamp. Be adversarial if there is any doubt."
    )
    try:
        proc = subprocess.run(
            ["claude", "-p", "--bare", "--model", "opus"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
        return f"PEER_ERROR: rc={proc.returncode} stderr={proc.stderr[-500:]}"
    except FileNotFoundError:
        return "PEER_UNAVAILABLE"
    except subprocess.TimeoutExpired:
        return "PEER_TIMEOUT"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    session_id = payload.get("session_id") or os.environ.get("SESSION_ID", "")
    if not session_id:
        return 0

    # PASS-THROUGH for non-swarm sessions: if there is no mission.yaml for this
    # session_id, this is not a swarm-managed session — the hook must not block.
    # This handles the case where settings.json is installed in a project but
    # the user is running an interactive session, not a launched mission.
    if not mission_yaml_path(session_id).exists():
        return 0

    ensure_session_dirs(session_id)
    emit_event(session_id=session_id, hook="Stop")

    pending = read_pending(session_id)

    complete = next((iv for iv in pending if iv.tier == "mission_complete"), None)
    if complete is not None:
        ack_all(session_id, pending, "stop_on_complete")
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "Stop",
                        "additionalContext": (
                            "Mission complete: " + complete.reason
                        ),
                    }
                }
            )
        )
        return 0

    # Compose a blocking reason. Always include at least the swarm's default
    # message so the worker has something concrete even when there are no
    # pending interventions and the peer is unavailable.
    reasons: list[str] = [
        "You were about to stop, but the mission is not verifiably complete. "
        "Continue working toward the success criteria. Stop ONLY when you "
        "see an intervention tagged MISSION_COMPLETE."
    ]
    for iv in pending:
        reasons.append(f"[{iv.tier.upper()}] {iv.reason}")

    # Pass only the mission prose to the peer, not the raw YAML config blocks
    mission_text = _mission_prose(session_id)
    peer_verdict = _peer_consult(mission_text, session_id)
    if peer_verdict.strip().startswith("DONE"):
        reasons.append(
            "Peer reports DONE but the completion_judge has NOT confirmed. "
            "The verifier is authoritative."
        )
    elif peer_verdict and peer_verdict not in {
        "PEER_DISABLED",
        "PEER_UNAVAILABLE",
        "PEER_TIMEOUT",
    } and not peer_verdict.startswith("PEER_ERROR"):
        reasons.append(f"Peer verdict: {peer_verdict[:1000]}")

    ack_all(session_id, pending, "stop_blocked")

    print(
        json.dumps(
            {"decision": "block", "reason": "\n\n".join(reasons)},
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
