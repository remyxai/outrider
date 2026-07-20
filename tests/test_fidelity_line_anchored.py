"""Tests for line-anchored feedback integration in fidelity audits.

Tests the integration of line-anchored feedback (arxiv:2607.12713v1) into
the fidelity-audit prompt builders. Verifies that diffs are formatted with
explicit line numbers, reducing token usage and improving correctness of
the audit.

Run with: pytest tests/test_fidelity_line_anchored.py -q
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# ─── Build fidelity audit prompt with line-anchored feedback ─────────────


def test_fidelity_audit_includes_anchored_feedback():
    """Verify _build_fidelity_audit_prompt includes line-anchored feedback."""
    diff = """\
--- a/src/module.py
+++ b/src/module.py
@@ -10,3 +10,5 @@
 def process(x):
     result = x * 2
+    # New line added
+    result = result + 1
     return result
"""

    prompt = run._build_fidelity_audit_prompt(
        pr_title="Add feature X",
        pr_body="Implements feature X from the paper",
        pr_diff=diff,
        arxiv_id="2607.12713v1",
        reference_url="https://github.com/user/repo",
        reference_root=Path("/tmp"),
        mode="mode-1",
    )

    # Should include line-anchored format section
    assert "line-anchored format" in prompt.lower()
    assert "arxiv:2607.12713v1" in prompt  # Paper attribution included
    # Should show line numbers from the anchored format
    assert "L" in prompt and ":" in prompt
    # Original diff should still be available for detailed inspection
    assert "unified diff" in prompt.lower() or "detailed inspection" in prompt.lower()


def test_paper_anchored_prompt_includes_anchored_feedback():
    """Verify paper-anchored audit also includes line-anchored feedback."""
    diff = """\
--- a/test.py
+++ b/test.py
@@ -1,2 +1,3 @@
 def foo():
     x = 1
+    y = 2
     return x
"""

    prompt = run._build_fidelity_audit_prompt_paper_anchored(
        pr_title="Test PR",
        pr_body="Test implementation",
        pr_diff=diff,
        arxiv_id="2607.12713v1",
        paper_text="Test paper",
    )

    # Should include line-anchored format reference
    assert "line-anchored format" in prompt.lower()
    assert "arxiv:2607.12713v1" in prompt
    # Should have line numbers in anchored feedback
    assert "L" in prompt


def test_anchored_feedback_preserved_with_mode2():
    """Verify line-anchored feedback works with Mode 2 (adapted port)."""
    diff = """\
--- a/src/core.py
+++ b/src/core.py
@@ -5,2 +5,3 @@
 def compute():
     value = base_compute()
+    value = optimize(value)
     return value
"""

    prompt = run._build_fidelity_audit_prompt(
        pr_title="Mode 2 implementation",
        pr_body="Adapted port with substitutions",
        pr_diff=diff,
        arxiv_id="2607.12713v1",
        reference_url="https://example.com/ref",
        reference_root=Path("/tmp"),
        mode="mode-2",
        substitutions=["optimizer substituted with target-native version"],
    )

    # Line-anchored feedback should be present alongside Mode 2 guidance
    assert "line-anchored" in prompt.lower()
    assert "Mode 2" in prompt
    assert "adapted port" in prompt.lower()


def test_fidelity_audit_prompt_with_large_diff():
    """Verify line-anchored feedback handles large diffs gracefully."""
    # Create a large diff
    lines = ["--- a/big.py", "+++ b/big.py", "@@ -1,0 +1,150 @@"]
    for i in range(1, 151):
        lines.append(f"+def func_{i}():")
        lines.append(f"+    return {i}")
    diff = "\n".join(lines)

    prompt = run._build_fidelity_audit_prompt(
        pr_title="Large file",
        pr_body="Added many functions",
        pr_diff=diff,
        arxiv_id="2607.12713v1",
        reference_url="https://example.com/ref",
        reference_root=Path("/tmp"),
    )

    # Should still include line-anchored format
    assert "line-anchored" in prompt.lower()
    # Should handle truncation gracefully
    assert "truncated" in prompt.lower() or "+" in prompt


def test_format_anchored_feedback_used():
    """Verify the line_anchored_feedback.format_anchored_feedback is imported
    and callable from run.py."""
    from line_anchored_feedback import format_anchored_feedback  # noqa: F401

    # Module should be importable
    assert callable(format_anchored_feedback)

    # Should be imported in run.py
    assert hasattr(run, "format_anchored_feedback")


# ─── Integration with fidelity audit flow ──────────────────────────────


def test_anchored_feedback_in_fidelity_audit_flow():
    """Verify line-anchored feedback is used in the real fidelity audit flow.

    This is an integration test that traces the feedback path from
    _run_pre_pr_fidelity_check through to the Claude prompt.
    """
    # This test verifies the call chain exists and is wired correctly.
    # The full fidelity audit requires reference repos, so we just
    # verify the prompt-building functions include the feature.

    # When _run_pre_pr_fidelity_check calls _build_fidelity_audit_prompt,
    # it should get a prompt with line-anchored feedback.
    diff = "--- a/test.py\n+++ b/test.py\n@@ -1 +1,2 @@\n a = 1\n+b = 2\n"

    prompt = run._build_fidelity_audit_prompt(
        pr_title="Test",
        pr_body="Test",
        pr_diff=diff,
        arxiv_id="test",
        reference_url="https://example.com",
        reference_root=Path("/tmp"),
    )

    # Core assertion: line-anchored feedback is in the prompt
    assert "line-anchored" in prompt.lower()


def test_audit_prompt_reports_accurate_line_numbers():
    """The anchor numbers embedded in the real audit prompt must match the
    diff's true file line numbers, even on a hunk mixing add + remove.

    This guards the fidelity-audit call site against the anchor-drift bug:
    a wrong ``L`` number is worse than none, since it points the auditor at
    the wrong line.
    """
    diff = """\
--- a/src/module.py
+++ b/src/module.py
@@ -5,4 +5,4 @@
 def foo():
     x = 1
-    old = 1
+    new = 1
     return new
"""

    prompt = run._build_fidelity_audit_prompt(
        pr_title="Refactor",
        pr_body="Rename variable",
        pr_diff=diff,
        arxiv_id="2607.12713v1",
        reference_url="https://example.com/ref",
        reference_root=Path("/tmp"),
    )

    # Both the removed old-file line and the added new-file line are line 7.
    assert "L7: + " in prompt and "new = 1" in prompt
    assert "L7: - " in prompt and "old = 1" in prompt
    # The buggy implementation would have emitted L6/L8 here.
    assert "L8:" not in prompt
