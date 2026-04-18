"""Per-criterion schema module — referenced by ``check_criterion`` activity.

Per spec §6.3 (activities) and §10 file layout (``schemas/criterion.py``).

Defines ``Criterion``, the type consumed by the ``check_criterion`` activity
and related verifier code. Today ``Criterion`` is structurally identical to
``SuccessCriterion`` from ``schemas.mission`` (the type read out of
``mission.yaml``). Re-exporting here gives callers a stable single-purpose
import path and leaves room for the per-criterion schema to grow fields that
are irrelevant to the mission.yaml surface (e.g. cached last-check
timestamps) without polluting the mission schema.
"""

from __future__ import annotations

from schemas.mission import SuccessCriterion

# Public single-purpose name. Keep the alias tight: downstream code imports
# ``Criterion`` exclusively from this module, regardless of how the type is
# modeled internally.
Criterion = SuccessCriterion

__all__ = ["Criterion"]
