# CLAUDE.md — Swarm project

This file is loaded automatically by Claude Code when the working directory
is under `/Users/npow/code/research/swarm/`. Read it first in any session
that touches this project.

## What this project is

The swarm is a coordination harness for Claude Code. It keeps a main agent
working on a clearly specified mission indefinitely — without the agent
stopping to ask the user for input, without drifting off task, without
gaming the success criteria — until the mission is verifiably complete or
the user manually terminates the session.

## Key design documents (READ THESE)

1. `docs/superpowers/specs/2026-04-16-swarm-coordination-design.md` —
   full design spec (15 sections). Source of truth for architecture.
2. `docs/superpowers/plans/2026-04-16-swarm-v0-plan.md` — v0 plan.
3. `swarm/CHANGES_FROM_REVIEW.md` — deltas from review cycles.
4. `swarm/metrics.json` — per-cycle progress metrics.

## Core principles (copy-paste from spec §3)

1. **Never stop on the agent's decision.** Only two terminal states:
   `mission_complete` (verified) or `user_killed`.
2. **No task-level budgets.** No time cap, iteration cap, cost cap.
3. **Concurrency limits exist** (process, depth, fan-out) to prevent
   resource exhaustion — but these are not budgets on the task.
4. **Success criteria are executable.** Each is a shell command whose
   exit code decides pass/fail.
5. **Coordinator orchestrates, critics evaluate.** All judgment is
   delegated to independent critic/judge agents.
6. **Structured output is the contract.** Unparseable output defaults
   to fail-safe most-severe.
7. **Worker content is untrusted data.** Injection-resistant framing
   is universal.
8. **Defense in depth.** Sensitive resources protected by multiple layers.
9. **User-gated escalation.** Tamper detection halts pending user review.
10. **The swarm never modifies its running code.** Self-improvement
    produces a new version in a staging directory; user promotes.

## Components (current as of v2)

```
swarm/
├── schemas/              # Pydantic models for Event, Finding, Intervention, Mission, Lock
├── lib/                  # paths, hashing, locking, transcript parser, ids, heartbeat, interventions
├── specialists/
│   ├── event_scribe.py          # append events.jsonl with fcntl locks, redact secrets, spill large detail
│   ├── pattern_detector.py      # deterministic: loops, oscillation, thrash, scope-shrinking
│   ├── success_verifier.py      # runs mission checks in clean subprocess, enforces invariants
│   ├── coordinator.py           # orchestrator; delegates all judgment
│   ├── completion_judge.py      # sole arbiter of mission_complete
│   ├── goal_drift_critic.py     # LLM: intent vs action
│   ├── progress_auditor.py      # LLM: claim grounding
│   ├── anticheat_critic_panel.py # LLM × 6: cheat taxonomy, triggered on pass-transition
│   ├── intervention_judge.py    # policy: tier + escape ladder rung
│   ├── recovery_spawn.py        # spawns fresh claude subprocess with briefing
│   ├── llm_loop.py              # daemon that runs the LLM critics on cadence
│   ├── supervisor.py            # heartbeat watchdog + rotation-exhaustion escalation
│   └── spawner.py               # admission control for heavy subagents
├── hooks/                # SessionStart, PostToolUse, Stop
├── launch.sh             # launcher: sets up session, pins hashes, starts daemons
├── settings.json.template # Claude Code permissions + hook registration
└── tests/                # pytest; all tests must pass
```

## How to make a change

Follow the **bootstrap loop**. Do not just implement things and declare
victory.

1. Write a new mission YAML at `swarm/examples/mission-vN-upgrade.yaml`
   with **executable success criteria** that name exactly what "done"
   means. Criteria are shell commands; exit code decides pass/fail.
2. Run the verifier against the baseline:

   ```python
   PYTHONPATH=. python3 -c "
   import yaml, sys
   sys.path.insert(0, '/Users/npow/code/research')
   from swarm.schemas.mission import Mission
   from swarm.specialists.success_verifier import run_all_checks
   m = Mission.model_validate(yaml.safe_load(open('swarm/examples/mission-vN-upgrade.yaml')))
   results = run_all_checks('baseline', m)
   for cid, r in results.items():
       marker = 'PASS' if r.status == 'pass' else 'FAIL'
       print(f'  [{marker}] {cid}')
   "
   ```

3. Do the work. After each substantial change, re-run the verifier.
   Criteria should flip from FAIL to PASS as you make progress.
4. When all criteria pass AND all tests pass AND ruff is clean:
   - Run `PYTHONPATH=. python3 -m pytest swarm/tests/ -q` — must be green
   - Run `ruff check swarm/ --select E,F,W,I --ignore E501` — must be clean
5. Append a new entry to `swarm/metrics.json` with the cycle's metrics.
6. Update this CLAUDE.md if the component inventory changed.

## What NOT to do

**Never declare scope reductions.** If a criterion cannot be met, that
is either a bug to fix OR a judgment call the human must make — NOT
something the agent decides unilaterally. Phrases like "out of scope",
"deferred to later", "will not implement", "let me know what's next"
are scope-shrinking signals the swarm is explicitly built to catch.
Don't produce them.

**Never claim "tests pass" without running them.** The progress_auditor
is specifically built to flag unsupported claims. Always run pytest and
show the output before claiming success.

**Never edit `.claude/settings.json` in the worker's workspace manually.**
The launcher writes/restores it. Manual edits during a session can lock
you out.

**Never modify currently-running swarm code.** If you want to change how
the swarm works, produce a v(N+1) in staging, not a hot-patch.

## Commands

```bash
# Run tests (use py310 conda env; verifier path_add includes it)
PYTHONPATH=. python3 -m pytest swarm/tests/ -q

# Lint
ruff check swarm/ --select E,F,W,I --ignore E501

# Autofix lint
ruff check swarm/ --select E,F,W,I --ignore E501 --fix

# Run the launcher on a mission
bash swarm/launch.sh swarm/examples/mission-v2-upgrade.yaml

# Verify a mission's criteria without launching claude
PYTHONPATH=. python3 -c "import yaml,sys;sys.path.insert(0,'.');from swarm.schemas.mission import Mission;from swarm.specialists.success_verifier import run_all_checks;m=Mission.model_validate(yaml.safe_load(open('swarm/examples/mission-v2-upgrade.yaml')));r=run_all_checks('x',m);[print(f\"{'PASS' if x.status=='pass' else 'FAIL'}: {c}\") for c,x in r.items()]"
```

## Known limitations (honest)

- **No UID separation yet** — everything runs as `$USER`. Deny list is
  only defense-in-depth layer 1. v3 target.
- **No immutable bits yet** — same reason. v3 target.
- **Multi-provider anticheat** — framework supports it but default is
  Claude-only. Mission authors can set `anticheat.second_opinion` to a
  different CLI. Enforced-diversity default in v3.
- **Deny patterns are over-broad** — `Bash(*~/.swarm/*)` and similar
  catch-alls also block legitimate debugging commands from inside the
  working dir. Trade-off: safety vs ergonomics. Being tightened.

## If you need to stop

Only the user can stop the swarm (Ctrl+C on the launcher) unless a
`mission_complete` intervention has fired. The Stop hook will block
forever otherwise — that is by design.

If you are running in a shell that has the swarm's settings.json
installed AND you are NOT a mission worker: the on_stop.py hook has a
pass-through for sessions with no mission.yaml. So normal shells work
fine. But if you see a Stop hook block you unexpectedly, check
`~/.swarm/missions/<your_session_id>/mission.yaml` — if it exists, you
are being treated as a worker; if it does not, something else is wrong.
