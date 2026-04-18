"""Tests for the ``run_anticheat_dimension`` Temporal activity.

Per plan Task 11 and spec §5.9:

    run_anticheat_dimension(dimension, context, anticheat_config) → AnticheatVerdict

    Runs ONE dimension of the 6-dim anti-cheat panel. The orchestrating
    LLMCriticWorkflow will schedule six of these in parallel (one per
    dimension) to preserve the panel semantics of today's
    ``specialists/anticheat_critic_panel.run_panel()``.

Ported from ``specialists/anticheat_critic_panel.py``. The dimension
prompts are preserved verbatim; the interface is restructured to:

* Take a single ``dimension`` name, a ``context`` dict with the four
  legacy placeholders (``criterion_id``, ``diff``, ``events``,
  ``check_command``), and an ``anticheat_config`` dict that supplies the
  reviewer command line (``claude -p --bare --model opus`` by default).
* Invoke the reviewer subprocess via ``asyncio.create_subprocess_shell``
  so the Temporal event loop stays non-blocking.
* Return an ``AnticheatVerdict`` with ``dimension``, ``verdict``,
  ``rationale`` and a ``finding`` dict ready for ``emit_finding``.

These tests drive the activity through
``temporalio.testing.ActivityEnvironment`` so no Temporal server is
needed. The ``_invoke_reviewer`` helper is mocked to avoid actually
spawning ``claude`` during the test suite.

Invariants covered:

1. Happy path per dimension — all six dimensions round-trip a
   ``pass`` verdict into a correctly-populated ``AnticheatVerdict``.
2. ``fail`` verdict produces a ``finding.type == "anticheat_fail"``.
3. ``suspicious`` verdict produces ``finding.type == "anticheat_suspicious"``.
4. Unknown dimension → ``ValueError`` (caller bug; non-retryable).
5. Missing context key → ``TerminalError`` (prompt template would KeyError).
6. Malformed JSON response → ``TerminalError`` (retries can't fix it).
7. Non-zero subprocess exit → ``TransientError`` (CLI is retryable).
8. Unknown verdict string in otherwise valid JSON → ``TerminalError``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from temporalio.testing import ActivityEnvironment

from swarm.durable.activities.run_anticheat_dimension import (
    AnticheatVerdict,
    _DIMENSION_PROMPTS,
    run_anticheat_dimension,
)
from swarm.durable.errors import TerminalError, TransientError


_ALL_DIMENSIONS = [
    "scope_reduction",
    "mock_out",
    "tautology",
    "hardcode",
    "off_criterion",
    "coordinated_edit",
]


def _context(**overrides) -> dict:
    """Default anticheat context with all four required placeholders."""
    base = {
        "criterion_id": "c-login-200",
        "diff": "+ def test_login_pass(): assert 1 == 1",
        "events": "Edit(path='tests/test_login.py')",
        "check_command": "pytest tests/test_login.py",
        # Metadata that should pass through into the finding:
        "mission_id": "m-abc",
        "session_id": "s-123",
        "spawner_id": "spawner-xyz",
    }
    base.update(overrides)
    return base


def _config(**overrides) -> dict:
    """Default anticheat_config — legacy-compatible opus reviewer."""
    base = {"primary": "claude -p --bare --model opus"}
    base.update(overrides)
    return base


@pytest.mark.parametrize("dimension", _ALL_DIMENSIONS)
@pytest.mark.asyncio
async def test_happy_path_pass_per_dimension(dimension):
    """Each of the six dimensions round-trips a ``pass`` verdict correctly.

    Verifies the activity routes the dimension-specific prompt into the
    reviewer and wraps the response into a populated ``AnticheatVerdict``
    whose ``finding.type == "info"`` because ``pass`` is benign.
    """
    mock_invoke = AsyncMock(
        return_value=json.dumps(
            {
                "verdict": "pass",
                "rationale": "Diff looks like a genuine fix.",
            }
        )
    )
    with patch(
        "swarm.durable.activities.run_anticheat_dimension._invoke_reviewer",
        mock_invoke,
    ):
        env = ActivityEnvironment()
        res = await env.run(
            run_anticheat_dimension, dimension, _context(), _config()
        )

    assert isinstance(res, AnticheatVerdict)
    assert res.dimension == dimension
    assert res.verdict == "pass"
    assert "genuine" in res.rationale.lower()
    assert res.finding["type"] == "info"
    assert res.finding["subtype"] == "pass"
    assert res.finding["source"] == f"anticheat.{dimension}"

    # Confirm the dimension-specific focus made it into the prompt we
    # sent to the reviewer — otherwise the critic would be judging the
    # wrong axis.
    (cmd, prompt), _ = mock_invoke.call_args
    assert _DIMENSION_PROMPTS[dimension] in prompt
    assert "claude -p --bare --model opus" in cmd


@pytest.mark.parametrize("dimension", _ALL_DIMENSIONS)
@pytest.mark.asyncio
async def test_fail_verdict_produces_anticheat_fail_finding(dimension):
    """``fail`` verdict → ``finding.type == "anticheat_fail"``.

    This is the hard verdict: the panel is confident the pass transition
    was cheating, so the finding type must be the critical one
    downstream routers look for.
    """
    mock_invoke = AsyncMock(
        return_value=json.dumps(
            {
                "verdict": "fail",
                "rationale": "Test was replaced with `assert True`.",
            }
        )
    )
    with patch(
        "swarm.durable.activities.run_anticheat_dimension._invoke_reviewer",
        mock_invoke,
    ):
        env = ActivityEnvironment()
        res = await env.run(
            run_anticheat_dimension, dimension, _context(), _config()
        )

    assert res.verdict == "fail"
    assert res.finding["type"] == "anticheat_fail"
    assert res.finding["subtype"] == "fail"
    assert res.finding["severity"] == "critical"


@pytest.mark.parametrize("dimension", _ALL_DIMENSIONS)
@pytest.mark.asyncio
async def test_suspicious_verdict_produces_anticheat_suspicious_finding(
    dimension,
):
    """``suspicious`` verdict → ``finding.type == "anticheat_suspicious"``.

    Softer than ``fail``: the critic saw signals but can't confirm cheat.
    Still blocks completion per the fail-safe policy.
    """
    mock_invoke = AsyncMock(
        return_value=json.dumps(
            {
                "verdict": "suspicious",
                "rationale": "Edit window overlaps with test file.",
            }
        )
    )
    with patch(
        "swarm.durable.activities.run_anticheat_dimension._invoke_reviewer",
        mock_invoke,
    ):
        env = ActivityEnvironment()
        res = await env.run(
            run_anticheat_dimension, dimension, _context(), _config()
        )

    assert res.verdict == "suspicious"
    assert res.finding["type"] == "anticheat_suspicious"
    assert res.finding["subtype"] == "suspicious"
    assert res.finding["severity"] == "major"


@pytest.mark.asyncio
async def test_unknown_dimension_raises_value_error():
    """Unknown dimension → ``ValueError`` — this indicates a caller bug
    (the workflow constructed a dimension name not in the panel)."""
    env = ActivityEnvironment()
    with pytest.raises(ValueError):
        await env.run(
            run_anticheat_dimension,
            "not_a_real_dimension",
            _context(),
            _config(),
        )


@pytest.mark.asyncio
async def test_missing_context_key_raises_terminal():
    """Missing context placeholder → ``TerminalError``.

    A missing key is a contract violation; retries won't grow keys.
    """
    ctx = _context()
    del ctx["check_command"]  # drop one required placeholder
    mock_invoke = AsyncMock(return_value=json.dumps({"verdict": "pass"}))
    with patch(
        "swarm.durable.activities.run_anticheat_dimension._invoke_reviewer",
        mock_invoke,
    ):
        env = ActivityEnvironment()
        with pytest.raises(TerminalError):
            await env.run(
                run_anticheat_dimension, "scope_reduction", ctx, _config()
            )


@pytest.mark.asyncio
async def test_malformed_json_raises_terminal():
    """Malformed reviewer response → ``TerminalError``.

    The reviewer promised JSON; anything else is unparseable. Retrying
    won't turn prose into JSON so Temporal should fail fast.
    """
    mock_invoke = AsyncMock(return_value="not json at all <<<>>>")
    with patch(
        "swarm.durable.activities.run_anticheat_dimension._invoke_reviewer",
        mock_invoke,
    ):
        env = ActivityEnvironment()
        with pytest.raises(TerminalError):
            await env.run(
                run_anticheat_dimension,
                "scope_reduction",
                _context(),
                _config(),
            )


@pytest.mark.asyncio
async def test_non_zero_exit_raises_transient():
    """Non-zero reviewer exit → ``TransientError``. The CLI is retryable
    because a transient environment failure (DNS, OOM, socket reset) is
    the usual cause; the spec chose ``RUN_ANTICHEAT_DIMENSION`` with 10
    attempts specifically to absorb this."""
    mock_invoke = AsyncMock(
        side_effect=TransientError("claude exited 1: network hiccup")
    )
    with patch(
        "swarm.durable.activities.run_anticheat_dimension._invoke_reviewer",
        mock_invoke,
    ):
        env = ActivityEnvironment()
        with pytest.raises(TransientError):
            await env.run(
                run_anticheat_dimension,
                "scope_reduction",
                _context(),
                _config(),
            )


@pytest.mark.asyncio
async def test_unknown_verdict_string_raises_terminal():
    """Recognized JSON with an unrecognized ``verdict`` string → ``TerminalError``."""
    mock_invoke = AsyncMock(
        return_value=json.dumps(
            {"verdict": "fizzbuzz", "rationale": "model fumbled"}
        )
    )
    with patch(
        "swarm.durable.activities.run_anticheat_dimension._invoke_reviewer",
        mock_invoke,
    ):
        env = ActivityEnvironment()
        with pytest.raises(TerminalError):
            await env.run(
                run_anticheat_dimension,
                "scope_reduction",
                _context(),
                _config(),
            )


@pytest.mark.asyncio
async def test_context_placeholders_reach_prompt():
    """The four legacy placeholders (``criterion_id``, ``diff``, ``events``,
    ``check_command``) must land in the prompt — otherwise the reviewer
    has nothing to judge."""
    ctx = _context(
        criterion_id="C-42",
        diff="+ assert x == expected",
        events="Edit(path='helpers.py')",
        check_command="pytest -q",
    )
    mock_invoke = AsyncMock(
        return_value=json.dumps({"verdict": "pass", "rationale": "ok"})
    )
    with patch(
        "swarm.durable.activities.run_anticheat_dimension._invoke_reviewer",
        mock_invoke,
    ):
        env = ActivityEnvironment()
        await env.run(
            run_anticheat_dimension, "scope_reduction", ctx, _config()
        )

    (_cmd, prompt), _ = mock_invoke.call_args
    assert "C-42" in prompt
    assert "+ assert x == expected" in prompt
    assert "Edit(path='helpers.py')" in prompt
    assert "pytest -q" in prompt


@pytest.mark.asyncio
async def test_reviewer_command_is_opus_by_default():
    """The spec requires Opus-level review for anti-cheat. Lock in the
    default primary command so a migration doesn't accidentally drop to
    Haiku (which would be too cheap for the adversarial judgment)."""
    mock_invoke = AsyncMock(
        return_value=json.dumps({"verdict": "pass", "rationale": "ok"})
    )
    with patch(
        "swarm.durable.activities.run_anticheat_dimension._invoke_reviewer",
        mock_invoke,
    ):
        env = ActivityEnvironment()
        await env.run(
            run_anticheat_dimension,
            "scope_reduction",
            _context(),
            _config(),
        )

    (cmd, _prompt), _ = mock_invoke.call_args
    assert "opus" in cmd.lower()


@pytest.mark.asyncio
async def test_json_fences_are_stripped():
    """Some reviewers wrap JSON in ```json ... ``` fences even when asked
    not to. The parser must tolerate that rather than raising
    TerminalError — otherwise the panel would fail on a common benign
    formatting quirk."""
    fenced = (
        "```json\n"
        + json.dumps({"verdict": "pass", "rationale": "fenced"})
        + "\n```"
    )
    mock_invoke = AsyncMock(return_value=fenced)
    with patch(
        "swarm.durable.activities.run_anticheat_dimension._invoke_reviewer",
        mock_invoke,
    ):
        env = ActivityEnvironment()
        res = await env.run(
            run_anticheat_dimension, "scope_reduction", _context(), _config()
        )

    assert res.verdict == "pass"
    assert res.rationale == "fenced"


@pytest.mark.asyncio
async def test_mission_metadata_passes_through_to_finding():
    """mission_id / session_id / spawner_id on the context must end up on
    the finding payload so downstream routers know which mission the
    verdict belongs to."""
    ctx = _context(
        mission_id="m-X",
        session_id="s-Y",
        spawner_id="spawner-Z",
    )
    mock_invoke = AsyncMock(
        return_value=json.dumps({"verdict": "fail", "rationale": "cheat"})
    )
    with patch(
        "swarm.durable.activities.run_anticheat_dimension._invoke_reviewer",
        mock_invoke,
    ):
        env = ActivityEnvironment()
        res = await env.run(
            run_anticheat_dimension, "mock_out", ctx, _config()
        )

    assert res.finding["mission_id"] == "m-X"
    assert res.finding["subject_session"] == "s-Y"
    assert res.finding["spawner_id"] == "spawner-Z"
