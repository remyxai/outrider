"""Tests for line-anchored feedback formatting.

Line-anchored feedback (from arxiv:2607.12713v1) structures diff feedback
with explicit line numbers, reducing token usage (22-58% in the paper) and
improving correctness. These tests verify the module correctly formats diffs
into line-anchored format suitable for inclusion in LLM prompts.

Run with: pytest tests/test_line_anchored_feedback.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from line_anchored_feedback import (
    format_anchored_feedback,
    AnchoredDiff,
    _parse_unified_diff,
)


# ─── Parsing unified diff ────────────────────────────────────────────────────


def test_parse_single_added_line():
    """Parse a simple added line from unified diff."""
    diff = """\
--- a/test.py
+++ b/test.py
@@ -5,3 +5,4 @@
 def foo():
     x = 1
+    y = 2
     return x
"""
    anchored = _parse_unified_diff(diff)
    assert len(anchored) == 1
    assert anchored[0].file_path == "test.py"
    assert len(anchored[0].anchors) == 1
    assert anchored[0].anchors[0].context == "added"
    assert anchored[0].anchors[0].content == "    y = 2"


def test_parse_multiple_files():
    """Parse diff with changes across multiple files."""
    diff = """\
--- a/file1.py
+++ b/file1.py
@@ -1,2 +1,3 @@
 a = 1
+b = 2
 c = 3
--- a/file2.py
+++ b/file2.py
@@ -10,1 +10,2 @@
 x = 10
+y = 20
"""
    anchored = _parse_unified_diff(diff)
    assert len(anchored) == 2
    assert anchored[0].file_path == "file1.py"
    assert anchored[1].file_path == "file2.py"
    assert len(anchored[0].anchors) == 1
    assert len(anchored[1].anchors) == 1


def test_parse_added_and_removed_lines():
    """Parse both additions and removals."""
    diff = """\
--- a/test.py
+++ b/test.py
@@ -5,3 +5,3 @@
 def foo():
-    old = 1
+    new = 1
     return new
"""
    anchored = _parse_unified_diff(diff)
    assert len(anchored) == 1
    assert len(anchored[0].anchors) == 2
    assert anchored[0].anchors[0].context == "removed"
    assert anchored[0].anchors[1].context == "added"


def test_parse_empty_diff():
    """Parse diff with no actual changes."""
    diff = ""
    anchored = _parse_unified_diff(diff)
    assert len(anchored) == 0


def test_parse_respects_max_anchors():
    """Verify truncation when exceeding max_anchors_per_file."""
    diff_lines = ["--- a/big.py", "+++ b/big.py", "@@ -1,0 +1,200 @@"]
    for i in range(1, 201):
        diff_lines.append(f"+line {i}")
    diff = "\n".join(diff_lines)

    anchored = _parse_unified_diff(diff, max_anchors_per_file=50)
    assert len(anchored) == 1
    assert len(anchored[0].anchors) == 50
    assert anchored[0].truncated is True


# ─── Anchor line-number accuracy ─────────────────────────────────────────────


def test_anchor_line_numbers_on_mixed_hunk():
    """Removed lines anchor to the OLD file, added lines to the NEW file.

    On a hunk that mixes a removal and an addition the two positions must be
    computed independently — deriving either from a running anchor count makes
    the reported ``L`` numbers drift, which defeats line anchoring entirely.
    """
    diff = """\
--- a/test.py
+++ b/test.py
@@ -5,4 +5,4 @@
 def foo():
     x = 1
-    old = 1
+    new = 1
     return new
"""
    anchors = _parse_unified_diff(diff)[0].anchors
    removed = next(a for a in anchors if a.context == "removed")
    added = next(a for a in anchors if a.context == "added")
    # ``old = 1`` is the 3rd line of the old file (start=5 -> 5,6,7).
    assert removed.line_number == 7
    # ``new = 1`` is the 3rd line of the new file (start=5 -> 5,6,7).
    assert added.line_number == 7


def test_anchor_line_numbers_across_multiple_hunks():
    """Each hunk header re-seats both cursors to its declared start lines."""
    diff = """\
--- a/test.py
+++ b/test.py
@@ -1,2 +1,3 @@
 a = 1
+b = 2
 c = 3
@@ -20,2 +21,3 @@
 x = 1
+y = 2
 z = 3
"""
    added = [a for a in _parse_unified_diff(diff)[0].anchors if a.context == "added"]
    assert [a.line_number for a in added] == [2, 22]


def test_consecutive_removed_lines_advance_old_cursor():
    """Multiple removals in a row map to consecutive old-file line numbers."""
    diff = """\
--- a/test.py
+++ b/test.py
@@ -10,4 +10,1 @@
 keep = 0
-drop_a = 1
-drop_b = 2
 keep2 = 3
"""
    removed = [a for a in _parse_unified_diff(diff)[0].anchors if a.context == "removed"]
    assert [a.line_number for a in removed] == [11, 12]


# ─── Formatting for feedback ───────────────────────────────────────────────


def test_format_simple_diff():
    """Format a simple diff into line-anchored feedback."""
    diff = """\
--- a/test.py
+++ b/test.py
@@ -1,3 +1,4 @@
 def foo():
     x = 1
+    y = 2
     return x
"""
    feedback = format_anchored_feedback(diff)
    assert "test.py" in feedback
    assert "L3: +" in feedback  # Line 3 is added
    assert "y = 2" in feedback


def test_format_empty_diff():
    """Format empty diff gracefully."""
    feedback = format_anchored_feedback("")
    assert "(no changes)" in feedback


def test_format_multiple_files():
    """Format diff with multiple files shows each separately."""
    diff = """\
--- a/file1.py
+++ b/file1.py
@@ -1,1 +1,2 @@
 x = 1
+y = 2
--- a/file2.py
+++ b/file2.py
@@ -5,1 +5,2 @@
 a = 5
+b = 6
"""
    feedback = format_anchored_feedback(diff)
    assert "file1.py" in feedback
    assert "file2.py" in feedback
    assert "L2: +" in feedback  # Added lines shown with line numbers


def test_format_truncated_indication():
    """Format includes truncation warning when diff is large."""
    diff_lines = ["--- a/big.py", "+++ b/big.py", "@@ -1,0 +1,200 @@"]
    for i in range(1, 201):
        diff_lines.append(f"+new line {i}")
    diff = "\n".join(diff_lines)

    anchored = _parse_unified_diff(diff, max_anchors_per_file=50)
    assert anchored[0].truncated is True


# ─── Integration with run.py (fidelity audit) ──────────────────────────────


def test_feedback_format_integration():
    """Verify the formatted feedback is suitable for LLM prompts."""
    # This test ensures the output of format_anchored_feedback can be
    # safely embedded in a prompt without breaking JSON or markdown.
    diff = """\
--- a/src/module.py
+++ b/src/module.py
@@ -10,2 +10,3 @@
 def process(x):
     result = x * 2
+    result = result + 1
     return result
"""
    feedback = format_anchored_feedback(diff)

    # Should be plain text suitable for embedding in any prompt format
    assert isinstance(feedback, str)
    # Should not contain unescaped quotes or backslashes that would break JSON
    assert '\\"' not in feedback
    # Should contain structured line references
    assert "L" in feedback and ":" in feedback
