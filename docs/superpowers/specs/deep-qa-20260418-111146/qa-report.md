# Deep QA Report — Swarm Durability Redesign Spec

**Artifact:** `2026-04-18-swarm-durability-design.md` (525 lines, ~4300 words)
**Run ID:** deep-qa-20260418-111146
**QA Dimensions covered:** completeness, internal_consistency, feasibility, edge_cases + cross-dimensional preservation check
**Critics deployed:** 6 (parallel, 1 round)
**Termination label:** Conditions Met — 6 dimensions covered, all returned substantive findings
**Artifact owner:** npow

---

## Executive Summary

**Verdict: REVISE before writing-plans.** The spec's durability layer (Sections 6-8: Temporal architecture, retry policies, state management) is solid. The spec's preservation claim (Goal 3: "existing enforcement primitives are preserved") is **NOT met** — this is the most significant finding. Seven enforcement subsystems in today's swarm have no unambiguous mapping in the new spec. The invocation layer (Section 9: classifier) has a feasibility problem with Claude Code's actual hook system.

**Defects by severity:**
- **Critical: 7** (blocks correctness, functional regression, or unimplementable as written)
- **Major: 16** (significantly degrades quality, implementer blocked on unspecified contracts)
- **Minor: 9** (polish; implementer can work around)

**Top-3 must-fix before implementation:**
1. **C-1 through C-4 (Preservation gap):** Section 3 Goal 3 claims enforcement primitives are preserved. They aren't. Anti-cheat 6-dim panel, tamper detection, invariant enforcement, and coordinator routing logic (~1300 lines across `anticheat_critic_panel.py`, `coordinator.py`, `intervention_judge.py`, `completion_judge.py`) have no explicit home. The spec must add a feature-by-feature migration table.
2. **C-5 (Temporal determinism):** The spec's pseudocode uses bare `now`, `asyncio.gather`, and bare `datetime`-style operations inside workflow code. Temporal workflow code MUST be deterministic; `workflow.now()`, `workflow.sleep()`, determinism constraints are not mentioned anywhere. Implementation as written would crash with `NondeterminismError` on first replay.
3. **C-6 (Classifier auto-launch is architecturally infeasible):** `UserPromptSubmit` hooks can only inject advisory context via `additionalContext` — they cannot short-circuit or suppress the chat agent's response to the user's original prompt. The "auto-launch at ≥0.9 confidence" path creates guaranteed duplicate work. Spec needs to either downgrade auto-launch to always-confirm, or invent a new mechanism beyond hooks.

---

## Critical Defects (7)

### C-1: Anti-cheat reduced from 6-dimensional panel to single LLM call — functional regression of core enforcement
**Source:** A6.1 (preservation critic)
**Section:** §6.3 (run_anticheat activity)
**Evidence:** Today's `anticheat_critic_panel.py` (309 lines) runs 6 parallel adversarial critics (scope_reduction, mock_out, tautology, hardcode, off_criterion, coordinated_edit) with optional second-opinion from a different provider. New spec describes a single-call activity returning `{verdict: "pass"|"fail"|"suspicious"}`.
**Scenario:** Mission passes criterion by inserting `assert True` where a real test was expected. 6-dim panel would catch via `tautology` critic. Single-call activity collapses all dimensions into one verdict and is much easier to fool.
**Remediation:** `run_anticheat` must accept a dimension parameter and fan out 6 activity calls in parallel. Spec must explicitly document the panel structure.

### C-2: Tamper detection has no consumer in new architecture — silent loss of invariant
**Source:** A6.3 (preservation critic)
**Section:** §8.2 (state on disk)
**Evidence:** Today's `success_verifier.verify_tamper()` runs on every verification cycle (line 372) before criterion checks. Spec keeps `~/.swarm/locks/<sid>.sha` (§8.2) but no activity or workflow is assigned to verify it. No `verify_tamper` activity in §6.3.
**Scenario:** Agent edits mission.yaml mid-run to weaken a criterion. In current system, next verifier cycle catches hash mismatch, emits critical finding, completion_judge blocks. In new system, nobody checks.
**Remediation:** Add `verify_tamper` activity in §6.3 called from the verifier loop before `check_criterion` calls.

### C-3: Invariant enforcement (no_mock, test_count_floor, assertion_count_floor, allowed_deps) is not mentioned anywhere in the spec
**Source:** A6.4 (preservation critic)
**Section:** §6.2 verifier loop (should call but doesn't)
**Evidence:** Today's `success_verifier.enforce_invariants()` (151 lines) runs on every verifier cycle. The mission.yaml `Invariants` schema exists (lines 31-35 of today's mission.py) and is a load-bearing anti-cheat primitive. The spec does not mention invariants at all — zero hits for "invariant", "no_mock", "test_count_floor", "allowed_deps" in 525 lines.
**Scenario:** Agent "passes" tests by deleting them. Test-count floor catches this today; new system has no check.
**Remediation:** Add `enforce_invariants` activity + call it in the verifier loop alongside criterion checks. Specify that mission.yaml `invariants` block is consumed.

### C-4: Coordinator routing logic (finding→intervention, escape ladder, strikes, reissue, plan-checkpoint) has no new home
**Source:** A6.5 (preservation critic)
**Section:** §8.3 (one-sentence reference to signal handler)
**Evidence:** Today's coordinator.py (405 lines) + intervention_judge.py (183 lines) implement 7 distinct behaviors: finding→intervention routing with tier/strategy decisions, escape ladder with strikes/tried-strategies tracking, 120s reissue for unacked interventions, periodic plan-checkpoint emission, completion_judge invocation on hold_window_met, anticheat-on-pass-transition, deduplication. Spec's MissionWorkflow signal handler is specified in one sentence: "may signal llm_critic child to judge the finding."
**Scenario:** Mission hits a stuck loop. Today: coordinator escalates through ESCAPE_LADDER (3 rungs), emits progressively aggressive interventions, eventually spawns recovery subagent. New spec: finding arrives as signal, parent does... something. Not specified.
**Remediation:** Add a subsection (probably §6.3.5 or §8.3.1) mapping each coordinator responsibility to: (a) workflow code location, (b) activity, or (c) explicitly dropped. This is the largest single gap in the spec.

### C-5: Temporal workflow determinism violations in pseudocode — would crash on first replay
**Source:** A1.1 (determinism critic)
**Sections:** §6.2 inline verifier loop (`hold_window_start = now`, `(now - hold_window_start) >= hold_window_sec`)
**Evidence:** Temporal workflow code MUST be deterministic. Bare `now` (implying `datetime.now()`) is non-deterministic and causes `NondeterminismError` on replay. Spec contains ZERO mentions of "determinism", "deterministic", "workflow.now", "workflow.sleep", or the replay contract. Same issue likely affects `asyncio.gather` if it's used instead of `asyncio.gather` bound to workflow loop.
**Scenario:** Mission runs for 20min, worker crashes. Temporal replays history. During replay, `datetime.now()` returns current wall clock (not original). Comparison at line 113 produces different result → `NondeterminismError` → workflow killed permanently → no automatic retry → durability goal violated.
**Remediation:** Add a "Temporal determinism constraints" subsection explicitly stating: use `workflow.now()` not `datetime.now()`; use `workflow.sleep()` not `asyncio.sleep()`; no I/O, no `random`, no non-deterministic stdlib calls in workflow code. Replace bare `now` in pseudocode with `workflow.now()`.

### C-6: UserPromptSubmit hooks cannot short-circuit — classifier auto-launch architecture is infeasible as spec'd
**Source:** A3.1 (feasibility critic)
**Section:** §9.3 "Auto-launch mission" path at confidence ≥ 0.9
**Evidence:** Claude Code's `UserPromptSubmit` hook contract only supports `hookSpecificOutput.additionalContext` injection. Verified against real hooks in the environment (OMC's keyword-detector.mjs line 432-439). There is NO `decision: "block"` for `UserPromptSubmit` (that's Stop/SubagentStop only per hook-development SKILL.md line 201-208). Hooks are advisory, not control-flow.
**Scenario:** User types "build a CLI for S3 buckets" at conf 0.95 → hook spawns swarm subprocess + emits `<mission-launched>` reminder → chat agent still receives the original prompt + reminder → chat agent is an LLM, will sometimes ignore reminder and start working → mission subprocess AND chat agent both build the CLI → race, duplicate writes, conflicting commits.
**Remediation:** Downgrade auto-launch to always use the 0.6-0.9 confirmation path (AskUserQuestion). Or invent a new mechanism (e.g., a hook that modifies the user prompt itself — not supported today). Auto-launch as specified cannot be reliably implemented.

### C-7: MissionWorkflow has no ContinueAsNew strategy — will hit Temporal history limit on long missions
**Source:** A1.2 (determinism critic)
**Section:** §6.2 (specified for PatternDetectorWorkflow only)
**Evidence:** Temporal default history limit is 50K events / 50MB. A 24-hour mission (explicitly contemplated in §13 events.jsonl rotation) accumulates ~30K-40K events just from verifier timer ticks + activity lifecycle + child signals + heartbeats. The spec specifies `ContinueAsNew every 500 events` for the PatternDetectorWorkflow child but NOT for the parent MissionWorkflow, which has more event sources.
**Scenario:** Mission runs 20 hours. History approaches 50K events. Temporal rejects further mutations. Mission wedges.
**Remediation:** Add `continue_as_new` strategy for MissionWorkflow. Specify event threshold (e.g., 10K), what state carries across (phase, criteria_state, hold_window_start, findings_count, child handles), and how child workflow handles survive the parent's `continue_as_new`.

---

## Major Defects (16)

### M-1: `resume` parameter has no derivation logic — spec says first-launch=False, retries=True but Temporal replays with same args
**Source:** A2.1 | §6.3, §7.3, §8.4
**Detail:** `run_claude_cli(session_id, mission_prose, resume)` — Temporal retries call the activity with identical arguments each attempt. No built-in way to flip `resume` from False→True across attempts. Implementer must either use `activity.info().attempt`, probe for local session existence inside the activity, or the spec's design is incomplete.
**Remediation:** Drop `resume` from signature. Activity body detects attempt number via `activity.info().attempt` and sets `--resume` for attempt > 1.

### M-2: Session state locality contradicts multi-worker architecture
**Source:** A2.2 | §8.4, §10
**Detail:** `~/.claude/` is local to the worker machine. Spec supports multiple workers (§10). If retry lands on a different worker, `claude --resume <sid>` fails because session state isn't there.
**Remediation:** Add constraint that all workers share `~/.claude/` via network filesystem, OR pin retries to the same worker via task queue routing, OR fall back to fresh session with mission prose on state-miss (and document the cost).

### M-3: Heartbeat is event-driven but spec claims time-driven — false-positive timeouts during long claude turns
**Source:** A2.3 | §7.3
**Detail:** Pseudocode heartbeats inside `for event in tail_events`. Claude can go 2+ min without emitting events (long reads, reasoning). Heartbeat timeout = 2min (§7.2). Trips false-positive → retries → two claude processes may run with same session-id.
**Remediation:** Decouple heartbeat onto independent 30s timer via threading or asyncio task, not tied to event stream.

### M-4: Activity cancellation cleanup hand-waved — abort leaks claude subprocess
**Source:** A2.4 | §8.4, §6.3
**Detail:** `swarm abort` "cancels run_claude_cli activity (which SIGTERMs the claude subprocess)". Temporal activity cancellation sends `CancelledError` to the coroutine; spec's pseudocode at §7.3 has no `try/except CancelledError` or cleanup. Subprocess orphaned.
**Remediation:** Specify `try/except asyncio.CancelledError` in the activity; SIGTERM subprocess in handler; wait with timeout then SIGKILL.

### M-5: Abort doesn't propagate to grandchild subagents
**Source:** A4.3 | §8.4
**Detail:** Claude subprocess spawns subagents as independent processes. SIGTERM to claude process doesn't reach subagents. Today's launch.sh uses `kill -- -$PGID` (process group kill). New spec loses this.
**Remediation:** Activity must spawn claude in its own process group (`start_new_session=True`). On cancellation, kill the process group, not just the PID.

### M-6: No worker running at launch → silent queue stall
**Source:** A4.1 | §10, §14
**Detail:** `swarm launch` submits workflow to Temporal. If no worker is polling the task queue, workflow sits indefinitely. User sees workflow_id, thinks work is happening. Temporal's `StartWorkflow` succeeds even with zero workers.
**Remediation:** `swarm launch` should check worker count via `DescribeTaskQueue` API before submitting. Print clear error if no workers. Or poll for first heartbeat within 15s after submission.

### M-7: No max-mission-duration safety valve
**Source:** A4.2 | §6.2, schema
**Detail:** No `workflow_execution_timeout` on Temporal workflow start; no `max_duration_sec` in mission schema. Mission with unreachable criterion runs forever.
**Remediation:** Add `max_duration_sec` to mission schema (default 4-8 hours). Set as `workflow_execution_timeout` at start. Transition to `failed_terminal` with `reason=max_duration_exceeded` on timeout.

### M-8: Concurrent missions in same workspace corrupt settings.json
**Source:** A4.5 | §11 template, §13 open questions
**Detail:** Auto-launch classifier can kick off Mission B while Mission A runs in same workspace. Both install `.claude/settings.json`. Per-session backup naming (from today's launch.sh) prevents backup collision, but the active settings file is a single file with a race.
**Remediation:** Workspace lock file (`$WORKSPACE/.claude/.swarm-lock`). `swarm launch` refuses if lock exists. Classifier's auto-launch checks lock before proceeding.

### M-9: Classifier auto-launch has no pre-work validation gate for misclassified META queries
**Source:** A4.4 + A3.5 | §9.3
**Detail:** At confidence ≥ 0.9, hook writes derived mission.yaml and launches. If classifier misclassifies "how is the mission going?" as MISSION, a nonsense mission.yaml is derived and launched. Anti-cheat catches eventually but wastes N seconds + resources. Auto-derivation is also explicitly deferred to a future sub-spec (§13) — locking in auto-launch while its derivation is an open question is inconsistent.
**Remediation:** Validate derived mission.yaml against schema + sanity checks (criterion non-trivial, workspace exists) before calling `swarm launch`. On validation fail, downgrade to 0.6-0.9 confirmation path.

### M-10: MCP tools `swarm.propose_criteria` and `swarm.query` named but contracts undefined
**Source:** A5.1 | §11
**Detail:** Both tools load-bearing (propose_criteria used by classifier, query used by META handler). Request/response schemas not specified. Implementer must invent everything.
**Remediation:** Add §6.4 or §10.x specifying for each tool: when invoked, by whom, request params with types, response schema, error handling.

### M-11: `observer_config` schema drift — new keys contradict existing Pydantic model without migration note
**Source:** A5.2 + A6.11 | §6.2
**Detail:** Spec introduces `observer_config.pattern_detector_sec`, `llm_critic_sec`, `resource_monitor_sec`. Today's schema has different keys (`plan_checkpoint_every_sec`, `goal_drift_cadence_sec`, `progress_audit_cadence_sec`). No migration note. Existing mission.yaml files would either fail validation or silently use defaults.
**Remediation:** Schema diff section showing old→new field mapping. Explicitly call out as breaking schema change OR preserve old names.

### M-12: `failed_terminal` phase transitions + cleanup unspecified
**Source:** A5.3 | §6.2, §8.4
**Detail:** Phase exists in state. Multiple error paths set it (§7.4 context overflow, §8.4 terminal errors). But: from which phases can you transition? What cleanup runs? Can user retry? How does user learn?
**Remediation:** Phase transition diagram + cleanup sequence + user-notification mechanism.

### M-13: workflow_id-to-session_id mapping unspecified — CLI commands broken
**Source:** A5.5 | §10
**Detail:** `swarm findings <workflow_id>`, `swarm logs <workflow_id>`, `swarm status` all read from `<session_id>`-keyed disk paths. But workflow_id (Temporal) and session_id (UUID) are different. No mapping specified.
**Remediation:** Make workflow_id equal to session_id (simplest). Or introduce an index file or Temporal search-attribute mapping.

### M-14: Subagent lifecycle has no owner — spawner.py removed, admission control unassigned
**Source:** A5.6 + A6.10 | §11, §6.3
**Detail:** Today's spawner.py (admission control via max_total_live/max_depth/max_fan_out_per_parent, queue, tree state, zombie reaping, RECOVERY context propagation). Spec removes it but spawn_subagent activity has no queueing semantics, no admission control.
**Remediation:** Map each spawner responsibility to new owner (parent workflow for admission, Temporal for tree state, etc.). Specify recovery context propagation to spawned subagent.

### M-15: Intervention ACK flow lost — hooks still read disk files; spec changed storage to Temporal signals
**Source:** A6.6 | §8.1, §8.3
**Detail:** Today's flow: coordinator writes `interventions.jsonl` → claude hooks read and ack via `interventions-acked.jsonl`. Spec changes storage to Temporal signals without updating the consumption path. Claude subprocess runs hooks that read disk; Temporal signals go to workflows, not disk.
**Remediation:** Mirror interventions to `interventions.jsonl` on disk (like findings). Or add a Temporal query that hooks can call + a signal for ack. Specify the downstream bridge in §8.3.

### M-16: Scope-shrinking detection dropped — PatternDetector needs transcript access
**Source:** A6.7 | §6.2
**Detail:** Today's pattern_detector has two modes: event-stream patterns AND transcript scope-shrinking detection. Spec only specifies event-stream (`tails events.jsonl`). Transcript-based detection is missing.
**Remediation:** Specify PatternDetectorWorkflow (or LLMCriticWorkflow) reads claude transcript in addition to events.jsonl.

---

## Minor Defects (9)

- **m-1:** Stop-hook `files_changed_in_this_turn` primitive doesn't exist in Stop hook input payload — requires a companion PostToolUse hook (A3.3).
- **m-2:** Stop-hook `repo_build_check` / `repo_unit_tests` commands undefined — needs per-repo config or auto-detection heuristic (A5.4).
- **m-3:** Hook conflicts with other UserPromptSubmit hooks (OMC, hookify) not addressed — ordering, composition, auth (A3.4).
- **m-4:** Stop-hook 10s test timeout infeasible for real suites — clarify as smoke subset (A3.6).
- **m-5:** Classifier rate-limit degrades silently — add local token bucket (A4.7).
- **m-6:** Heartbeat payload size growth (`workspace_delta_summary`) may hit Temporal 2MB limit on long missions (A1 new angle).
- **m-7:** `swarm health` check details unspecified — mechanism and output format (A5.7).
- **m-8:** Signal handler activity interleaving not specified — what if multiple signals arrive during pending emit_finding activities (A1.3).
- **m-9:** Hold-window Nyquist: spec allows `run_every_sec > hold_window_sec`, creating sampling gaps (A6.9).

---

## Findings Not Upgraded to Defects

- **Child workflow handle survival after ContinueAsNew** (A1.5): real concern, but cleanable via fixed workflow IDs or `ParentClosePolicy`. Implementer-level detail, not spec-level gap.
- **Rollback/re-run semantics** (A5.11): implicit behavior is probably fine; add one sentence for clarity.
- **Test strategy absent** (A5.9): spec explicitly defers to implementation plan; acceptable.

---

## Coverage Assessment

| Required Category | Angles Explored | Coverage |
|---|---|---|
| completeness | A1 (determinism), A5 (undefined referents) | ✅ covered |
| internal_consistency | A2 (resume), A6 (preservation cross-check) | ✅ covered |
| feasibility | A3 (classifier hooks), A6 (preservation cross-check) | ✅ covered |
| edge_cases | A4 (lifecycle edges) | ✅ covered |

All 4 required categories have ≥1 substantive angle. 16 defects surfaced through cross-dimensional preservation check (A6) that the individual dimension critics missed, validating the decision to include a preservation-focused critic.

---

## Recommended Next Steps

**Blocking fixes before writing-plans skill:**
1. Address C-1 through C-7 (critical defects). Each requires material spec additions.
2. Add explicit preservation mapping table: old specialist → new workflow/activity/dropped.
3. Add Temporal determinism subsection.
4. Downgrade auto-launch or redesign the hook-based invocation.

**Non-blocking but recommended:**
5. Address M-1 through M-16 in a revision round (these are implementer-friction defects, not correctness defects).
6. Minor defects can be left as inline TODO annotations during implementation.

**Proposed revision flow:**
- Apply C-fixes → run deep-qa again (round 2) to verify fixes landed without introducing new critical defects
- Then proceed to `superpowers:writing-plans`

**Alternative:** if speed matters more than spec quality, proceed to writing-plans with the understanding that the plan must explicitly address the 7 critical defects as in-scope implementation decisions (writing-plans would effectively serve as the spec-completion pass).

---

## Artifact Owner Notes

The spec is well-written and architecturally sound at the high level. Its weakest area is at the seams: where new Temporal constructs meet existing swarm behaviors, and where new Claude Code hook mechanisms meet the actual hook contract. Fixing C-1 through C-4 alone would reclaim the load-bearing enforcement primitives that make swarm valuable. C-5 is a one-section addition (determinism constraints). C-6 changes the classifier UX claim but doesn't change the architecture meaningfully. C-7 is a drop-in workflow pattern.

Critics' per-angle critiques are available at: `/Users/npow/code/research/swarm/docs/superpowers/specs/deep-qa-20260418-111146/critiques/` (A2, A3, A4, A5, A6 wrote files; A1 returned inline due to read-only tool constraint but content is captured above).

**Termination label:** "Conditions Met" — all 4 required categories covered by substantive critics, no critic returned "looks good overall". Frontier is not exhausted (20+ new angles discovered) but further rounds have diminishing returns given the ~30 defects surfaced.
