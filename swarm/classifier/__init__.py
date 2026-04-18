"""Classifier subsystem — routes user prompts to specialist workflows."""

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
]
