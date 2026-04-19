"""``intervention_judge`` Temporal activity — tier + strategy classifier.

Per spec §6.3 and plan Task 8:

    intervention_judge(finding, strikes_by_dimension, tried_strategies)
        → InterventionDecision | None

Ported from ``specialists/intervention_judge.decide()`` (lines 34-182). The
escape-ladder rungs and the strike/tamper/cheat/fabrication policy are
preserved verbatim; the legacy tier names are remapped to the new taxonomy
the Temporal workflow uses:

* legacy ``correct``             → ``soft``
* legacy ``urgent``              → ``hard``
* legacy ``recover``             → ``recover``
* legacy ``mission_level_alert`` → ``mission_level_alert``
* legacy ``info`` (catch-all)    → returns ``None`` (no intervention)

Returning ``None`` for the catch-all is deliberate — the plan lists
``meta/specialist_degraded`` as an explicit example of a finding that
should NOT trigger an intervention. Any finding that doesn't fit
drift/loop/thrash/cheat/fabrication/tamper also falls into this bucket.

Implementation contract:

* Signature: ``async def intervention_judge(finding, strikes_by_dimension,
  tried_strategies) -> InterventionDecision | None``.
* Pure function. No I/O. No Temporal primitives beyond ``@activity.defn``.
* Accepts ``finding`` as a plain dict (no dependency on legacy ``Finding``
  schema) so this module doesn't pull in ``swarm.schemas`` during the
  durability migration.
* ``strikes_by_dimension`` is keyed by whatever dimension the caller uses
  to bucket repeat patterns — typically ``finding["type"]``, but the
  workflow is free to use a finer signature. The judge only cares about
  the count it finds under the finding's ``type`` key.
* ``tried_strategies`` is the list of ladder-rung names already attempted
  on this pattern. The judge picks the first untried rung.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from temporalio import activity

# Escape ladder rungs. Each entry is ``(strategy_name, nudge_text)``. The
# order defines escalation: the judge always tries the first rung whose name
# is not already in ``tried_strategies``. If every rung has been tried, the
# ladder rotates to ``recover`` — see ``_pick_rung`` below.
#
# Ported verbatim from ``specialists/intervention_judge.py`` so the semantics
# stay identical through the migration.
ESCAPE_LADDER: list[tuple[str, str]] = [
    (
        "templated_diversity",
        "You have been repeating the same approach. Before your next action, "
        "propose THREE different approaches you could take instead, evaluate "
        "them briefly, pick the one least similar to what you have tried, and "
        "proceed with that one.",
    ),
    (
        "decomposition",
        "Your current approach is stuck. Decompose the problem into 2-4 "
        "smaller subproblems. Pick the smallest subproblem that would "
        "unblock the others. Work only on that.",
    ),
    (
        "counterfactual_probe",
        "Assume your current approach is fundamentally wrong. What would the "
        "correct approach look like? Explain in 3-5 sentences, then act on it.",
    ),
    (
        "bisection_reset",
        "Your last N actions contain both forward and backward steps. "
        "Identify the LAST action that was genuinely helpful. Revert the "
        "workspace to that state. Start over from there.",
    ),
    (
        "expert_framing",
        "A senior engineer reviewing this problem would say: this approach "
        "misses a constraint you have not named. Before your next action, "
        "state what constraint is being violated, then propose an approach "
        "that respects it.",
    ),
]


@dataclass(frozen=True)
class InterventionDecision:
    """One classified intervention.

    ``tier`` is one of ``soft | hard | recover | mission_level_alert`` — the
    coordinator uses this to decide how loudly to act. ``strategy`` names
    the ladder rung (or escalation name) the caller should apply next.
    ``nudge_text`` is the prose delivered to the subject agent.
    """

    tier: str
    strategy: str
    nudge_text: str


def _pick_rung(tried: list[str]) -> tuple[str, str]:
    """Return the first ladder rung whose name is not in ``tried``.

    If all rungs have been tried, return ``("recover", <rotate message>)``
    so the caller knows to rotate to a fresh recovery subagent rather than
    keep nudging the same one.
    """
    tried_set = set(tried)
    for name, nudge in ESCAPE_LADDER:
        if name not in tried_set:
            return name, nudge
    return (
        "recover",
        "All intervention strategies exhausted for this pattern. "
        "Rotate to a fresh recovery subagent carrying the scoped learnings.",
    )


@activity.defn(name="intervention_judge")
async def intervention_judge(
    finding: dict[str, Any],
    strikes_by_dimension: dict[str, int],
    tried_strategies: list[str],
) -> InterventionDecision | None:
    """Decide the intervention for a finding, or return ``None`` to skip.

    Policy (ported from ``specialists/intervention_judge.decide()``):

    * ``meta/tamper_detected``               → mission_level_alert + halt_and_alert
    * ``cheat`` (any subtype)                → hard + bisection_reset
    * ``fabrication/scope_shrinking``        → hard + scope_lock
    * ``fabrication`` (other)                → soft + grounding_required
    * ``drift`` / ``loop`` / ``thrash``      → escape ladder (next untried rung).
      If strikes on this dimension ≥ 3 OR every rung has been tried, rotate
      to ``recover``.
    * anything else                          → ``None`` (no intervention).

    ``strikes_by_dimension`` is keyed by ``finding["type"]``. The judge only
    inspects that single entry; missing key is treated as zero strikes.
    """
    ftype = finding.get("type")
    fsubtype = finding.get("subtype")
    verdict = finding.get("verdict") or ""

    # Tamper — always the highest tier. Never gated on strike count because
    # one tamper is one too many.
    if ftype == "meta" and fsubtype == "tamper_detected":
        return InterventionDecision(
            tier="mission_level_alert",
            strategy="halt_and_alert",
            nudge_text=(
                "Tamper detected on mission files. Mission paused pending "
                "user review. Continue your current work but do NOT mark "
                "anything complete."
            ),
        )

    # Cheat — always hard tier, bisection_reset strategy. The specific
    # subtype is surfaced in the nudge so the agent knows what to undo.
    if ftype == "cheat":
        return InterventionDecision(
            tier="hard",
            strategy="bisection_reset",
            nudge_text=(
                f"Cheat pattern detected: {fsubtype}. "
                f"{verdict[:300]}. "
                "Revert this change and solve the underlying problem properly."
            ),
        )

    # Fabrication with scope_shrinking subtype — hard tier, scope_lock. The
    # agent has tried to rewrite the contract (declaring criteria out of
    # scope), so we escalate past the soft "re-verify" nudge.
    if ftype == "fabrication" and fsubtype == "scope_shrinking":
        return InterventionDecision(
            tier="hard",
            strategy="scope_lock",
            nudge_text=(
                "You have signalled that parts of the mission are 'out of "
                "scope', 'deferred', or 'for future work'. This is NOT your "
                "call to make. The mission's success_criteria are the "
                "contract. Either complete every criterion, or identify one "
                "you cannot complete and explain in concrete technical terms "
                "why it is impossible — DO NOT declare anything out of scope."
            ),
        )

    # Other fabrications — soft tier, grounding_required.
    if ftype == "fabrication":
        return InterventionDecision(
            tier="soft",
            strategy="grounding_required",
            nudge_text=(
                f"Your recent claims are not supported by tool evidence: "
                f"{verdict[:300]}. Re-verify what you have actually "
                "done and correct any unfounded claims."
            ),
        )

    # Loop / thrash / drift — escape ladder. Repeat patterns at strike 3+
    # rotate to recover even if the ladder has untried rungs; rotating is
    # cheaper than asking a stuck agent to try one more angle.
    if ftype in {"loop", "thrash", "drift"}:
        strikes = strikes_by_dimension.get(ftype, 0)
        strategy, nudge = _pick_rung(tried_strategies)
        if strikes >= 3:
            strategy = "recover"
            nudge = (
                f"Strike {strikes} on the same pattern "
                f"({fsubtype}). Rotating to a fresh recovery subagent."
            )
        tier = "recover" if strategy == "recover" else "soft"
        return InterventionDecision(
            tier=tier,
            strategy=strategy,
            nudge_text=nudge,
        )

    # Catch-all: no intervention. The plan explicitly calls out
    # ``meta/specialist_degraded`` as an example — it's informational only.
    return None
