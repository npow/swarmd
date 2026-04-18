"""Tests for the ``emit_finding`` Temporal activity.

Per plan Task 7 and spec §6.3:

    emit_finding(session_state_dir, finding) → None

    Append ``finding`` as a JSONL line to ``{session_state_dir}/findings.jsonl``.
    If ``finding["type"] == "intervention"``, also append to
    ``interventions.jsonl`` so the existing hook-based consumers
    (``on_stop.py``, ``on_session_start.py``) keep working unchanged.

These tests drive the activity through ``temporalio.testing.ActivityEnvironment``
so no running Temporal server is required.

Invariants covered:

* Every finding is enriched with an ``emitted_at`` wall-clock timestamp.
* ``findings.jsonl`` is always written; ``interventions.jsonl`` is only
  written for ``type == "intervention"``.
* Missing state dir is created on first write.
* Repeated calls append — they do not clobber.
"""

from __future__ import annotations

import json

import pytest
from temporalio.testing import ActivityEnvironment

from swarm.durable.activities.emit_finding import emit_finding


@pytest.mark.asyncio
async def test_finding_appended(tmp_path):
    """A plain (non-intervention) finding is written as one JSONL line to
    ``findings.jsonl`` with ``emitted_at`` added."""
    env = ActivityEnvironment()
    await env.run(
        emit_finding,
        str(tmp_path),
        {"type": "progress", "severity": "minor", "verdict": "ok"},
    )

    lines = (tmp_path / "findings.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["verdict"] == "ok"
    assert rec["type"] == "progress"
    assert rec["severity"] == "minor"
    assert "emitted_at" in rec
    assert isinstance(rec["emitted_at"], (int, float))


@pytest.mark.asyncio
async def test_intervention_typed_finding_mirrored_to_interventions(tmp_path):
    """A finding with ``type == "intervention"`` is written to BOTH
    ``findings.jsonl`` and ``interventions.jsonl``. Both files must contain
    the same enriched record."""
    env = ActivityEnvironment()
    f = {
        "type": "intervention",
        "severity": "major",
        "verdict": "nudge",
        "intervention": {
            "tier": "soft",
            "strategy": "reprompt",
            "nudge_text": "...",
        },
    }
    await env.run(emit_finding, str(tmp_path), f)

    assert (tmp_path / "findings.jsonl").exists()
    assert (tmp_path / "interventions.jsonl").exists()

    findings_rec = json.loads((tmp_path / "findings.jsonl").read_text().strip())
    interventions_rec = json.loads(
        (tmp_path / "interventions.jsonl").read_text().strip()
    )
    assert findings_rec["type"] == "intervention"
    assert interventions_rec["type"] == "intervention"
    assert findings_rec["verdict"] == "nudge"
    assert interventions_rec["verdict"] == "nudge"


@pytest.mark.asyncio
async def test_creates_state_dir_if_missing(tmp_path):
    """If ``session_state_dir`` (and any parents) does not exist yet, the
    activity creates it. The workflow may call ``emit_finding`` for a
    just-starting mission before the state dir has been materialized on
    disk."""
    target = tmp_path / "nonexistent" / "nested"
    assert not target.exists()

    env = ActivityEnvironment()
    await env.run(
        emit_finding,
        str(target),
        {"type": "x", "severity": "minor", "verdict": "y"},
    )

    assert (target / "findings.jsonl").exists()


@pytest.mark.asyncio
async def test_non_intervention_does_not_touch_interventions_file(tmp_path):
    """Non-intervention findings must NOT create ``interventions.jsonl``.
    Creating it empty would confuse downstream hooks that read the file."""
    env = ActivityEnvironment()
    await env.run(
        emit_finding,
        str(tmp_path),
        {"type": "drift", "severity": "minor", "verdict": "z"},
    )

    assert (tmp_path / "findings.jsonl").exists()
    assert not (tmp_path / "interventions.jsonl").exists()


@pytest.mark.asyncio
async def test_multiple_appends_produce_multiple_lines(tmp_path):
    """Repeated emits append — the file is opened in ``a`` mode, not ``w``.
    Regression guard: a clobber here would silently lose findings."""
    env = ActivityEnvironment()
    for i in range(3):
        await env.run(
            emit_finding,
            str(tmp_path),
            {"type": "x", "severity": "minor", "verdict": f"{i}"},
        )

    lines = (tmp_path / "findings.jsonl").read_text().strip().splitlines()
    assert len(lines) == 3
    verdicts = [json.loads(ln)["verdict"] for ln in lines]
    assert verdicts == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_finding_argument_not_mutated(tmp_path):
    """The caller's ``finding`` dict must not gain an ``emitted_at`` key as
    a side effect. We enrich a copy so workflow state (which holds the
    original) stays clean."""
    env = ActivityEnvironment()
    original = {"type": "progress", "severity": "minor", "verdict": "ok"}

    await env.run(emit_finding, str(tmp_path), original)

    assert "emitted_at" not in original
