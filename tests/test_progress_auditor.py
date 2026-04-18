"""Tests for progress_auditor."""

from __future__ import annotations

import json
from pathlib import Path

from swarm.specialists.progress_auditor import AuditResult, _collect, _parse, audit


def _write_transcript(path: Path, turns: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


def _assistant(text: str = "", tool_uses: list[dict] | None = None) -> dict:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    for tu in tool_uses or []:
        content.append({"type": "tool_use", **tu})
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _parse_valid():
    raw = '{"verdict": "grounded", "unsupported_claims": [], "reason": "ok"}'
    r = _parse(raw)
    assert r.verdict == "grounded"


def test_parse_grounded():
    _parse_valid()


def test_parse_fabricated_with_claims():
    raw = '{"verdict": "fabricated", "unsupported_claims": ["tests pass"], "reason": "no pytest"}'
    r = _parse(raw)
    assert r.verdict == "fabricated"
    assert r.unsupported_claims == ["tests pass"]


def test_parse_unparseable_fails_safe():
    r = _parse("not json")
    assert r.verdict == "unclear"


def test_parse_bad_verdict():
    r = _parse('{"verdict": "maybe", "unsupported_claims": []}')
    assert r.verdict == "unclear"


def test_parse_strips_fences():
    r = _parse('```\n{"verdict": "partial", "unsupported_claims": ["x"], "reason": "r"}\n```')
    assert r.verdict == "partial"


def test_collect_empty_transcript(tmp_path):
    t = tmp_path / "e.jsonl"
    _write_transcript(t, [])
    out = _collect(t)
    assert out["claims"] == "(none)"
    assert out["evidence"] == "(none)"


def test_collect_separates_claims_and_evidence(tmp_path):
    t = tmp_path / "t.jsonl"
    _write_transcript(
        t,
        [
            _assistant(
                text="All tests pass",
                tool_uses=[
                    {"name": "Bash", "input": {"command": "pytest"}, "id": "tool1"}
                ],
            )
        ],
    )
    out = _collect(t)
    assert "All tests pass" in out["claims"]
    assert "Bash" in out["evidence"]


def test_audit_grounded_returns_no_finding(tmp_path):
    t = tmp_path / "g.jsonl"
    _write_transcript(t, [_assistant(text="x")])
    out = audit(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        transcript_path=t,
        llm=lambda _p: '{"verdict": "grounded", "unsupported_claims": [], "reason": "ok"}',
    )
    assert out == []


def test_audit_fabricated_returns_critical_finding(tmp_path):
    t = tmp_path / "f.jsonl"
    _write_transcript(t, [_assistant(text="tests pass")])
    out = audit(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        transcript_path=t,
        llm=lambda _p: (
            '{"verdict": "fabricated",'
            '"unsupported_claims": ["tests pass — no pytest evidence"],'
            '"reason": "no evidence"}'
        ),
    )
    assert len(out) == 1
    assert out[0].severity == "critical"
    assert out[0].type == "fabrication"
    assert "tests pass" in out[0].evidence.claim_excerpt


def test_audit_partial_returns_major_finding(tmp_path):
    t = tmp_path / "p.jsonl"
    _write_transcript(t, [_assistant(text="x")])
    out = audit(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        transcript_path=t,
        llm=lambda _p: '{"verdict": "partial", "unsupported_claims": ["x"], "reason": "r"}',
    )
    assert len(out) == 1
    assert out[0].severity == "major"


def test_audit_unclear_returns_no_finding(tmp_path):
    t = tmp_path / "u.jsonl"
    _write_transcript(t, [_assistant(text="x")])
    out = audit(
        session_id="abcdef012345",
        spawner_id="abcdef012345",
        transcript_path=t,
        llm=lambda _p: "garbage",
    )
    assert out == []


def test_audit_result_frozen():
    r = AuditResult("grounded", [], "")
    try:
        r.verdict = "fabricated"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("AuditResult should be frozen")
