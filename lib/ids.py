"""Monotonic-ish ID minting for events, findings, interventions.

Format: <prefix>-<unix_ms>-<6 hex chars>
The ms component allows recipient code to estimate finding age without
referencing wall-clock state elsewhere.
"""

from __future__ import annotations

import time
import uuid


def mint_event_id() -> str:
    return f"e-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


def mint_finding_id() -> str:
    return f"f-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


def mint_intervention_id() -> str:
    return f"i-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
