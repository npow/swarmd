"""Integration test: ensure the PostToolUse hook's output format is what
the pattern_detector expects to consume. This guards against silent breakage
of the data contract between the two components.
"""

from __future__ import annotations

from swarm.hooks.on_post_tool_use import _summarize_input, _summarize_response
from swarm.schemas.event import Event
from swarm.schemas.mission import PatternThresholds
from swarm.specialists.pattern_detector import (
    detect_loops,
    detect_oscillation,
    normalize_arg,
)


def _mk_edit_event(i: int, file_path: str, new_string: str) -> Event:
    """Simulate an event constructed via the hook's _summarize_*."""
    tool_input = {
        "file_path": file_path,
        "old_string": "before",
        "new_string": new_string,
    }
    tool_response = {"ok": True, "type": "edit"}
    in_summary = _summarize_input(tool_input, "Edit")
    resp_summary, _ = _summarize_response(tool_response, "Edit", tool_input)
    return Event(
        id=f"e-{i}",
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        ts_monotonic=float(i),
        ts_wall="2026-04-17T00:00:00Z",
        hook="PostToolUse",
        tool_name="Edit",
        tool_input_summary=in_summary,
        tool_response_summary=resp_summary,
    )


def test_loop_detector_can_consume_real_hook_output():
    """5 identical Edits should be detected as a loop."""
    # Same file_path, identical content → identical input summaries
    same = [_mk_edit_event(i, "/abs/foo.py", "constant") for i in range(5)]
    findings = detect_loops(same, PatternThresholds(loop_repeat_count=3))
    assert len(findings) == 1, f"loop detector failed on hook output: {[e.tool_input_summary for e in same]}"
    assert findings[0].subtype == "repeat_exact_args"


def test_oscillation_detector_can_consume_real_hook_output():
    """File going A→B→A→B should be detected via real hook content_hash output."""
    events = [
        _mk_edit_event(0, "/abs/x.py", "AAAA"),
        _mk_edit_event(1, "/abs/x.py", "BBBB"),
        _mk_edit_event(2, "/abs/x.py", "AAAA"),  # revert
        _mk_edit_event(3, "/abs/x.py", "BBBB"),  # revert
    ]
    # Verify the hook actually emits content_hash= and file= tokens the
    # detector can parse — fail loudly if the contract drifts
    for ev in events:
        assert ev.tool_input_summary and "file=" in ev.tool_input_summary, (
            f"hook did not emit `file=` tag: {ev.tool_input_summary}"
        )
        assert ev.tool_response_summary and "content_hash=" in ev.tool_response_summary, (
            f"hook did not emit `content_hash=` tag: {ev.tool_response_summary}"
        )

    findings = detect_oscillation(
        events, PatternThresholds(oscillation_revert_count=2)
    )
    assert len(findings) == 1, "oscillation detector failed on real hook output"
    assert findings[0].subtype == "oscillation"
    assert any("/abs/x.py" in f for f in findings[0].evidence.files)


def test_content_hash_survives_truncation():
    """A huge tool response must still expose its content_hash to the detector."""
    big_response = {"data": "x" * 50_000}
    in_blob = {"file_path": "/abs/big.py", "content": "small content"}
    summary, _ = _summarize_response(big_response, "Edit", in_blob)
    assert "content_hash=" in (summary or ""), (
        "content_hash was lost during truncation"
    )


def test_normalize_arg_idempotent_on_real_hook_input():
    in_blob = {"file_path": "/abs/foo.py", "new_string": "x"}
    s = _summarize_input(in_blob, "Edit") or ""
    assert normalize_arg(s) == normalize_arg(normalize_arg(s))
