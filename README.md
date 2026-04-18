# Swarm v0

A coordination harness that wraps Claude Code to keep an agent working on a
clearly specified mission until it is verifiably complete — without the agent
stopping to ask the user for input, without drifting off task, without gaming
the success criteria.

See `docs/superpowers/specs/2026-04-16-swarm-coordination-design.md` for the
full design and `docs/superpowers/plans/2026-04-16-swarm-v0-plan.md` for the
v0 scope.

## Status

**v0 — minimum viable end-to-end.** Deterministic specialists only
(`pattern_detector`, `success_verifier`). LLM critics (drift, progress,
anticheat panel) are the next version's scope.

## Quick start

Requirements: Python 3.11+, `pip install pydantic pyyaml pytest`, Claude Code
CLI available on PATH.

```bash
# 1. Run the unit tests
PYTHONPATH=. pytest swarm/tests/ -q

# 2. Launch the example mission (FizzBuzz)
bash swarm/launch.sh swarm/examples/mission.yaml
```

The launcher:
- Mints a session UUID
- Copies `mission.yaml` + checks into `~/.swarm/missions/<session>/`
- Hash-pins every file
- Starts `pattern_detector`, `success_verifier`, `coordinator` in the background
- Launches `claude --session-id <uuid>` with the mission prose

Terminate with `Ctrl+C`; the trap kills the specialists.

## Layout

```
swarm/
├── launch.sh                 # launcher
├── settings.json.template    # Claude Code settings with hooks + permission denies
├── schemas/                  # pydantic models for Event, Finding, Intervention, Mission
├── lib/                      # paths, hashing, fcntl locking, transcript parser
├── specialists/              # event_scribe, pattern_detector, success_verifier,
│                             # coordinator, completion_judge
├── hooks/                    # SessionStart, PostToolUse, Stop
├── examples/
│   ├── mission.yaml
│   ├── checks/
│   └── workspace/
└── tests/
```

## What v0 gives you

1. **Never stop on the agent's decision.** Stop hook always blocks unless a
   `mission_complete` intervention is pending.
2. **Executable mission criteria.** Each `check:` is a shell command run in a
   clean subprocess (isolated env + pristine working dir).
3. **Continuous pass hold window.** All criteria must pass for the full
   `hold_window_sec` to count.
4. **Loop + oscillation detection** via the deterministic pattern_detector.
5. **Structural invariants** enforced by the verifier: `no_mock` protected
   paths and `test_count_floor`.
6. **Tamper detection** via hash pinning of `mission.yaml` and any referenced
   check scripts.
7. **Escape ladder (3 rungs in v0).** On repeated loops, coordinator injects
   progressively different corrections; after all rungs tried, escalates to
   `recover` (v1 implements recovery subagent).
8. **Defense-in-depth permissions.** Settings template denies
   read/write/grep/glob of `~/.swarm/**`, `.claude/**`, and known-bypass
   Bash patterns.

## What v0 does NOT yet have (v1+ scope)

- LLM specialists (goal_drift_critic, progress_auditor, anticheat_critic_panel)
- severity_judge / intervention_judge
- spawner.py and heavy subagent admission control
- UID separation (everything runs as `$USER`)
- Immutable bits and append-only kernel flags
- supervisor crash-recovery watchdog
- Non-Anthropic second-opinion auditor
- Escape ladder rungs 4–10

## Self-bootstrap

v0's first mission (`swarm/examples/mission-v1-upgrade.yaml`, to be written)
will be: *"produce swarm v1 at `~/.swarm-staging/v1/` that fixes [v0
defects]."* When that mission is complete and the human approves, run
`swarm-cli promote v1` (future work) to atomically flip the active version.

## Running the tests

```bash
# From repo root
PYTHONPATH=. pytest swarm/tests/ -q

# Individual modules
PYTHONPATH=. pytest swarm/tests/test_pattern_detector.py -v
```

All tests use `tmp_swarm_root` fixture to isolate state into a tempdir —
nothing is written to your real `~/.swarm/`.
