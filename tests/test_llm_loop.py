"""Tests for llm_loop — the daemon that actually runs the LLM specialists."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from swarmd.lib.paths import mission_yaml_path
from swarmd.schemas.mission import Mission
from swarmd.specialists.llm_loop import CycleResult, one_cycle


def _write_mission(session_id, workspace):
    p = mission_yaml_path(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump(
            {
                "mission": "build auth",
                "workspace": str(workspace),
                "success_criteria": [
                    {"id": "a", "description": "", "check": "true"}
                ],
            }
        )
    )


def _assistant(text: str = "", thinking: str = "") -> dict:
    content = []
    if thinking:
        content.append({"type": "thinking", "text": thinking})
    if text:
        content.append({"type": "text", "text": text})
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _write_transcript(path: Path, turns: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


def _mk_mission(tmp_path) -> Mission:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return Mission.model_validate(
        {
            "mission": "build auth",
            "workspace": str(workspace),
            "success_criteria": [
                {"id": "a", "description": "", "check": "true"}
            ],
        }
    )


def test_one_cycle_both_clean(tmp_path, session_id):
    m = _mk_mission(tmp_path)
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [_assistant(text="working on auth")])

    def _drift(_p: str) -> str:
        return json.dumps(
            {"verdict": "on_track", "reason": "ok", "evidence_turn_ids": []}
        )

    def _progress(_p: str) -> str:
        return json.dumps(
            {"verdict": "grounded", "unsupported_claims": [], "reason": "ok"}
        )

    result = one_cycle(
        session_id,
        m,
        transcript_path=transcript,
        drift_llm=_drift,
        progress_llm=_progress,
    )
    assert isinstance(result, CycleResult)
    assert result.drift_findings == []
    assert result.progress_findings == []


def test_one_cycle_drift_emits_finding(tmp_path, session_id):
    m = _mk_mission(tmp_path)
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [_assistant(text="refactoring unrelated logger")],
    )

    def _drift(_p: str) -> str:
        return json.dumps(
            {
                "verdict": "drifting",
                "reason": "refactor off-mission",
                "evidence_turn_ids": ["t0"],
            }
        )

    def _progress(_p: str) -> str:
        return json.dumps(
            {"verdict": "grounded", "unsupported_claims": [], "reason": "ok"}
        )

    result = one_cycle(
        session_id,
        m,
        transcript_path=transcript,
        drift_llm=_drift,
        progress_llm=_progress,
    )
    assert len(result.drift_findings) == 1
    assert result.drift_findings[0].subtype == "drifting"
    assert result.progress_findings == []


def test_one_cycle_progress_emits_finding(tmp_path, session_id):
    m = _mk_mission(tmp_path)
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [_assistant(text="I'm told tests pass")],
    )

    def _drift(_p: str) -> str:
        return json.dumps(
            {"verdict": "on_track", "reason": "ok", "evidence_turn_ids": []}
        )

    def _progress(_p: str) -> str:
        return json.dumps(
            {
                "verdict": "fabricated",
                "unsupported_claims": ["tests pass — no pytest evidence"],
                "reason": "no tool evidence",
            }
        )

    result = one_cycle(
        session_id,
        m,
        transcript_path=transcript,
        drift_llm=_drift,
        progress_llm=_progress,
    )
    assert len(result.progress_findings) == 1
    assert result.progress_findings[0].subtype == "fabricated"


def test_one_cycle_both_fire(tmp_path, session_id):
    m = _mk_mission(tmp_path)
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [_assistant(text="off mission and lying")])

    def _drift(_p: str) -> str:
        return json.dumps(
            {
                "verdict": "off_task",
                "reason": "completely unrelated",
                "evidence_turn_ids": [],
            }
        )

    def _progress(_p: str) -> str:
        return json.dumps(
            {
                "verdict": "fabricated",
                "unsupported_claims": ["claim A"],
                "reason": "no evidence",
            }
        )

    result = one_cycle(
        session_id,
        m,
        transcript_path=transcript,
        drift_llm=_drift,
        progress_llm=_progress,
    )
    assert len(result.drift_findings) == 1
    assert len(result.progress_findings) == 1
    assert result.drift_findings[0].severity == "critical"
    assert result.progress_findings[0].severity == "critical"
