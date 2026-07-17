"""Tests for continuous verification scoring via logit distributions.

Adapted from LLM-as-a-Verifier (arXiv:2607.05391v1), which provides
fine-grained probabilistic feedback on code quality by extracting
expectations from LLM logit distributions instead of forcing discrete scores.

Run with: pytest tests/test_logit_verifier.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import logit_verifier  # noqa: E402


def test_verification_score_structure():
    """VerificationScore carries three criteria + overall aggregate + confidence."""
    score = logit_verifier.VerificationScore(
        correctness=0.85,
        style=0.92,
        integration=0.78,
        overall=0.85,
        confidence=0.70,
    )
    assert 0 <= score.correctness <= 1
    assert 0 <= score.style <= 1
    assert 0 <= score.integration <= 1
    assert 0 <= score.overall <= 1
    assert 0 <= score.confidence <= 1


def test_extract_logit_score_positive_tokens():
    """Score extraction: positive tokens (e.g., 'correct') → higher score."""
    logits = {"correct": 5.0, "incorrect": -2.0}
    score = logit_verifier.extract_logit_score(logits, "correctness")
    assert score > 0.5  # positive mass should dominate


def test_extract_logit_score_negative_tokens():
    """Score extraction: negative tokens dominate → lower score."""
    logits = {"correct": -3.0, "incorrect": 4.0}
    score = logit_verifier.extract_logit_score(logits, "correctness")
    assert score < 0.5  # negative mass should dominate


def test_extract_logit_score_empty_logits():
    """Score extraction returns neutral 0.5 when logits missing."""
    score = logit_verifier.extract_logit_score({}, "correctness")
    assert score == 0.5


def test_extract_logit_score_unknown_criterion():
    """Score extraction gracefully handles unknown criteria with fallback tokens."""
    logits = {"good": 2.0, "bad": 1.0}
    score = logit_verifier.extract_logit_score(logits, "unknown_criterion")
    assert 0 <= score <= 1  # should not crash, should return valid score


def test_score_implementation_returns_valid_range():
    """Integration: score_implementation returns [0,1] scores for all dimensions."""
    score = logit_verifier.score_implementation(
        diff_text="+ def hello(): return 'world'",
        repo_context="Python library",
        timeout_s=10,
    )
    assert 0 <= score.correctness <= 1
    assert 0 <= score.style <= 1
    assert 0 <= score.integration <= 1
    assert 0 <= score.overall <= 1
    assert 0 <= score.confidence <= 1


def test_score_vs_threshold_passes_high_score():
    """Routing: high overall score passes threshold check."""
    score = logit_verifier.VerificationScore(
        correctness=0.88,
        style=0.85,
        integration=0.90,
        overall=0.87,
        confidence=0.75,
    )
    assert logit_verifier.score_vs_threshold(score, threshold=0.70)


def test_score_vs_threshold_fails_low_score():
    """Routing: low overall score fails threshold check."""
    score = logit_verifier.VerificationScore(
        correctness=0.55,
        style=0.48,
        integration=0.60,
        overall=0.54,
        confidence=0.40,
    )
    assert not logit_verifier.score_vs_threshold(score, threshold=0.70)


def test_logit_tokens_for_criterion_has_all_criteria():
    """All standard criteria return token sets."""
    assert logit_verifier._logit_tokens_for_criterion("correctness")
    assert logit_verifier._logit_tokens_for_criterion("style")
    assert logit_verifier._logit_tokens_for_criterion("integration")
