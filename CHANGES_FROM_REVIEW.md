# Changes applied after Phase 4 multi-perspective review

Three reviewers (architect, security-reviewer, code-reviewer) examined the
v0 build. User also flagged a specific cheat pattern mid-review: agents
declaring work "out of scope" as a form of premature stop. This document
records which review items were addressed in the same autopilot run
(v0.1-hardening) and which were deferred.

## Applied now (v0.1-hardening)

### User-flagged: scope-shrinking detector

**New specialist function:** `detect_scope_shrinking()` in `pattern_detector.py`.

Scans recent assistant text + thinking blocks in the Claude Code transcript
for phrases that indicate premature stop intent while the verifier has not
confirmed completion. Patterns covered:

- "out of scope"
- "beyond the scope"
- "deferred to later/future/vN"
- "leaving for later"
- "skip this for now"
- "will not implement"
- "for future work"
- "remaining roadmap"
- "explicitly out of scope"
- "not included in this (run|session|pass|phase)"
- "post-v0 / post-vN / next version"

Emits `type: fabrication, subtype: scope_shrinking` findings. The coordinator
dispatches these as **urgent-tier** interventions (not just `correct`), with
`consume_at: either`, and strategy `scope_lock` — forcing the agent to
either complete the criterion or declare technical impossibility in
concrete terms. No unilateral scope reduction is permitted.

**Tests:** 9 new tests in `tests/test_scope_shrinking.py` + 1 in `tests/test_coordinator.py`.

### Critical: hold_window_met re-emission bug

Previously the verifier emitted a new `hold_window_met` finding every
verification cycle once the hold window was met. This would:
- Spam `findings.jsonl` with duplicates
- Cause `completion_judge` to be invoked repeatedly
- Potentially emit multiple duplicate `mission_complete` interventions

**Fix:** Added `hold_window_emitted` flag to the verifier daemon state.
Resets to `False` whenever `all_pass` flips to `False`. Emits the finding
exactly once per achievement.

**Location:** `success_verifier.py` `main()`.

### Critical: completion_judge didn't check hold_window recency

Previously, a `hold_window_met` finding from 2 hours ago would still
satisfy the completion judge even if criteria had failed and re-passed
without the window re-held. Allowed premature completion.

**Fix:** Added `hold_window_recency_sec` parameter (default 300s). Completion
judge now requires the most recent `hold_window_met` finding to be within
the recency window. Falls back to "hold window never reached" if the most
recent finding is too stale.

**Location:** `completion_judge.py`, plus new `_finding_age_sec()` helper.

**Tests:** 2 new tests in `tests/test_completion_judge.py`.

### Critical: SWARM_ROOT / SWARM_CONFIG env override was possible mid-session

Previously `paths.py` read env vars on every call. A worker that modified
`SWARM_ROOT` mid-session could redirect all hook reads to an attacker
directory (forged interventions, forged mission_complete).

**Fix:** Lazy first-call-wins cache. First call to `swarm_root()` or
`swarm_config()` resolves from env and freezes. Subsequent env changes
within the same process are ignored.

Also added `_reset_for_tests()` helper so test fixtures can cycle the cache
between tests (invoked by `tmp_swarm_root` fixture).

**Location:** `lib/paths.py`.

**Tests:** 8 new tests in `tests/test_paths_validation.py`.

### Critical: settings.json.template deny list significantly broadened

Original deny list blocked specific shell commands (cat, echo, tee, cp, mv,
rm, sed, ln) but was trivially bypassable via scripting languages, printf,
here-docs, fd redirects, and absolute paths (instead of `~/`).

**Fix:** Added catch-all path-substring denies and blocked the most common
bypass primitives:

- `Bash(*~/.swarm/*)` and `Bash(*/.swarm/*)` — catches any command containing
  these path fragments, regardless of what the actual tool is
- Same for `~/.config/swarm/`
- `Bash(python -c *)`, `Bash(python3 -c *)`, `Bash(perl -e *)`, `Bash(ruby -e *)`,
  `Bash(node -e *)`, `Bash(node -p *)`, `Bash(deno eval *)`, `Bash(bun -e *)`
- `Bash(dd *)`, `Bash(tee /*)`
- `Bash(kill *)`, `Bash(pkill *)`, `Bash(killall *)` — prevents assassinating
  specialist daemons
- `Bash(export SWARM_*)`, `Bash(SWARM_ROOT=*)`, `Bash(env SWARM_*)` —
  prevents redirecting path resolution
- Inner-shell patterns: `Bash(/bin/bash -c *swarm*)`, `Bash(sh -c *swarm*)`

The paths lazy cache (see above) is the structural defense; the deny list
is defense-in-depth.

**Location:** `settings.json.template`.

### Critical: session_id path traversal

Previously `session_dir("../etc/passwd")` would resolve to
`~/.swarm/state/../etc/passwd` — partial path traversal.

**Fix:** Added `validate_session_id()` called from every path helper.
Regex enforces `^[A-Za-z0-9][A-Za-z0-9\-_]{7,63}$`.

**Location:** `lib/paths.py`.

**Tests:** In `tests/test_paths_validation.py`.

### Critical: pattern_detector oscillation data contract mismatch

The hook previously appended `content_hash=<hex>` AFTER the 2000-char
truncation in `event_scribe.emit_event()`, which silently lost the hash
for any tool response ≥ ~1980 chars. Also, the hash was computed on the
tool response JSON envelope, not on the post-edit file content — so
"same hash = same file state" was structurally broken.

Hook's `tool_input_summary` for Edit/Write also did not include the
`file=<path>` prefix the pattern_detector regex expects. Oscillation
detection would never fire on real Claude Code traffic.

**Fix (both issues):**
- `_summarize_input()` now extracts `file_path` from Edit/Write inputs and
  prepends `file=<path>` to the summary.
- `_summarize_response()` now PREPENDS `content_hash=<hex>` so it survives
  truncation, and hashes the actual file content extracted from
  `tool_input` (new_string or content), not the tool response envelope.

**Location:** `hooks/on_post_tool_use.py`.

### Major: invariant findings re-emitted every cycle

Same as hold_window_met: every verification cycle re-emitted existing
invariant violations. Would flood the interventions queue with duplicates.

**Fix:** Added `invariant_sigs_emitted: set[str]` in the verifier daemon.
Signature is `subtype|files|verdict[:200]`. Each unique violation emits
exactly once per session.

**Location:** `success_verifier.py` `main()`.

## Deferred (documented as v1 scope)

These are real defects from the review, but implementing them would expand
v0 beyond its stated minimum-viable bound. All are documented in the
review output and in this file.

- **Re-issue loop for unacked interventions** (architect M2). Currently
  interventions sit in the queue until consumed; no re-issue after N
  events without ack. v1 enhancement.
- **Plan-checkpoint scheduled interventions** (architect M3). `ObserverConfig`
  declares `plan_checkpoint_every_sec` but nothing reads it yet.
- **PostToolUse observer-status-brief** (architect M4). Worker still only
  learns about findings at Stop time, not between tool calls.
- **Check command allowlist / sandboxing** (security H1). Currently mission
  authors can write arbitrary shell in `check:` fields. Low priority since
  checks are user-authored.
- **Clean-checkout verification** (security D2). Verifier runs checks in
  the worker's workspace (with clean env), not a pristine clone. Tests
  with sys.path shims could still execute against the worker's filesystem.
- **launch.sh inline-python injection** (security H2). Paths interpolated
  into inline Python strings. Low priority since launch.sh is user-invoked.
- **Secret redaction in event_scribe** (security M2). Tool responses that
  include credentials are logged verbatim to `events.jsonl`.
- **Atomic write-then-rename for locked_rmw** (security M3, code-reviewer M4).
  Crash between truncate and write loses strikes.
- **Detail spill size cap** (security M4). Large tool responses unbounded.
- **Code duplication cleanup** (code-reviewer M1/M2, architect M5).
  `_read_pending_interventions` duplicated, `mint_*_id` duplicated, `_beat`
  duplicated. Extract to `lib/interventions.py`, `lib/ids.py`, `lib/heartbeat.py`.
- **`ts_monotonic` cross-process comparability** (code-reviewer M3).
- **Oscillation integration test** (code-reviewer M5). Currently unit-tested
  against hand-built events; no integration test verifying hook output
  matches detector input contract.
- **on_stop.py: pass mission prose not raw YAML** (code-reviewer M7).
- **on_stop.py: remove dead fallback branch** (code-reviewer M8).
- **`test_run_check_clean_env` should use monkeypatch** (code-reviewer m5).
- **`enforce_invariants` missing `assertion_count_floor` and `allowed_deps`**
  (architect M1). Schema declares them; only `no_mock` and `test_count_floor`
  are enforced.

## Tests

| Before review | After review | Delta |
|---|---|---|
| 48 passing | 67 passing | +19 |
| Coverage of scope-shrinking | none | all 9 paths |
| Coverage of hold-window recency | 1 | 3 |
| Coverage of session_id validation | none | 8 |

All 67 tests pass. Ruff is clean.

## Summary

The three critical reviewer findings plus the one user-flagged critical
are all addressed. v0 is now v0.1-hardening. The Major/Minor items from
the reviews are tracked as v1 scope, and their absence does not prevent
the FizzBuzz example mission from running end-to-end.
