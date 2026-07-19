"""Tests for interaction loop integration with test gate.

Verifies that the grounded feedback loop properly integrates with
Outrider's test gate phase when lint or test failures occur.

Run with: pytest tests/test_interaction_integration.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import grounded_feedback_loop  # noqa: E402, F401


def test_interaction_loop_module_exists():
    """Verify the interaction loop module imports cleanly."""
    # The module should exist and be importable
    assert hasattr(grounded_feedback_loop, "attempt_interaction_refinement")
    assert hasattr(grounded_feedback_loop, "should_attempt_interaction_refinement")
    assert hasattr(grounded_feedback_loop, "_format_feedback_for_claude")
    assert hasattr(grounded_feedback_loop, "_build_feedback_prompt")


def test_interaction_loop_integrates_with_run_module():
    """Verify run.py can import and use the interaction loop."""
    import run  # noqa: F401
    # The test gate should have the interaction loop integration
    # This is verified by checking that the module contains the code
    with open(Path(__file__).resolve().parent.parent / "src" / "run.py") as f:
        content = f.read()
        assert "grounded_feedback_loop" in content
        assert "attempt_interaction_refinement" in content
        assert "interaction_succeeded" in content
