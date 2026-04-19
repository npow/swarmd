"""Tests for coordinator's finding-to-intervention policy."""

from __future__ import annotations

from swarmd.schemas.finding import Evidence, Finding
from swarmd.specialists.coordinator import (
    bump_strike,
    loop_signature,
    make_intervention_for,
    pick_rung,
    record_tried,
)


def _loop_finding(sid: str, suffix: str = "") -> Finding:
    return Finding(
        id=f"f-loop{suffix}",
        source="pattern_detector.loop",
        subject_session=sid,
        spawner_id=sid,
        type="loop",
        subtype="repeat_exact_args",
        severity="major",
        cited_events=["e-1", "e-2", "e-3"],
        evidence=Evidence(files=["foo.py"], claim_excerpt="Edit(file=foo.py)"),
        verdict="Edit repeated 5 times",
    )


def test_loop_signature_stable(session_id):
    f1 = _loop_finding(session_id, "1")
    f2 = _loop_finding(session_id, "2")
    # different ids, same pattern → same signature
    assert loop_signature(f1) == loop_signature(f2)


def test_bump_strike_increments(session_id):
    sig = "abc"
    assert bump_strike(session_id, sig) == 1
    assert bump_strike(session_id, sig) == 2
    assert bump_strike(session_id, sig) == 3


def test_pick_rung_rotates(session_id):
    sig = "sig1"
    first, _ = pick_rung(session_id, sig)
    record_tried(session_id, sig, first, "attempted")
    second, _ = pick_rung(session_id, sig)
    assert second != first


def test_pick_rung_exhaustion(session_id):
    sig = "sig2"
    from swarmd.specialists.coordinator import ESCAPE_LADDER

    for name, _ in ESCAPE_LADDER:
        record_tried(session_id, sig, name, "attempted")
    name, _ = pick_rung(session_id, sig)
    assert name == "recover"


def test_make_intervention_for_loop(session_id):
    f = _loop_finding(session_id)
    iv = make_intervention_for(session_id, f)
    assert iv is not None
    assert iv.tier in {"correct", "recover"}
    assert iv.strategy_used is not None


def test_make_intervention_for_cheat(session_id):
    f = Finding(
        id="f-cheat",
        source="anticheat.scope_reduction",
        subject_session=session_id,
        spawner_id=session_id,
        type="cheat",
        subtype="scope_reduction",
        severity="critical",
        verdict="test deleted",
    )
    iv = make_intervention_for(session_id, f)
    assert iv is not None
    assert iv.tier == "urgent"
    assert "revert" in iv.reason.lower() or "scope_reduction" in iv.reason.lower()


def test_make_intervention_for_tamper(session_id):
    f = Finding(
        id="f-t",
        source="success_verifier.tamper",
        subject_session=session_id,
        spawner_id=session_id,
        type="meta",
        subtype="tamper_detected",
        severity="critical",
        verdict="hash mismatch",
    )
    iv = make_intervention_for(session_id, f)
    assert iv is not None
    assert iv.tier == "mission_level_alert"


def test_make_intervention_for_fabrication(session_id):
    f = Finding(
        id="f-fab",
        source="progress_auditor",
        subject_session=session_id,
        spawner_id=session_id,
        type="fabrication",
        subtype="unsupported_claim",
        severity="major",
        verdict="claimed tests pass without running them",
    )
    iv = make_intervention_for(session_id, f)
    assert iv is not None
    assert iv.tier == "correct"


def test_make_intervention_for_scope_shrinking_is_urgent(session_id):
    f = Finding(
        id="f-scope",
        source="pattern_detector.scope_shrinking",
        subject_session=session_id,
        spawner_id=session_id,
        type="fabrication",
        subtype="scope_shrinking",
        severity="major",
        verdict="Agent said 'out of scope' without verifier confirmation",
    )
    iv = make_intervention_for(session_id, f)
    assert iv is not None
    # Scope shrinking must be urgent (agent is trying to stop prematurely)
    assert iv.tier == "urgent"
    assert iv.consume_at == "either"
    assert iv.strategy_used == "scope_lock"
    assert "out of scope" in iv.reason.lower() or "scope" in iv.reason.lower()
    assert "NOT your call" in iv.reason or "not your call" in iv.reason.lower()


def test_make_intervention_for_hold_window_blocks_without_completion(session_id):
    # hold_window_met fires before any criterion has actually passed
    f = Finding(
        id="f-hold",
        source="success_verifier.hold_window_met",
        subject_session=session_id,
        spawner_id=session_id,
        type="verification",
        subtype="hold_window_met",
        severity="major",
        verdict="held",
    )
    iv = make_intervention_for(session_id, f)
    assert iv is not None
    # Should NOT be mission_complete — verifier status.json is missing/all_pass=False
    assert iv.tier != "mission_complete"
