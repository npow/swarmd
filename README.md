# swarmd

[![CI](https://github.com/npow/swarmd/actions/workflows/ci.yml/badge.svg)](https://github.com/npow/swarmd/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/swarmd)](https://pypi.org/project/swarmd/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

**Mission-enforced Claude agent runner.** You write what "done" looks like as shell commands; `swarmd` keeps a Claude agent working until those commands pass — across crashes, API outages, and context resets. It doesn't let the agent quit early, game its own check, or drift off task.

## How it differs from plain `claude`

| | Plain `claude -p "do X; pytest should pass"` | `swarmd` |
|---|---|---|
| API 424 mid-run | session dies, work lost | workflow resumes from where it crashed |
| Agent "done early" | stops when Claude says so | blocks completion until criteria pass for `hold_window_sec` straight |
| Agent edits tests to pass | you find out later | 6-dim anti-cheat panel fires on every pass-transition |
| Agent stuck in loop | no detection | pattern detector emits a loop finding |
| Agent drifts off task | no detection | cadence-driven goal-drift + progress audits |
| Worker machine reboots | everything lost | Temporal persists state; next worker picks up |

## How it works

You give `swarmd` two things: a **mission** (natural language goal) and **success criteria** (shell commands whose exit codes define done). Everything else — planning, code, commits, decisions — is the agent's job, same as running `claude` directly.

When you `swarm launch mission.yaml`, this happens:

1. A **Temporal workflow** starts on the swarm worker daemon. Its job: run enforcement, not write code.
2. The workflow spawns a **`claude` subprocess** in your workspace with the mission prose. Claude does the actual work — files, tools, subagents, whatever it picks.
3. In parallel, the workflow runs a **verifier loop** every `run_every_sec`: check tamper (are locked files unmodified?) → enforce invariants (no_mock, test_count_floor, etc.) → run every criterion's shell check in parallel → update state.
4. Three **child workflows** run alongside:
   - **PatternDetector** — tails `events.jsonl`, flags loops, oscillation, scope-shrinking
   - **LLMCritic** — cadence-driven Haiku calls for progress audit + goal-drift; fires the 6-dim anti-cheat panel (scope_reduction, mock_out, tautology, hardcode, off_criterion, coordinated_edit) on every criterion pass-transition
   - **ResourceMonitor** — zombie processes, memory pressure, disk
5. When every criterion passes, the workflow enters a **hold window**. If they stay green for `hold_window_sec`, a **completion judge** runs six preconditions (no open cheat/fabrication/tamper findings, no critic disagreements, per-criterion anti-cheat verdict `pass`) before allowing the transition to `complete`.
6. Transient errors (HTTP 424/429/5xx, timeouts) become **Temporal retries**; terminal errors (400/401, auth) halt the mission with a clear reason.

**What you specify:** mission prose, workspace path, success criteria, optional invariants.
**What you don't specify:** any plan, any steps, any agent behavior. Claude figures that out.

## Components

- **Temporal server** — external dependency (`brew install temporal`). Persistent state.
- **`swarm worker`** — long-running daemon that polls Temporal and executes workflows + activities. Run one or more; more workers = more missions in parallel. Restart at will — state survives.
- **Per mission at runtime:** 1 parent workflow + 3 child workflows + 1 `claude` subprocess + up to 6 parallel anti-cheat activities on each pass-transition.

## Installation

```bash
pip install swarmd
```

Requires Python 3.10+, Temporal (`brew install temporal`), and `claude` on PATH.

## Example

```yaml
# mission.yaml
mission: "Add full test coverage to auth.py"
workspace: "/abs/path/to/your/project"
success_criteria:
  - id: tests_pass
    check: "pytest auth/ -q"
    timeout_sec: 120
  - id: coverage_floor
    check: "coverage report --include=auth.py --fail-under=90"
    timeout_sec: 30
  - id: no_mocks        # anti-cheat floor
    check: "! grep -rE 'unittest.mock|MagicMock' auth/"
    timeout_sec: 10
verification:
  run_every_sec: 30
  hold_window_sec: 60
```

```bash
temporal server start-dev &
swarm worker &
swarm launch mission.yaml       # → workflow_id=mission-abc123
swarm status mission-abc123
swarm findings mission-abc123 --tail 50
swarm abort mission-abc123 --reason "criteria were wrong"
```

## Documentation

- [Design spec](docs/superpowers/specs/2026-04-18-swarm-durability-design.md) — full architecture
- [Mission schema](swarmd/schemas/mission.py) — every field
- [Examples](examples/) — reference missions

## License

[MIT](LICENSE)
