"""Tests for intervention_judge."""

from __future__ import annotations

from swarmd.schemas.finding import Evidence, Finding
from swarmd.specialists.intervention_judge import (
    ESCAPE_LADDER,
    InterventionDecision,
    decide,
    pick_rung,
)


def _f(type_: str, subtype: str, verdict: str = "", severity: str = "major") -> Finding:
    return Finding(
        id="f-1",
        source="test",
        subject_session="sid",
        spawner_id="sid",
        type=type_,  # type: ignore[arg-type]
        subtype=subtype,
        severity=severity,  # type: ignore[arg-type]
        verdict=verdict,
        evidence=Evidence(),
    )


# -------- pick_rung --------


def test_pick_rung_returns_first_untried():
    name, reason = pick_rung([])
    assert name == ESCAPE_LADDER[0][0]
    assert reason == ESCAPE_LADDER[0][1]


def test_pick_rung_skips_tried():
    tried = [ESCAPE_LADDER[0][0]]
    name, _ = pick_rung(tried)
    assert name == ESCAPE_LADDER[1][0]


def test_pick_rung_exhaustion_returns_recover():
    tried = [r[0] for r in ESCAPE_LADDER]
    name, reason = pick_rung(tried)
    assert name == "recover"
    assert "recovery subagent" in reason.lower()


# -------- decide() --------


def test_decide_tamper_is_mission_level_alert():
    d = decide(_f("meta", "tamper_detected", "hash mismatch"), strikes=0, tried=[])
    assert d.tier == "mission_level_alert"
    assert "paused" in d.reason.lower()
    assert d.consume_at == "either"


def test_decide_cheat_is_urgent_bisection():
    d = decide(_f("cheat", "scope_reduction", "tests deleted"), strikes=0, tried=[])
    assert d.tier == "urgent"
    assert d.strategy == "bisection_reset"
    assert "revert" in d.reason.lower()


def test_decide_scope_shrinking_fabrication_is_urgent_scope_lock():
    d = decide(_f("fabrication", "scope_shrinking", "said out of scope"), strikes=0, tried=[])
    assert d.tier == "urgent"
    assert d.strategy == "scope_lock"
    assert "not your call" in d.reason.lower()


def test_decide_other_fabrication_is_correct():
    d = decide(_f("fabrication", "unsupported_claim", "no evidence"), strikes=0, tried=[])
    assert d.tier == "correct"
    assert d.strategy == "grounding_required"


def test_decide_loop_uses_first_rung_at_low_strikes():
    d = decide(_f("loop", "repeat_exact_args", "5x"), strikes=1, tried=[])
    assert d.tier == "correct"
    assert d.strategy == ESCAPE_LADDER[0][0]


def test_decide_loop_rotates_rungs():
    d = decide(
        _f("loop", "repeat_exact_args"),
        strikes=1,
        tried=[ESCAPE_LADDER[0][0], ESCAPE_LADDER[1][0]],
    )
    assert d.strategy == ESCAPE_LADDER[2][0]


def test_decide_loop_recover_at_3_strikes():
    d = decide(_f("thrash", "oscillation"), strikes=3, tried=[])
    assert d.tier == "recover"
    assert d.strategy == "recover"


def test_decide_loop_recover_also_when_ladder_exhausted():
    d = decide(
        _f("drift", "drifting"),
        strikes=1,
        tried=[r[0] for r in ESCAPE_LADDER],
    )
    assert d.strategy == "recover"
    assert d.tier == "recover"


def test_decide_unknown_type_is_info():
    d = decide(_f("verification", "pass_transition"), strikes=0, tried=[])
    assert d.tier == "info"


def test_decision_is_frozen():
    d = InterventionDecision(tier="info")
    try:
        d.tier = "urgent"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("InterventionDecision should be frozen")
