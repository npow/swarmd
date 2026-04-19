"""Snapshot tests for ``PROPOSE_CRITERIA_PROMPT``.

The prompt is a static module-level constant; these tests guard against
accidental deletion of the sections the Haiku contract depends on:

1. Schema keywords (mission.yaml fields) appear.
2. The JSON schema hints are present.
3. "JSON only" / "nothing else" instruction present.
4. ``{user_prompt}`` and ``{context}`` placeholders present.
5. Formatted size stays under 4000 chars (Haiku latency + cost guard).
6. At least one anti-cheat directive is present (per spec §9 / Task 23).

Intentionally loose — we're guarding sections, not exact wording. Edits
that reword but preserve the invariants should still pass.
"""

from __future__ import annotations

from swarmd.mcp.prompts import PROPOSE_CRITERIA_PROMPT


class TestProposeCriteriaPromptShape:
    def test_mentions_mission_yaml_schema(self):
        """The prompt must name the mission.yaml fields Haiku has to emit."""
        for key in ("mission", "workspace", "success_criteria", "verification"):
            assert key in PROPOSE_CRITERIA_PROMPT, (
                f"prompt missing schema key: {key}"
            )

    def test_contains_json_only_instruction(self):
        """Prompt must tell Haiku to emit JSON and nothing else."""
        candidates = ("nothing else", "only json", "json only", "no prose")
        lowered = PROPOSE_CRITERIA_PROMPT.lower()
        assert any(c in lowered for c in candidates), (
            "prompt must tell the model not to add prose outside JSON"
        )

    def test_has_user_prompt_placeholder(self):
        """``str.format(user_prompt=...)`` must have a slot to fill."""
        assert "{user_prompt}" in PROPOSE_CRITERIA_PROMPT

    def test_has_context_placeholder(self):
        """``str.format(context=...)`` must have a slot to fill."""
        assert "{context}" in PROPOSE_CRITERIA_PROMPT

    def test_formats_without_keyerror(self):
        """``str.format`` must succeed with the two expected kwargs — no
        stray unescaped braces left in the JSON schema examples."""
        formatted = PROPOSE_CRITERIA_PROMPT.format(
            user_prompt="fix the bug", context="cwd: /tmp"
        )
        assert "fix the bug" in formatted
        assert "cwd: /tmp" in formatted

    def test_formatted_size_under_4000_chars(self):
        """Guard against prompt-drift cost / latency. Haiku is a hot-path
        model; keep this compact."""
        formatted = PROPOSE_CRITERIA_PROMPT.format(
            user_prompt="fix the bug", context="(none)"
        )
        assert len(formatted) < 4000, (
            f"prompt is {len(formatted)} chars — should stay under 4000"
        )

    def test_mentions_anti_cheat_or_invariant(self):
        """Per spec §9 / Task 23: the prompt must bias Haiku toward at
        least one anti-cheat criterion. Accept any of the canonical
        anti-cheat terms."""
        lowered = PROPOSE_CRITERIA_PROMPT.lower()
        candidates = ("anti-cheat", "test_count_floor", "no_mock", "invariant")
        assert any(c in lowered for c in candidates), (
            "prompt should bias Haiku toward at least one anti-cheat signal"
        )

    def test_has_few_shot_examples(self):
        """Need ≥2 few-shot examples so Haiku has structured-output
        anchors. We count occurrences of the example-user header."""
        lowered = PROPOSE_CRITERIA_PROMPT.lower()
        # 'user:' appears once per example in our template
        count = lowered.count("user:")
        assert count >= 2, (
            f"prompt should have >=2 few-shot examples, got {count}"
        )
