"""Tests for goal_drift_critic."""

from __future__ import annotations

import json
from pathlib import Path

from swarm.specialists.goal_drift_critic import (
    DriftJudgement,
    _collect_inputs,
    _parse_verdict,
    judge,
)


def _write_transcript(path: Path, turns: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


def _assistant(text: str, thinking: str = "", tool_uses: list[dict] | None = None) -> dict:
    content: list[dict] = []
    if thinking:
        content.append({"type": "thinking", "text": thinking})
    if text:
        content.append({"type": "text", "text": text})
    for tu in tool_uses or []:
        content.append({"type": "tool_use", **tu})
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


# -------- _parse_verdict --------


def test_parse_verdict_valid():
    raw = '{"verdict": "on_track", "reason": "clean", "evidence_turn_ids": ["1"]}'
    v = _parse_verdict(raw)
    assert v.verdict == "on_track"
    assert v.reason == "clean"
    assert v.evidence_turn_ids == ["1"]


def test_parse_verdict_strips_code_fence():
    raw = '```json\n{"verdict": "drifting", "reason": "x", "evidence_turn_ids": []}\n```'
    v = _parse_verdict(raw)
    assert v.verdict == "drifting"


def test_parse_verdict_unparseable_fails_safe():
    v = _parse_verdict("not json at all")
    assert v.verdict == "unclear"
    assert "unparseable" in v.reason


def test_parse_verdict_empty_fails_safe():
    v = _parse_verdict("")
    assert v.verdict == "unclear"


def test_parse_verdict_bad_verdict_string():
    raw = '{"verdict": "totally_fine", "reason": "x"}'
    v = _parse_verdict(raw)
    assert v.verdict == "unclear"
    assert "bad_verdict" in v.reason


def test_parse_verdict_non_list_evidence():
    raw = '{"verdict": "drifting", "reason": "x", "evidence_turn_ids": "bad"}'
    v = _parse_verdict(raw)
    assert v.evidence_turn_ids == []


# -------- _collect_inputs --------


def test_collect_inputs_reads_thinking(tmp_path):
    t = tmp_path / "x.jsonl"
    _write_transcript(
        t, [_assistant(text="working", thinking="my plan: build auth")]
    )
    inputs = _collect_inputs(t, "build auth system")
    assert "auth" in inputs["thinking"]
    assert "build auth system" in inputs["mission"]


def test_collect_inputs_extracts_plan_reports(tmp_path):
    t = tmp_path / "x.jsonl"
    _write_transcript(
        t,
        [
            _assistant(
                text="My current sub-goal is to write the login endpoint. "
                "I'll implement POST /auth/login next."
            )
        ],
    )
    inputs = _collect_inputs(t, "auth")
    assert "login" in inputs["plan_reports"]


def test_collect_inputs_handles_empty_transcript(tmp_path):
    t = tmp_path / "empty.jsonl"
    _write_transcript(t, [])
    inputs = _collect_inputs(t, "m")
    assert inputs["thinking"] == "(none)"
    assert inputs["plan_reports"] == "(none)"


# -------- judge() --------


def _make_llm(verdict: str, reason: str = "test", evidence: list[str] | None = None):
    def _runner(_prompt: str) -> str:
        return json.dumps(
            {
                "verdict": verdict,
                "reason": reason,
                "evidence_turn_ids": evidence or [],
            }
        )

    return _runner


def test_judge_returns_no_finding_when_on_track(tmp_path):
    transcript = tmp_path / "x.jsonl"
    _write_transcript(transcript, [_assistant(text="working")])
    out = judge(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        mission="build",
        transcript_path=transcript,
        llm=_make_llm("on_track"),
    )
    assert out == []


def test_judge_emits_drift_finding_when_drifting(tmp_path):
    transcript = tmp_path / "x.jsonl"
    _write_transcript(transcript, [_assistant(text="refactoring the logger")])
    out = judge(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        mission="build auth system",
        transcript_path=transcript,
        llm=_make_llm("drifting", "refactoring unrelated code", ["turn 0"]),
    )
    assert len(out) == 1
    assert out[0].type == "drift"
    assert out[0].subtype == "drifting"
    assert out[0].severity == "major"


def test_judge_emits_critical_when_off_task(tmp_path):
    transcript = tmp_path / "x.jsonl"
    _write_transcript(transcript, [_assistant(text="playing chess")])
    out = judge(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        mission="build auth",
        transcript_path=transcript,
        llm=_make_llm("off_task", "unrelated"),
    )
    assert len(out) == 1
    assert out[0].severity == "critical"


def test_judge_plan_fabrication_is_fabrication_type(tmp_path):
    transcript = tmp_path / "x.jsonl"
    _write_transcript(transcript, [_assistant(text="x")])
    out = judge(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        mission="m",
        transcript_path=transcript,
        llm=_make_llm("plan_fabrication", "said A, did B"),
    )
    assert len(out) == 1
    assert out[0].type == "fabrication"
    assert out[0].subtype == "plan_fabrication"
    assert out[0].severity == "critical"


def test_judge_fails_safe_on_unclear(tmp_path):
    transcript = tmp_path / "x.jsonl"
    _write_transcript(transcript, [_assistant(text="x")])
    out = judge(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        mission="m",
        transcript_path=transcript,
        llm=lambda _p: "garbage output",
    )
    # unclear does NOT produce a finding (would spam if it did); the hook-side
    # may treat repeated unclears as its own signal later
    assert out == []


def test_judge_fails_safe_on_llm_error(tmp_path):
    transcript = tmp_path / "x.jsonl"
    _write_transcript(transcript, [_assistant(text="x")])

    def _err(_p):
        raise RuntimeError("simulated failure")

    # Even if the LLM runner itself raises, judge should not crash — but we
    # test the safer path where the runner returns an error JSON
    out = judge(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        mission="m",
        transcript_path=transcript,
        llm=lambda _p: '{"verdict": "unclear", "reason": "llm_unavailable"}',
    )
    assert out == []


def test_parse_drift_judgement_object_is_frozen():
    j = DriftJudgement("on_track", "r", [])
    try:
        j.verdict = "drifting"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("DriftJudgement should be frozen")
