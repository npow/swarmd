"""Tests for the ``goal_drift_check`` Temporal activity.

Per plan Task 10 and spec §6.3:

    goal_drift_check(context) → GoalDriftResult

    Cadence-driven LLM review that compares a subject agent's mission +
    stated plans against its actual tool calls. Emits a ``drift`` (or
    ``fabrication`` for thinking-action mismatch) finding.

Ported from ``specialists/goal_drift_critic.py``. The prompt template and
verdict parsing are preserved; the interface is restructured to:

* Accept a plain ``dict`` context (``mission``, ``thinking``, ``plan_reports``,
  ``post_plan_tools``, ``assistant_text``, plus mission metadata).
* Invoke Haiku via the anthropic SDK (mockable via ``_invoke_haiku``).
* Return a ``GoalDriftResult`` with ``verdict``, ``rationale``, and a
  ``finding`` dict ready for ``emit_finding``.

These tests drive the activity through ``temporalio.testing.ActivityEnvironment``
so no running Temporal server is required. All anthropic interactions are
mocked.

Invariants covered:

1. Happy path — a well-formed ``drifting`` verdict produces the right
   ProgressAuditResult with type=``drift``, severity=``major``.
2. ``on_track`` verdict returns a benign ``info``-typed finding.
3. ``off_task`` is critical severity.
4. ``plan_fabrication`` is a fabrication finding, not a drift finding.
5. Malformed JSON → ``TerminalError``.
6. HTTP 429 → ``TransientError``.
7. HTTP 401 → ``AuthError`` (terminal).
8. Verdict set matches the critic's dimension vocabulary
   (``on_track | drifting | off_task | plan_fabrication | unclear``).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from anthropic import APIStatusError, AuthenticationError, RateLimitError
from temporalio.testing import ActivityEnvironment

from swarmd.durable.activities.goal_drift_check import (
    GoalDriftResult,
    goal_drift_check,
)
from swarmd.durable.errors import AuthError, TerminalError, TransientError


def _make_mock_anthropic(text: str) -> MagicMock:
    """Build a MagicMock that, when used as ``Anthropic()``, returns a
    client whose ``messages.create(...)`` returns an object with
    ``content[0].text`` equal to ``text``. Mirrors the SDK surface we use."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    content_block = MagicMock()
    content_block.text = text
    mock_response.content = [content_block]
    mock_client.messages.create.return_value = mock_response
    mock_ctor = MagicMock(return_value=mock_client)
    return mock_ctor


def _context(**overrides) -> dict:
    """Default goal_drift_check context used across tests."""
    base = {
        "mission_id": "m-abc",
        "session_id": "s-123",
        "spawner_id": "spawner-xyz",
        "mission": "Implement the login endpoint with JWT.",
        "thinking": "[turn 1] I should implement JWT.",
        "plan_reports": "[turn 2] I'll write the login handler.",
        "post_plan_tools": "[after turn 2] Write(path='login.py')",
        "assistant_text": "[turn 2] Starting on login.py now.",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_happy_path_drifting():
    """Drifting verdict → type=drift, severity=major, evidence turn IDs
    preserved on the finding."""
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {
                "verdict": "drifting",
                "reason": "Agent started working on analytics, not login.",
                "evidence_turn_ids": ["t3", "t4"],
            }
        )
    )
    with patch("swarmd.durable.activities.goal_drift_check.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        res = await env.run(goal_drift_check, _context())

    assert isinstance(res, GoalDriftResult)
    assert res.verdict == "drifting"
    assert "analytics" in res.rationale.lower() or "login" in res.rationale.lower()
    assert res.finding["type"] == "drift"
    assert res.finding["subtype"] == "drifting"
    assert res.finding["severity"] == "major"
    assert res.finding["source"].startswith("goal_drift_check")


@pytest.mark.asyncio
async def test_happy_path_on_track():
    """on_track verdict returns a benign ``info`` finding. No drift finding
    is raised."""
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {
                "verdict": "on_track",
                "reason": "Every tool call maps to the login mission.",
                "evidence_turn_ids": [],
            }
        )
    )
    with patch("swarmd.durable.activities.goal_drift_check.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        res = await env.run(goal_drift_check, _context())

    assert res.verdict == "on_track"
    assert res.finding["type"] == "info"
    assert res.finding["subtype"] == "on_track"


@pytest.mark.asyncio
async def test_off_task_is_critical():
    """off_task is a critical-severity drift — the agent is working on
    something entirely unrelated."""
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {
                "verdict": "off_task",
                "reason": "Agent is reorganizing unrelated files.",
                "evidence_turn_ids": ["t7"],
            }
        )
    )
    with patch("swarmd.durable.activities.goal_drift_check.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        res = await env.run(goal_drift_check, _context())

    assert res.verdict == "off_task"
    assert res.finding["type"] == "drift"
    assert res.finding["severity"] == "critical"


@pytest.mark.asyncio
async def test_plan_fabrication_is_fabrication_type():
    """plan_fabrication is a fabrication, not a drift — the thinking-action
    mismatch is a grounding failure, not a topic shift."""
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {
                "verdict": "plan_fabrication",
                "reason": "Stated plan to write tests; then wrote no tests.",
                "evidence_turn_ids": ["t9"],
            }
        )
    )
    with patch("swarmd.durable.activities.goal_drift_check.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        res = await env.run(goal_drift_check, _context())

    assert res.verdict == "plan_fabrication"
    assert res.finding["type"] == "fabrication"
    assert res.finding["severity"] == "critical"


@pytest.mark.asyncio
async def test_unclear_returns_info():
    """unclear is a valid terminal verdict and maps to a benign info finding."""
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {
                "verdict": "unclear",
                "reason": "Evidence too thin to judge drift.",
                "evidence_turn_ids": [],
            }
        )
    )
    with patch("swarmd.durable.activities.goal_drift_check.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        res = await env.run(goal_drift_check, _context())

    assert res.verdict == "unclear"
    assert res.finding["type"] == "info"


@pytest.mark.asyncio
async def test_malformed_json_raises_terminal():
    """Malformed JSON → TerminalError. Retries won't produce a valid parse."""
    mock_ctor = _make_mock_anthropic("totally not json {")
    with patch("swarmd.durable.activities.goal_drift_check.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        with pytest.raises(TerminalError):
            await env.run(goal_drift_check, _context())


@pytest.mark.asyncio
async def test_bad_verdict_value_raises_terminal():
    """A recognized-JSON body with an unrecognized ``verdict`` value is
    still a terminal parse failure."""
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {
                "verdict": "fizzbuzz",
                "reason": "nope",
                "evidence_turn_ids": [],
            }
        )
    )
    with patch("swarmd.durable.activities.goal_drift_check.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        with pytest.raises(TerminalError):
            await env.run(goal_drift_check, _context())


def _make_api_status_error(status_code: int, cls=APIStatusError) -> Exception:
    """Construct an anthropic APIStatusError (or subclass) with a specific
    HTTP status. The SDK normally builds these from real responses; we mock."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code=status_code, request=request)
    return cls(message=f"HTTP {status_code}", response=response, body=None)


@pytest.mark.asyncio
async def test_rate_limit_raises_transient():
    """anthropic.RateLimitError (429) → TransientError via classify_http_status."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _make_api_status_error(
        429, cls=RateLimitError
    )
    mock_ctor = MagicMock(return_value=mock_client)

    with patch("swarmd.durable.activities.goal_drift_check.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        with pytest.raises(TransientError):
            await env.run(goal_drift_check, _context())


@pytest.mark.asyncio
async def test_overloaded_raises_transient():
    """A 424 (mistral overloaded-style) APIStatusError routes to TransientError."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _make_api_status_error(424)
    mock_ctor = MagicMock(return_value=mock_client)

    with patch("swarmd.durable.activities.goal_drift_check.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        with pytest.raises(TransientError):
            await env.run(goal_drift_check, _context())


@pytest.mark.asyncio
async def test_auth_error_raises_terminal():
    """anthropic.AuthenticationError (401) → AuthError (TerminalError)."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _make_api_status_error(
        401, cls=AuthenticationError
    )
    mock_ctor = MagicMock(return_value=mock_client)

    with patch("swarmd.durable.activities.goal_drift_check.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        with pytest.raises(AuthError):
            await env.run(goal_drift_check, _context())


@pytest.mark.asyncio
async def test_model_id_is_haiku():
    """Lock the model choice: must be Haiku, not Opus or Sonnet."""
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {"verdict": "on_track", "reason": "ok", "evidence_turn_ids": []}
        )
    )
    with patch("swarmd.durable.activities.goal_drift_check.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        await env.run(goal_drift_check, _context())

    mock_client = mock_ctor.return_value
    kwargs = mock_client.messages.create.call_args.kwargs
    assert "haiku" in kwargs["model"].lower()


@pytest.mark.asyncio
async def test_context_content_passed_into_prompt():
    """The mission + thinking + plan_reports + tools must reach the prompt."""
    ctx = _context(
        mission="Build the auth module.",
        thinking="[turn 3] I'll write middleware.",
        plan_reports="[turn 4] I'll implement the password hasher.",
        post_plan_tools="[after turn 4] Write(path='hasher.py')",
        assistant_text="[turn 4] Starting on hasher.py.",
    )
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {"verdict": "on_track", "reason": "ok", "evidence_turn_ids": []}
        )
    )
    with patch("swarmd.durable.activities.goal_drift_check.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        await env.run(goal_drift_check, ctx)

    mock_client = mock_ctor.return_value
    kwargs = mock_client.messages.create.call_args.kwargs
    messages_str = json.dumps(kwargs["messages"])
    assert "Build the auth module." in messages_str
    assert "hasher.py" in messages_str
    assert "Starting on hasher.py." in messages_str


@pytest.mark.asyncio
async def test_evidence_turn_ids_preserved_on_finding():
    """Evidence turn IDs from the LLM output must end up on the finding so
    downstream consumers can jump straight to the flagged turns."""
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {
                "verdict": "drifting",
                "reason": "wandering",
                "evidence_turn_ids": ["t3", "t5", "t9"],
            }
        )
    )
    with patch("swarmd.durable.activities.goal_drift_check.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        res = await env.run(goal_drift_check, _context())

    assert res.finding["evidence"]["tool_calls"] == ["t3", "t5", "t9"]
