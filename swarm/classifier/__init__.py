"""Classifier subsystem — routes user prompts to specialist workflows."""

from swarm.classifier.llm import classify_llm
from swarm.classifier.prompts import CLASSIFIER_PROMPT
from swarm.classifier.rules import (
    ClassifierResult,
    ClassifierVerdict,
    classify,
    classify_prefix,
    classify_rules,
)

__all__ = [
    "ClassifierVerdict",
    "ClassifierResult",
    "classify",
    "classify_prefix",
    "classify_rules",
    "classify_llm",
    "CLASSIFIER_PROMPT",
]
