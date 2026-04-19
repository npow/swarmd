"""Stages 1 and 2 of the swarm prompt classifier.

Stage 1 — explicit prefix detection (`/swarm`, `/chat`, `/meta`, ...).
    Deterministic, case-insensitive, microsecond cost, confidence = 1.0.

Stage 2 — rule-based heuristic (keyword + regex signals).
    Pure Python regex matching. May return UNCERTAIN to defer to stage 3 (LLM).
    Confidence capped at 0.95 — 1.0 is reserved for stage 1.

Design constraints:
- Zero dependencies on the rest of the swarm codebase (not even ``swarm.schemas``).
- Pure functions. 100% deterministic. <10 ms per call on 5 KB prompts.
- No I/O, no network, no logging. Safe to call from workflow code (though
  classifier is typically invoked from the hook layer, not the workflow).

See spec §9.1 for the classifier cascade.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Pattern, Tuple


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ClassifierVerdict(str, Enum):
    """Three canonical verdicts plus UNCERTAIN (stage 2 defers to stage 3)."""

    MISSION = "mission"
    CHAT = "chat"
    META = "meta"
    UNCERTAIN = "uncertain"


@dataclass
class ClassifierResult:
    """Structured result from any classifier stage."""

    verdict: ClassifierVerdict
    stage: int  # 1 = explicit prefix, 2 = rule gate, 3 = LLM (not this module)
    confidence: float  # 1.0 for stage 1; [0.0, 0.95] for stage 2
    reason: str  # short human-readable rationale


# ---------------------------------------------------------------------------
# Stage 1 — explicit prefix
# ---------------------------------------------------------------------------

# Map of canonical prefix token (lowercased) → verdict.
_PREFIX_VERDICTS: dict[str, ClassifierVerdict] = {
    "/swarm": ClassifierVerdict.MISSION,
    "/swarm!": ClassifierVerdict.MISSION,
    "/swarm?": ClassifierVerdict.META,
    "/mission": ClassifierVerdict.MISSION,
    "/chat": ClassifierVerdict.CHAT,
    "/meta": ClassifierVerdict.META,
}


def classify_prefix(prompt: str) -> Optional[ClassifierResult]:
    """Stage 1: classify if the prompt starts with an explicit prefix.

    Strips leading whitespace. If the first whitespace-delimited token is one
    of the recognized prefixes, returns a deterministic ClassifierResult.
    Otherwise returns None so the caller can fall through to stage 2.

    Case-insensitive. ``/swarm-related-thing`` does NOT match because the
    prefix must be followed by whitespace or end-of-string (not a hyphen or
    any other non-whitespace character).

    >>> classify_prefix("/swarm fix the bug").verdict
    <ClassifierVerdict.MISSION: 'mission'>
    >>> classify_prefix("hello world") is None
    True
    >>> classify_prefix("/swarm-like-thing") is None
    True
    """
    if not prompt:
        return None

    stripped = prompt.lstrip()
    if not stripped:
        return None

    # Split on the FIRST whitespace run to get the leading token. We avoid
    # ``stripped.split()`` because we only need the first piece and splitting
    # the whole string is wasteful for long prompts.
    match = re.match(r"(\S+)", stripped)
    if not match:
        return None

    token = match.group(1).lower()
    verdict = _PREFIX_VERDICTS.get(token)
    if verdict is None:
        return None

    return ClassifierResult(
        verdict=verdict,
        stage=1,
        confidence=1.0,
        reason=f"explicit prefix {match.group(1)!r}",
    )


# ---------------------------------------------------------------------------
# Stage 2 — rule gate
# ---------------------------------------------------------------------------

# Strength is a rough confidence contribution per match. We aggregate strengths
# per verdict and pick the argmax, then normalize to a [0, 0.95] confidence.
#
# Rule format: (name, compiled_regex, verdict, strength)
#
# Patterns are compiled once at import.

_MISSION_IMPERATIVES = (
    # Imperative verbs at start of prompt OR after polite prefixes like
    # "please ", "can you ", "could you ". The optional leading polite phrase
    # is a non-capturing group.
    r"(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|pls\s+)?"
    r"(?:fix|build|implement|add|create|refactor|make|write|port|migrate|"
    r"upgrade|update|rename|replace|remove|delete|revert|test|setup|"
    r"configure|deploy|release|ship)\b"
)

_CHAT_INTERROGATIVES = (
    # "what is", "how does", etc. — open-ended Q about concepts.
    r"(?:what|why|how|when|where|who)\s+"
    r"(?:is|are|was|were|does|do|did|would|should|could)\b"
)

_CHAT_SMALL_TALK = (
    r"(?:hi|hello|hey|thanks?|thank\s+you|good\s+(?:morning|evening|afternoon)|"
    r"ok|okay|cool|nice|great|awesome)\b"
)

_CHAT_EXPLAIN = (
    r"(?:explain|describe|tell\s+me|show\s+me|walk\s+me\s+through|"
    r"help\s+me\s+understand)\b"
)

# Meta signals: "swarm" used reflectively (about swarm itself).
_META_SWARM_REFLECTIVE = (
    r"(?:what\s+(?:is|does|are)\s+swarm|"
    r"how\s+does\s+swarm|"
    r"swarm\s+(?:doc|docs|config|configs|setting|settings|hook|hooks|"
    r"classifier|classifiers|workflow|workflows))"
)

_META_SLASH_CONFIG = r"/swarm-(?:config|settings?|status)\b"

# File-path pattern: looks for a word that ends in a source-file extension.
# We require the extension to be part of a path-like token (contains a
# letter/digit before the dot) to avoid matching bare ".py" etc. in prose.
_MISSION_FILE_PATH = r"\b[\w./\-]+\.(?:py|js|ts|rs|go|tsx|jsx|md|yml|yaml|toml|sh|rb|java|kt|swift|c|cpp|h|hpp)\b"

# Ticket / issue references.
_MISSION_TICKET = r"(?:#\d+|\bhttps?://(?:github|gitlab|bitbucket|jira)\b)"

# "the bug", "the issue", "the test", "the flaky test", etc.
_MISSION_THE_THING = (
    r"\bthe\s+(?:bug|issue|test|tests|flaky\s+test|failure|regression|"
    r"crash|error|problem)\b"
)


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: Pattern[str]
    verdict: ClassifierVerdict
    strength: float


# Order is immaterial — we iterate all rules and aggregate.
#
# Strength calibration: strong, unambiguous signals score 0.8+; supporting
# signals score 0.4-0.6; weak signals score 0.2-0.3.
_RULES: List[_Rule] = [
    # --- META (highest precedence via tie-break logic below) ---
    _Rule(
        "meta_swarm_reflective",
        re.compile(_META_SWARM_REFLECTIVE, re.IGNORECASE),
        ClassifierVerdict.META,
        0.9,
    ),
    _Rule(
        "meta_slash_config",
        re.compile(_META_SLASH_CONFIG, re.IGNORECASE),
        ClassifierVerdict.META,
        0.9,
    ),
    # --- MISSION ---
    _Rule(
        "mission_imperative_start",
        re.compile(r"^\s*" + _MISSION_IMPERATIVES, re.IGNORECASE),
        ClassifierVerdict.MISSION,
        0.8,
    ),
    _Rule(
        "mission_file_path",
        re.compile(_MISSION_FILE_PATH, re.IGNORECASE),
        ClassifierVerdict.MISSION,
        0.4,
    ),
    _Rule(
        "mission_ticket_ref",
        re.compile(_MISSION_TICKET, re.IGNORECASE),
        ClassifierVerdict.MISSION,
        0.5,
    ),
    _Rule(
        "mission_the_thing",
        re.compile(_MISSION_THE_THING, re.IGNORECASE),
        ClassifierVerdict.MISSION,
        0.4,
    ),
    # --- CHAT ---
    _Rule(
        "chat_interrogative",
        re.compile(r"^\s*" + _CHAT_INTERROGATIVES, re.IGNORECASE),
        ClassifierVerdict.CHAT,
        0.75,
    ),
    _Rule(
        "chat_small_talk",
        re.compile(r"^\s*" + _CHAT_SMALL_TALK, re.IGNORECASE),
        ClassifierVerdict.CHAT,
        0.7,
    ),
    _Rule(
        "chat_explain",
        re.compile(r"^\s*" + _CHAT_EXPLAIN, re.IGNORECASE),
        ClassifierVerdict.CHAT,
        0.75,
    ),
]


def _aggregate_signals(
    prompt: str,
) -> Tuple[dict[ClassifierVerdict, float], List[str]]:
    """Run all rules, return (strength_by_verdict, names_matched)."""
    strengths: dict[ClassifierVerdict, float] = {
        ClassifierVerdict.MISSION: 0.0,
        ClassifierVerdict.CHAT: 0.0,
        ClassifierVerdict.META: 0.0,
    }
    matched_names: List[str] = []

    for rule in _RULES:
        if rule.pattern.search(prompt):
            strengths[rule.verdict] += rule.strength
            matched_names.append(rule.name)

    return strengths, matched_names


def classify_rules(prompt: str) -> ClassifierResult:
    """Stage 2: rule-based heuristic classifier.

    Always returns a result; verdict may be UNCERTAIN to defer to stage 3.

    Tie-breaking rules (per spec):
      - MISSION and META both present → META (about swarm, so not actionable).
      - MISSION and CHAT both present → UNCERTAIN (stage 3 decides).
      - No signals at all → UNCERTAIN, confidence 0.0.
    """
    # Fast path: empty / whitespace-only prompts have no signals.
    if not prompt or not prompt.strip():
        return ClassifierResult(
            verdict=ClassifierVerdict.UNCERTAIN,
            stage=2,
            confidence=0.0,
            reason="empty prompt",
        )

    strengths, matched = _aggregate_signals(prompt)

    mission = strengths[ClassifierVerdict.MISSION]
    chat = strengths[ClassifierVerdict.CHAT]
    meta = strengths[ClassifierVerdict.META]

    # --- Tie-breaking ---

    # No signals at all → UNCERTAIN with zero confidence.
    if mission == 0 and chat == 0 and meta == 0:
        return ClassifierResult(
            verdict=ClassifierVerdict.UNCERTAIN,
            stage=2,
            confidence=0.0,
            reason="no signals matched",
        )

    # META dominates MISSION (the prompt is about swarm itself).
    if meta > 0:
        return ClassifierResult(
            verdict=ClassifierVerdict.META,
            stage=2,
            confidence=min(meta, 0.95),
            reason=f"meta signals: {matched}",
        )

    # Both MISSION and CHAT present → defer to stage 3.
    if mission > 0 and chat > 0:
        return ClassifierResult(
            verdict=ClassifierVerdict.UNCERTAIN,
            stage=2,
            confidence=0.4,  # under the 0.6 threshold by construction
            reason=f"mixed signals: mission={mission:.2f}, chat={chat:.2f}",
        )

    # Clear MISSION.
    if mission > 0:
        return ClassifierResult(
            verdict=ClassifierVerdict.MISSION,
            stage=2,
            confidence=min(mission, 0.95),
            reason=f"mission signals: {matched}",
        )

    # Clear CHAT.
    if chat > 0:
        return ClassifierResult(
            verdict=ClassifierVerdict.CHAT,
            stage=2,
            confidence=min(chat, 0.95),
            reason=f"chat signals: {matched}",
        )

    # Unreachable (all-zero branch taken above), but keep for safety.
    return ClassifierResult(  # pragma: no cover
        verdict=ClassifierVerdict.UNCERTAIN,
        stage=2,
        confidence=0.0,
        reason="unreachable",
    )


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------


def classify(prompt: str) -> ClassifierResult:
    """Run stage 1 first; on no match, run stage 2."""
    result = classify_prefix(prompt)
    if result is not None:
        return result
    return classify_rules(prompt)


__all__ = [
    "ClassifierVerdict",
    "ClassifierResult",
    "classify",
    "classify_prefix",
    "classify_rules",
]
