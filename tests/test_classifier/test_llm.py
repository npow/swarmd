"""Tests for stage 3 — ``swarm.classifier.llm.classify_llm``.

Mocks ``_invoke_haiku`` (the narrow test seam) for response-shape tests,
and mocks ``Anthropic`` directly for HTTP error translation tests. Both
patterns are documented in the module docstring.

pytest-asyncio is configured in ``asyncio_mode=auto`` (see pyproject.toml),
so async tests run without per-test decorators.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from anthropic import APIStatusError, AuthenticationError, RateLimitError

from swarmd.classifier.llm import classify_llm
from swarmd.classifier.rules import ClassifierResult, ClassifierVerdict
from swarmd.durable.errors import AuthError, TerminalError, TransientError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_response(
    verdict: str = "mission",
    confidence: float = 0.85,
    reason: str = "imperative verb on code",
    **extra,
) -> str:
    """Build a canonical JSON response body as a single line."""
    payload = {"verdict": verdict, "confidence": confidence, "reason": reason}
    payload.update(extra)
    return json.dumps(payload)


def _make_api_status_error(status_code: int, cls=APIStatusError) -> Exception:
    """Build an anthropic APIStatusError (or subclass) with a given HTTP
    status. The SDK constructs these internally; we mimic the shape we
    need — the status comes from ``exc.response.status_code``."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code=status_code, request=request)
    return cls(message=f"HTTP {status_code}", response=response, body=None)


# ---------------------------------------------------------------------------
# Happy paths — one per verdict
# ---------------------------------------------------------------------------


class TestHappyPaths:
    async def test_mission_verdict(self):
        raw = _json_response(
            verdict="mission",
            confidence=0.85,
            reason="imperative verb on code",
        )
        with patch(
            "swarmd.classifier.llm._invoke_haiku", AsyncMock(return_value=raw)
        ):
            result = await classify_llm("fix the flaky test")

        assert isinstance(result, ClassifierResult)
        assert result.verdict == ClassifierVerdict.MISSION
        assert result.stage == 3
        assert result.confidence == pytest.approx(0.85)
        assert "imperative" in result.reason

    async def test_chat_verdict(self):
        raw = _json_response(
            verdict="chat",
            confidence=0.9,
            reason="conceptual question",
        )
        with patch(
            "swarmd.classifier.llm._invoke_haiku", AsyncMock(return_value=raw)
        ):
            result = await classify_llm("what is asyncio?")

        assert result.verdict == ClassifierVerdict.CHAT
        assert result.stage == 3
        assert result.confidence == pytest.approx(0.9)

    async def test_meta_verdict(self):
        raw = _json_response(
            verdict="meta",
            confidence=0.92,
            reason="read-only query about mission",
        )
        with patch(
            "swarmd.classifier.llm._invoke_haiku", AsyncMock(return_value=raw)
        ):
            result = await classify_llm("how is the mission going?")

        assert result.verdict == ClassifierVerdict.META
        assert result.stage == 3
        assert result.confidence == pytest.approx(0.92)

    async def test_uncertain_verdict(self):
        raw = _json_response(
            verdict="uncertain",
            confidence=0.3,
            reason="genuinely ambiguous",
        )
        with patch(
            "swarmd.classifier.llm._invoke_haiku", AsyncMock(return_value=raw)
        ):
            result = await classify_llm("thoughts on this?")

        assert result.verdict == ClassifierVerdict.UNCERTAIN
        assert result.stage == 3
        assert result.confidence == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# Confidence parsing + clamping
# ---------------------------------------------------------------------------


class TestConfidenceClamping:
    async def test_confidence_clamped_to_one(self):
        raw = _json_response(verdict="mission", confidence=1.5, reason="x")
        with patch(
            "swarmd.classifier.llm._invoke_haiku", AsyncMock(return_value=raw)
        ):
            result = await classify_llm("fix X")
        assert result.confidence == 1.0

    async def test_confidence_clamped_to_zero(self):
        raw = _json_response(verdict="mission", confidence=-0.5, reason="x")
        with patch(
            "swarmd.classifier.llm._invoke_haiku", AsyncMock(return_value=raw)
        ):
            result = await classify_llm("fix X")
        assert result.confidence == 0.0

    async def test_confidence_parsed_as_int(self):
        """Int values from JSON should still parse as floats."""
        raw = json.dumps(
            {"verdict": "chat", "confidence": 1, "reason": "x"}
        )
        with patch(
            "swarmd.classifier.llm._invoke_haiku", AsyncMock(return_value=raw)
        ):
            result = await classify_llm("hi")
        assert result.confidence == 1.0


# ---------------------------------------------------------------------------
# Reason field parsing
# ---------------------------------------------------------------------------


class TestReasonField:
    async def test_missing_reason_uses_placeholder(self):
        raw = json.dumps({"verdict": "mission", "confidence": 0.8})
        with patch(
            "swarmd.classifier.llm._invoke_haiku", AsyncMock(return_value=raw)
        ):
            result = await classify_llm("fix X")
        assert result.reason == "(no reason provided)"

    async def test_long_reason_truncated_to_500(self):
        long_reason = "x" * 600
        raw = _json_response(
            verdict="mission", confidence=0.8, reason=long_reason
        )
        with patch(
            "swarmd.classifier.llm._invoke_haiku", AsyncMock(return_value=raw)
        ):
            result = await classify_llm("fix X")
        assert len(result.reason) == 500

    async def test_empty_reason_uses_placeholder(self):
        raw = _json_response(verdict="mission", confidence=0.8, reason="   ")
        with patch(
            "swarmd.classifier.llm._invoke_haiku", AsyncMock(return_value=raw)
        ):
            result = await classify_llm("fix X")
        assert result.reason == "(no reason provided)"


# ---------------------------------------------------------------------------
# JSON parsing — fences, verdict validation, malformed input
# ---------------------------------------------------------------------------


class TestJsonParsing:
    async def test_json_with_markdown_fence(self):
        """Haiku sometimes wraps JSON in ```json despite instructions."""
        payload = _json_response(verdict="mission", confidence=0.7, reason="x")
        fenced = f"```json\n{payload}\n```"
        with patch(
            "swarmd.classifier.llm._invoke_haiku",
            AsyncMock(return_value=fenced),
        ):
            result = await classify_llm("fix X")
        assert result.verdict == ClassifierVerdict.MISSION
        assert result.confidence == pytest.approx(0.7)

    async def test_json_with_bare_fence(self):
        """Plain ``` fence (no 'json' suffix) also strips correctly."""
        payload = _json_response(verdict="chat", confidence=0.8, reason="x")
        fenced = f"```\n{payload}\n```"
        with patch(
            "swarmd.classifier.llm._invoke_haiku",
            AsyncMock(return_value=fenced),
        ):
            result = await classify_llm("hi")
        assert result.verdict == ClassifierVerdict.CHAT

    async def test_bad_verdict_raises_terminal(self):
        """An unrecognized verdict value is a contract violation → terminal."""
        raw = _json_response(
            verdict="yes_do_it", confidence=0.9, reason="whatever"
        )
        with patch(
            "swarmd.classifier.llm._invoke_haiku", AsyncMock(return_value=raw)
        ):
            with pytest.raises(TerminalError):
                await classify_llm("fix X")

    async def test_malformed_json_raises_terminal(self):
        """Plain prose → TerminalError. Retries won't turn prose into JSON."""
        with patch(
            "swarmd.classifier.llm._invoke_haiku",
            AsyncMock(return_value="I think this is a mission"),
        ):
            with pytest.raises(TerminalError):
                await classify_llm("fix X")

    async def test_empty_response_raises_terminal(self):
        with patch(
            "swarmd.classifier.llm._invoke_haiku", AsyncMock(return_value="")
        ):
            with pytest.raises(TerminalError):
                await classify_llm("fix X")

    async def test_verdict_case_insensitive(self):
        """Upper-case ``MISSION`` should still parse (the enum values are
        lowercase; we normalize before lookup)."""
        raw = json.dumps(
            {"verdict": "MISSION", "confidence": 0.8, "reason": "x"}
        )
        with patch(
            "swarmd.classifier.llm._invoke_haiku", AsyncMock(return_value=raw)
        ):
            result = await classify_llm("fix X")
        assert result.verdict == ClassifierVerdict.MISSION


# ---------------------------------------------------------------------------
# HTTP error translation (patches Anthropic directly, not _invoke_haiku)
# ---------------------------------------------------------------------------


class TestHttpErrors:
    async def test_auth_error_from_401(self):
        """anthropic.AuthenticationError (401) → AuthError."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = _make_api_status_error(
            401, cls=AuthenticationError
        )
        mock_ctor = MagicMock(return_value=mock_client)
        with patch("swarmd.classifier.llm.Anthropic", mock_ctor):
            with pytest.raises(AuthError):
                await classify_llm("fix X")

    async def test_rate_limit_raises_transient(self):
        """anthropic.RateLimitError (429) → TransientError."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = _make_api_status_error(
            429, cls=RateLimitError
        )
        mock_ctor = MagicMock(return_value=mock_client)
        with patch("swarmd.classifier.llm.Anthropic", mock_ctor):
            with pytest.raises(TransientError):
                await classify_llm("fix X")

    async def test_overloaded_raises_transient(self):
        """A 424 APIStatusError also routes to TransientError."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = _make_api_status_error(424)
        mock_ctor = MagicMock(return_value=mock_client)
        with patch("swarmd.classifier.llm.Anthropic", mock_ctor):
            with pytest.raises(TransientError):
                await classify_llm("fix X")


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------


class TestTimeout:
    async def test_timeout_raises_transient(self, monkeypatch):
        """A _invoke_haiku that sleeps longer than _TIMEOUT_SEC → TransientError.

        We shrink the timeout to 0.05s so the test doesn't actually wait 10s.
        Per-test monkeypatch keeps the production constant intact.
        """
        import swarmd.classifier.llm as llm_mod

        monkeypatch.setattr(llm_mod, "_TIMEOUT_SEC", 0.05)

        async def slow_invoke(_prompt: str) -> str:
            await asyncio.sleep(1.0)  # longer than the patched timeout
            return _json_response()

        with patch(
            "swarmd.classifier.llm._invoke_haiku", AsyncMock(side_effect=slow_invoke)
        ):
            with pytest.raises(TransientError):
                await classify_llm("fix X")


# ---------------------------------------------------------------------------
# Prompt + context wiring
# ---------------------------------------------------------------------------


class TestPromptWiring:
    async def test_user_prompt_reaches_haiku(self):
        """The user prompt must appear in the text sent to _invoke_haiku,
        or Haiku has nothing to classify."""
        raw = _json_response()
        captured: list[str] = []

        async def capturing_invoke(prompt_text: str) -> str:
            captured.append(prompt_text)
            return raw

        with patch(
            "swarmd.classifier.llm._invoke_haiku",
            AsyncMock(side_effect=capturing_invoke),
        ):
            await classify_llm("fix the flaky test in test_auth.py")

        assert len(captured) == 1
        assert "fix the flaky test in test_auth.py" in captured[0]

    async def test_context_reaches_haiku(self):
        """The context dict must be rendered into the prompt body."""
        raw = _json_response()
        captured: list[str] = []

        async def capturing_invoke(prompt_text: str) -> str:
            captured.append(prompt_text)
            return raw

        context = {"cwd": "/tmp/project", "recent_file": "auth.py"}
        with patch(
            "swarmd.classifier.llm._invoke_haiku",
            AsyncMock(side_effect=capturing_invoke),
        ):
            await classify_llm("fix X", context=context)

        assert len(captured) == 1
        assert "/tmp/project" in captured[0]
        assert "auth.py" in captured[0]

    async def test_none_context_renders_as_none_literal(self):
        """``context=None`` should render as ``(none)`` so the prompt slot
        isn't left empty."""
        raw = _json_response()
        captured: list[str] = []

        async def capturing_invoke(prompt_text: str) -> str:
            captured.append(prompt_text)
            return raw

        with patch(
            "swarmd.classifier.llm._invoke_haiku",
            AsyncMock(side_effect=capturing_invoke),
        ):
            await classify_llm("fix X", context=None)

        assert "(none)" in captured[0]
