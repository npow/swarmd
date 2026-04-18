#!/usr/bin/env python3
"""PostToolUse hook — emit event; deliver urgent interventions as context."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from swarm.lib.interventions import ack as ack_intervention  # noqa: E402
from swarm.lib.interventions import read_pending  # noqa: E402
from swarm.lib.paths import ensure_session_dirs  # noqa: E402
from swarm.specialists.event_scribe import emit_event  # noqa: E402


def _summarize_input(d: object, tool_name: str | None) -> str | None:
    """Structured summary of tool_input with explicit file= tag for Edit/Write.

    The pattern_detector regex expects `file=<path>` in the input summary to
    detect oscillation per file. Claude Code's Edit/Write tools pass the path
    in a `file_path` key — extract it explicitly so the regex can match.
    """
    if d is None:
        return None
    prefix = ""
    if tool_name in {"Edit", "Write"} and isinstance(d, dict):
        fp = d.get("file_path") or d.get("path")
        if fp:
            prefix = f"file={fp} "
    try:
        s = json.dumps(d, ensure_ascii=False)
    except Exception:
        s = str(d)
    summary = (prefix + s)[:2000]
    return summary


def _extract_file_content(d: object) -> str:
    """For Edit/Write tool_input, extract the post-edit file content if present."""
    if not isinstance(d, dict):
        return ""
    # Write tool: content is in d["content"]
    if "content" in d and isinstance(d["content"], str):
        return d["content"]
    # Edit tool: new_string is in d["new_string"]
    if "new_string" in d and isinstance(d["new_string"], str):
        return d["new_string"]
    return ""


def _summarize_response(
    d: object, tool_name: str | None, tool_input: object = None
) -> tuple[str | None, str | None]:
    """Return (summary, full). Summary PREPENDS content_hash= for Edit/Write so
    the hash survives the downstream 2000-char truncation in event_scribe.
    """
    if d is None:
        return None, None
    full = json.dumps(d, default=str, ensure_ascii=False) if not isinstance(d, str) else d
    prefix = ""
    if tool_name in {"Edit", "Write"}:
        # Hash the actual content the agent produced, not the tool response JSON.
        content = _extract_file_content(tool_input)
        basis = content if content else full
        h = hashlib.sha256(basis.encode("utf-8", errors="ignore")).hexdigest()[:16]
        prefix = f"content_hash={h} "
    summary = (prefix + full)[:2000].strip()
    return summary, full


# Emit an observer status brief every Nth tool call, so the worker has some
# awareness between Stop events. Counter persists in a tiny per-session file.
OBSERVER_BRIEF_EVERY = 10


def _bump_brief_counter(session_id: str) -> int:
    from swarm.lib.paths import session_dir

    p = session_dir(session_id) / "tool_call_count.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        n = int(p.read_text().strip()) if p.exists() else 0
    except (ValueError, OSError):
        n = 0
    n += 1
    p.write_text(str(n))
    return n


def _observer_brief(session_id: str) -> str | None:
    """Compact summary of recent finding types + strike count for the worker."""
    from swarm.lib.paths import findings_path, strikes_path
    from swarm.schemas.finding import Finding

    fp = findings_path(session_id)
    sp = strikes_path(session_id)
    if not fp.exists():
        return None
    type_counts: dict[str, int] = {}
    last_finding_summary: str | None = None
    with fp.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                fobj = Finding.model_validate_json(line)
            except Exception:
                continue
            key = f"{fobj.type}.{fobj.subtype}"
            type_counts[key] = type_counts.get(key, 0) + 1
            last_finding_summary = (
                f"{fobj.severity}: {key} — {fobj.verdict[:140]}"
                if fobj.verdict
                else f"{fobj.severity}: {key}"
            )
    if not type_counts:
        return None
    strike_total = 0
    if sp.exists():
        try:
            strike_total = sum(json.loads(sp.read_text()).values())
        except Exception:
            pass
    summary = ", ".join(f"{k}×{v}" for k, v in sorted(type_counts.items()))
    return (
        f"[SWARM OBSERVER STATUS] strikes={strike_total}; "
        f"recent findings: {summary}; "
        f"last: {last_finding_summary}"
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    session_id = payload.get("session_id") or os.environ.get("SESSION_ID", "")
    if not session_id:
        return 0

    ensure_session_dirs(session_id)
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    tool_response = payload.get("tool_response")

    summary, full = _summarize_response(tool_response, tool_name, tool_input)
    emit_event(
        session_id=session_id,
        hook="PostToolUse",
        tool_name=tool_name,
        tool_input_summary=_summarize_input(tool_input, tool_name),
        tool_response_summary=summary,
        tool_response_full=full if full and len(full) > 2000 else None,
    )

    context_parts: list[str] = []

    # Deliver urgent interventions as non-blocking additionalContext
    urgent = [
        iv
        for iv in read_pending(session_id)
        if iv.tier in {"urgent", "mission_level_alert"}
        and iv.consume_at in {"post_tool", "either"}
    ]
    if urgent:
        context_parts.extend(
            f"[SWARM INTERVENTION {iv.tier.upper()}] {iv.reason}" for iv in urgent
        )
        for iv in urgent:
            ack_intervention(session_id, iv.id, "post_tool")

    # Periodic observer status brief
    n_calls = _bump_brief_counter(session_id)
    if n_calls % OBSERVER_BRIEF_EVERY == 0:
        brief = _observer_brief(session_id)
        if brief:
            context_parts.append(brief)

    if context_parts:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": "\n\n".join(context_parts),
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
