"""Import smoke test for Task 1 scaffolding.

Verifies that the new swarm sub-packages and all pinned third-party
dependencies import cleanly. If any of these fail, the project is not
ready for subsequent tasks.
"""


def test_swarm_durable_imports() -> None:
    import swarm.durable  # noqa: F401


def test_swarm_classifier_imports() -> None:
    import swarm.classifier  # noqa: F401


def test_swarm_mcp_imports() -> None:
    import swarm.mcp  # noqa: F401


def test_temporalio_imports() -> None:
    import temporalio  # noqa: F401


def test_anthropic_imports() -> None:
    import anthropic  # noqa: F401


def test_pydantic_imports() -> None:
    import pydantic  # noqa: F401


def test_click_imports() -> None:
    import click  # noqa: F401


def test_mcp_imports() -> None:
    import mcp  # noqa: F401


def test_yaml_imports() -> None:
    import yaml  # noqa: F401
