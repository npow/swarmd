"""``LLMCriticWorkflow`` — cadence audits + anti-cheat fan-out (Task 16).

Per spec §6.2 lines 163-175 and §6.4 rows 4a/4b/1:

    LLMCriticWorkflow(mission_id, session_id, cadence_sec)
      Three distinct functions:
        (a) cadence-driven progress audits (progress_audit activity)
        (b) cadence-driven goal-drift checks (goal_drift_check activity)
        (c) event-triggered anti-cheat panel on anticheat_requested
            signal from parent — 6 parallel dimension activities,
            aggregated into findings

All three emit findings to the parent via signal. ``continue_as_new``
every 200 cycles to bound Temporal history (per spec §6.2 line 175).

Determinism contract:

* Uses ``workflow.execute_activity`` for all LLM calls — no direct
  anthropic calls in workflow code.
* Uses ``asyncio.gather`` for the anti-cheat fan-out — deterministic
  inside Temporal's event loop per spec §5.
* Activity exceptions are caught per-call so one flaky critic doesn't
  kill the others; the retry policy inside the activity already absorbs
  transient upstream errors. This matches the legacy specialist's
  fail-open behaviour.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from swarmd.durable import retry_policies
    from swarmd.durable.specialists._utils import _emit_to_parent


# History-bounding knob — matches spec §6.2 line 175.
_CONTINUE_AS_NEW_CYCLE_THRESHOLD = 200

# Six dimensions of the anti-cheat panel — preserved verbatim from
# ``specialists/anticheat_critic_panel._DIMENSION_PROMPTS`` keys and from
# ``swarm.durable.activities.run_anticheat_dimension._DIMENSION_PROMPTS``.
# Kept as a module constant so the workflow and the activity cannot
# drift apart.
_ANTICHEAT_DIMENSIONS = [
    "scope_reduction",
    "mock_out",
    "tautology",
    "hardcode",
    "off_criterion",
    "coordinated_edit",
]

# Verdicts that ARE actionable — the workflow emits findings for these
# two and drops the "pass" verdict (per Anticheat design, "pass" is
# informational and clutters findings.jsonl).
_ACTIONABLE_VERDICTS = frozenset({"fail", "suspicious"})


@workflow.defn
class LLMCriticWorkflow:
    """Long-running observer child workflow for LLM-driven audits.

    One instance per mission, spawned by ``MissionWorkflow._start_children``.
    The run signature is positional — three strings plus an optional
    ``cycles`` carry value used by the ``continue_as_new`` chain.
    """

    def __init__(self) -> None:
        # Anti-cheat requests are queued by signal and drained inside
        # the main loop so the panel fan-out is observable in Temporal
        # history at a predictable position. Using a list (not a set)
        # preserves FIFO ordering — helpful for diagnosis when two
        # criteria flip in quick succession.
        self._anticheat_queue: list[dict[str, Any]] = []

    @workflow.run
    async def run(
        self,
        mission_id: str,
        session_id: str,
        cadence_sec: int,
        cycles: int = 0,
    ) -> dict[str, Any]:
        cycles = int(cycles)

        while True:
            # ---- (a) progress audit ------------------------------------------
            # Wrapped in try/except so a terminal classification in the
            # activity (unparseable LLM output etc.) surfaces as a
            # log-and-continue instead of killing the whole workflow.
            # The retry policy inside the activity already absorbs the
            # transient class.
            try:
                pa = await workflow.execute_activity(
                    "progress_audit",
                    args=[
                        {
                            "mission_id": mission_id,
                            "session_id": session_id,
                            "spawner_id": session_id,
                            # Claims / evidence are filled by the activity
                            # from context; for v0 we pass empty strings so
                            # Task 18+ can wire up real transcript scraping.
                            "claims": "",
                            "evidence": "",
                        }
                    ],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policies.PROGRESS_AUDIT,
                )
                pa_finding = _finding_of(pa)
                if _should_emit(pa_finding):
                    await _emit_to_parent(mission_id, pa_finding)
            except Exception as exc:  # noqa: BLE001 — defensive
                workflow.logger.warning(
                    "llm_critic: progress_audit failed: %r", exc
                )

            # ---- (b) goal-drift check ---------------------------------------
            try:
                gd = await workflow.execute_activity(
                    "goal_drift_check",
                    args=[
                        {
                            "mission_id": mission_id,
                            "session_id": session_id,
                            "spawner_id": session_id,
                            # Same v0 placeholder — activity accepts
                            # "(none)" defaults. Task 18+ wires real
                            # mission/thinking/plan inputs.
                            "mission": "",
                            "thinking": "",
                            "plan_reports": "",
                            "post_plan_tools": "",
                            "assistant_text": "",
                        }
                    ],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policies.GOAL_DRIFT_CHECK,
                )
                gd_finding = _finding_of(gd)
                if _should_emit(gd_finding):
                    await _emit_to_parent(mission_id, gd_finding)
            except Exception as exc:  # noqa: BLE001 — defensive
                workflow.logger.warning(
                    "llm_critic: goal_drift_check failed: %r", exc
                )

            # ---- (c) drain anti-cheat queue ---------------------------------
            # Drain each queued request — one call fans out six
            # dimensions in parallel. ``asyncio.gather`` is deterministic
            # inside Temporal's event loop; every dimension activity
            # call gets its own history entry in call order.
            while self._anticheat_queue:
                req = self._anticheat_queue.pop(0)
                await self._run_anticheat_panel(mission_id, req)

            # ---- history-bounding -------------------------------------------
            cycles += 1
            if cycles >= _CONTINUE_AS_NEW_CYCLE_THRESHOLD:
                workflow.continue_as_new(
                    args=[mission_id, session_id, cadence_sec, 0]
                )

            await workflow.sleep(cadence_sec)

    # --------------------------------------------------------- signals ----

    @workflow.signal
    async def anticheat_requested(
        self, criterion: dict[str, Any], context: dict[str, Any]
    ) -> None:
        """Parent fires this on criterion pass-transition per spec §6.2 3a.

        The parent passes the criterion dict and a context dict that
        supplies the four prompt placeholders (``criterion_id``, ``diff``,
        ``events``, ``check_command``) plus the optional
        ``anticheat_config`` (from ``mission.yaml``) that names the
        reviewer command. The signal enqueues the request; the main
        loop drains the queue on its next tick.

        Extracting the anticheat config on signal ingestion (not on
        drain) means the parent can change the config mid-mission (e.g.
        swap the primary model) and queued requests still use their
        original config — matching intuition.
        """
        anticheat_config = context.get(
            "anticheat_config",
            {"primary": "claude -p --bare --model opus"},
        )
        self._anticheat_queue.append(
            {
                "criterion": criterion,
                "context": context,
                "anticheat_config": anticheat_config,
            }
        )

    # --------------------------------------------------------- helpers ----

    async def _run_anticheat_panel(
        self, mission_id: str, req: dict[str, Any]
    ) -> None:
        """Fan out one anti-cheat request across the 6 dimensions.

        Preserves ``anticheat_critic_panel.run_panel`` semantics:
          * Per-dimension verdict classified by
            ``run_anticheat_dimension``.
          * Only ``fail`` / ``suspicious`` verdicts produce findings;
            ``pass`` is informational.
          * Dimension activities run in parallel via ``asyncio.gather``
            with ``return_exceptions=True`` so one bad dimension doesn't
            nuke the other five — failures are logged and skipped.
        """
        # Build the minimal context required by run_anticheat_dimension's
        # ``str.format(**context)`` call. Extra keys pass through safely.
        ctx = dict(req["context"])
        # Pass-through metadata for finding tagging.
        ctx.setdefault("mission_id", mission_id)
        ctx.setdefault("session_id", mission_id)
        ctx.setdefault("spawner_id", mission_id)
        # Ensure the four prompt placeholders exist — activity raises
        # TerminalError on missing keys; v0 callers may omit any of
        # them, so we fill with a sentinel string.
        ctx.setdefault("criterion_id", str(req["criterion"].get("id", "")))
        ctx.setdefault("diff", "(no diff captured)")
        ctx.setdefault("events", "(no events captured)")
        ctx.setdefault("check_command", str(req["criterion"].get("check", "")))

        anticheat_config = req["anticheat_config"]

        # Fan out — each dimension is an independent activity call.
        verdicts = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "run_anticheat_dimension",
                    args=[dim, ctx, anticheat_config],
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=retry_policies.RUN_ANTICHEAT_DIMENSION,
                )
                for dim in _ANTICHEAT_DIMENSIONS
            ],
            return_exceptions=True,
        )

        for v in verdicts:
            if isinstance(v, BaseException):
                workflow.logger.warning(
                    "llm_critic: run_anticheat_dimension failed: %r", v
                )
                continue
            verdict_name = _verdict_str_of(v)
            if verdict_name in _ACTIONABLE_VERDICTS:
                finding = _finding_of(v)
                if finding is not None:
                    await _emit_to_parent(mission_id, finding)


# ----------------------------------------------------------------- helpers --


def _finding_of(r: Any) -> dict[str, Any] | None:
    """Extract the ``finding`` dict from an activity result.

    All three critic result shapes
    (``ProgressAuditResult``/``GoalDriftResult``/``AnticheatVerdict``)
    expose ``.finding``. Dict fallback for test mocks.
    """
    if r is None:
        return None
    if hasattr(r, "finding"):
        return getattr(r, "finding")
    if isinstance(r, dict):
        return r.get("finding")
    return None


def _should_emit(finding: dict[str, Any] | None) -> bool:
    """True iff the finding is actionable — i.e. not a type=info noise
    entry. Matches the progress_audit / goal_drift_check semantics:
    ``grounded`` / ``on_track`` / ``unclear`` → type=info (dropped);
    ``fabricated`` / ``partial`` / ``drifting`` / ``off_task`` /
    ``plan_fabrication`` → type != info (emitted).
    """
    if not isinstance(finding, dict):
        return False
    return finding.get("type") not in (None, "info")


def _verdict_str_of(r: Any) -> str:
    """Return the ``verdict`` string from an ``AnticheatVerdict`` result.

    Dict fallback for test mocks that return a dict instead of a
    dataclass.
    """
    if hasattr(r, "verdict"):
        return str(getattr(r, "verdict"))
    if isinstance(r, dict):
        return str(r.get("verdict", ""))
    return ""
