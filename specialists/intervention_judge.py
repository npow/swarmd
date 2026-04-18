"""intervention_judge — decides intervention tier and escape-ladder strategy.

Given a finding (with severity already classified) + current strike count
for the finding's loop signature + history of tried strategies, picks:
  - tier: info | correct | urgent | recover | mission_level_alert
  - strategy: which escape-ladder rung to apply next

Independence: this is the coordinator's delegate for tier/strategy choice;
the coordinator never makes these decisions directly.

v1 implementation is deterministic (policy-based), which mirrors v0 but
moves the logic into its own module so the coordinator stays a pure router.
A future LLM-backed judge can replace `decide` without changing callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swarm.schemas.finding import Finding


@dataclass(frozen=True)
class InterventionDecision:
    tier: str  # info|correct|urgent|recover|mission_level_alert|mission_complete
    strategy: str | None = None
    reason: str = ""
    consume_at: str = "stop"  # stop|post_tool|either
    extra: dict[str, str] = field(default_factory=dict)


# Escape ladder rungs. Each entry is (name, reason text). The order defines
# the escalation sequence; intervention_judge tries the first untried rung.
ESCAPE_LADDER = [
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


def pick_rung(tried: list[str]) -> tuple[str, str]:
    """Return the first ladder rung whose name is not in `tried`.

    If all rungs have been tried, return ("recover", ...) to trigger
    recovery-subagent rotation.
    """
    tried_set = set(tried)
    for name, reason in ESCAPE_LADDER:
        if name not in tried_set:
            return name, reason
    return (
        "recover",
        "All intervention strategies exhausted for this pattern. "
        "Rotate to a fresh recovery subagent carrying the scoped learnings.",
    )


def decide(
    finding: Finding,
    *,
    strikes: int,
    tried: list[str],
) -> InterventionDecision:
    """Decide the intervention for a finding.

    Policy:
      - hold_window_met    → upstream coordinator invokes completion_judge
      - tamper_detected    → mission_level_alert
      - cheat              → urgent, bisection_reset strategy
      - fabrication (scope_shrinking) → urgent, scope_lock strategy
      - fabrication (other) → correct
      - drift/loop/thrash   → escape ladder; recover if strikes >= 3 OR all
                              rungs tried
    """
    # Tamper
    if finding.type == "meta" and finding.subtype == "tamper_detected":
        return InterventionDecision(
            tier="mission_level_alert",
            strategy="halt_and_alert",
            reason=(
                "Tamper detected on mission files. Mission paused pending "
                "user review. Continue your current work but do NOT mark "
                "anything complete."
            ),
            consume_at="either",
        )

    # Cheat findings → always urgent
    if finding.type == "cheat":
        return InterventionDecision(
            tier="urgent",
            strategy="bisection_reset",
            reason=(
                f"Cheat pattern detected: {finding.subtype}. "
                f"{finding.verdict[:300]}. "
                "Revert this change and solve the underlying problem properly."
            ),
            consume_at="either",
        )

    # Fabrication with scope_shrinking subtype → urgent, scope_lock
    if finding.type == "fabrication" and finding.subtype == "scope_shrinking":
        return InterventionDecision(
            tier="urgent",
            strategy="scope_lock",
            reason=(
                "You have signalled that parts of the mission are 'out of "
                "scope', 'deferred', or 'for future work'. This is NOT your "
                "call to make. The mission's success_criteria are the "
                "contract. Either complete every criterion, or identify one "
                "you cannot complete and explain in concrete technical terms "
                "why it is impossible — DO NOT declare anything out of scope."
            ),
            consume_at="either",
        )

    # Other fabrications → correct
    if finding.type == "fabrication":
        return InterventionDecision(
            tier="correct",
            strategy="grounding_required",
            reason=(
                f"Your recent claims are not supported by tool evidence: "
                f"{finding.verdict[:300]}. Re-verify what you have actually "
                "done and correct any unfounded claims."
            ),
            consume_at="stop",
        )

    # Loop / thrash / drift → escape ladder
    if finding.type in {"loop", "thrash", "drift"}:
        strategy, reason_text = pick_rung(tried)
        # Recover at strike 3+ even if ladder has untried rungs
        if strikes >= 3:
            strategy = "recover"
            reason_text = (
                f"Strike {strikes} on the same pattern "
                f"({finding.subtype}). Rotating to a fresh recovery subagent."
            )
        tier = "recover" if strategy == "recover" else "correct"
        return InterventionDecision(
            tier=tier,
            strategy=strategy,
            reason=reason_text,
            consume_at="stop",
        )

    # Catch-all: no intervention
    return InterventionDecision(
        tier="info",
        strategy=None,
        reason=f"noted: {finding.type}.{finding.subtype}",
        consume_at="stop",
    )
