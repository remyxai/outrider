"""Context compaction for experiment history, inspired by CliffCompaction.

This module implements selective truncation of repository experiment history
to reduce token cost while maintaining semantic faithfulness. The core principle
is that truncation only drops content, never rephrasing or rewriting. This keeps
the compacted history faithful to the original, preventing context drift.

Paper: "CliffCompaction: Cost-Efficient Compaction for Long-Horizon Coding Agents"
https://arxiv.org/abs/2609.26779v1

The implementation applies CliffCompaction's truncation strategy to Outrider's
context window by selectively removing older or less relevant experiment entries
based on age and content characteristics, while preserving the integrity of what
remains.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional


def compact_experiment_history(
    experiment_history: str,
    max_tokens: int = 2000,
    tokens_per_char: float = 0.25,
) -> str:
    """Truncate experiment history to fit within a token budget.

    Args:
        experiment_history: Markdown bullet list of experiments (format: "- YYYY-MM-DD — description [metadata]").
        max_tokens: Target token limit for the compacted history.
        tokens_per_char: Rough conversion factor (Claude uses ~4 chars per token average).

    Returns:
        Truncated experiment_history with only complete bullet entries.
        Entries are removed from the oldest backward; never rephrased.
        Returns empty string if all entries are removed.
    """
    if not experiment_history or not experiment_history.strip():
        return ""

    # Parse bullets: each line is a complete entry. We drop them wholesale, never edit them.
    lines = experiment_history.strip().split("\n")
    entries = [line for line in lines if line.startswith("- ")]

    if not entries:
        return ""

    # Estimate tokens; conservative approximation.
    max_chars = int(max_tokens / tokens_per_char)

    # If already under budget, return as-is.
    current_len = sum(len(e) for e in entries) + len(entries) - 1  # +1 for \n between entries
    if current_len <= max_chars:
        return "\n".join(entries)

    # Drop entries from the oldest (first in list) backward until we fit.
    # This preserves recency and the most recent decisions.
    compacted = []
    for entry in reversed(entries):
        test_len = sum(len(e) for e in compacted) + len(entry) + len(compacted)
        if test_len <= max_chars:
            compacted.insert(0, entry)
        # else: entry is too old to fit; drop it.

    return "\n".join(compacted) if compacted else ""


def extract_entry_date(entry: str) -> Optional[dt.datetime]:
    """Extract YYYY-MM-DD date from a bullet entry.

    Args:
        entry: A single experiment bullet line (e.g., "- 2026-05-28 — description").

    Returns:
        Parsed datetime, or None if the date cannot be extracted.
    """
    # Format: "- YYYY-MM-DD — ..."
    match = re.match(r"^- (\d{4})-(\d{2})-(\d{2})\s+", entry)
    if match:
        try:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
            return dt.datetime(year, month, day)
        except ValueError:
            return None
    return None


def get_compaction_note() -> str:
    """Generate an inline note explaining that compaction was applied.

    This is appended to the CONTEXT.md footer when truncation occurs,
    so readers understand the history was selectively truncated, not lost.
    """
    return (
        "\n\n**Note:** Experiment history has been compacted using "
        "cost-efficient truncation (inspired by CliffCompaction) to fit "
        "within token budgets. Older entries were removed; remaining entries "
        "are presented verbatim without rephrasing."
    )
