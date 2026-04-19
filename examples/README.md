# Example missions

Each file is a complete `mission.yaml` you can adapt. Drop your repo path into `workspace:` and `swarm launch`.

| File | Scenario |
|---|---|
| [`fix-bug.yaml`](fix-bug.yaml) | A failing test exists. Make it pass without editing the test. |
| [`add-feature.yaml`](add-feature.yaml) | Implement a new module with tests, typing, and no mocks of the subject. |
| [`refactor-module.yaml`](refactor-module.yaml) | Refactor a module while keeping existing tests green and lines-of-code floor. |

## Why the anti-cheat criteria?

Every mission includes a negative check alongside the happy-path check. Without those, the agent can satisfy the mission trivially:

- `pytest passes` alone → agent can delete the failing test
- `tests exist` alone → agent can write a single `def test_noop(): pass`
- `coverage ≥ 90%` alone → agent can mock out the subject
- `function returns correct value` alone → agent can hardcode the cases

The anti-cheat criteria (grep-for-mocks, test-count floors, assertion-count floors) close those loopholes.
