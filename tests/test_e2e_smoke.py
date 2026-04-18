"""End-to-end smoke test: exercise the whole v0 loop without spawning Claude.

We simulate hook invocations, run specialists as in-process functions, and verify
the intervention pipeline produces a `mission_complete` when a mission is met.
"""

from __future__ import annotations

import json
from pathlib import Path

from swarm.lib.locking import write_line
from swarm.lib.paths import (
    findings_path,
    mission_lock_path,
    mission_yaml_path,
    out_of_tree_lock_path,
    session_dir,
)
from swarm.schemas.finding import Finding
from swarm.schemas.mission import Mission
from swarm.specialists.completion_judge import judge
from swarm.specialists.coordinator import make_intervention_for
from swarm.specialists.success_verifier import run_all_checks, verify_tamper


def _write_mission(session_id: str, workspace: Path) -> Mission:
    import yaml

    m = Mission.model_validate(
        {
            "mission": "smoke test",
            "workspace": str(workspace),
            "success_criteria": [
                {"id": "ok", "description": "", "check": "true", "timeout_sec": 5},
            ],
            "verification": {"run_every_sec": 1, "hold_window_sec": 1},
        }
    )
    path = mission_yaml_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(json.loads(m.model_dump_json())))
    return m


def _write_lock(session_id: str) -> None:
    from swarm.lib.hashing import sha256_file
    from swarm.schemas.lock import MissionLock

    path = mission_yaml_path(session_id)
    lock = MissionLock(
        session_id=session_id,
        locked_at="2026-04-16T00:00:00Z",
        files={"mission.yaml": sha256_file(path)},
    )
    mission_lock_path(session_id).write_text(lock.model_dump_json(indent=2))
    out_of_tree_lock_path(session_id).parent.mkdir(parents=True, exist_ok=True)
    out_of_tree_lock_path(session_id).write_text(
        json.dumps(lock.files, sort_keys=True)
    )


def test_mission_complete_happy_path(tmp_swarm_root, session_id, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    m = _write_mission(session_id, workspace)
    _write_lock(session_id)

    # 1. No tamper
    assert verify_tamper(session_id) is None

    # 2. Checks all pass (trivial `true`)
    results = run_all_checks(session_id, m)
    assert all(r.status == "pass" for r in results.values())

    # 3. Simulate verifier emitting hold_window_met + writing verifier_status
    (session_dir(session_id) / "verifier_status.json").write_text(
        json.dumps({"ts": 0, "all_pass": True, "per_criterion": {}})
    )
    hold = Finding(
        id="f-hold",
        source="success_verifier.hold_window_met",
        subject_session=session_id,
        spawner_id=session_id,
        type="verification",
        subtype="hold_window_met",
        severity="major",
    )
    write_line(findings_path(session_id), hold.model_dump_json())

    # 4. completion_judge should return complete
    v = judge(session_id)
    assert v.verdict == "complete", v.outstanding

    # 5. Coordinator should produce a mission_complete intervention
    iv = make_intervention_for(session_id, hold)
    assert iv is not None
    assert iv.tier == "mission_complete"


def test_tamper_detection(tmp_swarm_root, session_id, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_mission(session_id, workspace)
    _write_lock(session_id)

    # Now tamper with mission.yaml
    mission_path = mission_yaml_path(session_id)
    mission_path.write_text(mission_path.read_text() + "\n# tampered\n")

    finding = verify_tamper(session_id)
    assert finding is not None
    assert finding.type == "meta"
    assert finding.subtype == "tamper_detected"


def test_cheat_blocks_completion(tmp_swarm_root, session_id, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_mission(session_id, workspace)
    _write_lock(session_id)

    (session_dir(session_id) / "verifier_status.json").write_text(
        json.dumps({"ts": 0, "all_pass": True, "per_criterion": {}})
    )
    # Add hold_window AND a cheat finding
    write_line(
        findings_path(session_id),
        Finding(
            id="f-hold",
            source="success_verifier.hold_window_met",
            subject_session=session_id,
            spawner_id=session_id,
            type="verification",
            subtype="hold_window_met",
            severity="major",
        ).model_dump_json(),
    )
    write_line(
        findings_path(session_id),
        Finding(
            id="f-cheat",
            source="anticheat",
            subject_session=session_id,
            spawner_id=session_id,
            type="cheat",
            subtype="scope_reduction",
            severity="critical",
        ).model_dump_json(),
    )

    v = judge(session_id)
    assert v.verdict == "cheat_suspected"
