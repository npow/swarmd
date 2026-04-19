"""Tests for the ``completion_judge`` Temporal activity.

Per plan Task 8 and spec §6.3:

    completion_judge(mission_state, session_state_dir) → CompletionDecision

Ported from ``specialists/completion_judge.py`` (lines 62-168). Six
preconditions must all hold for ``approved=True``:

    1. Hold-window recency (``mission_state["hold_window_start"]`` within
       ``hold_window_recency_sec``).
    2. No open cheat findings in ``findings.jsonl``.
    3. No open fabrication findings in ``findings.jsonl``.
    4. No open tamper findings in ``findings.jsonl``.
    5. No critic disagreements (``meta/critic_disagreement``).
    6. Per-criterion anticheat passes (no unresolved anticheat findings).

These tests drive the activity through ``temporalio.testing.ActivityEnvironment``
so no running Temporal server is required.

``findings.jsonl`` is a newline-delimited JSON file — one finding per line. The
activity must handle the file being absent (treat as empty).
"""

from __future__ import annotations

import json
import time

import pytest
from temporalio.testing import ActivityEnvironment

from swarmd.durable.activities.completion_judge import (
    CompletionDecision,
    completion_judge,
)


def _write_findings(session_dir, findings: list[dict]) -> None:
    """Write one JSON finding per line to ``findings.jsonl`` under
    ``session_dir``. Factored here so the tests stay declarative."""
    p = session_dir / "findings.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for rec in findings:
            f.write(json.dumps(rec) + "\n")


def _mission_state(hold_window_age_sec: float = 0.0) -> dict:
    """Helper: build a mission_state dict with ``hold_window_start`` at
    ``now - hold_window_age_sec``. Default is 'just now'."""
    return {"hold_window_start": time.time() - hold_window_age_sec}


@pytest.mark.asyncio
async def test_clean_state_approves(tmp_path):
    """All six preconditions satisfied → ``approved=True`` and no reasons."""
    env = ActivityEnvironment()
    dec = await env.run(completion_judge, _mission_state(), str(tmp_path))

    assert isinstance(dec, CompletionDecision)
    assert dec.approved is True
    assert dec.reasons == []


@pytest.mark.asyncio
async def test_missing_findings_file_is_treated_as_empty(tmp_path):
    """If ``findings.jsonl`` never existed (fresh session), the finding-scan
    preconditions must pass — no findings to block on."""
    # No file created at all.
    assert not (tmp_path / "findings.jsonl").exists()
    env = ActivityEnvironment()
    dec = await env.run(completion_judge, _mission_state(), str(tmp_path))

    assert dec.approved is True
    assert dec.reasons == []


@pytest.mark.asyncio
async def test_tamper_finding_blocks(tmp_path):
    """An unresolved tamper finding blocks completion. The reason must
    mention ``tamper`` so operators can route it quickly."""
    _write_findings(
        tmp_path,
        [
            {
                "id": "f-1",
                "type": "meta",
                "subtype": "tamper_detected",
                "severity": "critical",
                "verdict": "lock hash mismatch",
                "source": "verify_tamper",
            }
        ],
    )
    env = ActivityEnvironment()
    dec = await env.run(completion_judge, _mission_state(), str(tmp_path))

    assert dec.approved is False
    assert any("tamper" in r.lower() for r in dec.reasons)


@pytest.mark.asyncio
async def test_cheat_finding_blocks(tmp_path):
    """An unresolved cheat finding blocks completion."""
    _write_findings(
        tmp_path,
        [
            {
                "id": "f-2",
                "type": "cheat",
                "subtype": "test_weakened",
                "severity": "critical",
                "verdict": "asserts removed",
                "source": "anticheat.static",
            }
        ],
    )
    env = ActivityEnvironment()
    dec = await env.run(completion_judge, _mission_state(), str(tmp_path))

    assert dec.approved is False
    assert any("cheat" in r.lower() for r in dec.reasons)


@pytest.mark.asyncio
async def test_fabrication_finding_blocks(tmp_path):
    """An unresolved fabrication finding blocks completion."""
    _write_findings(
        tmp_path,
        [
            {
                "id": "f-3",
                "type": "fabrication",
                "subtype": "unfounded_claim",
                "severity": "major",
                "verdict": "claimed tests pass without running them",
                "source": "progress_auditor",
            }
        ],
    )
    env = ActivityEnvironment()
    dec = await env.run(completion_judge, _mission_state(), str(tmp_path))

    assert dec.approved is False
    assert any("fabrication" in r.lower() for r in dec.reasons)


@pytest.mark.asyncio
async def test_critic_disagreement_blocks(tmp_path):
    """An unresolved critic disagreement (multi-provider anticheat panel
    disagree) blocks completion."""
    _write_findings(
        tmp_path,
        [
            {
                "id": "f-4",
                "type": "meta",
                "subtype": "critic_disagreement",
                "severity": "major",
                "verdict": "two critics disagree",
                "source": "anticheat.panel",
            }
        ],
    )
    env = ActivityEnvironment()
    dec = await env.run(completion_judge, _mission_state(), str(tmp_path))

    assert dec.approved is False
    assert any(
        "critic" in r.lower() or "disagree" in r.lower() for r in dec.reasons
    )


@pytest.mark.asyncio
async def test_missing_hold_window_start_blocks(tmp_path):
    """Without ``hold_window_start`` in mission_state, the hold-window
    recency precondition fails and completion is blocked."""
    env = ActivityEnvironment()
    dec = await env.run(completion_judge, {}, str(tmp_path))

    assert dec.approved is False
    assert any("hold" in r.lower() for r in dec.reasons)


@pytest.mark.asyncio
async def test_stale_hold_window_blocks(tmp_path):
    """A ``hold_window_start`` older than the recency threshold (default 300s)
    fails the hold-window precondition — the hold was met in a past pass-fail
    cycle, not the current one."""
    stale_state = _mission_state(hold_window_age_sec=10_000)
    env = ActivityEnvironment()
    dec = await env.run(completion_judge, stale_state, str(tmp_path))

    assert dec.approved is False
    assert any("hold" in r.lower() for r in dec.reasons)


@pytest.mark.asyncio
async def test_anticheat_non_genuine_blocks(tmp_path):
    """An anticheat-sourced finding that isn't a GENUINE_FIX verdict blocks
    completion. We simulate this with a cheat-typed finding whose source
    starts with ``anticheat.``."""
    _write_findings(
        tmp_path,
        [
            {
                "id": "f-5",
                "type": "cheat",
                "subtype": "asserts_weakened",
                "severity": "critical",
                "verdict": "anticheat rejected",
                "source": "anticheat.panel",
            }
        ],
    )
    env = ActivityEnvironment()
    dec = await env.run(completion_judge, _mission_state(), str(tmp_path))

    assert dec.approved is False
    assert any("cheat" in r.lower() or "anticheat" in r.lower() for r in dec.reasons)


@pytest.mark.asyncio
async def test_multiple_failures_reported(tmp_path):
    """When multiple preconditions fail, all of them surface in ``reasons`` —
    operators shouldn't have to re-run to uncover the next issue."""
    _write_findings(
        tmp_path,
        [
            {
                "id": "f-6",
                "type": "cheat",
                "subtype": "stub",
                "severity": "critical",
                "verdict": "stub",
                "source": "x",
            },
            {
                "id": "f-7",
                "type": "fabrication",
                "subtype": "stub",
                "severity": "major",
                "verdict": "stub",
                "source": "x",
            },
        ],
    )
    env = ActivityEnvironment()
    dec = await env.run(completion_judge, {}, str(tmp_path))  # also no hold window

    assert dec.approved is False
    # hold-window + cheat + fabrication = 3 reasons minimum
    assert len(dec.reasons) >= 3


@pytest.mark.asyncio
async def test_malformed_json_line_is_skipped(tmp_path):
    """A malformed line in ``findings.jsonl`` must not crash the judge; it's
    simply skipped. A clean state plus a junk line should still approve."""
    p = tmp_path / "findings.jsonl"
    p.write_text("not json\n")
    env = ActivityEnvironment()
    dec = await env.run(completion_judge, _mission_state(), str(tmp_path))

    assert dec.approved is True
