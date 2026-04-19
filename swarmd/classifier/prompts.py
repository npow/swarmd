"""Stage 3 classifier prompt templates.

One module-level constant, ``CLASSIFIER_PROMPT``, drives the Haiku fallback
classifier in ``swarm.classifier.llm``. The prompt:

1. Explains the four verdicts (MISSION / CHAT / META / UNCERTAIN).
2. Provides a handful of few-shot examples covering each verdict.
3. Pins the output format to strict single-line JSON — no prose fence, no
   chain-of-thought.
4. Explicitly instructs the model to return UNCERTAIN with confidence < 0.6
   rather than guessing when the prompt is genuinely ambiguous. This matters
   because the downstream confidence gate (§9.3 of the design spec, Task 22)
   treats <0.6 as "treat as CHAT" — which is the safe default.

The prompt uses ``str.format`` placeholders (``{user_prompt}`` and
``{context}``). Because the JSON schema example contains literal braces, we
double them (``{{...}}``) so ``format`` treats them as literal characters.

Keep this module dependency-free — it's imported by both the classifier hook
(synchronous, imported at every chat turn) and the llm module.
"""

from __future__ import annotations

# The prompt is intentionally compact. Haiku 4.5 is small + fast; verbose
# prompts dominate latency without improving accuracy on a 4-class problem.
# Size check enforced by tests/test_classifier/test_prompts.py: < 4000 chars
# after ``.format()``.
CLASSIFIER_PROMPT = """\
You are a strict classifier. Given a user's message and optional context,
classify the message into exactly one of four verdicts:

- mission: a closed-form task with verifiable artifact output. Imperative
  ("fix X", "build Y", "add tests for Z"), references files or tickets,
  produces a concrete end state that can be checked without human opinion.
- chat: open-ended, exploratory, or conversational. Questions about concepts
  ("what is X", "how does Y work"), explanations, brainstorming, small talk.
  No artifact is produced; completion is "human approved" not verifiable.
- meta: about swarm itself or an existing mission. "How's the mission?",
  "show findings", "abort the current run", "what does /swarm do?". These
  are read-only; the answer comes from swarm state, not from doing work.
- uncertain: the prompt is genuinely ambiguous, OR mixes mission-and-chat
  signals, OR you cannot decide without more context. Use this instead of
  guessing.

Strict output contract:
- Respond with EXACTLY ONE line of JSON and NOTHING ELSE.
- No markdown fences, no prose, no chain-of-thought, no preamble.
- Schema: {{"verdict": "<mission|chat|meta|uncertain>", "confidence": <0.0-1.0>, "reason": "<one sentence>"}}
- ``confidence`` is your calibrated certainty in the chosen verdict.
- If genuinely ambiguous, return "uncertain" with confidence < 0.6.

Examples:

User: "fix the flaky test in test_auth.py"
Output: {{"verdict": "mission", "confidence": 0.95, "reason": "imperative verb plus concrete file path"}}

User: "what is the difference between asyncio and threading?"
Output: {{"verdict": "chat", "confidence": 0.9, "reason": "open-ended conceptual question with no artifact"}}

User: "how's the current swarm mission going?"
Output: {{"verdict": "meta", "confidence": 0.92, "reason": "read-only query about an existing mission"}}

User: "abort the current run"
Output: {{"verdict": "meta", "confidence": 0.95, "reason": "control command addressed to swarm itself"}}

User: "thoughts on refactoring this?"
Output: {{"verdict": "uncertain", "confidence": 0.4, "reason": "mixes exploratory discussion with an implied code change"}}

Now classify this user message.

Context (may be empty):
{context}

User message:
{user_prompt}

Respond with one line of JSON, nothing else.
"""


__all__ = ["CLASSIFIER_PROMPT"]
