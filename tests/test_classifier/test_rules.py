"""Tests for swarm.classifier.rules — stages 1+2 (explicit prefix + rule gate).

Pure unit tests: no I/O, no LLM, fully deterministic. Should complete in well
under 2 seconds total.
"""

from __future__ import annotations

import pytest

from swarmd.classifier.rules import (
    ClassifierResult,
    ClassifierVerdict,
    classify,
    classify_prefix,
    classify_rules,
)


# ---------------------------------------------------------------------------
# Stage 1 tests — explicit prefix
# ---------------------------------------------------------------------------


class TestStage1ExplicitPrefix:
    def test_swarm_prefix_with_body_is_mission(self):
        result = classify_prefix("/swarm fix the bug")
        assert result is not None
        assert result.verdict == ClassifierVerdict.MISSION
        assert result.stage == 1
        assert result.confidence == 1.0

    def test_swarm_prefix_leading_whitespace_and_caps(self):
        result = classify_prefix("  /SWARM build X")
        assert result is not None
        assert result.verdict == ClassifierVerdict.MISSION
        assert result.stage == 1
        assert result.confidence == 1.0

    def test_chat_prefix(self):
        result = classify_prefix("/chat hey")
        assert result is not None
        assert result.verdict == ClassifierVerdict.CHAT
        assert result.stage == 1
        assert result.confidence == 1.0

    def test_meta_prefix(self):
        result = classify_prefix("/meta what are your settings")
        assert result is not None
        assert result.verdict == ClassifierVerdict.META
        assert result.stage == 1
        assert result.confidence == 1.0

    def test_swarm_question_prefix_is_meta(self):
        result = classify_prefix("/swarm?")
        assert result is not None
        assert result.verdict == ClassifierVerdict.META
        assert result.stage == 1
        assert result.confidence == 1.0

    def test_swarm_prefix_no_body_is_mission(self):
        result = classify_prefix("/swarm")
        assert result is not None
        assert result.verdict == ClassifierVerdict.MISSION
        assert result.stage == 1
        assert result.confidence == 1.0

    def test_no_prefix_returns_none(self):
        result = classify_prefix("hello world")
        assert result is None

    def test_prefix_requires_boundary(self):
        # "/swarm-related-thing" should NOT match — prefix requires whitespace
        # after or end-of-string.
        result = classify_prefix("/swarm-related-thing")
        assert result is None

    def test_mission_prefix(self):
        result = classify_prefix("/mission build the thing")
        assert result is not None
        assert result.verdict == ClassifierVerdict.MISSION

    def test_swarm_bang_prefix(self):
        result = classify_prefix("/swarm! fix it")
        assert result is not None
        assert result.verdict == ClassifierVerdict.MISSION

    def test_empty_string_no_prefix(self):
        result = classify_prefix("")
        assert result is None

    def test_whitespace_only_no_prefix(self):
        result = classify_prefix("   \t\n  ")
        assert result is None

    def test_tab_after_prefix_counts_as_boundary(self):
        result = classify_prefix("/swarm\tfix bug")
        assert result is not None
        assert result.verdict == ClassifierVerdict.MISSION


# ---------------------------------------------------------------------------
# Stage 2 tests — rule gate
# ---------------------------------------------------------------------------


class TestStage2Rules:
    def test_imperative_plus_file_path_is_mission(self):
        result = classify_rules("fix the login bug in auth.py")
        assert result.verdict == ClassifierVerdict.MISSION
        assert result.stage == 2
        assert result.confidence >= 0.6

    def test_interrogative_about_concept_is_chat(self):
        result = classify_rules("what is a closure in JavaScript")
        assert result.verdict == ClassifierVerdict.CHAT
        assert result.stage == 2
        assert result.confidence >= 0.6

    def test_small_talk_greeting_is_chat(self):
        result = classify_rules("hello, how are you")
        assert result.verdict == ClassifierVerdict.CHAT
        assert result.stage == 2

    def test_imperative_build_is_mission(self):
        result = classify_rules("build a new user-profile page")
        assert result.verdict == ClassifierVerdict.MISSION
        assert result.stage == 2

    def test_imperative_plus_ticket_ref_is_mission(self):
        result = classify_rules("refactor #123")
        assert result.verdict == ClassifierVerdict.MISSION
        assert result.stage == 2

    def test_swarm_meta_question_is_meta(self):
        result = classify_rules("how does swarm classify prompts")
        assert result.verdict == ClassifierVerdict.META
        assert result.stage == 2

    def test_politeness_plus_imperative_is_mission(self):
        result = classify_rules("can you fix the flaky test")
        assert result.verdict == ClassifierVerdict.MISSION
        assert result.stage == 2

    def test_explain_is_chat(self):
        result = classify_rules("explain recursion to me")
        assert result.verdict == ClassifierVerdict.CHAT
        assert result.stage == 2

    def test_mixed_mission_and_chat_signals_is_uncertain(self):
        result = classify_rules(
            "I want to ship the feature, but what is a deployment artifact?"
        )
        assert result.verdict == ClassifierVerdict.UNCERTAIN
        assert result.stage == 2
        assert result.confidence < 0.6

    def test_small_talk_thanks_is_chat(self):
        result = classify_rules("ok cool thanks")
        assert result.verdict == ClassifierVerdict.CHAT
        assert result.stage == 2

    def test_no_strong_signals_is_uncertain(self):
        result = classify_rules("cat /etc/hosts")
        assert result.verdict == ClassifierVerdict.UNCERTAIN
        assert result.stage == 2
        assert result.confidence < 0.6


# ---------------------------------------------------------------------------
# Combined classify() tests
# ---------------------------------------------------------------------------


class TestCombinedClassify:
    def test_explicit_prefix_wins_over_rules(self):
        result = classify("/swarm fix the bug")
        assert result.verdict == ClassifierVerdict.MISSION
        assert result.stage == 1
        assert result.confidence == 1.0

    def test_rule_mission_via_imperative(self):
        result = classify("fix the bug")
        assert result.verdict == ClassifierVerdict.MISSION
        assert result.stage == 2

    def test_rule_chat_via_interrogative(self):
        result = classify("what is recursion")
        assert result.verdict == ClassifierVerdict.CHAT
        assert result.stage == 2

    def test_nonsense_defers_to_uncertain(self):
        result = classify("asdf qwer")
        assert result.verdict == ClassifierVerdict.UNCERTAIN
        assert result.stage == 2
        assert result.confidence < 0.6


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_string_is_uncertain(self):
        result = classify("")
        assert result.verdict == ClassifierVerdict.UNCERTAIN
        assert result.confidence == 0.0

    def test_whitespace_only_is_uncertain(self):
        result = classify("   \t\n  ")
        assert result.verdict == ClassifierVerdict.UNCERTAIN

    def test_very_long_prompt_mission_prefix_matches(self):
        body = "fix the bug in auth.py " + ("x" * 5000)
        result = classify(body)
        assert result.verdict == ClassifierVerdict.MISSION
        # Ensure no pathological regex behavior: should be well under 100ms.
        # (pytest will fail fast if something is catastrophically slow.)

    def test_classify_returns_classifier_result_type(self):
        result = classify("hello")
        assert isinstance(result, ClassifierResult)
        assert isinstance(result.verdict, ClassifierVerdict)
        assert isinstance(result.stage, int)
        assert isinstance(result.confidence, float)
        assert isinstance(result.reason, str)

    def test_classifier_verdict_is_str_enum(self):
        # ClassifierVerdict is (str, Enum) — values round-trip as strings.
        assert ClassifierVerdict.MISSION.value == "mission"
        assert ClassifierVerdict.CHAT.value == "chat"
        assert ClassifierVerdict.META.value == "meta"
        assert ClassifierVerdict.UNCERTAIN.value == "uncertain"

    def test_mission_confidence_capped(self):
        # Stage 2 confidence must be capped at 0.95 (1.0 reserved for stage 1).
        result = classify_rules("fix the bug in auth.py #123")
        if result.verdict == ClassifierVerdict.MISSION:
            assert result.confidence <= 0.95

    def test_uncertain_confidence_below_threshold(self):
        # UNCERTAIN verdicts always have confidence < 0.6.
        result = classify_rules("asdf qwer zxcv")
        assert result.verdict == ClassifierVerdict.UNCERTAIN
        assert result.confidence < 0.6


# ---------------------------------------------------------------------------
# Stage 1 precedence (bonus — defense-in-depth)
# ---------------------------------------------------------------------------


class TestStage1Precedence:
    @pytest.mark.parametrize(
        "prompt,expected",
        [
            ("/swarm anything", ClassifierVerdict.MISSION),
            ("/swarm? what", ClassifierVerdict.META),
            ("/chat whatever", ClassifierVerdict.CHAT),
            ("/meta help", ClassifierVerdict.META),
            ("/mission build it", ClassifierVerdict.MISSION),
        ],
    )
    def test_prefix_matrix(self, prompt, expected):
        result = classify(prompt)
        assert result.verdict == expected
        assert result.stage == 1
        assert result.confidence == 1.0
