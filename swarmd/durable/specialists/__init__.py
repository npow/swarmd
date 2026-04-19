"""Child workflow classes for the three observer specialists.

Imported by ``swarm.durable.workflow`` to spawn child workflows on mission
launch; imported by tests to register them with the test Worker. The
public surface is the three ``@workflow.defn`` classes plus the
``_emit_to_parent`` helper (re-exported so tests can monkey-patch it if
they want to assert without a real parent workflow running).

Task 14 registered these as stub classes in a single file
(``specialists.py``). Tasks 15-17 split the file into a package — one
file per workflow — because the real implementations grew too big for
a single module. The public import path
``from swarmd.durable.specialists import PatternDetectorWorkflow``
(and the other two) is preserved unchanged.
"""

from __future__ import annotations

from swarmd.durable.specialists._utils import _emit_to_parent
from swarmd.durable.specialists.llm_critic import LLMCriticWorkflow
from swarmd.durable.specialists.pattern_detector import (
    PatternDetectorWorkflow,
    _run_pattern_rules,
)
from swarmd.durable.specialists.resource_monitor import (
    ResourceMonitorWorkflow,
)

__all__ = [
    "LLMCriticWorkflow",
    "PatternDetectorWorkflow",
    "ResourceMonitorWorkflow",
    "_emit_to_parent",
    "_run_pattern_rules",
]
