"""Tests for canonical agent-instruction-file discovery in the orientation pass.

Outrider's orientation pass injects a target repo's instruction files into the
Claude Code prompt. Following *Toward Instructions-as-Code* (arXiv:2606.13449)
— which finds that the canonical set of instruction files, and especially
their length/section structure, correlates with agentic-PR merge rate — the
canonical set is completed (`.cursorrules`, `.github/copilot-instructions.md`)
and each file is annotated with a structural-signal line.

Covers:
  - `instruction_files` discovers/renders the full canonical set in order
  - `section_count` / `structural_summary` compute the paper's structure proxy
  - The wiring: `run._orient_contributor_guides` (the existing call site) and
    `run._collect_repo_orientation` surface the newly-added canonical files

Run with: pytest tests/test_instruction_files.py -q
"""
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import instruction_files  # noqa: E402
import run  # noqa: E402
from run import Target  # noqa: E402


# ─── instruction_files module ──────────────────────────────────────────────


def test_discover_returns_canonical_files_in_precedence_order(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# Claude\n")
    (tmp_path / "AGENTS.md").write_text("# Agents\n")
    (tmp_path / ".cursorrules").write_text("be terse\n")
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "copilot-instructions.md").write_text("# Copilot\n")
    (tmp_path / "CONTRIBUTING.md").write_text("# Contributing\n")
    (tmp_path / "CONTEXT.md").write_text("# Context\n")

    names = [
        p.relative_to(tmp_path).as_posix()
        for p in instruction_files.discover_instruction_files(tmp_path)
    ]

    assert names == [
        "CLAUDE.md",
        "AGENTS.md",
        ".cursorrules",
        ".github/copilot-instructions.md",
        "CONTRIBUTING.md",
        "CONTEXT.md",
    ]


def test_discover_skips_absent_files(tmp_path: Path) -> None:
    (tmp_path / ".cursorrules").write_text("only this\n")
    found = instruction_files.discover_instruction_files(tmp_path)
    assert [p.name for p in found] == [".cursorrules"]


def test_section_count_counts_atx_headings() -> None:
    body = "# Top\nintro\n## Sub A\nx\n### Sub-sub\ny\nnot # a heading\n## Sub B\n"
    assert instruction_files.section_count(body) == 4


def test_structural_summary_reports_length_and_sections() -> None:
    body = "# Title\n\n## Setup\nrun tests\n\n## Style\nbe terse\n"
    summary = instruction_files.structural_summary(body)
    assert "3 sections" in summary
    assert "lines" in summary


def test_structural_summary_empty_body_is_blank() -> None:
    assert instruction_files.structural_summary("   \n  ") == ""


def test_render_truncates_at_cap(tmp_path: Path) -> None:
    (tmp_path / ".cursorrules").write_text("Z" * 10_000)
    out = instruction_files.render_instruction_files(tmp_path, cap=400)
    assert "…[truncated]" in out
    assert len(out) < 1500


def test_render_empty_when_no_canonical_files(tmp_path: Path) -> None:
    assert instruction_files.render_instruction_files(tmp_path) == ""


# ─── wiring: existing run.py call sites pick up the new canonical files ─────


def test_orient_contributor_guides_reads_cursorrules_and_copilot(tmp_path: Path) -> None:
    """The existing call site now surfaces the completed canonical set."""
    (tmp_path / ".cursorrules").write_text("# Cursor rules\nPrefer small diffs.\n")
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "copilot-instructions.md").write_text(
        "# Copilot instructions\nAlways add a test.\n"
    )

    body = run._orient_contributor_guides(tmp_path)

    assert "`.cursorrules`" in body
    assert "Prefer small diffs." in body
    assert "`.github/copilot-instructions.md`" in body
    assert "Always add a test." in body


def test_orient_contributor_guides_annotates_structure(tmp_path: Path) -> None:
    """Each instruction file carries the paper's structural-signal line."""
    (tmp_path / "CLAUDE.md").write_text(
        "# Guide\n\n## Build\nmake\n\n## Test\npytest\n"
    )

    body = run._orient_contributor_guides(tmp_path)

    assert "`CLAUDE.md`" in body
    assert "3 sections" in body  # one #, two ## headings


def test_collect_repo_orientation_surfaces_copilot_instructions(tmp_path: Path) -> None:
    """End-to-end through the orientation assembler: a repo whose only
    instruction file is .github/copilot-instructions.md still gets it into
    the orientation block."""
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "copilot-instructions.md").write_text(
        "# Copilot\nFollow the house style.\n"
    )

    with patch.object(run, "_orient_recent_merged_prs", return_value=""):
        body = run._collect_repo_orientation(
            tmp_path, Target(repo="", interest_id="iid"), "demo_pkg"
        )

    assert "Contributor guides" in body
    assert "`.github/copilot-instructions.md`" in body
    assert "Follow the house style." in body
