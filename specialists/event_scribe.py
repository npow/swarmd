"""Event scribe — write events to events.jsonl under exclusive lock.

In v0 this is NOT a daemon with a named pipe. It's a module used from hook
scripts that appends directly. Reasonable up to modest event rates.
"""

from __future__ import annotations

import json
import re
import time

from swarm.lib.ids import mint_event_id
from swarm.lib.locking import write_line
from swarm.lib.paths import events_path, session_dir
from swarm.schemas.event import Event

# Cap on per-event detail spill (bytes). Prevents disk exhaustion via
# very large tool responses (security review M4).
MAX_DETAIL_BYTES = 1_000_000

# Lightweight secret redaction. Pattern catches common API key shapes;
# this is a best-effort defense, not a guarantee. (Security review M2.)
_REDACT_PATTERNS = [
    # AWS-style access keys
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-AWS-KEY]"),
    # Long bearer tokens
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE), "Bearer [REDACTED]"),
    # Generic high-entropy `<key>=<value>` for sensitive-looking keys
    (
        re.compile(
            r"(?i)\b((?:secret|password|api[_\-]?key|token|access[_\-]?token|"
            r"private[_\-]?key|client[_\-]?secret)\s*[:=]\s*)"
            r"[\"']?([A-Za-z0-9._\-]{8,})[\"']?"
        ),
        r"\1[REDACTED]",
    ),
    # GitHub-style tokens
    (re.compile(r"\bghp_[A-Za-z0-9]{36,}\b"), "[REDACTED-GH-TOKEN]"),
    # Anthropic's keys are also sk- prefixed; their pattern matches first
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED-ANTHROPIC-KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), "[REDACTED-OPENAI-KEY]"),
]


def redact(s: str) -> str:
    if not s:
        return s
    for pat, repl in _REDACT_PATTERNS:
        s = pat.sub(repl, s)
    return s


def emit_event(
    *,
    session_id: str,
    hook: str,
    spawner_id: str | None = None,
    parent_id: str | None = None,
    depth: int = 0,
    tool_name: str | None = None,
    tool_input_summary: str | None = None,
    tool_response_summary: str | None = None,
    tool_response_full: str | None = None,
) -> Event:
    """Write a single event row. Spills large tool_response into events_detail/."""
    ev = Event(
        id=mint_event_id(),
        session_id=session_id,
        spawner_id=spawner_id or session_id,
        parent_id=parent_id,
        depth=depth,
        ts_monotonic=time.monotonic(),
        ts_wall=_now_iso(),
        hook=hook,  # type: ignore[arg-type]
        tool_name=tool_name,
        tool_input_summary=_trim(tool_input_summary, 2000),
        tool_response_summary=_trim(tool_response_summary, 2000),
    )
    if tool_response_full and len(tool_response_full) > 2000:
        detail_dir = session_dir(session_id) / "events_detail"
        detail_dir.mkdir(parents=True, exist_ok=True)
        detail_path = detail_dir / f"{ev.id}.json"
        capped = tool_response_full[:MAX_DETAIL_BYTES]
        truncated = len(tool_response_full) > MAX_DETAIL_BYTES
        # Redact secrets in the full body before writing to disk
        capped = redact(capped)
        detail_path.write_text(
            json.dumps(
                {
                    "tool_response": capped,
                    "truncated": truncated,
                    "original_size": len(tool_response_full),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        ev = ev.model_copy(update={"detail_ref": str(detail_path)})

    write_line(events_path(session_id), ev.model_dump_json())
    return ev


def _trim(s: str | None, limit: int) -> str | None:
    if s is None:
        return None
    # Redact before length check; redacted content is what gets stored
    s = redact(s)
    if len(s) <= limit:
        return s
    return s[:limit] + f"...[truncated {len(s) - limit} chars]"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_events(session_id: str) -> list[Event]:
    """Read all events for a session."""
    path = events_path(session_id)
    if not path.exists():
        return []
    out: list[Event] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Event.model_validate_json(line))
            except Exception:
                continue
    return out
