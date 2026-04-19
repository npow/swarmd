"""Tests for event_scribe size cap + secret redaction (review M2, M4)."""

from __future__ import annotations

import json
from pathlib import Path

from swarmd.specialists.event_scribe import MAX_DETAIL_BYTES, emit_event, redact


def test_redact_aws_key():
    s = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE was leaked"
    assert "AKIA" not in redact(s)


def test_redact_bearer_token():
    s = "Authorization: Bearer abcd1234efgh5678ijkl"
    out = redact(s)
    assert "abcd1234" not in out
    assert "REDACTED" in out


def test_redact_api_key_keyword_value():
    s = "secret = my_super_long_secret_value_123"
    out = redact(s)
    assert "my_super_long" not in out


def test_redact_github_token():
    s = "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    assert "ghp_" not in redact(s)


def test_redact_openai_key():
    s = "OPENAI_API_KEY=sk-proj-abcdefghij1234567890"
    out = redact(s)
    assert "sk-proj-abcdefghij1234567890" not in out


def test_redact_anthropic_key():
    s = "ANTHROPIC_API_KEY=sk-ant-api03-abcdefghij1234"
    assert "sk-ant-api03-abcdefghij1234" not in redact(s)


def test_redact_passes_through_clean_text():
    s = "Just regular log output, nothing sensitive."
    assert redact(s) == s


def test_emit_event_caps_large_detail(session_id):
    huge = "x" * (MAX_DETAIL_BYTES * 2)
    ev = emit_event(
        session_id=session_id,
        hook="PostToolUse",
        tool_name="Read",
        tool_response_full=huge,
    )
    assert ev.detail_ref is not None
    payload = json.loads(Path(ev.detail_ref).read_text())
    assert payload["truncated"] is True
    assert len(payload["tool_response"]) <= MAX_DETAIL_BYTES
    assert payload["original_size"] == len(huge)


def test_emit_event_redacts_in_summary(session_id):
    leaky = '{"output": "Bearer abcdefghijklmnopqrstuvwxyz1234567890"}'
    ev = emit_event(
        session_id=session_id,
        hook="PostToolUse",
        tool_name="Read",
        tool_response_summary=leaky,
    )
    assert "abcdefghijklmnopqrstuvwxyz1234567890" not in (ev.tool_response_summary or "")


def test_emit_event_redacts_in_detail(session_id):
    leaky = "ghp_" + "a" * 50  # >2000 char trigger... actually need bigger
    huge_leaky = leaky + ("x" * 5000)
    ev = emit_event(
        session_id=session_id,
        hook="PostToolUse",
        tool_name="Read",
        tool_response_full=huge_leaky,
    )
    if ev.detail_ref:
        body = Path(ev.detail_ref).read_text()
        assert "ghp_" + "a" * 50 not in body
