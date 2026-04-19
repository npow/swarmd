# swarmd

[![CI](https://github.com/npow/swarmd/actions/workflows/ci.yml/badge.svg)](https://github.com/npow/swarmd/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/swarmd)](https://pypi.org/project/swarmd/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

**Durable Claude agent runner.** Hand it a mission and success criteria; it keeps working until the criteria pass, through crashes, API outages, and context resets.

- **Durable** — missions run as Temporal workflows; kill the worker and the next one resumes exactly where it left off
- **Verified completion** — a mission is only `complete` when your shell-command criteria all pass continuously for a hold window
- **Anti-cheat** — 6-dimension LLM critic panel flags scope-reduction, mocking-out, tautologies, hardcoding, off-criterion work, and coordinated edits
- **Pattern detection** — loop, oscillation, and drift detectors run alongside the mission
- **One line to launch** — `swarm launch mission.yaml`

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
swarm abort mission-abc123      # if you need to stop it
```

## Documentation

- [Design spec](docs/superpowers/specs/2026-04-18-swarm-durability-design.md)
- [Mission schema reference](swarmd/schemas/mission.py)
- [Examples](examples/)

## License

[MIT](LICENSE)
