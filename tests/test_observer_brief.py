"""Tests for the PostToolUse observer-status-brief feature."""

from __future__ import annotations

from swarmd.hooks.on_post_tool_use import _bump_brief_counter, _observer_brief
from swarmd.lib.locking import write_line
from swarmd.lib.paths import findings_path, strikes_path
from swarmd.schemas.finding import Finding


def test_brief_counter_increments(session_id):
    assert _bump_brief_counter(session_id) == 1
    assert _bump_brief_counter(session_id) == 2
    assert _bump_brief_counter(session_id) == 3


def test_observer_brief_none_with_no_findings(session_id):
    # No findings file
    assert _observer_brief(session_id) is None


def test_observer_brief_summarizes_findings(session_id):
    f1 = Finding(
        id="f-1",
        source="pattern_detector.loop",
        subject_session=session_id,
        spawner_id=session_id,
        type="loop",
        subtype="repeat_exact_args",
        severity="major",
        verdict="Edit repeated 5×",
    )
    f2 = Finding(
        id="f-2",
        source="anticheat",
        subject_session=session_id,
        spawner_id=session_id,
        type="cheat",
        subtype="scope_reduction",
        severity="critical",
        verdict="test deleted",
    )
    write_line(findings_path(session_id), f1.model_dump_json())
    write_line(findings_path(session_id), f2.model_dump_json())

    # Seed a strike
    strikes_path(session_id).write_text('{"sig1": 3, "sig2": 1}')

    brief = _observer_brief(session_id)
    assert brief is not None
    assert "strikes=4" in brief
    assert "loop.repeat_exact_args" in brief
    assert "cheat.scope_reduction" in brief
    assert "test deleted" in brief
