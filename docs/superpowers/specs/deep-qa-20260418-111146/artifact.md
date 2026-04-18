# Swarm Durability & Auto-Invocation Redesign

**Date:** 2026-04-18
**Status:** Draft — pending review
**Authors:** npow + Claude (brainstorming session)

## 1. TL;DR

Rebuild swarm on top of Temporal so mission execution survives any transient failure (API errors, process death, machine reboot), and add an LLM-based classifier on `UserPromptSubmit` so mission-shaped tasks get enforcement automatically — without relying on the human to remember `/swarm`.

The existing mission-enforcement value (success criteria, anti-cheat, pattern detection, hash-pinned missions) is preserved. The existing `launch.sh` bash harness and the six daemon-process specialists (pattern_detector, success_verifier, coordinator, supervisor, llm_loop, resource_monitor) are replaced by a Temporal workflow tree and a cascading classifier.

## 2. Motivation

Two concrete pain points drove this redesign:

**2.1. Fragility.** On 2026-04-18, a swarm session running for 28 minutes died with `API Error: 424 status code (no body)` from Anthropic. The `claude` CLI exited once, the `trap cleanup EXIT` in `launch.sh` fired, all six specialists were SIGKILL'd, settings were restored, and the session was gone. State on disk (events.jsonl, findings.jsonl, verifier_status.json, and the mission's actual output in `DESIGN.md`) survived — but there was no way to resume. 28 minutes of work lost to a single transient API blip.

This is structurally unavoidable with the current architecture because `launch.sh` invokes `claude` exactly once, not inside any retry loop, with no resume primitive:

```bash
# launch.sh:98
PYTHONPATH="$REPO_ROOT" SESSION_ID="$SESSION_ID" SWARM_ROOT="$SWARM_ROOT" \
  claude --session-id "$SESSION_ID" "$MISSION_PROSE"
```

Any non-zero exit from `claude` tears everything down.

**2.2. Forgotten enforcement.** The `/swarm` slash command requires the human to remember to invoke it. For tasks that *should* be mission-enforced ("build X", "fix Y", "refactor Z"), forgetting means the task runs under normal chat semantics — no success criteria, no anti-cheat LLM review, no pattern detection, no hold-window guarantee. The work proceeds but whatever premature-completion / scope-drift / drift-to-over-scope failure modes the enforcement layer exists to catch are now unchecked.

Humans forget. Discipline is the worst failure mode to rely on.

## 3. Goals

1. **A mission survives any transient failure and resumes automatically.** API errors (424, 429, 5xx), process crashes, worker reboots, machine reboots — none of these should require human intervention.
2. **Mission-shaped work gets enforcement automatically.** Without the human typing `/swarm`, without the chat agent self-classifying (which it has incentive to misclassify toward "not mission").
3. **Existing enforcement primitives (criteria, anti-cheat, patterns) are preserved** — this is swarm's unique value.
4. **Pre-mission chat stays unencumbered.** Brainstorming, Q&A, exploration, meta-queries about missions — all remain ordinary conversation, not wrapped in mission enforcement.

## 4. Non-goals

- Migration plan from today's swarm to new swarm. (Will be a separate spec / plan when ready to build.)
- Temporal server lifecycle management. Temporal is a user-managed prerequisite (`temporal server start-dev`), not something swarm starts or monitors.
- Multi-user, multi-tenant, or distributed operation. Swarm is a single-user personal tool at N=1.
- Replacing Claude Code CLI as the agent runtime. The mission subprocess continues to be `claude --session-id <sid>` on first launch and `claude --resume <sid>` on every retry. (The `run_agent` activity boundary is narrow enough that a future swap to Goose or custom SDK is possible without redesigning the workflow layer.)
- Fine-tuning the classifier. Static prompt with an audit log for manual review. Learning is future work.

## 5. Design principles

1. **Workflow = entity with independent lifecycle; Activity = side-effectful unit of work.** Use Temporal primitives idiomatically. Don't decompose for aesthetics.
2. **The jailer is not the prisoner.** The classifier that decides "is this a mission?" runs outside the chat agent's session, because the chat agent has incentive to say "no".
3. **Fail open, never block.** Every safety layer (classifier, hooks, verifier) has a timeout and defaults to letting the user continue. A broken classifier should never wedge chat.
4. **Temporal history is source of truth; disk is grep-friendly mirror.** Workflow state lives in Temporal. Raw claude hook events and findings are mirrored to JSONL files for external consumption (grep, Monitor tools, human inspection), but are never authoritative.
5. **Missions are separate processes from chat.** The mission-enforced subprocess is a second `claude` process with its own workspace and settings. The human's chat session is never "mission-enforced" — chat stays chat.

## 6. Architecture

### 6.1 Layering

```
┌───────────────────────────────────────────────────────────────────┐
│ Layer 3: Durability / orchestration                               │
│   Temporal (user-managed local server)                            │
├───────────────────────────────────────────────────────────────────┤
│ Layer 2: Mission controller (this spec)                           │
│   MissionWorkflow + child workflows + activities                  │
│   Preserves enforcement: criteria, anti-cheat, patterns, tamper   │
├───────────────────────────────────────────────────────────────────┤
│ Layer 1: Agent runtime (unchanged)                                │
│   claude CLI — session-id resume, hooks, skills, MCP, subagents   │
└───────────────────────────────────────────────────────────────────┘
```

### 6.2 Shape D: mission as parent workflow

One Temporal workflow per mission. Three child workflows for the genuinely-independent observational concerns. Everything else is activities called from the parent.

```
MissionWorkflow  (parent, long-running — one per mission)
├── state (authoritative, persisted by Temporal):
│     phase: "launching" | "running" | "passing" | "hold_window" |
│            "complete" | "aborting" | "aborted" | "failed_terminal"
│     criteria_state: {criterion_id: {pass: bool, last_check_ts, streak_sec, ...}}
│     hold_window_start: datetime | None
│     findings_count: int
│     abort_reason: str | None
│
├── signals received:
│     finding_emitted(finding)           ← from any child workflow
│     intervention_request(action)       ← from pattern_detector
│     abort(reason)                      ← from user CLI
│     pause() / resume()                 ← from user CLI (future)
│
├── queries (read-only, no history entry):
│     get_status()  -> phase + criteria summary
│     get_findings() -> recent findings
│
├── activity: run_claude_cli (long-running, one outstanding at a time)
│     heartbeat every 30s with progress payload
│     retries transient errors, classifies per §7
│     uses --resume <sid> on every retry
│
├── inline verifier loop (timer-driven, in the workflow):
│     every verification.run_every_sec:
│       parallel: execute_activity(check_criterion, c) for c in mission.criteria
│       update criteria_state from results
│       if all(pass) and phase != "hold_window": transition to "hold_window"
│                                                 set hold_window_start = now
│       elif any(not pass) and phase == "hold_window":
│                                                 reset hold_window_start = None
│                                                 transition back to "running"
│       elif phase == "hold_window" and
│            (now - hold_window_start) >= hold_window_sec:
│                                                 transition to "complete"
│                                                 break
│     await workflow.sleep(verification.run_every_sec)
│
└── child workflows (started at mission launch, cancelled at completion):
    ├── PatternDetectorWorkflow(mission_id, session_id, cadence_sec)
    │     tails ~/.swarm/state/<sid>/events.jsonl
    │     runs pattern rules, emits findings via signal to parent
    │     ContinueAsNew every 500 events to bound history
    │     cadence_sec default 10s, override via mission.yaml observer_config.pattern_detector_sec
    │
    ├── LLMCriticWorkflow(mission_id, session_id, cadence_sec)
    │     progress & drift checks on cadence
    │     invokes activity run_anticheat(sample, policy)
    │     emits findings via signal to parent
    │     cadence_sec default 120s, override via mission.yaml observer_config.llm_critic_sec
    │
    └── ResourceMonitorWorkflow(mission_id, session_id, cadence_sec)
          zombie/memory/disk checks on cadence
          emits findings via signal to parent
          cadence_sec default 30s, override via mission.yaml observer_config.resource_monitor_sec
```

Why these three are child workflows (not activities called by parent): each runs a continuous loop with its own cadence, its own failure mode, its own state. They can be independently restarted without disturbing the others. They can be versioned independently.

Why criteria are NOT child workflows: a criterion is a predicate evaluated on a timer. It has no independent lifecycle — it's a parameter to the mission. Making each criterion a workflow adds coordination plumbing (signals between parent and N children) for no lifecycle benefit.

Why `run_claude_cli` is an activity (not a workflow): it has a clear beginning and end (one process invocation), it's the primary work the mission does, and its state (the conversation) is managed by the `claude` CLI itself via session-id. Wrapping it as an activity gives us retry + heartbeat + timeout + cancellation without the history-size cost of making it a workflow.

### 6.3 Activities

| Activity | Purpose | Notes |
|---|---|---|
| `run_claude_cli(session_id, mission_prose, resume)` | Launches the mission subprocess. Streams stdout/stderr to disk. Heartbeats every 30s with progress payload. Returns on clean exit or raises classified error. | Long-running (minutes to hours). `resume=True` passes `--resume <sid>`. |
| `check_criterion(criterion)` | Runs one criterion's shell check. Returns `{pass: bool, exit_code, stdout_tail, stderr_tail, duration_ms}`. Idempotent. | Short (seconds). Subject to `criterion.timeout_sec`. |
| `run_anticheat(sample, policy)` | Invokes Opus-level reviewer: `claude -p --bare --model opus <prompt>`. Returns `{verdict: "pass"|"fail"|"suspicious", rationale}`. | Medium (seconds to a minute). |
| `spawn_subagent(config)` | Starts a subagent process with given config. Heartbeats until subagent exits. Returns final status + output. | Long-running. Tracked via PID. |
| `restart_subprocess(name)` | Restarts a specialist or subagent subprocess by name. Idempotent by design. | Short. Currently covered by child workflows but retained for ad-hoc restarts. |
| `emit_finding(finding)` | Appends to `findings.jsonl` on disk AND returns. Called by parent workflow from the signal-handler path (so disk mirror stays in sync with Temporal history). | Short. |
| `classify_prompt(prompt, context)` | Calls Haiku to classify user prompt as MISSION / CHAT / META. Returns `{verdict, confidence, reason}`. | Short (<2s with timeout). Not called by MissionWorkflow — called by the UserPromptSubmit hook directly. Listed here for completeness. |

## 7. Error classification & retry policies

### 7.1 Error taxonomy

| Code / condition | Class | Rationale |
|---|---|---|
| HTTP 200 | Success | — |
| HTTP 400 | Terminal | Malformed request; won't succeed on retry. |
| HTTP 401, 403 | Terminal | Auth/authz; needs human. |
| HTTP 404 | Terminal | Endpoint or resource gone. |
| HTTP 408 | Transient | Timeout; retry. |
| HTTP 424 | Transient | Observed in the 2026-04-18 crash. Anthropic-specific; treat as transient-but-loud (emit finding). |
| HTTP 429 | Transient | Rate limit. Honor `Retry-After` header. |
| HTTP 500, 502, 503, 504 | Transient | Server-side transient. |
| Network refused / reset / timeout | Transient | Local blip or server down. |
| Subprocess killed by OOM | Transient | Retry after resource_monitor clears OOM pressure. |
| Subprocess timeout (heartbeat expired) | Transient | Temporal retries on fresh worker. |
| Context window exceeded | Terminal | Requires compaction or fresh session; fail mission with clear message. |
| Billing exceeded | Terminal | Needs human. |
| `SIGKILL` from user | Terminal | Explicit user cancellation. |

Implementation:

```python
# swarm/durable/errors.py
TRANSIENT_HTTP = {408, 424, 429, 500, 502, 503, 504}
TERMINAL_HTTP = {400, 401, 403, 404}

class SwarmActivityError(Exception):
    classification: Literal["transient", "terminal"]
    retry_after: Optional[float] = None

# Temporal retry policy consumes non_retryable_error_types
NON_RETRYABLE = [
    "TerminalHTTPError",
    "AuthError",
    "BillingError",
    "ContextOverflowError",
    "UserCancelledError",
]
```

### 7.2 Retry policies per activity

| Activity | Initial interval | Max interval | Backoff | Max attempts | Heartbeat timeout |
|---|---|---|---|---|---|
| `run_claude_cli` | 2s | 5min | ×2 | 20 | 2min |
| `check_criterion` | 1s | 30s | ×2 | 5 | (short activity, no heartbeat needed) |
| `run_anticheat` | 5s | 5min | ×2 | 10 | 2min |
| `spawn_subagent` | 2s | 1min | ×2 | 3 | 1min |
| `restart_subprocess` | 1s | 10s | ×2 | 5 | — |
| `emit_finding` | 100ms | 5s | ×2 | 3 | — |
| `classify_prompt` | (no retry — fail-open to CHAT) | — | — | 1 | — |

All activities set `non_retryable_error_types=NON_RETRYABLE`.

### 7.3 Heartbeats

`run_claude_cli` is the durability-critical activity. The activity runs the subprocess and heartbeats back to Temporal on a cadence:

```python
@activity.defn
async def run_claude_cli(session_id, mission_prose, resume) -> ClaudeResult:
    proc = spawn_claude(session_id, mission_prose, resume)
    for event in tail_events(session_id):
        activity.heartbeat({
            "last_event_id": event.id,
            "last_tool": event.tool_name,
            "event_count": event.count,
            "workspace_delta": event.workspace_delta_summary,
        })
        if proc.exited:
            break
    if proc.exit_code != 0:
        raise classify_error(proc.exit_code, proc.stderr)
    return ClaudeResult(events=event.count, ...)
```

The heartbeat payload also powers the `get_status` query — the parent workflow can read the most recent heartbeat without waiting for activity completion.

If the worker dies silently (segfault, OOM, machine reboot), Temporal detects the heartbeat gap after `heartbeat_timeout` and retries the activity on a fresh worker. The fresh worker calls `run_claude_cli` with `resume=True`, which invokes `claude --resume <sid>` — conversation state is preserved by the claude CLI's own session storage (`~/.claude/`), so the mission picks up from the last completed tool call.

### 7.4 Context overflow policy

If `run_claude_cli` raises `ContextOverflowError`, the mission is marked `failed_terminal` with a structured reason. Auto-compaction is deferred (see §11 open questions).

## 8. State management

### 8.1 What lives in Temporal (authoritative)

All state that affects resume or completion semantics:

- Mission phase and transitions
- Per-criterion pass/fail + streak timer + last-check metadata
- Hold-window start timestamp (bug fix from today's implementation where this isn't durably tracked)
- Pending and acked interventions (delivered as signals, persisted in history)
- Specialist-child workflow state (each child owns its own)
- All signals received and activity results (standard Temporal history)

### 8.2 What stays on disk (telemetry / mirror)

Written by hooks or as side effects; cheap, grep-able, consumable by external tools:

- `~/.swarm/state/<sid>/events.jsonl` — raw claude hook events. Written by `PostToolUse` shell-script hook inside the claude subprocess. Never touches Temporal directly.
- `~/.swarm/state/<sid>/events_detail/<event_id>.json` — per-event detail blobs.
- `~/.swarm/state/<sid>/findings.jsonl` — mirror of signal-delivered findings. Written by the parent workflow's signal handler via the `emit_finding` activity. Temporal history is authoritative; this file is for external grep.
- `~/.swarm/state/<sid>/*.log` — subprocess stdout/stderr logs (specialists + claude).
- `~/.swarm/locks/<sid>.sha` — mission hash-pin for tamper detection. Kept.
- Workspace artifacts — the actual mission output, lives in the user's workspace dir. Not swarm state.

### 8.3 Hook-to-workflow bridge

The `claude` subprocess runs `PostToolUse` hooks as shell scripts (fast, no Temporal client in the hot path). Events flow:

```
claude subprocess
    │ PostToolUse hook (appends one line)
    ▼
~/.swarm/state/<sid>/events.jsonl      [raw telemetry on disk]
    │
    │ PatternDetectorWorkflow's tail activity reads the stream
    ▼
PatternDetectorWorkflow
    │ pattern matched → finding
    ▼
    signal to parent MissionWorkflow: finding_emitted(finding)
    │
    ▼
MissionWorkflow signal handler
    ├── calls emit_finding activity (mirrors to findings.jsonl for external grep)
    ├── updates internal state (e.g., tamper detected → sets abort_reason)
    └── may signal llm_critic child to judge the finding
```

This avoids two problems that would otherwise arise from running a Temporal client inside the hook script: (1) hook startup latency (Temporal SDK import ~100ms) and (2) history bloat (one history entry per hook invocation; thousands per mission).

### 8.4 Resume semantics

**API error mid-mission.** Activity raises transient error. Temporal retries per policy. `run_claude_cli` re-invokes with `--resume` on every retry. For the 2026-04-18 failure class, the retry interval would have been ~2-8s. Mission continues.

**Worker process death (kill -9, segfault, OOM).** Temporal detects heartbeat gap after 2min. Retries `run_claude_cli` on a fresh worker (which may be the same PID respawned or a different process if multiple workers run). Fresh worker calls `claude --resume <sid>`; conversation state restored from `~/.claude/`.

**Machine reboot.** Temporal server's SQLite persistence survives. On reboot: user starts Temporal (`brew services start temporal` or similar), user starts `swarm worker`, Temporal replays workflow history to reconstruct state, activities resume. No human intervention on mission content.

**User aborts.** `swarm abort <workflow_id>` sends `abort` signal. Parent sets phase=`aborting`, cancels `run_claude_cli` activity (which SIGTERMs the claude subprocess), cancels child workflows, workflow exits with `{status: "aborted", reason: "user"}`.

**Mission completes.** Parent workflow returns result; child workflows are cancelled automatically via Temporal parent-child cleanup. Specialists shut down cleanly.

**Terminal error.** Parent catches `TerminalHTTPError` / `AuthError` / etc., sets phase=`failed_terminal`, returns result with reason. No retry. Human must fix (e.g., re-auth).

## 9. Invocation layer — classifier cascade

### 9.1 Shape

A `UserPromptSubmit` hook runs on every user turn and decides: MISSION, CHAT, or META. The decision is made by a cascade from cheap-and-certain to LLM-adjudicated:

```
UserPromptSubmit hook receives (prompt, last_5_turns)
    │
    ▼
Stage 1 — explicit prefix (microseconds)
    "/mission" | "/swarm" prefix → MISSION (confidence=1.0)
    "/chat" | "/explore" | "just chat:" prefix → CHAT (confidence=1.0)
    → if matched, skip to §9.3
    │
    ▼
Stage 2 — rule gate (milliseconds)
    regex: trailing "?" AND len(words) < 20 → CHAT (confidence=0.8)
    regex: len(words) < 5 → CHAT (confidence=0.7)
    regex: leading word in {explain, review, what, why, how, walk me through, help me understand}
           → CHAT (confidence=0.75)
    regex: leading verb in {build, fix, implement, add, create, refactor, write (tests|docs|code for)}
           → MISSION (confidence=0.75)
    regex: "how's the (mission|swarm|task)", "what did the mission", "show me the findings",
           "abort|pause|resume" → META (confidence=0.85)
    → if any rule fires with confidence >= 0.75, skip to §9.3
    │
    ▼
Stage 3 — LLM classifier (Haiku 4.5, ~500ms)
    Invokes classify_prompt activity
    Returns {verdict: MISSION|CHAT|META, confidence: 0-1, reason: str}
    2-second deadline; on timeout → CHAT, confidence=0 (fail open)
    │
    ▼
Stage 4 — confidence gate (§9.3)
```

### 9.2 Classifier prompt (Stage 3)

```
You are classifying a user message into exactly one of three classes:

MISSION — closed-form task with verifiable artifact output.
  Examples: "build a CLI for X", "fix the bug in Y", "implement feature Z",
            "add tests for Q", "refactor W to use pattern P".
  Signals: has an observable end state, completion can be verified without
           human opinion, produces a file or state change.

CHAT — open-ended, exploratory, or conversational.
  Examples: "explain this function", "what do you think about X",
            "walk me through the codebase", "help me brainstorm Y".
  Signals: no artifact, criterion would be "human approved", conversational.

META — about an existing mission or swarm itself.
  Examples: "how's the mission going?", "show me the findings",
            "abort the current mission", "why did it stall?"
  Signals: asks ABOUT a mission; is itself read-only.

Recent conversation (last 5 turns, most recent last):
{recent_context}

User message:
{prompt}

Respond with JSON only:
{"verdict": "MISSION|CHAT|META", "confidence": 0.0-1.0, "reason": "<short>"}
```

### 9.3 Confidence gate & actions

| Verdict + confidence | Action |
|---|---|
| MISSION, conf ≥ 0.9 | Auto-launch mission. Hook writes a mission.yaml (derived from prompt + context) and calls `swarm launch`. Chat agent proceeds with a `<mission-launched workflow_id=X>` system-reminder so it knows not to redo the work. |
| MISSION, 0.6 ≤ conf < 0.9 | Hook injects system-reminder: chat agent calls AskUserQuestion with 3 options: "launch mission (recommended) / edit criteria / skip enforcement". 1 tap confirms. |
| MISSION, conf < 0.6 | Treat as CHAT. Log as "suspected-mission, skipped" for audit. |
| CHAT (any conf) | No-op. Normal chat. |
| META (any conf) | Hook injects system-reminder: chat agent should invoke `swarm.query` MCP tool to answer (reads from Temporal query + disk findings), not re-do work. |
| Classifier timeout or error | Treat as CHAT. Log as classifier-failure. |

### 9.4 Classifier configuration (locked)

| Param | Value | Why |
|---|---|---|
| Model | Haiku 4.5 | Fast (~500ms), cheap (~$0.001/classification), accurate enough for 3-class problem with engineered prompt. Escalate to Sonnet tiebreaker only if accuracy is measurably bad in production. |
| Caching | None | Hit rate <5% (prompts rarely verbatim-identical, context differs). Not worth invalidation complexity. |
| Learning loop | Log-only | N=1 user, insufficient data for fine-tuning. Log `{ts, prompt_hash, context_hash, verdict, confidence, user_override, final_action}` to `~/.swarm/classifier.jsonl`. Manual review quarterly. |
| Backend | Anthropic API | Reuses claude CLI's auth. No local model (offline not a priority, and 8B-class local models are slower and less accurate). |
| Timeout | 2000ms | Longer kills responsiveness. |
| Failure mode | Fail open to CHAT | Broken classifier never blocks the user. |

### 9.5 Safety net — Stop-hook regression guard

Even when the classifier routes to CHAT, a lightweight `Stop` hook runs after each chat turn that wrote files:

```bash
# ~/.claude/hooks/stop-regression-check.sh
if files_changed_in_this_turn; then
    timeout 5s repo_build_check && timeout 10s repo_unit_tests
    if regression_detected; then
        emit_system_reminder("Regression detected: <summary>. Investigate before continuing.")
    fi
fi
```

This catches the false-negative case where a task should have been classified MISSION (e.g., "add a log statement" that silently breaks the build). Not mission-level enforcement — a thin always-on regression guard.

The hook must:
- Have a hard 15-second total deadline (never block the user).
- Skip if the repo has no build/test commands configured.
- Be idempotent (safe to run on every turn).

## 10. CLI surface

```
swarm launch <mission.yaml>          # starts MissionWorkflow, prints workflow_id
swarm launch --interactive           # prompts for workspace + criteria, then launches
swarm status [<workflow_id>]         # queries workflow (phase, criteria, hold_window)
                                       # with no arg, shows all active missions
swarm abort <workflow_id> [--reason] # sends abort signal, returns when phase=aborted
swarm findings <workflow_id>         # tails findings.jsonl (disk mirror)
swarm logs <workflow_id>             # streams claude subprocess stdout/stderr
swarm worker                         # starts a worker daemon (long-running)
                                       # multiple workers can run for higher availability
swarm health                         # checks Temporal connectivity, worker liveness,
                                       # classifier API, and prints readiness report
swarm history <workflow_id>          # dumps Temporal workflow history
                                       # (for post-mortem / debugging)
```

`swarm resume <workflow_id>` is deliberately not a command: resume is automatic, driven by Temporal. If a user wants to "resume", they start a worker and the in-flight workflows resume themselves. `swarm health` reports whether resume is progressing.

## 11. File layout

```
swarm/
├── cli.py                           # swarm launch / status / abort / worker / ...
├── classifier/
│   ├── hook.py                      # UserPromptSubmit hook entry point
│   ├── rules.py                     # Stage 1 + 2 rule gate
│   ├── llm.py                       # Stage 3 Haiku client
│   └── prompts.py                   # classifier prompt templates
├── durable/
│   ├── workflow.py                  # MissionWorkflow (parent)
│   ├── specialists/                 # child workflows
│   │   ├── pattern_detector.py
│   │   ├── llm_critic.py
│   │   └── resource_monitor.py
│   ├── activities/                  # activity implementations
│   │   ├── run_claude_cli.py
│   │   ├── check_criterion.py
│   │   ├── run_anticheat.py
│   │   ├── spawn_subagent.py
│   │   └── emit_finding.py
│   ├── errors.py                    # error classification (§7.1)
│   ├── retry_policies.py            # per-activity policies (§7.2)
│   └── worker.py                    # Temporal worker entrypoint (swarm worker)
├── schemas/
│   ├── mission.py                   # mission.yaml schema (existing)
│   ├── criterion.py                 # per-criterion schema
│   └── finding.py                   # finding schema (used by signals)
├── hooks/
│   ├── post_tool_use.sh             # writes to events.jsonl (existing, kept)
│   ├── user_prompt_submit.py        # classifier hook entrypoint (new)
│   └── stop_regression_check.sh     # safety net (new, §9.5)
├── mcp/
│   └── server.py                    # MCP tools: swarm.propose_criteria, swarm.query
├── docs/
│   └── superpowers/specs/           # this document lives here
└── settings.json.template           # kept, applied to workspace .claude/settings.json
```

What goes away:
- `launch.sh` (replaced by `swarm launch` in cli.py, which talks to Temporal)
- `_launch_helper.py` (mission prose extraction moves into cli.py)
- `swarm-spawn` / `swarm-cli` (merged into unified `swarm` CLI)
- `specialists/supervisor.py`, `specialists/spawner.py`, `specialists/coordinator.py` as daemon processes (orchestration moves into MissionWorkflow and its children)
- `swarm/specialists/*.py` as module entrypoints (logic moves into `durable/specialists/*.py` as workflows)

## 12. Locked decisions

| Decision | Locked value |
|---|---|
| Workflow topology | Shape D: mission as parent; 3 child workflows (pattern_detector, llm_critic, resource_monitor); criteria as parent state; run_claude_cli as heartbeating activity |
| Temporal deployment | User-managed local server (`temporal server start-dev`) as a prerequisite. Swarm fails fast with start-instructions if unreachable. |
| Rewrite scope | Full — all specialists become workflows/activities. launch.sh goes away. |
| Invocation | Classifier cascade on UserPromptSubmit; explicit `/mission` and `/chat` prefixes override. |
| Classifier model | Haiku 4.5 |
| Classifier caching | None |
| Classifier learning | Log-only, manual review |
| Classifier backend | Anthropic API |
| Confidence thresholds | ≥0.9 auto-launch; 0.6-0.9 confirm via AskUserQuestion; <0.6 treat as CHAT |
| Classifier timeout | 2000ms, fail-open to CHAT |
| Safety net | Stop-hook regression check on file-writing turns, 15s hard deadline |
| Agent runtime (mission subprocess) | `claude --session-id <sid>` on first launch; `claude --resume <sid>` on every retry. Unchanged from today. Activity boundary is narrow enough to swap later. |

## 13. Open questions / future work

- **Context overflow handling.** Today: fail mission with terminal error. Future: auto-compact via `claude --compact` + re-attach, OR summarize-and-restart with fresh session. Decision deferred until we see the failure in practice.
- **Classifier accuracy in production.** If false-positive rate on MISSION > 10% or false-negative rate > 5% (measured via `user_override` in `classifier.jsonl`), consider (a) Sonnet tiebreaker for `0.6-0.9 confidence`, (b) prompt tuning, (c) per-user rules layer.
- **findings.jsonl ordering guarantees.** Temporal signals are durable and ordered per workflow. Disk mirror is eventual-consistency. If any external tool depends on strict ordering, switch to reading Temporal query instead.
- **events.jsonl rotation.** Today's 28-min crashed session wrote 309KB + 68 detail files. At 24-hour missions this would be >10MB. Add rotation trigger in pattern_detector: when events.jsonl > 10MB, rotate to events.jsonl.1 and start fresh.
- **Multi-mission concurrency.** Nothing in this design prevents N parallel missions on the same worker. Resource (API rate limits, disk I/O, memory) contention is unmodeled. If multi-mission becomes common, add worker-level admission control.
- **Auto-derived mission.yaml for classifier auto-launch.** Stage 4 confidence ≥ 0.9 launches without human confirmation. The mission.yaml must be derived from the prompt + conversation context. Needs a template + heuristics for workspace, criteria, and hold_window defaults. Treat as a separate sub-spec.
- **Removing the Stop-hook safety net.** If classifier recall on MISSION-shaped prompts is high enough (measured), the safety net becomes redundant and adds latency. Keep until we have confidence in classifier recall.
- **Anti-cheat LLM output handling.** `run_anticheat` verdicts currently feed into findings. Consider: should a `verdict=fail` from anti-cheat auto-trigger a user intervention signal (human must ack), or just emit a finding?

## 14. Success criteria for this redesign (meta)

A shell-checkable checklist for evaluating the implementation, once it exists:

- Mission survives simulated API 424 for 60 continuous seconds without human intervention (Temporal retries succeed, criteria continue being verified).
- `kill -9` on the worker process is followed by automatic recovery within 3 minutes when the worker restarts.
- Machine reboot during mission leaves a runnable state: after Temporal + worker restart, `swarm status` shows `phase` matching pre-reboot and activities resume.
- Classifier latency < 1s at p95 on real prompts.
- Classifier accuracy > 90% on a hand-labeled set of 100 real prompts (measured before production rollout).
- Hook overhead < 100ms on CHAT-classified prompts (rule gate short-circuits, no LLM call).
- `swarm abort` transitions to `aborted` phase within 10 seconds.

---

**Next steps after user approval:** invoke `superpowers:writing-plans` skill to produce an implementation plan broken into executable chunks with per-step verification.
