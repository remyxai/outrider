"""Tests for the v1.4.9 step-summary selection_reasoning section:

  - Renders when selection_reasoning is non-empty
  - Renders OPEN (no collapse) for skipped_by_selection_verification —
    the field is the only meaningful payload for that outcome
  - Renders COLLAPSED for outcomes with a paper / PR / Issue link, so
    the cost line stays above the fold
  - Omits the section when the field is empty / missing
  - Omits the section when the field is the "(selection pass
    unavailable — used highest-relevance candidate as fallback)"
    placeholder — that string is a non-signal, not a real explanation
  - Renders BEFORE the per-paper "Why this paper" section so the
    selection narrative reads first

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


def _capture(result, tmp_path, monkeypatch) -> str:
    """Drive _write_step_summary and return its rendered Markdown."""
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    run._write_step_summary(result)
    return summary_file.read_text()


def test_renders_when_selection_reasoning_present(tmp_path, monkeypatch):
    out = _capture({
        "status": "pr_opened_draft",
        "paper": "Sample Paper",
        "arxiv": "2601.00001",
        "selection_reasoning": "Selected because it cleanly slots into vqa/depth.py.",
        "reasoning": "Per-paper interest reasoning.",
    }, tmp_path, monkeypatch)
    assert "Why this selection" in out
    assert "cleanly slots into vqa/depth.py" in out


def test_renders_open_for_skipped_by_selection_verification(tmp_path, monkeypatch):
    """The verification-skip outcome has no paper / no PR / no Issue —
    selection_reasoning is the only signal. Render it open so the
    maintainer sees it without expanding."""
    out = _capture({
        "status": "skipped_by_selection_verification",
        "selection_reasoning": (
            "Every candidate failed verification — depth slot blocked "
            "by closed Issue #91, no other contract-anchored fit."
        ),
    }, tmp_path, monkeypatch)
    assert "<details open>" in out
    # The narrative is in the file (not behind a collapsed summary)
    assert "Every candidate failed verification" in out


def test_renders_collapsed_for_pr_or_issue_outcomes(tmp_path, monkeypatch):
    """For successful pr_opened / issue_opened outcomes, the section
    collapses so the cost line stays above the fold. The maintainer
    can expand for the narrative if they want it."""
    out = _capture({
        "status": "pr_opened_draft",
        "paper": "Sample Paper",
        "arxiv": "2601.00001",
        "selection_reasoning": "Picked over 9 alternatives because clean call site.",
    }, tmp_path, monkeypatch)
    # Collapsed details — `<details>` without `open` attribute
    assert "<details><summary>Why this selection</summary>" in out
    # Content still in the file (browsers expand on click)
    assert "Picked over 9 alternatives" in out


def test_omits_section_when_field_missing(tmp_path, monkeypatch):
    out = _capture({
        "status": "pr_opened_draft",
        "paper": "Sample Paper",
        "arxiv": "2601.00001",
    }, tmp_path, monkeypatch)
    assert "Why this selection" not in out


def test_omits_section_when_field_empty_string(tmp_path, monkeypatch):
    out = _capture({
        "status": "pr_opened_draft",
        "paper": "Sample Paper",
        "arxiv": "2601.00001",
        "selection_reasoning": "",
    }, tmp_path, monkeypatch)
    assert "Why this selection" not in out


def test_omits_section_when_field_is_whitespace_only(tmp_path, monkeypatch):
    out = _capture({
        "status": "pr_opened_draft",
        "paper": "x",
        "arxiv": "2601.00001",
        "selection_reasoning": "   \n  ",
    }, tmp_path, monkeypatch)
    assert "Why this selection" not in out


def test_omits_section_when_field_is_fallback_placeholder(tmp_path, monkeypatch):
    """The "(selection pass unavailable — used highest-relevance
    candidate as fallback)" string is a non-signal placeholder that
    other code paths inject when selection failed/timed out. Rendering
    it would be worse than rendering nothing — it draws attention to
    a non-explanation."""
    out = _capture({
        "status": "pr_opened_draft",
        "paper": "x",
        "arxiv": "2601.00001",
        "selection_reasoning": (
            "(selection pass unavailable — used highest-relevance "
            "candidate as fallback)"
        ),
    }, tmp_path, monkeypatch)
    assert "Why this selection" not in out


def test_renders_before_per_paper_reasoning(tmp_path, monkeypatch):
    """Section ordering: selection narrative is more decisive than the
    per-paper interest reasoning — render it first so the reader
    encounters it before the per-paper context."""
    out = _capture({
        "status": "pr_opened_draft",
        "paper": "x",
        "arxiv": "2601.00001",
        "selection_reasoning": "Selection narrative goes here.",
        "reasoning": "Per-paper interest reasoning goes here.",
    }, tmp_path, monkeypatch)
    assert out.index("Why this selection") < out.index("Why this paper")


def test_renders_above_cost_line(tmp_path, monkeypatch):
    """Cost line is the runtime-visible payload — but the selection
    narrative is the human-facing payload for skipped_by_selection_
    verification. Both must appear in the file; selection narrative
    appears above cost so the maintainer sees it first."""
    out = _capture({
        "status": "skipped_by_selection_verification",
        "selection_reasoning": "no actionable paper this run",
        "cost_usd": 1.23,
    }, tmp_path, monkeypatch)
    assert out.index("Why this selection") < out.index("Cost & tokens")


# ── claude_failed rendering ─────────────────────────────────────────────────


def test_step_summary_credit_balance_renders_topup_section(tmp_path, monkeypatch):
    out = _capture({
        "status": "claude_failed",
        "claude_log_tail": "ERROR: Credit balance is too low to make request",
        "claude_calls": 4,
    }, tmp_path, monkeypatch)
    assert "Anthropic credit balance exhausted" in out
    assert "console.anthropic.com/settings/billing" in out
    assert "4 Claude calls" in out


def test_step_summary_invalid_key_renders_secret_section(tmp_path, monkeypatch):
    out = _capture({
        "status": "claude_failed",
        "claude_log_tail": "401 Authentication error: invalid x-api-key",
    }, tmp_path, monkeypatch)
    assert "ANTHROPIC_API_KEY secret invalid" in out
    assert "console.anthropic.com/settings/keys" in out
    assert "gh secret set ANTHROPIC_API_KEY" in out


def test_step_summary_rate_limit_renders_no_action_section(tmp_path, monkeypatch):
    out = _capture({
        "status": "claude_failed",
        "claude_log_tail": "HTTP 429 Too Many Requests rate_limit_error",
    }, tmp_path, monkeypatch)
    assert "Rate limited" in out
    assert "no action needed" in out
    assert "next scheduled run will retry" in out


def test_step_summary_unknown_failure_renders_tail(tmp_path, monkeypatch):
    out = _capture({
        "status": "claude_failed",
        "claude_log_tail": "Unexpected error: model returned malformed json",
    }, tmp_path, monkeypatch)
    # Generic failures fall through to a collapsed details block with the tail.
    assert "Claude agent failure tail" in out
    assert "malformed json" in out
    # Specific-action sections should NOT fire.
    assert "Anthropic credit balance" not in out
    assert "ANTHROPIC_API_KEY secret invalid" not in out


def test_step_summary_succeeds_when_claude_log_tail_missing(tmp_path, monkeypatch):
    # claude_failed without a log tail shouldn't crash; just no extra block.
    out = _capture({
        "status": "claude_failed",
        "claude_calls": 0,
    }, tmp_path, monkeypatch)
    # Headline still renders even without the tail-derived action block.
    assert "claude_failed" in out
    # No specific-action section fires when there's no tail content.
    assert "Anthropic credit balance" not in out
    assert "ANTHROPIC_API_KEY secret invalid" not in out
    assert "Claude agent failure tail" not in out
