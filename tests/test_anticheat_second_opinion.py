"""Tests for the second-opinion critic path in anticheat_critic_panel."""

from __future__ import annotations

import json

from swarmd.specialists.anticheat_critic_panel import run_panel


def _llm(d: dict):
    def _r(_prompt: str) -> str:
        return json.dumps(d)

    return _r


def test_no_second_opinion_unchanged():
    primary = _llm({"verdict": "GENUINE_FIX", "citations": [], "reason": "ok"})
    out = run_panel(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        criterion_id="x",
        criterion_description="x",
        check_command="x",
        diff="x",
        events="x",
        llm=primary,
    )
    assert out == []


def test_second_opinion_agrees_no_disagreement():
    primary = _llm({"verdict": "GENUINE_FIX", "citations": [], "reason": "p"})
    second = _llm({"verdict": "GENUINE_FIX", "citations": [], "reason": "s"})
    out = run_panel(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        criterion_id="x",
        criterion_description="x",
        check_command="x",
        diff="x",
        events="x",
        llm=primary,
        second_opinion=second,
    )
    assert out == []


def test_second_opinion_disagrees_emits_finding():
    primary = _llm({"verdict": "GENUINE_FIX", "citations": [], "reason": "p"})
    second = _llm({"verdict": "SCOPE_REDUCTION", "citations": [], "reason": "s"})
    out = run_panel(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        criterion_id="x",
        criterion_description="x",
        check_command="x",
        diff="x",
        events="x",
        llm=primary,
        second_opinion=second,
    )
    # At least one disagreement finding per dimension where primary=genuine, second!=
    disagreements = [f for f in out if f.subtype == "critic_disagreement"]
    assert len(disagreements) >= 1
    for d in disagreements:
        assert d.type == "meta"
        assert "second_opinion" in d.evidence.claim_excerpt
        assert "disagree" in d.verdict.lower()


def test_second_opinion_both_non_genuine_no_disagreement_finding():
    # Both critics agree it's not genuine → primary verdict stands as a cheat
    # finding; no extra disagreement finding needed
    primary = _llm({"verdict": "MOCK_OUT", "citations": [], "reason": "p"})
    second = _llm({"verdict": "MOCK_OUT", "citations": [], "reason": "s"})
    out = run_panel(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        criterion_id="x",
        criterion_description="x",
        check_command="x",
        diff="x",
        events="x",
        llm=primary,
        second_opinion=second,
    )
    # Primary cheats are emitted; no disagreement findings
    disagreements = [f for f in out if f.subtype == "critic_disagreement"]
    assert disagreements == []
    cheats = [f for f in out if f.type == "cheat" and f.subtype == "mock_out"]
    assert len(cheats) >= 1
