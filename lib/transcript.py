"""Parser for Claude Code's per-session transcript.

Claude Code writes a JSONL file at ~/.claude/projects/<encoded-cwd>/<session>.jsonl.
Each line is one user/assistant/tool message in a known (but versioned) schema.

This module gives a tolerant read API — we only use fields we care about and
skip anything we don't recognize.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Turn:
    """One logical turn extracted from the transcript."""

    role: str  # "user" | "assistant" | "tool_result"
    text: str  # best-effort flattened text content
    thinking: str  # concatenated thinking blocks, "" if none
    tool_uses: list[dict[str, Any]]  # tool_use blocks from the turn
    raw: dict[str, Any]  # the full JSON line


def read_turns(transcript_path: Path) -> list[Turn]:
    if not transcript_path.exists():
        return []
    turns: list[Turn] = []
    with transcript_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            turn = _parse_turn(obj)
            if turn is not None:
                turns.append(turn)
    return turns


def _parse_turn(obj: dict[str, Any]) -> Turn | None:
    # Claude Code's schema has evolved; tolerant extraction:
    msg = obj.get("message") or obj
    role = msg.get("role") or obj.get("type") or "unknown"

    content = msg.get("content")
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_uses: list[dict[str, Any]] = []

    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "thinking":
                thinking_parts.append(block.get("thinking") or block.get("text", ""))
            elif btype == "tool_use":
                tool_uses.append(block)
            elif btype == "tool_result":
                # surface tool result content as text for grounding
                tr = block.get("content")
                if isinstance(tr, str):
                    text_parts.append(tr)
                elif isinstance(tr, list):
                    for inner in tr:
                        if isinstance(inner, dict) and inner.get("type") == "text":
                            text_parts.append(inner.get("text", ""))

    return Turn(
        role=role,
        text="\n".join(p for p in text_parts if p),
        thinking="\n".join(p for p in thinking_parts if p),
        tool_uses=tool_uses,
        raw=obj,
    )


def last_n_turns(transcript_path: Path, n: int) -> list[Turn]:
    turns = read_turns(transcript_path)
    return turns[-n:]


def flatten_tool_calls(turns: list[Turn]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in turns:
        out.extend(t.tool_uses)
    return out
