"""Classifier subsystem — routes user prompts to specialist workflows."""

from swarmd.classifier.llm import classify_llm
from swarmd.classifier.prompts import CLASSIFIER_PROMPT
from swarmd.classifier.rules import (
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
