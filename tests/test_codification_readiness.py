"""Tests for codification readiness assessment of research-method specifications.

Adapted from IdeaAMBIG: Benchmarking Implementation-Critical Gaps in
Research-Idea Specifications (https://arxiv.org/abs/2609.10539v1).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from codification_readiness import assess_codification_readiness


def test_no_gaps_for_well_specified_method():
    """Well-specified methods should not trigger gap warnings."""
    abstract = (
        "We propose a method with training procedures including learning rate "
        "0.001, batch size 32, dropout 0.1, cross-entropy loss, and Adam optimizer. "
        "The model uses a 3-layer neural network with 256 hidden units and "
        "ReLU activation. We evaluate using standard metrics on CIFAR-10."
    )
    experiment = "Implement the proposed optimizer."

    gaps = assess_codification_readiness(
        "A Well-Specified Method", abstract, experiment
    )
    assert gaps == "", "well-specified methods should have no gaps"


def test_detects_vague_specification_patterns():
    """Specifications with multiple vague terms should be flagged."""
    abstract = (
        "We propose a method that uses suitable parameters, typically chosen "
        "appropriately for the task. Generally, one can use similar approaches "
        "to related work, which may be adapted as needed."
    )
    experiment = "Implement the approach."

    gaps = assess_codification_readiness(
        "Vague Method", abstract, experiment
    )
    assert "vague specification patterns" in gaps, "should flag vague language"
    assert "advisory" in gaps.lower(), "should be marked as advisory"


def test_detects_missing_training_details():
    """Methods missing details on training procedures should be flagged."""
    abstract = (
        "We propose a novel neural network architecture. The model performs "
        "well on various benchmarks."
    )
    experiment = "Implement the model architecture."

    gaps = assess_codification_readiness(
        "Underspecified Training", abstract, experiment
    )
    # Should flag missing training details since few critical categories present
    assert gaps != "", "should detect missing training details"
    assert "training or architectural setup" in gaps


def test_detects_missing_evaluation_metrics():
    """Specifications without evaluation details should be flagged."""
    abstract = (
        "Our method trains a model with learning rate 0.01 and batch size 64 "
        "using the Adam optimizer. The architecture uses 5 layers."
    )
    experiment = "Implement the training procedure."

    gaps = assess_codification_readiness(
        "Missing Evaluation", abstract, experiment
    )
    assert gaps != "", "should detect missing evaluation"
    assert "success criteria and benchmarks" in gaps


def test_no_gaps_when_empty_specification():
    """Empty or missing specifications should return empty (no false positives)."""
    gaps = assess_codification_readiness("Title", "", "")
    assert gaps == "", "empty specs should not trigger false positive gaps"

    gaps = assess_codification_readiness("Title", None, None)
    assert gaps == "", "None specs should not crash"


def test_gap_text_includes_advisory_markers():
    """Returned gap text should be clearly marked as advisory."""
    abstract = (
        "A suitable method, typically chosen appropriately, generally useful, "
        "can be adapted as needed, often improved."
    )
    experiment = "Implement something."

    gaps = assess_codification_readiness("Title", abstract, experiment)
    assert gaps != "", "should return gaps for vague specs"
    assert "IdeaAMBIG" in gaps, "should attribute to the paper"
    assert "advisory" in gaps.lower(), "should mark as advisory"
    assert "Note:" in gaps, "should include usage note"


def test_gap_text_is_markdown_formatted():
    """Gap assessment should be valid markdown."""
    abstract = (
        "Method that is suitable and typically chosen appropriately, "
        "generally useful, often adapted, can be improved."
    )
    experiment = "Some experiment."

    gaps = assess_codification_readiness("Title", abstract, experiment)
    assert gaps.startswith("### Codification"), "should have markdown heading"
    assert gaps.count("-") >= 1, "should have bullet-point list"
