"""Tests for pattern_detector — pure-function level."""

from __future__ import annotations

from swarm.schemas.event import Event
from swarm.schemas.mission import PatternThresholds
from swarm.specialists.pattern_detector import (
    detect_loops,
    detect_once,
    detect_oscillation,
    normalize_arg,
)


def _mk_event(i: int, tool: str, inp: str, resp: str = "") -> Event:
    return Event(
        id=f"e-{i}",
        session_id="s",
        spawner_id="s",
        ts_monotonic=float(i),
        ts_wall="2026-04-16T00:00:00Z",
        hook="PostToolUse",
        tool_name=tool,
        tool_input_summary=inp,
        tool_response_summary=resp,
    )


def test_normalize_arg_whitespace():
    assert normalize_arg("a  b") == "a b"
    assert normalize_arg("a/") == "a"
    assert normalize_arg("'x'") == '"x"'
    assert normalize_arg(None) == ""


def test_detect_loops_emits_when_threshold_met():
    thresh = PatternThresholds(loop_repeat_count=3, loop_window_events=50)
    evs = [_mk_event(i, "Edit", "file=foo.py") for i in range(5)]
    findings = detect_loops(evs, thresh)
    assert len(findings) == 1
    assert findings[0].type == "loop"
    assert findings[0].subtype == "repeat_exact_args"
    assert len(findings[0].cited_events) == 5


def test_detect_loops_below_threshold():
    thresh = PatternThresholds(loop_repeat_count=5)
    evs = [_mk_event(i, "Edit", "file=foo.py") for i in range(3)]
    assert detect_loops(evs, thresh) == []


def test_detect_loops_distinct_args_dont_trigger():
    thresh = PatternThresholds(loop_repeat_count=3)
    evs = [_mk_event(i, "Edit", f"file=f{i}.py") for i in range(5)]
    assert detect_loops(evs, thresh) == []


def test_detect_oscillation():
    thresh = PatternThresholds(oscillation_revert_count=2, oscillation_window_events=50)
    # simulate file going state A→B→A→B (two reverts to prior state)
    evs = [
        _mk_event(0, "Edit", "file=x.py", "content_hash=aaa"),
        _mk_event(1, "Edit", "file=x.py", "content_hash=bbb"),
        _mk_event(2, "Edit", "file=x.py", "content_hash=aaa"),  # revert
        _mk_event(3, "Edit", "file=x.py", "content_hash=bbb"),  # revert
    ]
    findings = detect_oscillation(evs, thresh)
    assert len(findings) == 1
    assert findings[0].subtype == "oscillation"
    assert "x.py" in findings[0].evidence.files


def test_detect_oscillation_no_reverts():
    thresh = PatternThresholds(oscillation_revert_count=2)
    evs = [
        _mk_event(i, "Edit", "file=x.py", f"content_hash={chr(ord('a')+i)*3}")
        for i in range(5)
    ]
    assert detect_oscillation(evs, thresh) == []


def test_detect_once(sample_mission):
    evs = [_mk_event(i, "Edit", "file=foo.py") for i in range(10)]
    # crank thresholds down
    sample_mission.observer_config.pattern_thresholds.loop_repeat_count = 3
    findings = detect_once(evs, sample_mission)
    assert any(f.type == "loop" for f in findings)
