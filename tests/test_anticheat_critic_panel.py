"""Tests for anticheat_critic_panel."""

from __future__ import annotations

import json
from unittest.mock import patch

from swarm.specialists.anticheat_critic_panel import (
    TAXONOMY,
    CriticVerdict,
    _parse,
    default_llm_runner,
    is_transition_genuine,
    run_panel,
)

# --- _parse ---


def test_parse_genuine():
    r = _parse('{"verdict": "GENUINE_FIX", "citations": [], "reason": "clean"}', "scope_reduction")
    assert r.verdict == "GENUINE_FIX"
    assert r.dimension == "scope_reduction"


def test_parse_unparseable():
    r = _parse("not json", "mock_out")
    assert r.verdict == "UNCLEAR"


def test_parse_bad_verdict():
    r = _parse('{"verdict": "PROBABLY_FINE"}', "tautology")
    assert r.verdict == "UNCLEAR"


def test_parse_strips_fence():
    r = _parse(
        '```\n{"verdict": "MOCK_OUT", "citations": ["tests/x.py:10"], "reason": "r"}\n```',
        "mock_out",
    )
    assert r.verdict == "MOCK_OUT"
    assert "tests/x.py:10" in r.citations


def test_taxonomy_is_complete():
    expected = {
        "GENUINE_FIX",
        "SCOPE_REDUCTION",
        "MOCK_OUT",
        "TAUTOLOGY",
        "HARDCODE",
        "OFF_CRITERION",
        "UNCLEAR",
    }
    assert set(TAXONOMY) == expected


# --- run_panel ---


def _mk_llm(map_by_prompt_fragment: dict[str, dict]):
    """Return a runner that picks verdict based on a substring match in prompt."""
    def _r(prompt: str) -> str:
        for frag, verdict_dict in map_by_prompt_fragment.items():
            if frag in prompt:
                return json.dumps(verdict_dict)
        return json.dumps({"verdict": "UNCLEAR", "citations": [], "reason": "default"})
    return _r


def test_panel_all_genuine_returns_no_findings():
    runner = _mk_llm({"Dimension: ": {"verdict": "GENUINE_FIX", "citations": [], "reason": "ok"}})
    out = run_panel(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        criterion_id="tests_pass",
        criterion_description="tests pass",
        check_command="pytest",
        diff="+ added = actual_fix()",
        events="Edit(file=fix.py)",
        llm=runner,
    )
    assert out == []


def test_panel_scope_reduction_emits_critical():
    def _r(prompt: str) -> str:
        if "Dimension: scope_reduction" in prompt:
            return json.dumps(
                {
                    "verdict": "SCOPE_REDUCTION",
                    "citations": ["file:tests/test_x.py:5"],
                    "reason": "xfail added",
                }
            )
        return json.dumps({"verdict": "GENUINE_FIX", "citations": [], "reason": "ok"})

    out = run_panel(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        criterion_id="tests_pass",
        criterion_description="tests pass",
        check_command="pytest",
        diff="+ @pytest.mark.xfail",
        events="Edit",
        llm=_r,
    )
    assert len(out) == 1
    assert out[0].type == "cheat"
    assert out[0].subtype == "scope_reduction"
    assert out[0].severity == "critical"
    assert any("test_x.py" in f for f in out[0].evidence.files)


def test_panel_multiple_cheats_multiple_findings():
    def _r(prompt: str) -> str:
        if "Dimension: mock_out" in prompt:
            return json.dumps(
                {"verdict": "MOCK_OUT", "citations": [], "reason": "m"}
            )
        if "Dimension: tautology" in prompt:
            return json.dumps(
                {"verdict": "TAUTOLOGY", "citations": [], "reason": "t"}
            )
        return json.dumps({"verdict": "GENUINE_FIX", "citations": [], "reason": "ok"})

    out = run_panel(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        criterion_id="x",
        criterion_description="x",
        check_command="x",
        diff="x",
        events="x",
        llm=_r,
    )
    assert len(out) == 2
    subtypes = {f.subtype for f in out}
    assert subtypes == {"mock_out", "tautology"}


def test_panel_unclear_blocks_completion():
    runner = _mk_llm({"Dimension: ": {"verdict": "UNCLEAR", "citations": [], "reason": "can't tell"}})
    out = run_panel(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        criterion_id="x",
        criterion_description="x",
        check_command="x",
        diff="x",
        events="x",
        llm=runner,
    )
    # UNCLEAR produces meta findings (blocks completion by fail-safe)
    assert len(out) == len(TAXONOMY) - 1  # one per non-GENUINE_FIX dimension
    for f in out:
        assert f.subtype == "unclear"
        assert f.type == "meta"


def test_panel_runs_only_specified_dimensions():
    runner = _mk_llm({"Dimension: ": {"verdict": "GENUINE_FIX", "citations": [], "reason": "ok"}})
    out = run_panel(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        criterion_id="x",
        criterion_description="x",
        check_command="x",
        diff="x",
        events="x",
        llm=runner,
        dimensions=["scope_reduction", "mock_out"],
    )
    assert out == []


# --- is_transition_genuine ---


def test_is_transition_genuine_all_pass():
    vs = [
        CriticVerdict("a", "GENUINE_FIX"),
        CriticVerdict("b", "GENUINE_FIX"),
    ]
    assert is_transition_genuine(vs) is True


def test_is_transition_genuine_one_fails():
    vs = [
        CriticVerdict("a", "GENUINE_FIX"),
        CriticVerdict("b", "MOCK_OUT"),
    ]
    assert is_transition_genuine(vs) is False


def test_is_transition_genuine_empty_is_false():
    # Empty panel = no confirmation = not genuine
    assert is_transition_genuine([]) is False


# --- default_llm_runner gateway client contract ---


def test_default_runner_uses_gateway_client():
    """default_llm_runner must delegate to swarm.lib.llm_client.call, not subprocess."""
    with patch("swarm.lib.llm_client.call", return_value='{"verdict": "GENUINE_FIX", "citations": [], "reason": "ok"}') as mock_call:
        result = default_llm_runner("test prompt")
    mock_call.assert_called_once_with("test prompt")
    assert result == '{"verdict": "GENUINE_FIX", "citations": [], "reason": "ok"}'


def test_default_runner_returns_empty_on_LLMError():
    """When llm_client.call raises LLMError, default_llm_runner returns '' (UNCLEAR)."""
    from swarm.lib.llm_client import LLMError
    with patch("swarm.lib.llm_client.call", side_effect=LLMError("gateway down")):
        result = default_llm_runner("test prompt")
    assert result == ""
