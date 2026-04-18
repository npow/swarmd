"""Tests for completion_judge."""

from __future__ import annotations

import json

from swarm.lib.locking import write_line
from swarm.lib.paths import findings_path, session_dir
from swarm.schemas.finding import Finding
from swarm.specialists.completion_judge import judge


def _write_verifier_status(session_id: str, all_pass: bool) -> None:
    p = session_dir(session_id) / "verifier_status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ts": 0, "all_pass": all_pass, "per_criterion": {}}))


def _write_finding(session_id: str, **kwargs) -> None:
    f = Finding(
        subject_session=session_id,
        spawner_id=session_id,
        **kwargs,
    )
    write_line(findings_path(session_id), f.model_dump_json())


def test_incomplete_when_status_missing(session_id):
    v = judge(session_id)
    assert v.verdict == "incomplete"


def test_incomplete_when_all_pass_false(session_id):
    _write_verifier_status(session_id, False)
    _write_finding(
        session_id,
        id="f-1",
        source="success_verifier.hold_window_met",
        type="verification",
        subtype="hold_window_met",
        severity="major",
    )
    v = judge(session_id)
    assert v.verdict == "incomplete"


def test_incomplete_without_hold_window(session_id):
    _write_verifier_status(session_id, True)
    v = judge(session_id)
    assert v.verdict == "incomplete"
    assert any("hold window" in o.lower() for o in v.outstanding)


def test_cheat_suspected_when_cheat_finding_present(session_id):
    _write_verifier_status(session_id, True)
    _write_finding(
        session_id,
        id="f-hold",
        source="success_verifier.hold_window_met",
        type="verification",
        subtype="hold_window_met",
        severity="major",
    )
    _write_finding(
        session_id,
        id="f-cheat",
        source="anticheat.scope_reduction",
        type="cheat",
        subtype="scope_reduction",
        severity="critical",
    )
    v = judge(session_id)
    assert v.verdict == "cheat_suspected"


def test_cheat_suspected_when_tamper(session_id):
    _write_verifier_status(session_id, True)
    _write_finding(
        session_id,
        id="f-hold",
        source="success_verifier.hold_window_met",
        type="verification",
        subtype="hold_window_met",
        severity="major",
    )
    _write_finding(
        session_id,
        id="f-tamper",
        source="success_verifier.tamper",
        type="meta",
        subtype="tamper_detected",
        severity="critical",
    )
    v = judge(session_id)
    assert v.verdict == "cheat_suspected"


def test_complete_when_all_preconditions_met(session_id):
    import time as _t

    _write_verifier_status(session_id, True)
    now_ms = int(_t.time() * 1000)
    _write_finding(
        session_id,
        id=f"f-{now_ms}-abc",  # recent timestamp
        source="success_verifier.hold_window_met",
        type="verification",
        subtype="hold_window_met",
        severity="major",
    )
    v = judge(session_id)
    assert v.verdict == "complete", v.outstanding
    assert v.outstanding == []


def test_stale_hold_window_blocks_completion(session_id):
    import time as _t

    _write_verifier_status(session_id, True)
    # hold_window_met finding from 1 hour ago
    old_ms = int((_t.time() - 3600) * 1000)
    _write_finding(
        session_id,
        id=f"f-{old_ms}-abc",
        source="success_verifier.hold_window_met",
        type="verification",
        subtype="hold_window_met",
        severity="major",
    )
    v = judge(session_id, hold_window_recency_sec=60)
    assert v.verdict == "incomplete"
    assert any("older than" in o for o in v.outstanding)


def test_fresh_hold_window_allows_completion(session_id):
    import time as _t

    _write_verifier_status(session_id, True)
    now_ms = int(_t.time() * 1000)
    _write_finding(
        session_id,
        id=f"f-{now_ms}-abc",
        source="success_verifier.hold_window_met",
        type="verification",
        subtype="hold_window_met",
        severity="major",
    )
    v = judge(session_id, hold_window_recency_sec=300)
    assert v.verdict == "complete"


def test_critic_disagreement_blocks_completion(session_id):
    """When anticheat critics disagree, completion_judge must not return complete."""
    import time as _t

    _write_verifier_status(session_id, True)
    now_ms = int(_t.time() * 1000)
    _write_finding(
        session_id,
        id=f"f-{now_ms}-hw",
        source="success_verifier.hold_window_met",
        type="verification",
        subtype="hold_window_met",
        severity="major",
    )
    _write_finding(
        session_id,
        id="f-disagree",
        source="anticheat.scope_reduction.disagreement",
        type="meta",
        subtype="critic_disagreement",
        severity="major",
    )
    v = judge(session_id)
    assert v.verdict != "complete"
    assert any("disagree" in o for o in v.outstanding)


def test_anticheat_unclear_blocks_completion(session_id):
    """A pass-transition with UNCLEAR anticheat verdict blocks completion."""
    import time as _t

    _write_verifier_status(session_id, True)
    now_ms = int(_t.time() * 1000)
    _write_finding(
        session_id,
        id=f"f-{now_ms}-hw",
        source="success_verifier.hold_window_met",
        type="verification",
        subtype="hold_window_met",
        severity="major",
    )
    # A pass_transition occurred
    _write_finding(
        session_id,
        id="f-pt",
        source="success_verifier.transition",
        type="verification",
        subtype="pass_transition",
        severity="major",
    )
    # Anticheat returned UNCLEAR → meta finding
    _write_finding(
        session_id,
        id="f-ac-unclear",
        source="anticheat.scope_reduction",
        type="meta",
        subtype="unclear",
        severity="major",
    )
    v = judge(session_id)
    # The cheat or anticheat verdict should block completion
    assert v.verdict != "complete"


def test_completion_judge_surfaces_anticheat_in_reasoning(session_id):
    """The judge's reasoning text mentions anticheat when blocking on it."""
    import time as _t

    _write_verifier_status(session_id, True)
    now_ms = int(_t.time() * 1000)
    _write_finding(
        session_id,
        id=f"f-{now_ms}-hw",
        source="success_verifier.hold_window_met",
        type="verification",
        subtype="hold_window_met",
        severity="major",
    )
    _write_finding(
        session_id,
        id="f-pt",
        source="success_verifier.transition",
        type="verification",
        subtype="pass_transition",
        severity="major",
    )
    _write_finding(
        session_id,
        id="f-ac",
        source="anticheat.mock_out",
        type="cheat",
        subtype="mock_out",
        severity="critical",
    )
    v = judge(session_id)
    assert v.verdict in {"cheat_suspected", "incomplete"}
    # reasoning should reference anticheat or cheat
    assert "anticheat" in v.reasoning.lower() or "cheat" in v.reasoning.lower()
