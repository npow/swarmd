"""Tests for lib/llm_client.py — gateway LLM client."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

from swarm.lib.llm_client import LLMError, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(text: str) -> MagicMock:
    """Build a mock Anthropic message response with a single TextBlock."""
    block = MagicMock()
    block.text = text
    msg = MagicMock()
    msg.content = [block]
    return msg


def _make_rate_limit_error() -> "anthropic.RateLimitError":
    import anthropic
    resp = httpx.Response(429, request=httpx.Request("POST", "http://test"))
    return anthropic.RateLimitError("rate limited", response=resp, body=None)


def _make_bad_request_error() -> "anthropic.BadRequestError":
    import anthropic
    resp = httpx.Response(400, request=httpx.Request("POST", "http://test"))
    return anthropic.BadRequestError("bad request", response=resp, body=None)


# ---------------------------------------------------------------------------
# Test 1: basic happy path
# ---------------------------------------------------------------------------

def test_call_returns_text_from_mocked_client():
    mock_create = MagicMock(return_value=_make_response("hello world"))
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with patch("swarm.lib.llm_client.anthropic.Anthropic", return_value=mock_client):
        result = call("hi")

    assert result == "hello world"


# ---------------------------------------------------------------------------
# Test 2: env var ANTHROPIC_BASE_URL overrides default
# ---------------------------------------------------------------------------

def test_env_var_overrides_base_url(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://example.com")
    mock_create = MagicMock(return_value=_make_response("ok"))
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with patch("swarm.lib.llm_client.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value = mock_client
        call("hi")
        _, kwargs = mock_cls.call_args
        assert kwargs.get("base_url") == "http://example.com"


# ---------------------------------------------------------------------------
# Test 3: env var SWARM_LLM_MODEL overrides model
# ---------------------------------------------------------------------------

def test_env_var_overrides_model(monkeypatch):
    monkeypatch.setenv("SWARM_LLM_MODEL", "claude-haiku-4-5-20251001")
    mock_create = MagicMock(return_value=_make_response("ok"))
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with patch("swarm.lib.llm_client.anthropic.Anthropic", return_value=mock_client):
        call("hi")
        _, kwargs = mock_create.call_args
        assert kwargs.get("model") == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Test 4: retries on RateLimitError, then succeeds
# ---------------------------------------------------------------------------

def test_retries_on_rate_limit_succeeds():
    err = _make_rate_limit_error()
    success = _make_response("done")
    mock_create = MagicMock(side_effect=[err, err, success])
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with patch("swarm.lib.llm_client.anthropic.Anthropic", return_value=mock_client):
        with patch("swarm.lib.llm_client.time.sleep"):  # skip real sleeps
            result = call("hi")

    assert result == "done"
    assert mock_create.call_count == 3


# ---------------------------------------------------------------------------
# Test 5: retries exhausted raises LLMError (4 attempts total)
# ---------------------------------------------------------------------------

def test_retries_exhausted_raises_LLMError():
    err = _make_rate_limit_error()
    mock_create = MagicMock(side_effect=[err, err, err, err])
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with patch("swarm.lib.llm_client.anthropic.Anthropic", return_value=mock_client):
        with patch("swarm.lib.llm_client.time.sleep"):
            with pytest.raises(LLMError):
                call("hi")

    assert mock_create.call_count == 4


# ---------------------------------------------------------------------------
# Test 6: BadRequestError not retried — exactly 1 attempt
# ---------------------------------------------------------------------------

def test_bad_request_not_retried():
    err = _make_bad_request_error()
    mock_create = MagicMock(side_effect=err)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with patch("swarm.lib.llm_client.anthropic.Anthropic", return_value=mock_client):
        with pytest.raises(LLMError):
            call("hi")

    assert mock_create.call_count == 1


# ---------------------------------------------------------------------------
# Test 7: timeout is passed through to messages.create
# ---------------------------------------------------------------------------

def test_timeout_is_passed_through():
    mock_create = MagicMock(return_value=_make_response("ok"))
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with patch("swarm.lib.llm_client.anthropic.Anthropic", return_value=mock_client):
        call("hi", timeout=42.0)
        _, kwargs = mock_create.call_args
        assert kwargs.get("timeout") == 42.0


# ---------------------------------------------------------------------------
# Test 8: system prompt is passed through to messages.create
# ---------------------------------------------------------------------------

def test_system_prompt_is_passed_through():
    mock_create = MagicMock(return_value=_make_response("ok"))
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with patch("swarm.lib.llm_client.anthropic.Anthropic", return_value=mock_client):
        call("hi", system="you are X")
        _, kwargs = mock_create.call_args
        assert kwargs.get("system") == "you are X"
