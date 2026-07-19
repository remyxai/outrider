"""Tests for grounded feedback loop interaction refinement.

Tests the interaction scaling mechanism (arXiv:2607.11598v1) that uses
real observations (lint errors, test failures) to drive iterative
refinement of generated code.

Run with: pytest tests/test_grounded_feedback_loop.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from grounded_feedback_loop import (
    should_attempt_interaction_refinement,
    _format_feedback_for_claude,
    _build_feedback_prompt,
)


# ─── should_attempt_interaction_refinement ─────────────────────────────────


def test_attempts_refinement_on_lint_failure():
    """Should attempt refinement when lint fails and there are Python files."""
    result = should_attempt_interaction_refinement(
        lint_status="failed",
        test_status="passed",
        touched_py=["src/module.py"],
    )
    assert result is True


def test_attempts_refinement_on_test_failure():
    """Should attempt refinement when tests fail and there are Python files."""
    result = should_attempt_interaction_refinement(
        lint_status="passed",
        test_status="failed",
        touched_py=["src/module.py"],
    )
    assert result is True


def test_skips_refinement_when_no_py_files():
    """Should not attempt refinement if there are no Python files."""
    result = should_attempt_interaction_refinement(
        lint_status="failed",
        test_status="failed",
        touched_py=[],
    )
    assert result is False


def test_skips_refinement_when_no_failure():
    """Should not attempt refinement when both lint and tests pass."""
    result = should_attempt_interaction_refinement(
        lint_status="passed",
        test_status="passed",
        touched_py=["src/module.py"],
    )
    assert result is False


def test_skips_refinement_on_catastrophic_lint_error():
    """Should not attempt refinement if lint itself errored (setup issue)."""
    result = should_attempt_interaction_refinement(
        lint_status="error",
        test_status="failed",
        touched_py=["src/module.py"],
    )
    assert result is False


# ─── _format_feedback_for_claude ────────────────────────────────────────────


def test_formats_feedback_under_budget():
    """Should trim feedback to stay under character budget."""
    lint_output = "error: unused import on line 1\n" * 100
    test_output = "FAILED test_foo on line 50\n" * 100
    max_chars = 2000

    lint_fb, test_fb = _format_feedback_for_claude(lint_output, test_output, max_chars)

    total = len(lint_fb) + len(test_fb)
    assert total <= max_chars


def test_preserves_feedback_shape():
    """Should preserve line breaks and structure when trimming."""
    lint_output = "error: line 1\nerror: line 2\nerror: line 3"
    test_output = "FAILED: test_a\nFAILED: test_b"

    lint_fb, test_fb = _format_feedback_for_claude(lint_output, test_output)

    assert "error" in lint_fb
    assert "FAILED" in test_fb


# ─── _build_feedback_prompt ────────────────────────────────────────────────


def test_builds_valid_prompt():
    """Should produce a valid refinement prompt."""
    lint_issues = "unused import on line 5"
    test_failures = "AssertionError: expected 5, got 6"
    iteration = 1

    prompt = _build_feedback_prompt(lint_issues, test_failures, iteration)

    assert "Refinement iteration 1" in prompt
    assert "unused import on line 5" in prompt
    assert "AssertionError" in prompt
    assert "Fix the issues" in prompt


def test_prompt_includes_iteration_number():
    """Should reflect the iteration count in the prompt."""
    prompt = _build_feedback_prompt("issue", "failure", iteration=2)
    assert "iteration 2" in prompt
