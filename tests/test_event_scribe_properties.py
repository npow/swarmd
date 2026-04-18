"""Property-based tests for event_scribe.

Fuzzes emit_event + read_events to verify:
  - Every emitted event is readable back (round-trip)
  - Redaction doesn't eat non-secret content
  - Large tool responses always spill to detail files and are truncated at the cap
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from swarm.specialists.event_scribe import (
    MAX_DETAIL_BYTES,
    emit_event,
    read_events,
    redact,
)


def _fresh_session(monkeypatch, tmp_path_factory) -> str:
    """Build an isolated swarm_root + session for one hypothesis example."""
    import uuid as _uuid

    from swarm.lib.paths import _reset_for_tests, ensure_session_dirs

    root = tmp_path_factory.mktemp(f"hs-{_uuid.uuid4().hex[:8]}")
    monkeypatch.setenv("SWARM_ROOT", str(root))
    monkeypatch.setenv("SWARM_CONFIG", str(root / "cfg"))
    _reset_for_tests()
    sid = _uuid.uuid4().hex[:12]
    ensure_session_dirs(sid)
    return sid


@given(
    n=st.integers(min_value=1, max_value=15),
    tool=st.sampled_from(["Edit", "Read", "Bash", "Grep"]),
)
@settings(
    deadline=None,
    max_examples=10,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_emit_read_roundtrip_multiple(tmp_path_factory, monkeypatch, n, tool):
    session_id = _fresh_session(monkeypatch, tmp_path_factory)
    """Every emitted event is readable back via read_events."""
    ids = []
    for i in range(n):
        ev = emit_event(
            session_id=session_id,
            hook="PostToolUse",
            tool_name=tool,
            tool_input_summary=f"input-{i}",
            tool_response_summary=f"response-{i}",
        )
        ids.append(ev.id)
    events = read_events(session_id)
    assert len(events) == n
    got_ids = [e.id for e in events]
    assert got_ids == ids  # order preserved


@given(
    # Alphabet deliberately excludes `:`, `=`, digits, and token-prefix letters
    # to avoid accidentally triggering redaction patterns on prefix/suffix.
    prefix=st.text(alphabet="qwertyuiopasdfghjklzxcvbnm ,.", min_size=0, max_size=60),
    suffix=st.text(alphabet="qwertyuiopasdfghjklzxcvbnm ,.", min_size=0, max_size=60),
)
@settings(deadline=None, max_examples=30)
def test_redact_preserves_surrounding_content(prefix, suffix):
    """Redaction of a sensitive pattern leaves non-sensitive context intact."""
    secret = "ghp_" + "a" * 40
    orig = prefix + " " + secret + " " + suffix  # guard with spaces
    out = redact(orig)
    assert prefix in out
    assert suffix in out
    assert secret not in out


@given(
    size_mb=st.integers(min_value=2, max_value=5),
)
@settings(
    deadline=None,
    max_examples=3,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_large_response_caps_at_max_detail(tmp_path_factory, monkeypatch, size_mb):
    session_id = _fresh_session(monkeypatch, tmp_path_factory)
    """Tool responses larger than MAX_DETAIL_BYTES are capped on disk."""
    import json
    from pathlib import Path

    big = "x" * (size_mb * 1_000_000)
    ev = emit_event(
        session_id=session_id,
        hook="PostToolUse",
        tool_name="Read",
        tool_response_full=big,
    )
    assert ev.detail_ref is not None
    detail = json.loads(Path(ev.detail_ref).read_text())
    assert len(detail["tool_response"]) <= MAX_DETAIL_BYTES
    assert detail["original_size"] == len(big)
    assert detail["truncated"] is True


@given(text=st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=0,
    max_size=500,
))
@settings(deadline=None, max_examples=50)
def test_redact_idempotent_on_clean_text(text):
    """redact(redact(x)) == redact(x) for any input."""
    once = redact(text)
    twice = redact(once)
    assert twice == once
