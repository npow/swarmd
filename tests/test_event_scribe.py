"""Tests for event_scribe."""

from __future__ import annotations

import json

from swarmd.lib.paths import events_path
from swarmd.specialists.event_scribe import emit_event, read_events


def test_emit_and_read(session_id):
    ev = emit_event(
        session_id=session_id,
        hook="PostToolUse",
        tool_name="Edit",
        tool_input_summary="file=foo.py",
    )
    assert ev.id.startswith("e-")
    events = read_events(session_id)
    assert len(events) == 1
    assert events[0].tool_name == "Edit"
    assert events[0].id == ev.id


def test_multiple_events_monotonic_ids(session_id):
    ids = [
        emit_event(session_id=session_id, hook="PostToolUse", tool_name="Read").id
        for _ in range(5)
    ]
    assert len(set(ids)) == 5  # all unique


def test_large_response_spills_to_detail(session_id):
    big = "x" * 50000
    ev = emit_event(
        session_id=session_id,
        hook="PostToolUse",
        tool_name="Read",
        tool_response_full=big,
    )
    assert ev.detail_ref is not None
    from pathlib import Path

    p = Path(ev.detail_ref)
    assert p.exists()
    payload = json.loads(p.read_text())
    assert payload["tool_response"] == big


def test_read_empty_session(session_id):
    # events.jsonl exists but is empty
    p = events_path(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    assert read_events(session_id) == []
