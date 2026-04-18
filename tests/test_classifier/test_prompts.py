"""Snapshot tests for the stage 3 classifier prompt.

The prompt is a static module-level constant; these tests verify it contains
the sections the downstream LLM contract depends on:

1. Each verdict keyword appears (MISSION / CHAT / META / UNCERTAIN or their
   lowercase forms).
2. The JSON schema example is present.
3. The "no prose outside JSON" instruction is present.
4. Placeholders ``{user_prompt}`` and ``{context}`` are present.
5. Formatted size < 4000 chars — we don't want prompt drift inflating cost.

These are intentionally loose — we're guarding against accidental deletion of
required sections, not testing exact wording. Wording changes that still
satisfy the invariants should pass.
"""

from __future__ import annotations

from swarm.classifier.prompts import CLASSIFIER_PROMPT


class TestClassifierPromptShape:
    def test_contains_all_verdict_keywords(self):
        """All four verdicts (lowercase) must appear at least once."""
        lowered = CLASSIFIER_PROMPT.lower()
        for verdict in ("mission", "chat", "meta", "uncertain"):
            assert verdict in lowered, f"missing verdict keyword: {verdict}"

    def test_contains_json_schema_example(self):
        """The exact JSON field names must appear so the model knows the
        schema. We check for the three keys individually — ordering may
        drift in future edits."""
        for key in ('"verdict"', '"confidence"', '"reason"'):
            assert key in CLASSIFIER_PROMPT, f"missing schema key: {key}"

    def test_contains_no_prose_outside_json_instruction(self):
        """The prompt must instruct the model to emit JSON and nothing else.
        We look for any of a few natural phrasings so wording tweaks don't
        break the test."""
        candidates = (
            "nothing else",
            "only json",
            "only the json",
            "no prose",
            "and only",
        )
        lowered = CLASSIFIER_PROMPT.lower()
        assert any(c in lowered for c in candidates), (
            "prompt must tell the model not to add prose outside JSON"
        )

    def test_has_user_prompt_placeholder(self):
        """``str.format(user_prompt=...)`` must have a slot to fill."""
        assert "{user_prompt}" in CLASSIFIER_PROMPT

    def test_has_context_placeholder(self):
        """``str.format(context=...)`` must have a slot to fill."""
        assert "{context}" in CLASSIFIER_PROMPT

    def test_formats_without_keyerror(self):
        """``str.format`` must succeed with the two expected kwargs — no
        stray unescaped ``{...}`` left over from the schema examples."""
        formatted = CLASSIFIER_PROMPT.format(
            user_prompt="test prompt", context="cwd: /tmp"
        )
        assert "test prompt" in formatted
        assert "cwd: /tmp" in formatted

    def test_formatted_size_under_4000_chars(self):
        """Guard against runaway prompt growth — Haiku latency + cost scale
        with prompt size, and we're a hot-path classifier."""
        formatted = CLASSIFIER_PROMPT.format(
            user_prompt="fix the bug", context="(none)"
        )
        assert len(formatted) < 4000, (
            f"prompt is {len(formatted)} chars — should stay under 4000"
        )

    def test_mentions_uncertain_threshold(self):
        """The prompt must tell the model to use UNCERTAIN + confidence<0.6
        for ambiguous cases — this is load-bearing for the confidence gate."""
        lowered = CLASSIFIER_PROMPT.lower()
        assert "uncertain" in lowered
        # Either "< 0.6" or "below 0.6" or similar. Be loose.
        assert "0.6" in CLASSIFIER_PROMPT, "prompt must reference 0.6 threshold"

    def test_has_at_least_one_example_per_verdict(self):
        """Few-shot coverage: at least one example JSON line per verdict.

        We look for the canonical pattern ``"verdict": "<name>"`` in the
        prompt body. Five or so examples is plenty; four is the minimum to
        anchor each verdict."""
        for verdict in ("mission", "chat", "meta", "uncertain"):
            needle = f'"verdict": "{verdict}"'
            assert needle in CLASSIFIER_PROMPT, (
                f"prompt lacks a few-shot example for verdict={verdict}"
            )
