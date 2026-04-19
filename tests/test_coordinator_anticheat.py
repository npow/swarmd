"""Tests for the coordinator → anticheat panel wiring on pass-transitions."""

from __future__ import annotations

from swarmd.schemas.finding import Evidence, Finding
from swarmd.specialists import coordinator


def _pass_transition(cid: str = "tests_pass") -> Finding:
    return Finding(
        id="f-hw",
        source="success_verifier.transition",
        subject_session="abcdef012345",
        spawner_id="abcdef012345",
        type="verification",
        subtype="pass_transition",
        severity="major",
        evidence=Evidence(claim_excerpt=f"criterion={cid} exit=0 stdout_tail=..."),
        verdict=f"{cid} transitioned fail→pass",
    )


def test_pass_transition_invokes_anticheat_panel(session_id, monkeypatch):
    called = {"n": 0, "criterion_id": None}

    def _stub_panel(**kw):
        called["n"] += 1
        called["criterion_id"] = kw.get("criterion_id")
        return []  # all critics return GENUINE_FIX

    monkeypatch.setattr(coordinator, "_anticheat_runner", _stub_panel)

    iv = coordinator.make_intervention_for(session_id, _pass_transition("my_crit"))
    assert iv is not None
    assert iv.tier == "info"
    assert called["n"] == 1
    assert called["criterion_id"] == "my_crit"
    assert "my_crit" in iv.reason


def test_anticheat_cheat_verdicts_written_to_findings(session_id, monkeypatch):
    cheat_f = Finding(
        id="f-cheat",
        source="anticheat.scope_reduction",
        subject_session=session_id,
        spawner_id=session_id,
        type="cheat",
        subtype="scope_reduction",
        severity="critical",
        verdict="xfail added",
    )

    def _stub_panel(**kw):
        return [cheat_f]

    monkeypatch.setattr(coordinator, "_anticheat_runner", _stub_panel)

    iv = coordinator.make_intervention_for(session_id, _pass_transition())
    assert iv is not None

    # Verify cheat finding was appended to findings.jsonl
    from swarmd.lib.paths import findings_path

    content = findings_path(session_id).read_text()
    assert "scope_reduction" in content
    assert "f-cheat" in content


def test_anticheat_panel_failure_is_nonfatal(session_id, monkeypatch):
    def _fail(**kw):
        raise RuntimeError("llm unavailable")

    monkeypatch.setattr(coordinator, "_anticheat_runner", _fail)
    iv = coordinator.make_intervention_for(session_id, _pass_transition())
    # Still returns an info intervention (doesn't crash the coordinator)
    assert iv is not None
    assert iv.tier == "info"


def test_set_anticheat_runner_api(session_id, monkeypatch):
    called = {"n": 0}

    def _stub(**kw):
        called["n"] += 1
        return []

    # Use the public setter
    coordinator.set_anticheat_runner(_stub)
    try:
        coordinator.make_intervention_for(session_id, _pass_transition())
        assert called["n"] == 1
    finally:
        from swarmd.specialists.anticheat_critic_panel import run_panel

        coordinator.set_anticheat_runner(run_panel)


def test_non_pass_transition_does_not_invoke_panel(session_id, monkeypatch):
    called = {"n": 0}

    def _stub(**kw):
        called["n"] += 1
        return []

    monkeypatch.setattr(coordinator, "_anticheat_runner", _stub)

    # A loop finding should NOT invoke anticheat
    loop = Finding(
        id="f-loop",
        source="pattern_detector.loop",
        subject_session=session_id,
        spawner_id=session_id,
        type="loop",
        subtype="repeat_exact_args",
        severity="major",
        evidence=Evidence(claim_excerpt="Edit(file=foo.py)"),
        verdict="5x repeat",
    )
    coordinator.make_intervention_for(session_id, loop)
    assert called["n"] == 0
