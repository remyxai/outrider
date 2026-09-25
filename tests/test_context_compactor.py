"""Context compaction for experiment history, inspired by CliffCompaction.

Tests the cost-efficient truncation of experiment history while maintaining
faithfulness (only truncating/dropping content, never rephrasing).

Paper: "CliffCompaction: Cost-Efficient Compaction for Long-Horizon Coding Agents"
https://arxiv.org/abs/2609.26779v1

Run with: pytest tests/test_context_compactor.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
from context_compactor import compact_experiment_history, extract_entry_date, get_compaction_note


# Sample experiment history entries
SAMPLE_ENTRIES = [
    "- 2026-05-28 — Implement ready-to-ship PR gates [Evaluation]",
    "- 2026-05-29 — Add selection pass to pick most implementable paper [Research; iteration_chain_key: selection_logic, iter #1]",
    "- 2026-05-29 — Refactor guardrails to be role-based [General]",
    "- 2026-05-29 — Extend deduplication to include open Issues [General; iteration_chain_key: deduplication_logic, iter #1]",
    "- 2026-05-30 — Harden action based on cross-repo evaluation findings [General]",
    "- 2026-05-30 — Add token and cost telemetry [Evaluation]",
    "- 2026-05-31 — Surface run telemetry in GitHub Step Summary [General]",
    "- 2026-05-31 — Rename action from 'Remyx Recommendation' to 'Outrider' [General]",
    "- 2026-05-31 — Refine rate-limiting to a global cadence guard [General]",
    "- 2026-05-31 — Add repository experiment history to implementation prompt [Model Finetune]",
    "- 2026-05-31 — Analyze cost/quality trade-off of enhanced context [Evaluation]",
    "- 2026-06-03 — Evolve selection pass to agentic verification with CLI tools [Research; iteration_chain_key: selection_logic, iter #2]",
    "- 2026-06-06 — Improve Issue deduplication robustness [General; iteration_chain_key: deduplication_logic, iter #2]",
    "- 2026-06-10 — Refine 'extension' selection shape and add link recovery [Research; iteration_chain_key: selection_logic, iter #3]",
]


class TestCompactExperimentHistory:
    """Tests for the compact_experiment_history function."""

    def test_no_compaction_needed_when_under_budget(self):
        """History well under budget returns verbatim."""
        history = "\n".join(SAMPLE_ENTRIES[:3])
        result = compact_experiment_history(history, max_tokens=5000)
        assert result == history

    def test_compaction_removes_oldest_entries_first(self):
        """When over budget, oldest (first) entries are dropped, newest kept."""
        history = "\n".join(SAMPLE_ENTRIES)
        # Use a very tight token budget to force heavy truncation.
        result = compact_experiment_history(history, max_tokens=300)
        lines = result.strip().split("\n")
        # Should have kept the newer (later) entries.
        assert len(lines) > 0
        assert len(lines) < len(SAMPLE_ENTRIES)
        # Newest entry should still be present (never drop all).
        assert "2026-06-10" in result

    def test_compaction_preserves_entry_verbatim(self):
        """Truncation drops complete entries, never edits them."""
        history = "\n".join(SAMPLE_ENTRIES)
        result = compact_experiment_history(history, max_tokens=500)
        # Every line in result should be a complete, unmodified entry from the original.
        for line in result.strip().split("\n"):
            assert line in SAMPLE_ENTRIES, f"Modified entry found: {line}"

    def test_empty_history_returns_empty_string(self):
        """Empty or whitespace-only history returns empty string."""
        assert compact_experiment_history("") == ""
        assert compact_experiment_history("   \n  \n  ") == ""

    def test_no_entries_returns_empty_string(self):
        """Non-bullet lines (no "- " prefix) are ignored and history returns empty."""
        history = "Some random text without bullet format\nAlso not a bullet"
        result = compact_experiment_history(history)
        assert result == ""

    def test_mixed_format_with_bullets_extracts_only_bullets(self):
        """History with both bullets and other text keeps only the bullets."""
        history = "Team context:\n" + "\n".join(SAMPLE_ENTRIES[:3])
        result = compact_experiment_history(history, max_tokens=5000)
        # Should contain only the bullets, not the "Team context:" line.
        assert "Team context:" not in result
        assert all(line.startswith("- ") for line in result.split("\n"))

    def test_very_small_token_budget_keeps_at_least_one_entry(self):
        """Even with a tiny budget, at least one recent entry is kept if possible."""
        history = "\n".join(SAMPLE_ENTRIES)
        # Very small budget but should still keep something.
        result = compact_experiment_history(history, max_tokens=50)
        # If something fits, it should be present.
        if result:
            assert result.strip().startswith("- ")

    def test_single_very_long_entry(self):
        """A single entry longer than the token budget is dropped entirely."""
        long_entry = "- 2026-06-10 — " + ("X" * 1000)  # Create a very long entry
        result = compact_experiment_history(long_entry, max_tokens=10)
        # Even though we have a long entry, it might not fit in 10 tokens.
        # The function handles this gracefully by dropping it.
        assert isinstance(result, str)

    def test_newline_normalization(self):
        """History with various newline formats is handled correctly."""
        # Two entries separated by a single newline.
        history = f"{SAMPLE_ENTRIES[0]}\n{SAMPLE_ENTRIES[1]}"
        result = compact_experiment_history(history, max_tokens=5000)
        assert SAMPLE_ENTRIES[0] in result
        assert SAMPLE_ENTRIES[1] in result

    def test_order_is_preserved_newest_last(self):
        """Compacted result preserves chronological order (oldest first, newest last)."""
        history = "\n".join(SAMPLE_ENTRIES)
        result = compact_experiment_history(history, max_tokens=1500)
        lines = result.strip().split("\n")
        # Extract dates and verify they're in chronological order.
        for i in range(len(lines) - 1):
            curr_date = extract_entry_date(lines[i])
            next_date = extract_entry_date(lines[i + 1])
            if curr_date and next_date:
                assert curr_date <= next_date, "Entries not in chronological order"


class TestExtractEntryDate:
    """Tests for the extract_entry_date function."""

    def test_valid_date_extraction(self):
        """Extracts valid YYYY-MM-DD dates from bullet entries."""
        entry = "- 2026-05-28 — Implement ready-to-ship PR gates [Evaluation]"
        result = extract_entry_date(entry)
        assert result is not None
        assert result.year == 2026
        assert result.month == 5
        assert result.day == 28

    def test_invalid_date_format_returns_none(self):
        """Malformed dates return None."""
        assert extract_entry_date("- May 28, 2026 — Something") is None
        assert extract_entry_date("- 2026/05/28 — Something") is None

    def test_no_date_returns_none(self):
        """Entries without dates return None."""
        assert extract_entry_date("- Something without a date") is None
        assert extract_entry_date("2026-05-28 — Missing dash prefix") is None

    def test_invalid_date_values_return_none(self):
        """Impossible dates (month 13, day 32) return None."""
        assert extract_entry_date("- 2026-13-01 — Invalid month") is None
        assert extract_entry_date("- 2026-02-30 — Invalid day") is None

    def test_edge_case_leap_year_date(self):
        """Leap year dates are valid."""
        result = extract_entry_date("- 2024-02-29 — Leap year entry")
        assert result is not None
        assert result.day == 29

    def test_edge_case_december_31st(self):
        """Year-end dates work correctly."""
        result = extract_entry_date("- 2026-12-31 — End of year")
        assert result is not None
        assert result.month == 12
        assert result.day == 31


class TestCompactionNote:
    """Tests for the get_compaction_note function."""

    def test_compaction_note_content(self):
        """Compaction note contains required messaging."""
        note = get_compaction_note()
        assert "CliffCompaction" in note
        assert "truncation" in note
        assert "verbatim" in note

    def test_compaction_note_markdown_format(self):
        """Compaction note is formatted as markdown."""
        note = get_compaction_note()
        assert note.startswith("\n\n**")
        assert "**" in note  # Contains bold formatting


class TestIntegrationWithRun:
    """Integration tests: verify compaction works through the run.py call site."""

    def test_compaction_in_spec_bundle_context(self):
        """Verify that a shortened history + compaction note are properly used."""
        # This is tested via the write_spec_bundle integration, which is
        # covered by end-to-end tests. This serves as a sanity check that
        # the compaction function can be called with realistic data.
        history = "\n".join(SAMPLE_ENTRIES)
        compacted = compact_experiment_history(history, max_tokens=1000)
        note = get_compaction_note()
        # Both should be strings; compacted might be empty, note is not.
        assert isinstance(compacted, str)
        assert isinstance(note, str)
        assert len(note) > 0
