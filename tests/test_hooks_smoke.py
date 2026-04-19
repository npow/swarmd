"""Smoke tests for the hook scripts — invoke them as subprocesses with mock payloads."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / "swarm" / "hooks"


def _run_hook(
    script: str, payload: dict, *, env: dict | None = None
) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    full_env["PYTHONPATH"] = str(REPO_ROOT)
    full_env["PEER_CONSULT_DISABLED"] = "1"
    if env:
        full_env.update(env)
    return subprocess.run(
        ["python3", str(HOOKS_DIR / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=full_env,
        timeout=30,
    )


def test_session_start_emits_event(tmp_swarm_root, session_id):
    r = _run_hook(
        "on_session_start.py",
        {"session_id": session_id, "cwd": str(tmp_swarm_root), "hook_event_name": "SessionStart"},
    )
    assert r.returncode == 0, r.stderr
    # Hook prints additionalContext JSON
    out = json.loads(r.stdout)
    assert "hookSpecificOutput" in out
    assert "observed" in out["hookSpecificOutput"]["additionalContext"].lower()

    from swarmd.specialists.event_scribe import read_events

    events = read_events(session_id)
    assert any(e.hook == "SessionStart" for e in events)


def test_post_tool_use_emits_event(tmp_swarm_root, session_id):
    r = _run_hook(
        "on_post_tool_use.py",
        {
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file": "foo.py", "old": "x", "new": "y"},
            "tool_response": {"ok": True},
        },
    )
    assert r.returncode == 0, r.stderr
    from swarmd.specialists.event_scribe import read_events

    events = read_events(session_id)
    assert any(e.tool_name == "Edit" for e in events)


def _seed_mission(session_id, workspace):
    """Seed a minimal mission.yaml so the Stop hook treats this as a swarm session."""
    import yaml as _yaml

    from swarmd.lib.paths import mission_yaml_path

    p = mission_yaml_path(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        _yaml.safe_dump(
            {
                "mission": "test",
                "workspace": str(workspace),
                "success_criteria": [
                    {"id": "ok", "description": "", "check": "true"}
                ],
            }
        )
    )


def test_stop_hook_passes_through_when_no_mission(tmp_swarm_root, session_id):
    """If session has no mission.yaml, Stop hook is a no-op (not a block)."""
    r = _run_hook(
        "on_stop.py",
        {"session_id": session_id, "hook_event_name": "Stop"},
    )
    assert r.returncode == 0, r.stderr
    # No output (passes through)
    assert r.stdout.strip() == ""


def test_stop_hook_blocks_when_no_completion(tmp_swarm_root, session_id, tmp_path):
    _seed_mission(session_id, tmp_path)
    r = _run_hook(
        "on_stop.py",
        {"session_id": session_id, "hook_event_name": "Stop"},
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["decision"] == "block"
    assert "reason" in out
    assert len(out["reason"]) > 0


def test_stop_hook_allows_on_mission_complete(tmp_swarm_root, session_id, tmp_path):
    _seed_mission(session_id, tmp_path)
    # Seed a mission_complete intervention
    from swarmd.lib.locking import write_line
    from swarmd.lib.paths import interventions_path
    from swarmd.schemas.intervention import Intervention

    iv = Intervention(
        id="i-complete",
        tier="mission_complete",
        reason="all criteria pass",
    )
    write_line(interventions_path(session_id), iv.model_dump_json())

    r = _run_hook(
        "on_stop.py",
        {"session_id": session_id, "hook_event_name": "Stop"},
    )
    assert r.returncode == 0, r.stderr
    # On mission complete, hook prints additionalContext (not a block decision)
    out = json.loads(r.stdout)
    # Must NOT be a block
    assert out.get("decision") != "block"
