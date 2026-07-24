"""Tests for REVIEW.md discovery and orientation-block inclusion.

Covers the consumer half of the REVIEW.md convention: Outrider reads the
target repo's ``REVIEW.md`` (or ``.github/REVIEW.md``), threads it into
the orientation block ahead of the contributor-guide block, and falls
back silently when absent.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# --- _orient_review_conventions ---------------------------------------------

def test_review_root_file_is_read(tmp_path: Path) -> None:
    (tmp_path / "REVIEW.md").write_text("## Verdicts\n\n- approve when scoped\n")
    got = run._orient_review_conventions(tmp_path)
    assert "`REVIEW.md`" in got
    assert "approve when scoped" in got


def test_review_dotgithub_fallback(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "REVIEW.md").write_text("## Test bar\n\n- CI green\n")
    got = run._orient_review_conventions(tmp_path)
    assert "`.github/REVIEW.md`" in got
    assert "CI green" in got


def test_review_root_takes_precedence_over_dotgithub(tmp_path: Path) -> None:
    (tmp_path / "REVIEW.md").write_text("root wins\n")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "REVIEW.md").write_text("dotgithub loses\n")
    got = run._orient_review_conventions(tmp_path)
    assert "root wins" in got
    assert "dotgithub loses" not in got


def test_review_missing_returns_empty(tmp_path: Path) -> None:
    assert run._orient_review_conventions(tmp_path) == ""


def test_review_empty_file_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "REVIEW.md").write_text("   \n\n")
    assert run._orient_review_conventions(tmp_path) == ""


def test_review_truncated_at_cap(tmp_path: Path) -> None:
    body = "A" * 5000
    (tmp_path / "REVIEW.md").write_text(body)
    got = run._orient_review_conventions(tmp_path, cap=1000)
    # snippet cap + truncation marker
    assert "[truncated]" in got
    assert "A" * 1000 in got


# --- _collect_repo_orientation includes REVIEW.md block --------------------

def test_orientation_block_includes_review_section(tmp_path: Path) -> None:
    (tmp_path / "REVIEW.md").write_text("## Anti-patterns\n\n- brittle regexes\n")
    # Minimal Target stub — _collect_repo_orientation only reads .repo for
    # the recent-merged-PRs block, which returns "" when repo is empty.
    class _Target:
        repo = ""
    body = run._collect_repo_orientation(tmp_path, _Target(), "src")
    assert "Review conventions" in body
    assert "brittle regexes" in body


def test_orientation_block_omits_review_section_when_absent(tmp_path: Path) -> None:
    # Provide something else so the whole block isn't empty (which would
    # short-circuit _collect_repo_orientation to "").
    (tmp_path / "CONTRIBUTING.md").write_text("## How to contribute\n\nRun tests.\n")
    class _Target:
        repo = ""
    body = run._collect_repo_orientation(tmp_path, _Target(), "src")
    # The section HEADER should be absent; the how-to-use bullet mentioning
    # REVIEW.md is fine (agents ignore it when the block is empty).
    assert "## Review conventions (`REVIEW.md`)" not in body
