# swarmd

[![CI](https://github.com/npow/swarmd/actions/workflows/ci.yml/badge.svg)](https://github.com/npow/swarmd/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/swarmd)](https://pypi.org/project/swarmd/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Docs](https://img.shields.io/badge/docs-mintlify-18a34a?style=flat-square)](https://mintlify.com/npow/swarmd)

Keep a Claude agent working on a mission until it actually finishes — across crashes, API outages, and context resets.

## The problem

You launch a long-running coding task with Claude and come back an hour later to find the session died on an API 424, or the agent declared victory after writing one stub function, or it drifted into cleaning up unrelated code. Plain `claude` has no notion of what "done" means, no recovery from transient failures, and no guard against the agent gaming its own success check. A checkpoint worth hours of reasoning evaporates because one HTTP error wasn't retried.

## Quick start

```bash
# 1. Install
pip install swarmd

# 2. One-time setup (starts Temporal + worker + registers MCP)
swarm bootstrap

# 3. Launch a mission
cat > mission.yaml <<'EOF'
mission: "Add full test coverage to auth.py"
workspace: "/abs/path/to/your/project"
success_criteria:
  - id: tests_pass
    check: "pytest auth/ -q"
    timeout_sec: 120
  - id: coverage_floor
    check: "coverage report --include=auth.py --fail-under=90"
    timeout_sec: 30
verification:
  run_every_sec: 30
  hold_window_sec: 60
EOF

swarm launch mission.yaml
# → workflow_id=mission-abc123

swarm status mission-abc123
```

## Install

```bash
pip install swarmd
```

Requires Python 3.10+, a Temporal server on `localhost:7233`, and the `claude` CLI on PATH. `swarm bootstrap` installs Temporal via Homebrew and registers the worker as a launchd service.

From source:

```bash
git clone https://github.com/npow/swarmd
cd swarmd
pip install -e ".[dev]"
```

## Usage

### Launch, watch, abort

```bash
swarm launch mission.yaml          # submit; prints workflow_id
swarm status <workflow_id>         # JSON status: phase, criteria_state, findings_count
swarm findings <workflow_id> --tail 50
swarm abort <workflow_id> --reason "criteria were wrong"
```

### Run the worker daemon manually

```bash
swarm worker &
# or configure it as a launchd service via `swarm bootstrap`
```

### Health check before launching

```bash
swarm health
# → Temporal: PASS
# → Worker:   PASS (1 poller)
# → Anthropic: PASS
```

## How it works

Every mission is a Temporal workflow. The workflow runs a verifier loop on a fixed cadence — check tampering, enforce invariants, run all criterion shell commands in parallel, update state. When every criterion passes, the workflow enters a hold window; if they stay green for `hold_window_sec`, a completion judge runs six preconditions before allowing the transition to `complete`. Transient errors (HTTP 424/429/5xx) become retries; terminal errors (400/401/auth) halt the mission with a clear reason.

Three child workflows run alongside the parent:
- **Pattern detector** tails `events.jsonl`, flags loops, oscillation, and scope-shrinking.
- **LLM critic** runs cadence-driven progress audits + goal-drift checks, and fans out a six-dimension anti-cheat panel on every criterion pass-transition.
- **Resource monitor** watches for zombies, memory pressure, disk exhaustion.

Because state lives in Temporal, `kill -9`-ing the worker doesn't kill the mission — the next worker to poll the task queue picks up exactly where the last one left off.

## Configuration

Mission YAML fields: `mission`, `workspace`, `success_criteria`, `verification`, `invariants`, `concurrency`, `observer_config`, `anticheat`, `max_duration_sec`.

See [`examples/`](examples/) for a reference mission. See [`docs/superpowers/specs/2026-04-18-swarm-durability-design.md`](docs/superpowers/specs/2026-04-18-swarm-durability-design.md) for the full design.

## Development

```bash
git clone https://github.com/npow/swarmd
cd swarmd
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v
```

Integration tests against a real Temporal server:

```bash
pytest tests/test_integration/ --run-integration
```

## License

MIT — see [LICENSE](LICENSE).
