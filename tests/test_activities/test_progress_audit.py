"""Tests for the ``progress_audit`` Temporal activity.

Per plan Task 10 and spec §6.3:

    progress_audit(context) → ProgressAuditResult

    Cadence-driven LLM review that grounds an agent's claims against tool
    evidence. Emits a ``fabrication`` finding (via ``ProgressAuditResult``)
    when recent assistant claims aren't supported by matching tool calls.

Ported from ``specialists/progress_auditor.py``. The prompt template and
verdict parsing are preserved; the interface is restructured to:

* Accept a plain ``dict`` context (``claims``, ``evidence``, plus mission
  metadata) rather than walking a JSONL transcript file.
* Invoke Haiku via the anthropic SDK (mockable via ``_invoke_haiku``).
* Return a ``ProgressAuditResult`` with ``verdict``, ``rationale``, and a
  ``finding`` dict ready for ``emit_finding``.

These tests drive the activity through ``temporalio.testing.ActivityEnvironment``
so no running Temporal server is required. All anthropic interactions are
mocked.

Invariants covered:

1. Happy path — a well-formed ``fabrication`` verdict JSON produces a
   populated ``ProgressAuditResult`` with the right dimension/verdict.
2. ``grounded`` verdict is a valid terminal outcome (the spec-named "progressing"
   equivalent in the auditor's vocabulary) and still returns a finding-ready
   payload tagged ``grounded`` with type=``info``.
3. Malformed JSON → ``TerminalError`` — retries won't fix a bad response.
4. Transient HTTP error (424/429) from anthropic → ``TransientError``.
5. Auth error (401/403) from anthropic → ``AuthError`` (a ``TerminalError``
   subclass — the mission can't recover without fresh credentials).
6. Verdict values match the auditor's dimension (``grounded`` | ``partial``
   | ``fabricated`` | ``unclear``).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from anthropic import APIStatusError, AuthenticationError, RateLimitError
from temporalio.testing import ActivityEnvironment

from swarm.durable.activities.progress_audit import (
    ProgressAuditResult,
    progress_audit,
)
from swarm.durable.errors import AuthError, TerminalError, TransientError


def _make_mock_anthropic(text: str) -> MagicMock:
    """Build a MagicMock that, when used as Anthropic(), returns a client
    whose ``messages.create(...)`` returns an object with ``content[0].text``
    equal to ``text``. Mirrors the real SDK surface we use."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    # Real SDK returns a Message with content: list of ContentBlock objects,
    # each exposing a .text attribute on TextBlock instances.
    content_block = MagicMock()
    content_block.text = text
    mock_response.content = [content_block]
    mock_client.messages.create.return_value = mock_response
    mock_ctor = MagicMock(return_value=mock_client)
    return mock_ctor


def _context(**overrides) -> dict:
    """Default progress_audit context used across tests. Override any field
    per-test by keyword."""
    base = {
        "mission_id": "m-abc",
        "session_id": "s-123",
        "spawner_id": "spawner-xyz",
        "claims": "[turn 1] Tests pass on the new API.",
        "evidence": "[turn 1] Read(path='/x.py')",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_happy_path_fabricated():
    """Fabricated verdict → dataclass populated, finding tagged ``fabrication``."""
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {
                "verdict": "fabricated",
                "unsupported_claims": ["Tests pass on the new API."],
                "reason": "No pytest tool_use was observed in the window.",
            }
        )
    )
    with patch("swarm.durable.activities.progress_audit.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        res = await env.run(progress_audit, _context())

    assert isinstance(res, ProgressAuditResult)
    assert res.verdict == "fabricated"
    assert "pytest" in res.rationale.lower() or "tool" in res.rationale.lower()
    assert res.finding["type"] == "fabrication"
    assert res.finding["subtype"] == "fabricated"
    assert res.finding["severity"] == "critical"
    assert res.finding["source"].startswith("progress_audit")


@pytest.mark.asyncio
async def test_happy_path_grounded():
    """Grounded verdict is still a valid outcome — it produces an
    informational finding (type=info) so callers can decide whether to
    emit or drop based on policy."""
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {
                "verdict": "grounded",
                "unsupported_claims": [],
                "reason": "Every claim matches a tool_use.",
            }
        )
    )
    with patch("swarm.durable.activities.progress_audit.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        res = await env.run(progress_audit, _context())

    assert isinstance(res, ProgressAuditResult)
    assert res.verdict == "grounded"
    # Finding payload is still present; caller decides whether to emit.
    assert res.finding["type"] == "info"
    assert res.finding["subtype"] == "grounded"


@pytest.mark.asyncio
async def test_partial_verdict_is_major_severity():
    """``partial`` verdict is a fabrication but only ``major`` severity
    (some claims supported, some not) — the severity distinction matters
    for how loudly the coordinator reacts."""
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {
                "verdict": "partial",
                "unsupported_claims": ["The endpoint returns 401"],
                "reason": "One of two claims is unsupported.",
            }
        )
    )
    with patch("swarm.durable.activities.progress_audit.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        res = await env.run(progress_audit, _context())

    assert res.verdict == "partial"
    assert res.finding["type"] == "fabrication"
    assert res.finding["subtype"] == "partial"
    assert res.finding["severity"] == "major"


@pytest.mark.asyncio
async def test_unclear_verdict_returned():
    """``unclear`` is a valid terminal verdict. No fabrication finding is
    raised — the payload is type=info so callers treat it as benign."""
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {
                "verdict": "unclear",
                "unsupported_claims": [],
                "reason": "Evidence is too thin to judge.",
            }
        )
    )
    with patch("swarm.durable.activities.progress_audit.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        res = await env.run(progress_audit, _context())

    assert res.verdict == "unclear"
    assert res.finding["type"] == "info"


@pytest.mark.asyncio
async def test_malformed_json_raises_terminal():
    """Malformed JSON from the model → ``TerminalError``. Retries won't
    turn nonsense into valid JSON, so Temporal should fail fast."""
    mock_ctor = _make_mock_anthropic("not valid json at all <<<>>>")
    with patch("swarm.durable.activities.progress_audit.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        with pytest.raises(TerminalError):
            await env.run(progress_audit, _context())


@pytest.mark.asyncio
async def test_bad_verdict_value_raises_terminal():
    """A JSON body with an unrecognized ``verdict`` value is also a
    terminal parse failure — the model output doesn't satisfy the contract."""
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {
                "verdict": "tubular",
                "unsupported_claims": [],
                "reason": "whatever",
            }
        )
    )
    with patch("swarm.durable.activities.progress_audit.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        with pytest.raises(TerminalError):
            await env.run(progress_audit, _context())


def _make_api_status_error(status_code: int, cls=APIStatusError) -> Exception:
    """Build an anthropic APIStatusError (or subclass) with a given HTTP
    status. The SDK constructs these internally; we mimic the shape we
    need (the status comes from the response)."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code=status_code, request=request)
    return cls(message=f"HTTP {status_code}", response=response, body=None)


@pytest.mark.asyncio
async def test_rate_limit_raises_transient():
    """anthropic.RateLimitError (429) → TransientError. Retries with backoff
    are the correct remedy."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _make_api_status_error(
        429, cls=RateLimitError
    )
    mock_ctor = MagicMock(return_value=mock_client)

    with patch("swarm.durable.activities.progress_audit.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        with pytest.raises(TransientError):
            await env.run(progress_audit, _context())


@pytest.mark.asyncio
async def test_overloaded_raises_transient():
    """A 424 / 529 (overloaded) APIStatusError also routes to TransientError
    via classify_http_status."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _make_api_status_error(424)
    mock_ctor = MagicMock(return_value=mock_client)

    with patch("swarm.durable.activities.progress_audit.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        with pytest.raises(TransientError):
            await env.run(progress_audit, _context())


@pytest.mark.asyncio
async def test_auth_error_raises_terminal():
    """anthropic.AuthenticationError (401) → AuthError (which is a
    TerminalError). Retries are pointless when credentials are invalid."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _make_api_status_error(
        401, cls=AuthenticationError
    )
    mock_ctor = MagicMock(return_value=mock_client)

    with patch("swarm.durable.activities.progress_audit.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        with pytest.raises(AuthError):
            await env.run(progress_audit, _context())


@pytest.mark.asyncio
async def test_model_id_is_haiku():
    """The activity must call Haiku, not Opus or Sonnet. We assert on the
    ``model`` kwarg passed to ``messages.create`` to lock the choice in."""
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {"verdict": "grounded", "unsupported_claims": [], "reason": "ok"}
        )
    )
    with patch("swarm.durable.activities.progress_audit.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        await env.run(progress_audit, _context())

    mock_client = mock_ctor.return_value
    kwargs = mock_client.messages.create.call_args.kwargs
    assert "haiku" in kwargs["model"].lower()


@pytest.mark.asyncio
async def test_context_content_passed_into_prompt():
    """The ``claims`` and ``evidence`` from the context must reach the
    prompt body — otherwise the LLM has nothing to audit."""
    ctx = _context(
        claims="[turn 5] Database migration succeeded.",
        evidence="[turn 5] Bash(cmd='ls')",
    )
    mock_ctor = _make_mock_anthropic(
        json.dumps(
            {
                "verdict": "fabricated",
                "unsupported_claims": ["Database migration succeeded."],
                "reason": "No migration tool call observed.",
            }
        )
    )
    with patch("swarm.durable.activities.progress_audit.Anthropic", mock_ctor):
        env = ActivityEnvironment()
        await env.run(progress_audit, ctx)

    mock_client = mock_ctor.return_value
    kwargs = mock_client.messages.create.call_args.kwargs
    # The SDK takes ``messages=[{"role": ..., "content": ...}]``; flatten to a
    # single string so we can search it without caring which slot the prompt
    # text landed in.
    messages_str = json.dumps(kwargs["messages"])
    assert "Database migration succeeded." in messages_str
    assert "Bash(cmd='ls')" in messages_str
