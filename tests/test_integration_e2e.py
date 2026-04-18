"""True end-to-end integration test.

Exercises the full pipeline with real events, real findings, real
coordinator logic, and real completion_judge — NO mocks at the contract
seams. Only the LLM runners themselves are mocked (because we can't run
real LLMs in unit tests).

What this proves:
  1. An Edit event emitted by the hook can be read by pattern_detector
  2. A loop pattern triggers a correct-tier intervention through the
     coordinator + intervention_judge
  3. A verifier pass-transition + hold_window → completion_judge → mission_complete
  4. The scope_shrinker catches deflection in assistant text
  5. A cheat finding blocks mission_complete
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from swarm.hooks.on_post_tool_use import _summarize_input, _summarize_response
from swarm.lib.hashing import sha256_file
from swarm.lib.locking import write_line
from swarm.lib.paths import (
    claude_transcript_path,
    findings_path,
    mission_lock_path,
    mission_yaml_path,
    out_of_tree_lock_path,
    session_dir,
)
from swarm.schemas.event import Event
from swarm.schemas.finding import Finding
from swarm.schemas.lock import MissionLock
from swarm.schemas.mission import Mission, PatternThresholds
from swarm.specialists.completion_judge import judge as completion_judge
from swarm.specialists.coordinator import make_intervention_for
from swarm.specialists.event_scribe import emit_event, read_events
from swarm.specialists.pattern_detector import (
    detect_once,
    detect_scope_shrinking,
)
from swarm.specialists.success_verifier import run_all_checks


def _mk_mission(session_id: str, tmp_path: Path) -> Mission:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    m = Mission.model_validate(
        {
            "mission": "build a thing",
            "workspace": str(workspace),
            "success_criteria": [
                {"id": "ok", "description": "", "check": "true", "timeout_sec": 5}
            ],
            "verification": {"run_every_sec": 1, "hold_window_sec": 1},
        }
    )
    path = mission_yaml_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(json.loads(m.model_dump_json())))
    return m


def _pin_mission(session_id: str) -> None:
    path = mission_yaml_path(session_id)
    lock = MissionLock(
        session_id=session_id,
        locked_at="2026-04-17T00:00:00Z",
        files={"mission.yaml": sha256_file(path)},
    )
    mission_lock_path(session_id).write_text(lock.model_dump_json(indent=2))
    out_of_tree_lock_path(session_id).parent.mkdir(parents=True, exist_ok=True)
    out_of_tree_lock_path(session_id).write_text(
        json.dumps(lock.files, sort_keys=True)
    )


def _simulate_hook_emits_edit(session_id: str, file_path: str, content: str) -> Event:
    """Simulate what the real PostToolUse hook would do for an Edit."""
    tool_input = {"file_path": file_path, "old_string": "x", "new_string": content}
    tool_response = {"ok": True}
    summary, full = _summarize_response(tool_response, "Edit", tool_input)
    return emit_event(
        session_id=session_id,
        hook="PostToolUse",
        tool_name="Edit",
        tool_input_summary=_summarize_input(tool_input, "Edit"),
        tool_response_summary=summary,
    )


def test_e2e_edit_events_flow_through_detectors(session_id, tmp_path):
    m = _mk_mission(session_id, tmp_path)
    _pin_mission(session_id)

    # 5 identical Edit events → loop
    for _ in range(5):
        _simulate_hook_emits_edit(session_id, "/abs/foo.py", "identical content")

    events = read_events(session_id)
    assert len(events) == 5

    m.observer_config.pattern_thresholds = PatternThresholds(
        loop_repeat_count=3, loop_window_events=50
    )
    findings = detect_once(events, m)
    assert any(f.type == "loop" for f in findings), (
        f"Loop not detected from hook-emitted events. Summaries: "
        f"{[e.tool_input_summary for e in events]}"
    )


def test_e2e_loop_produces_correct_intervention(session_id, tmp_path):
    m = _mk_mission(session_id, tmp_path)
    _pin_mission(session_id)

    for _ in range(5):
        _simulate_hook_emits_edit(session_id, "/abs/x.py", "same")
    events = read_events(session_id)
    m.observer_config.pattern_thresholds = PatternThresholds(loop_repeat_count=3)
    findings = detect_once(events, m)
    loop_finding = next(f for f in findings if f.type == "loop")
    iv = make_intervention_for(session_id, loop_finding)
    assert iv is not None
    assert iv.tier == "correct"
    assert iv.strategy_used is not None
    assert iv.loop_signature is not None


def test_e2e_verifier_pass_triggers_complete(session_id, tmp_path):
    m = _mk_mission(session_id, tmp_path)
    _pin_mission(session_id)

    # Run all checks; the trivial `true` check passes
    results = run_all_checks(session_id, m)
    assert all(r.status == "pass" for r in results.values())

    # Seed verifier_status for completion_judge
    (session_dir(session_id) / "verifier_status.json").write_text(
        json.dumps({"ts": 0, "all_pass": True, "per_criterion": {}})
    )
    # Seed a recent hold_window_met finding
    import time as _t

    now_ms = int(_t.time() * 1000)
    hold = Finding(
        id=f"f-{now_ms}-hw",
        source="success_verifier.hold_window_met",
        subject_session=session_id,
        spawner_id=session_id,
        type="verification",
        subtype="hold_window_met",
        severity="major",
    )
    write_line(findings_path(session_id), hold.model_dump_json())

    # Verify completion judge gives green
    v = completion_judge(session_id)
    assert v.verdict == "complete", v.outstanding

    # Coordinator should produce mission_complete
    iv = make_intervention_for(session_id, hold)
    assert iv is not None
    assert iv.tier == "mission_complete"


def test_e2e_scope_shrinking_caught_before_completion(session_id, tmp_path):
    m = _mk_mission(session_id, tmp_path)
    _pin_mission(session_id)

    # Write a transcript with deflection language
    transcript = claude_transcript_path(session_id, m.workspace)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    with transcript.open("w") as f:
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": "I've finished the main features. "
                                "The rest is out of scope for this run.",
                            }
                        ],
                    },
                }
            )
            + "\n"
        )

    findings = detect_scope_shrinking(
        session_id=session_id,
        spawner_id=session_id,
        transcript_path=transcript,
        verifier_all_pass=False,
    )
    assert len(findings) == 1
    # Route through coordinator → must be urgent scope_lock
    iv = make_intervention_for(session_id, findings[0])
    assert iv is not None
    assert iv.tier == "urgent"
    assert iv.strategy_used == "scope_lock"


def test_e2e_cheat_finding_blocks_completion(session_id, tmp_path):
    _mk_mission(session_id, tmp_path)
    _pin_mission(session_id)

    (session_dir(session_id) / "verifier_status.json").write_text(
        json.dumps({"ts": 0, "all_pass": True, "per_criterion": {}})
    )
    import time as _t

    now_ms = int(_t.time() * 1000)
    hold = Finding(
        id=f"f-{now_ms}-hw",
        source="success_verifier.hold_window_met",
        subject_session=session_id,
        spawner_id=session_id,
        type="verification",
        subtype="hold_window_met",
        severity="major",
    )
    write_line(findings_path(session_id), hold.model_dump_json())
    # And a cheat finding
    cheat = Finding(
        id=f"f-{now_ms}-c",
        source="anticheat.scope_reduction",
        subject_session=session_id,
        spawner_id=session_id,
        type="cheat",
        subtype="scope_reduction",
        severity="critical",
        verdict="xfail added",
    )
    write_line(findings_path(session_id), cheat.model_dump_json())

    v = completion_judge(session_id)
    assert v.verdict == "cheat_suspected"
    # Coordinator's mission_complete path must NOT fire
    iv = make_intervention_for(session_id, hold)
    assert iv is not None
    assert iv.tier != "mission_complete"
