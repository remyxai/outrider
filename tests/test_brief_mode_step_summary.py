"""Brief / branch-mode results must render usefully in the step summary.

Before this, brief-mode statuses had no emoji entry (all fell through to
ℹ️), the branch URL was never printed at all (only ``pr_url`` / ``issue_url``
were), and un-pushable paths stripped before the push weren't surfaced.

Pins:
  - ``branch_url`` is rendered with the "you open the PR" nudge.
  - ``dropped_blocked_paths`` get a visible ⚠️ section.
  - ``brief_failed`` renders as ❌ (red), not the ℹ️ fallthrough.

Run with: pytest tests/test_brief_mode_step_summary.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


def _render(result, tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    run._write_step_summary(result)
    return summary.read_text()


def test_branch_url_and_pr_nudge_rendered(tmp_path, monkeypatch):
    out = _render(
        {
            "status": "branch_pushed_no_pr",
            "branch": "feature/chebyshev-cache",
            "branch_url":
                "https://github.com/o/r/tree/feature/chebyshev-cache",
        },
        tmp_path, monkeypatch,
    )
    assert "🟢" in out
    assert "https://github.com/o/r/tree/feature/chebyshev-cache" in out
    assert "gh pr create --head feature/chebyshev-cache" in out


def test_dropped_blocked_paths_section(tmp_path, monkeypatch):
    out = _render(
        {
            "status": "branch_pushed_no_pr",
            "branch_url": "https://github.com/o/r/tree/b",
            "dropped_blocked_paths": [".github/workflows/claude_review.yml"],
        },
        tmp_path, monkeypatch,
    )
    assert "Un-pushable paths dropped" in out
    assert ".github/workflows/claude_review.yml" in out


def test_brief_failed_renders_red(tmp_path, monkeypatch):
    out = _render(
        {
            "status": "brief_failed",
            "error": "git push rejected: workflows permission",
        },
        tmp_path, monkeypatch,
    )
    assert "❌" in out
    assert "git push rejected" in out


def test_branch_name_derived_from_url_when_absent(tmp_path, monkeypatch):
    # No explicit "branch" key → derive it from the /tree/ URL suffix.
    out = _render(
        {
            "status": "branch_pushed_no_pr",
            "branch_url": "https://github.com/o/r/tree/feature/afu-objective",
        },
        tmp_path, monkeypatch,
    )
    assert "gh pr create --head feature/afu-objective" in out
