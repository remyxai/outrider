"""
Line-anchored feedback formatting for efficient code review.

This module implements the core insight from "Line-Anchored Feedback Cuts Token
Costs and Improves Correctness in AI Code Editing" (arxiv:2607.12713v1) by
providing diff feedback in a structured, line-numbered format. This reduces token
costs (22-58% reduction observed in the paper) and improves correctness, especially
for larger files (100+ lines).

The module formats diff segments with explicit line numbers, making feedback more
precise and reducing the model's need to infer context from surrounding text.
"""
from __future__ import annotations

import re
from typing import NamedTuple


class LineAnchor(NamedTuple):
    """A single line-anchored feedback item."""

    line_number: int
    context: str  # "added" | "removed" | "modified"
    content: str  # The actual line content
    note: str | None = None  # Optional annotation


class AnchoredDiff(NamedTuple):
    """A structured, line-anchored representation of a diff."""

    file_path: str
    anchors: list[LineAnchor]
    truncated: bool = False  # True if diff was too large and truncated


def _parse_unified_diff(diff_text: str, max_anchors_per_file: int = 100) -> list[AnchoredDiff]:
    """Parse unified diff format into line-anchored segments.

    Extracts file paths and line numbers from unified diff, producing a
    structured format suitable for line-anchored feedback. Limits output
    to max_anchors_per_file per file to keep feedback concise.

    Args:
        diff_text: Unified diff output (e.g., from git diff)
        max_anchors_per_file: Maximum anchors to extract per file

    Returns:
        List of AnchoredDiff objects, one per modified file.
    """
    files = []
    current_file = None
    current_anchors = []
    current_line_offset = 0
    current_truncated = False

    lines = diff_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        # File header: "--- a/path/to/file" or "diff --git a/... b/..."
        if line.startswith("--- a/"):
            # Save previous file if any
            if current_file is not None:
                files.append(AnchoredDiff(
                    file_path=current_file,
                    anchors=current_anchors,
                    truncated=current_truncated,
                ))

            # Extract filename from "--- a/path/to/file"
            current_file = line[6:]  # strip "--- a/"
            current_anchors = []
            current_truncated = False
            i += 1
            continue

        # Hunk header: "@@ -10,5 +20,7 @@"
        if line.startswith("@@"):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                current_line_offset = int(match.group(1)) - 1

        # Content lines
        if line.startswith("+") and not line.startswith("+++"):
            # Added line
            if len(current_anchors) < max_anchors_per_file:
                current_anchors.append(LineAnchor(
                    line_number=current_line_offset + len(current_anchors) + 1,
                    context="added",
                    content=line[1:],  # Strip leading '+'
                ))
            else:
                current_truncated = True
        elif line.startswith("-") and not line.startswith("---"):
            # Removed line
            if len(current_anchors) < max_anchors_per_file:
                current_anchors.append(LineAnchor(
                    line_number=current_line_offset,
                    context="removed",
                    content=line[1:],  # Strip leading '-'
                ))
            else:
                current_truncated = True
        elif line.startswith(" "):
            # Context line (unchanged)
            current_line_offset += 1

        i += 1

    # Save last file
    if current_file is not None:
        files.append(AnchoredDiff(
            file_path=current_file,
            anchors=current_anchors,
            truncated=current_truncated,
        ))

    return files


def format_anchored_feedback(diff_text: str) -> str:
    """Format diff as structured, line-anchored feedback.

    Converts a unified diff into a structured format that lists changed
    lines with explicit line numbers. This reduces token usage by making
    feedback more precise and reducing context-inference work.

    Args:
        diff_text: Unified diff output

    Returns:
        Formatted line-anchored feedback string suitable for inclusion
        in an LLM prompt.
    """
    anchored_diffs = _parse_unified_diff(diff_text)

    if not anchored_diffs:
        return "(no changes)"

    sections = []
    for diff in anchored_diffs:
        file_section = [f"**File: {diff.file_path}**"]

        if not diff.anchors:
            file_section.append("  (no changed lines extracted)")
            if diff.truncated:
                file_section.append("  ⚠️ (diff was truncated)")
            sections.append("\n".join(file_section))
            continue

        for anchor in diff.anchors:
            status_icon = "+" if anchor.context == "added" else "-"
            file_section.append(f"  L{anchor.line_number}: {status_icon} {anchor.content[:80]}")
            if anchor.note:
                file_section.append(f"      → {anchor.note}")

        if diff.truncated:
            file_section.append("  ⚠️ (diff was truncated; additional changes not shown)")

        sections.append("\n".join(file_section))

    return "\n\n".join(sections)
