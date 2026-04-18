"""Coordinator — routes findings to interventions. Orchestrates; does not evaluate.

v0 uses a simple deterministic policy. v1+ invokes severity_judge /
intervention_judge / anticheat_critic_panel for real judgment.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time

import yaml

from swarm.lib.heartbeat import beat
from swarm.lib.ids import mint_intervention_id
from swarm.lib.interventions import (
    intervention_age_sec,
)
from swarm.lib.launcher_liveness import exit_if_launcher_dead
from swarm.lib.interventions import (
    read_all as read_all_interventions,
)
from swarm.lib.locking import locked_rmw, write_line
from swarm.lib.paths import (
    ensure_session_dirs,
    findings_path,
    interventions_path,
    mission_yaml_path,
    strikes_path,
    tried_strategies_path,
)
from swarm.schemas.finding import Finding
from swarm.schemas.intervention import Intervention
from swarm.schemas.mission import Mission
from swarm.specialists.anticheat_critic_panel import run_panel as anticheat_run_panel
from swarm.specialists.completion_judge import judge as completion_judge
from swarm.specialists.intervention_judge import decide as judge_decide

LOG = logging.getLogger("swarm.coordinator")


# v0 escape ladder rungs
ESCAPE_LADDER = [
    (
        "templated_diversity",
        "You have been repeating the same approach. Before your next action, "
        "propose THREE different approaches you could take instead, evaluate them "
        "briefly, pick the one least similar to what you have tried, and proceed "
        "with that one.",
    ),
    (
        "decomposition",
        "Your current approach is stuck. Decompose the problem into 2-4 smaller "
        "subproblems. Pick the smallest subproblem that would unblock the others. "
        "Work only on that.",
    ),
    (
        "counterfactual_probe",
        "Assume your current approach is fundamentally wrong. What would the "
        "correct approach look like? Explain in 3-5 sentences, then act on it.",
    ),
]


def loop_signature(finding: Finding) -> str:
    """Stable signature for grouping strikes across re-emissions of the same loop."""
    tool = finding.evidence.claim_excerpt or finding.verdict or ""
    files = tuple(sorted(finding.evidence.files))
    key = f"{finding.subtype}|{files}|{tool[:200]}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def bump_strike(session_id: str, signature: str) -> int:
    """Increment strikes[signature] atomically and return the new count."""
    path = strikes_path(session_id)
    with locked_rmw(path, default=b"{}") as (fd, data):
        try:
            state = json.loads(data.decode() or "{}")
        except json.JSONDecodeError:
            state = {}
        state[signature] = state.get(signature, 0) + 1
        os.write(fd, json.dumps(state).encode())
        return state[signature]


def record_tried(session_id: str, signature: str, strategy: str, outcome: str) -> None:
    row = {
        "signature": signature,
        "strategy": strategy,
        "tried_at": time.time(),
        "outcome": outcome,
    }
    write_line(tried_strategies_path(session_id), json.dumps(row))


def read_tried(session_id: str, signature: str) -> list[str]:
    path = tried_strategies_path(session_id)
    if not path.exists():
        return []
    out: list[str] = []
    with path.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("signature") == signature:
                out.append(d["strategy"])
    return out


def pick_rung(session_id: str, signature: str) -> tuple[str, str]:
    already = set(read_tried(session_id, signature))
    for name, reason in ESCAPE_LADDER:
        if name not in already:
            return name, reason
    # All rungs tried — escalate to recover
    return "recover", (
        "All intervention strategies exhausted for this pattern. "
        "Rotate to a fresh recovery subagent."
    )


# Dependency-injectable anticheat runner (lets tests stub it without mocking
# subprocesses). Signature matches run_panel kwargs.
_anticheat_runner = anticheat_run_panel


def set_anticheat_runner(fn):
    """Inject a panel runner for tests."""
    global _anticheat_runner
    _anticheat_runner = fn


def _run_anticheat_on_pass_transition(session_id: str, f: Finding) -> list[Finding]:
    """Invoke anticheat panel when a criterion transitions fail→pass.

    Returns any cheat findings the panel emits. Callers write them to
    findings.jsonl; the coordinator will then route them as cheat interventions
    via the normal path on the next iteration.
    """
    # Extract criterion info from the pass_transition finding's evidence.
    # The verifier emits `claim_excerpt=f"criterion={cid} exit=0 stdout_tail=..."`
    import re as _re

    crit_id = "?"
    check_cmd = "(unknown)"
    excerpt = f.evidence.claim_excerpt or ""
    m = _re.search(r"criterion=(\S+)", excerpt)
    if m:
        crit_id = m.group(1)
    # Diff / events captured in ad-hoc form; future versions will pass richer
    # context. For v3, we pass what we have.
    return _anticheat_runner(
        session_id=session_id,
        spawner_id=session_id,
        criterion_id=crit_id,
        criterion_description=f"pass_transition for {crit_id}",
        check_command=check_cmd,
        diff="(v3: diff not yet captured; anticheat operates on summaries)",
        events=excerpt[:1000],
    )


def make_intervention_for(
    session_id: str, f: Finding
) -> Intervention | None:
    """Route a finding to an intervention. Delegates all judgment to
    intervention_judge (§5.11 of the spec) — the coordinator never decides
    tier/strategy itself.
    """
    # Pass-transition → run anticheat panel, write any cheat findings back
    # to findings.jsonl, then emit an info-tier intervention so the worker
    # knows the transition was observed. Subsequent coordinator cycles will
    # route the cheat findings normally.
    if (
        f.type == "verification"
        and f.subtype == "pass_transition"
    ):
        try:
            cheat_findings = _run_anticheat_on_pass_transition(session_id, f)
        except Exception as e:
            LOG.error("anticheat panel failed: %s", e)
            cheat_findings = []
        for cf in cheat_findings:
            write_line(findings_path(session_id), cf.model_dump_json())
            LOG.info("anticheat verdict: %s", cf.subtype)
        return Intervention(
            id=mint_intervention_id(),
            tier="info",
            reason=(
                f"pass_transition for {f.evidence.claim_excerpt[:200] if f.evidence.claim_excerpt else '?'} "
                f"observed; anticheat panel returned {len(cheat_findings)} non-GENUINE verdicts"
            ),
            consume_at="stop",
            requires_ack=True,
            referenced_findings=[f.id],
        )

    # Mission-complete path is the one case coordinator still handles directly,
    # because it requires invoking completion_judge and composing a specific
    # mission_complete intervention.
    if f.source == "success_verifier.hold_window_met":
        verdict = completion_judge(session_id)
        if verdict.verdict == "complete":
            return Intervention(
                id=mint_intervention_id(),
                tier="mission_complete",
                reason=(
                    "All success criteria held for the required window. "
                    "The mission is complete. You may stop."
                ),
                consume_at="stop",
                requires_ack=True,
                referenced_findings=[f.id],
            )
        return Intervention(
            id=mint_intervention_id(),
            tier="info",
            reason=(
                "Verifier reports hold window met, but completion judge blocks: "
                + verdict.reasoning
            ),
            consume_at="stop",
            requires_ack=True,
            referenced_findings=[f.id],
        )

    # Everything else goes through intervention_judge
    sig = loop_signature(f) if f.type in {"loop", "thrash", "drift"} else None
    strikes = bump_strike(session_id, sig) if sig else 0
    tried = read_tried(session_id, sig) if sig else []

    decision = judge_decide(f, strikes=strikes, tried=tried)
    if decision.tier == "info" and decision.strategy is None:
        # No-op intervention — skip (judge returned info-only)
        return None
    if sig and decision.strategy and decision.strategy != "recover":
        record_tried(session_id, sig, decision.strategy, "attempted")
    return Intervention(
        id=mint_intervention_id(),
        tier=decision.tier,  # type: ignore[arg-type]
        reason=decision.reason,
        consume_at=decision.consume_at,  # type: ignore[arg-type]
        requires_ack=True,
        referenced_findings=[f.id],
        strategy_used=decision.strategy,
        loop_signature=sig,
    )


# -------- daemon --------




_PLAN_CHECKPOINT_REASON = (
    "Pause before your next action. In 3-5 sentences, state: "
    "(a) the current sub-goal you are pursuing, "
    "(b) the approach you have chosen and why, "
    "(c) the next concrete step. Then continue. "
    "This checkpoint is for the swarm's drift detector — answer honestly "
    "and specifically; vague answers are themselves a drift signal."
)

# Reissue any pending intervention older than this many seconds with no ack.
REISSUE_AFTER_SEC = 120.0


def _load_mission(session_id: str) -> Mission | None:
    p = mission_yaml_path(session_id)
    if not p.exists():
        return None
    try:
        return Mission.model_validate(yaml.safe_load(p.read_text()))
    except Exception:
        return None


def _reissue_stale(session_id: str, reissued_ids: set[str]) -> int:
    """Append a copy of any unacked intervention older than REISSUE_AFTER_SEC.

    The copy gets a fresh id so the hook will pick it up as new. The original
    intervention's id is recorded in `reissued_ids` so we don't reissue it twice.
    """
    from swarm.lib.interventions import _acked_ids  # internal — same package

    n = 0
    acked = _acked_ids(session_id)
    for iv in read_all_interventions(session_id):
        if iv.id in acked or iv.id in reissued_ids:
            continue
        if intervention_age_sec(iv) < REISSUE_AFTER_SEC:
            continue
        copy = iv.model_copy(
            update={
                "id": mint_intervention_id(),
                "reason": (
                    f"[REISSUE — earlier intervention {iv.id} was not consumed]\n"
                    + iv.reason
                ),
            }
        )
        write_line(interventions_path(session_id), copy.model_dump_json())
        reissued_ids.add(iv.id)
        n += 1
    return n


def _maybe_emit_checkpoint(
    session_id: str,
    mission: Mission | None,
    last_checkpoint_ts: float,
) -> tuple[bool, float]:
    """Emit a periodic plan-checkpoint intervention.

    Returns (emitted, new_last_ts).
    """
    if mission is None:
        return False, last_checkpoint_ts
    cadence = mission.observer_config.plan_checkpoint_every_sec
    now = time.time()
    if now - last_checkpoint_ts < cadence:
        return False, last_checkpoint_ts
    iv = Intervention(
        id=mint_intervention_id(),
        tier="info",
        reason=_PLAN_CHECKPOINT_REASON,
        consume_at="stop",
        requires_ack=True,
        strategy_used="plan_checkpoint",
    )
    write_line(interventions_path(session_id), iv.model_dump_json())
    LOG.info("plan_checkpoint emitted (%.0fs cadence)", cadence)
    return True, now


def main(session_id: str, period_sec: float = 5.0) -> None:
    ensure_session_dirs(session_id)
    # Fail fast if the launcher is already gone — avoids a half-started
    # daemon leaking forever when launch.sh was SIGKILL'd before we booted.
    exit_if_launcher_dead(session_id, LOG)
    seen_finding_ids: set[str] = set()
    reissued_ids: set[str] = set()
    last_checkpoint_ts: float = time.time()  # don't fire immediately
    cycles = 0
    mission = _load_mission(session_id)
    LOG.info("coordinator starting for session=%s", session_id)
    while True:
        exit_if_launcher_dead(session_id, LOG)
        # 1. Process new findings
        path = findings_path(session_id)
        if path.exists():
            with path.open() as fobj:
                for line in fobj:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        f = Finding.model_validate_json(line)
                    except Exception:
                        continue
                    if f.id in seen_finding_ids:
                        continue
                    seen_finding_ids.add(f.id)
                    iv = make_intervention_for(session_id, f)
                    if iv is None:
                        continue
                    write_line(
                        interventions_path(session_id), iv.model_dump_json()
                    )
                    LOG.info(
                        "intervention %s tier=%s strategy=%s",
                        iv.id,
                        iv.tier,
                        iv.strategy_used,
                    )

        # 2. Re-issue stale unacked interventions
        n_reissued = _reissue_stale(session_id, reissued_ids)
        if n_reissued:
            LOG.info("re-issued %d stale interventions", n_reissued)

        # 3. Maybe emit a plan checkpoint
        emitted, last_checkpoint_ts = _maybe_emit_checkpoint(
            session_id, mission, last_checkpoint_ts
        )
        if emitted:
            LOG.info("plan_checkpoint at %.0f", last_checkpoint_ts)

        cycles += 1
        beat(session_id, "coordinator", cycles)
        time.sleep(period_sec)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: coordinator.py <session_id>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
