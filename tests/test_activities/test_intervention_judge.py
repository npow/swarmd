"""Tests for the ``intervention_judge`` Temporal activity.

Per plan Task 8 and spec §6.3:

    intervention_judge(finding, strikes_by_dimension, tried_strategies)
        → InterventionDecision | None

Ported from ``specialists/intervention_judge.decide()`` (lines 34-182). The
escape-ladder rungs and the tier-escalation policy are preserved; the tier
names are remapped to the new taxonomy:

* ``correct`` → ``soft``
* ``urgent``  → ``hard``
* ``recover`` → ``recover``
* ``mission_level_alert`` → ``mission_level_alert``
* ``info`` (catch-all) → ``None`` (no intervention)

These tests drive the activity through ``temporalio.testing.ActivityEnvironment``
so no running Temporal server is required.

Invariants covered:

* First strike on a drift/loop/thrash finding → ``soft`` tier, first untried
  ladder rung selected as the strategy.
* Strike count ≥ 3 on the same dimension escalates to ``recover`` regardless
  of which rungs have been tried — that's the hard wall on repeat patterns.
* Already-tried ladder rungs are skipped; the next untried rung is picked.
* Specialist-degraded findings (meta/specialist_degraded) do not trigger an
  intervention — return ``None``.
* Tamper findings always produce ``mission_level_alert``.
* Cheat findings always produce ``hard`` tier with ``bisection_reset``.
* Fabrication with ``scope_shrinking`` subtype → ``hard`` + ``scope_lock``.
* Other fabrications → ``soft`` + ``grounding_required``.
"""

from __future__ import annotations

import pytest
from temporalio.testing import ActivityEnvironment

from swarmd.durable.activities.intervention_judge import (
    ESCAPE_LADDER,
    InterventionDecision,
    intervention_judge,
)


@pytest.mark.asyncio
async def test_first_strike_drift_returns_soft():
    """Baseline: a first-strike drift finding produces a ``soft`` intervention
    with the first ladder rung as the strategy and a non-empty nudge."""
    finding = {
        "type": "drift",
        "subtype": "off_criterion",
        "severity": "major",
        "verdict": "agent drifted off criterion",
    }
    env = ActivityEnvironment()
    dec = await env.run(intervention_judge, finding, {}, [])

    assert isinstance(dec, InterventionDecision)
    assert dec.tier == "soft"
    # First untried rung is the head of ESCAPE_LADDER
    assert dec.strategy == ESCAPE_LADDER[0][0]
    assert dec.nudge_text  # non-empty


@pytest.mark.asyncio
async def test_third_strike_drift_escalates_to_recover():
    """At strike >= 3 on the same dimension, we escalate to ``recover`` even
    if some ladder rungs haven't been tried. Repeat patterns get a fresh agent."""
    finding = {
        "type": "drift",
        "subtype": "off_criterion",
        "severity": "major",
        "verdict": "agent drifted",
    }
    strikes = {"drift": 3}
    env = ActivityEnvironment()
    dec = await env.run(
        intervention_judge,
        finding,
        strikes,
        [ESCAPE_LADDER[0][0], ESCAPE_LADDER[1][0]],
    )

    assert isinstance(dec, InterventionDecision)
    assert dec.tier == "recover"
    assert dec.strategy == "recover"


@pytest.mark.asyncio
async def test_specialist_degraded_returns_none():
    """Findings that don't fit any intervention path must return ``None``.
    ``meta/specialist_degraded`` is explicitly call-out in the plan."""
    finding = {
        "type": "meta",
        "subtype": "specialist_degraded",
        "severity": "minor",
        "verdict": "one specialist flaky",
    }
    env = ActivityEnvironment()
    dec = await env.run(intervention_judge, finding, {}, [])

    assert dec is None


@pytest.mark.asyncio
async def test_tried_rungs_are_skipped():
    """If the first rung has been tried, the judge picks the next untried one."""
    finding = {
        "type": "loop",
        "subtype": "tool_repeat",
        "severity": "major",
        "verdict": "same call 5x",
    }
    first_rung = ESCAPE_LADDER[0][0]
    second_rung = ESCAPE_LADDER[1][0]

    env = ActivityEnvironment()
    dec = await env.run(intervention_judge, finding, {}, [first_rung])

    assert dec is not None
    assert dec.strategy == second_rung


@pytest.mark.asyncio
async def test_tamper_returns_mission_level_alert():
    """A tamper finding must always produce a mission-level alert regardless
    of strike count — it's the system's 'all hands' signal."""
    finding = {
        "type": "meta",
        "subtype": "tamper_detected",
        "severity": "critical",
        "verdict": "lock hash mismatch",
    }
    env = ActivityEnvironment()
    dec = await env.run(intervention_judge, finding, {}, [])

    assert dec is not None
    assert dec.tier == "mission_level_alert"
    assert dec.strategy == "halt_and_alert"


@pytest.mark.asyncio
async def test_cheat_returns_hard_bisection_reset():
    """Any cheat finding lands in ``hard`` tier with ``bisection_reset``."""
    finding = {
        "type": "cheat",
        "subtype": "test_weakened",
        "severity": "critical",
        "verdict": "test suite weakened to force a green",
    }
    env = ActivityEnvironment()
    dec = await env.run(intervention_judge, finding, {}, [])

    assert dec is not None
    assert dec.tier == "hard"
    assert dec.strategy == "bisection_reset"


@pytest.mark.asyncio
async def test_scope_shrinking_fabrication_is_hard_scope_lock():
    """The ``scope_shrinking`` fabrication subtype gets its own escalation:
    ``hard`` tier + ``scope_lock`` strategy."""
    finding = {
        "type": "fabrication",
        "subtype": "scope_shrinking",
        "severity": "major",
        "verdict": "agent declared a criterion out of scope",
    }
    env = ActivityEnvironment()
    dec = await env.run(intervention_judge, finding, {}, [])

    assert dec is not None
    assert dec.tier == "hard"
    assert dec.strategy == "scope_lock"


@pytest.mark.asyncio
async def test_other_fabrication_is_soft_grounding_required():
    """Non-scope-shrinking fabrications land in ``soft`` tier with
    ``grounding_required``."""
    finding = {
        "type": "fabrication",
        "subtype": "unfounded_claim",
        "severity": "major",
        "verdict": "agent claimed tests pass without running them",
    }
    env = ActivityEnvironment()
    dec = await env.run(intervention_judge, finding, {}, [])

    assert dec is not None
    assert dec.tier == "soft"
    assert dec.strategy == "grounding_required"


@pytest.mark.asyncio
async def test_all_rungs_tried_without_strike3_still_rotates():
    """When every ladder rung has been tried, the judge rotates to
    ``recover`` even if the strike count hasn't hit 3 — the ladder is
    exhausted either way."""
    finding = {
        "type": "thrash",
        "subtype": "oscillation",
        "severity": "major",
        "verdict": "forward-backward steps",
    }
    tried = [rung[0] for rung in ESCAPE_LADDER]
    env = ActivityEnvironment()
    dec = await env.run(intervention_judge, finding, {}, tried)

    assert dec is not None
    assert dec.tier == "recover"
    assert dec.strategy == "recover"
