**QA Dimension:** internal_consistency
**Angle:** claude CLI resume semantics across sections
**Critique Date:** 2026-04-18

## Defects Found

### Defect 1: `resume` parameter has no derivation logic — spec contradicts Temporal retry semantics
**Severity:** critical

**Scenario:** The activity signature is `run_claude_cli(session_id, mission_prose, resume)` (§6.3 line 147). §4 line 45 says "first launch uses `--session-id <sid>`" and "every retry" uses `--resume <sid>`. §7.3 line 235 says "The fresh worker calls `run_claude_cli` with `resume=True`". But Temporal retries invoke the activity function with the **same arguments** it was originally called with. There is no Temporal-native mechanism to flip `resume` from `False` to `True` across attempts.

The spec describes the desired outcome (retries use `--resume`) but not the mechanism. Three sections assert it happens; zero sections explain how. The pseudo-code in §7.3 (lines 217-231) accepts `resume` as a parameter but shows no logic to derive it — it just passes it through to `spawn_claude`.

**Root Cause:** The spec treats `resume` as a caller-supplied parameter but the caller (MissionWorkflow or Temporal retry machinery) has no specified logic for setting it. The workflow code that calls the activity is never shown. The implementor must invent the mechanism: either (a) the activity internally checks `activity.info().attempt` and sets `resume = attempt > 1`, or (b) the activity checks whether `~/.claude/` already has a session for `session_id` and infers resume, or (c) the workflow always passes `resume=True` and relies on `claude --resume` being a no-op on first launch. None of these is stated.

**Remediation:** Add a "Resume derivation" paragraph to §7.3 specifying the exact mechanism. Recommendation: drop `resume` from the activity signature entirely; have the activity body check `activity.info().attempt > 1` or probe for an existing session file. This eliminates the contradiction with Temporal's same-argument retry semantics.

---

### Defect 2: Session state locality assumption contradicts multi-worker claims
**Severity:** critical

**Scenario:** §8.4 line 295 says: `Fresh worker calls claude --resume <sid>; conversation state restored from ~/.claude/`. §10 line 427 says: `multiple workers can run for higher availability`. If worker A dies and Temporal schedules the retry on worker B (a different process, possibly different machine), `~/.claude/` on worker B has no session state for that `session_id`. The `claude --resume <sid>` call will fail — the session does not exist locally.

§4 line 44 says "Multi-user, multi-tenant, or distributed operation" is a non-goal, and "Swarm is a single-user personal tool at N=1". This partially mitigates: if all workers run on the same machine, they share `~/.claude/`. But the spec never explicitly constrains workers to the same machine, and "multiple workers can run" naturally suggests process-level (possibly multi-machine) redundancy.

Even on a single machine: if the user rebooted and `~/.claude/` was on a tmpfs, or if `claude` prunes old sessions, the session state is gone. The spec provides no fallback.

**Root Cause:** The spec assumes `~/.claude/` is a durable, shared filesystem accessible to all workers, but never states this assumption or addresses what happens when it is violated.

**Remediation:** Add an explicit assumption: "All workers MUST share a filesystem with the `~/.claude/` directory containing session state." Add a fallback policy for when session state is missing: either (a) fail the activity with a terminal error (mission lost), or (b) fall back to `--session-id` (fresh start, mission context lost but execution continues), with a clear user-visible warning.

---

### Defect 3: Heartbeat timeout vs. long-thinking claude turns — false-positive death detection
**Severity:** major

**Scenario:** §7.2 line 201 sets `heartbeat_timeout = 2min`. §7.3 line 213 says heartbeats fire on each event from `tail_events(session_id)`. The heartbeat loop in the pseudo-code (lines 219-227) only heartbeats when `tail_events` yields an event. If `claude` is in a long think (reading a large file, extended reasoning, waiting for a slow API response) for >2 minutes without producing a hook event, zero heartbeats fire, and Temporal declares the activity dead and retries it.

This is a false-positive kill. The claude process is healthy and working. Temporal then starts a second instance via `--resume`, which may collide with the still-running original (race condition: two claude processes with the same session_id).

The spec's own motivating example (§2.1) describes a 28-minute session. A mission that long will almost certainly have >2min gaps between tool-use events during complex reasoning or large file reads.

**Root Cause:** The heartbeat is event-driven (fires when `tail_events` yields), not time-driven (fires every 30s regardless). The §6.2 line 99 claim "heartbeat every 30s with progress payload" is inconsistent with the §7.3 pseudo-code which only heartbeats on event arrival.

**Remediation:** The heartbeat loop must be time-based, not event-based. Either: (a) run the heartbeat on an independent timer (e.g., `asyncio.create_task` that heartbeats every 30s with a "still alive, no new events" payload), or (b) use `asyncio.wait_for(tail_events.__anext__(), timeout=25)` and heartbeat on timeout with a "waiting" status. Update the pseudo-code in §7.3 to reflect whichever approach is chosen. Consider increasing `heartbeat_timeout` to 5min for `run_claude_cli` specifically.

---

### Defect 4: Cancellation cleanup for `swarm abort` is hand-waved
**Severity:** major

**Scenario:** §8.4 line 299 says: `cancels run_claude_cli activity (which SIGTERMs the claude subprocess)`. §10 line 423 says: `swarm abort <workflow_id> cancels run_claude_cli activity (which SIGTERMs the claude subprocess)`. But Temporal activity cancellation delivers a `CancelledError` (Python) or `CancellationError` to the activity coroutine. The activity code must catch this and explicitly send SIGTERM to the subprocess. The §7.3 pseudo-code (lines 217-231) has no `try/except CancelledError` block and no subprocess cleanup logic.

This means: if implemented as-is, `swarm abort` sends the cancellation signal, the activity coroutine is interrupted, the `claude` subprocess is orphaned (still running), the mission shows `aborted` in Temporal but the claude process keeps writing to the workspace. The three child workflows may also leave orphaned processes.

**Root Cause:** The spec describes the desired outcome of cancellation (subprocess killed) but not the mechanism (catching CancelledError, sending SIGTERM, waiting for exit, handling SIGTERM timeout with SIGKILL escalation).

**Remediation:** Add a cancellation handler to the §7.3 pseudo-code:
```python
try:
    # ... existing heartbeat loop ...
except asyncio.CancelledError:
    proc.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.kill()
    raise
```
Also specify cleanup for child workflow cancellation (pattern_detector, llm_critic, resource_monitor each presumably have subprocesses too).

---

### Defect 5: Session state exhaustion after 20 retries with no degradation strategy
**Severity:** minor

**Scenario:** §7.2 gives `max_attempts=20` for `run_claude_cli`. Each retry invokes `claude --resume <sid>`. If the session state is corrupted or the underlying issue is persistent (e.g., Anthropic API is down for 30+ minutes), the activity burns through 20 retries with exponential backoff (2s, 4s, 8s, ... capped at 5min). Total wall time: roughly 20 * 5min = ~100min in the worst case. After 20 failures, the mission is dead with no specified recovery path.

The spec does not address: what error the mission surfaces after max_attempts exhausted, whether the user is notified, or whether a manual `swarm resume` (which §10 explicitly says does not exist) could restart it.

**Root Cause:** §7.2 defines the retry budget but §8.4 resume semantics only covers the success path (retry works). The terminal-failure path after retry exhaustion is unspecified for transient errors.

**Remediation:** Add a paragraph to §8.4: "If `run_claude_cli` exhausts `max_attempts`, the mission transitions to `failed_terminal` with `reason: retry_budget_exhausted`. User can re-launch the mission with the same session_id to attempt recovery." Alternatively, allow the workflow to re-schedule the activity after a longer cooldown rather than terminating.

---

### Defect 6: §6.2 "uses --resume on every retry" contradicts §4 / §12 "first launch uses --session-id"
**Severity:** minor

**Scenario:** Three statements about first-launch behavior:
- §4 line 45: `claude --session-id <sid> on first launch and claude --resume <sid> on every retry`
- §12 line 498: `claude --session-id <sid> on first launch; claude --resume <sid> on every retry`
- §6.2 line 101: `uses --resume <sid> on every retry`

These are consistent on the surface, but §6.2 line 101 omits the first-launch case entirely — it only mentions retry. The pseudo-code in §7.3 has `spawn_claude(session_id, mission_prose, resume)` which presumably branches, but the branching logic is never shown. Combined with Defect 1, an implementor reading §6.2 alone could conclude that `--resume` is always used (including first launch), which would fail because there is no session to resume.

**Root Cause:** §6.2 is a summary view that omits the first-launch path. Not a direct contradiction, but an omission that compounds Defect 1.

**Remediation:** Amend §6.2 line 101 to: `uses --session-id <sid> on first launch; --resume <sid> on retries`.

## Mini-synthesis

The resume/retry story is the spec's central durability claim and it has a load-bearing gap at its core: six sections assert that retries use `--resume` but none specifies the mechanism for switching from `--session-id` to `--resume` across Temporal retry attempts, which by design replay with identical arguments. This is not a cosmetic omission — an implementor will hit this on day one and must invent the solution.

The second systemic issue is that the heartbeat pseudo-code is event-driven but the spec claims time-driven heartbeats. In practice, this will cause false-positive kills during long claude thinking periods, which then trigger the resume path, which has the argument-mutation problem from Defect 1. The defects compound.

The session-state locality assumption is the third leg: `~/.claude/` must be shared and durable across all workers, but this is never stated as a constraint, and the spec advertises multi-worker support without addressing it.

Defects 1, 2, and 3 are independently blocking; together they make the entire retry/resume mechanism unimplementable as specified.

## New Angles Discovered

- **Race condition on false-positive heartbeat timeout**: If Temporal kills the activity due to heartbeat gap but the claude process is still running, and then retries on the same machine, you get two claude processes with the same session-id writing to the same workspace. The spec has no mutex or fencing mechanism for this. Worth a dedicated consistency check.
- **`claude --resume` failure modes**: The spec never catalogs what `claude --resume <sid>` returns when the session is expired, corrupted, or from a different claude version. These should be classified in §7.1's error taxonomy.
- **Heartbeat payload vs. query read path**: §7.3 says "the parent workflow can read the most recent heartbeat" but Temporal heartbeat details are only available via `activity.info().heartbeat_details` inside the activity, not directly queryable from the workflow. The workflow would need to store heartbeat data received via a different mechanism (e.g., a signal). This is a separate consistency issue worth checking.
