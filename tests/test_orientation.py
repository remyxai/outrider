"""Tests for the repo-orientation pass that pre-reads target-repo conventions.

The orientation pass writes `ORIENTATION.md` into the agent's bundle so the
coding agent can adhere to the target repo's conventions (contributor guides,
PR template, recent merged PRs, lint/type config, nearby files/tests, detected
verification stack) without re-exploring those files itself.

Covers:
  - Each `_orient_*` helper reads its inputs correctly + returns "" when absent
  - `_detect_verification_stack` returns the right package manager + commands
    across uv / poetry / pip / Makefile-based / pyproject-based setups
  - `_collect_repo_orientation` assembles a full markdown block when any input
    is present and returns "" when all inputs are absent (graceful skip)

Run with: pytest tests/test_orientation.py -q
"""
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Target  # noqa: E402


# ─── _orient_contributor_guides ───────────────────────────────────────────


def test_contributor_guides_reads_all_three(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# Claude Guide\nUse the verification stack.\n")
    (tmp_path / "AGENTS.md").write_text("# Agents Guide\nFollow these rules.\n")
    (tmp_path / "CONTRIBUTING.md").write_text("# Contributing\nWelcome contributors.\n")

    body = run._orient_contributor_guides(tmp_path)

    assert "`CLAUDE.md`" in body
    assert "Use the verification stack." in body
    assert "`AGENTS.md`" in body
    assert "Follow these rules." in body
    assert "`CONTRIBUTING.md`" in body
    assert "Welcome contributors." in body


def test_contributor_guides_returns_empty_when_none_present(tmp_path: Path) -> None:
    body = run._orient_contributor_guides(tmp_path)
    assert body == ""


def test_contributor_guides_truncates_at_cap(tmp_path: Path) -> None:
    long_body = "X" * 10_000
    (tmp_path / "CLAUDE.md").write_text(long_body)

    body = run._orient_contributor_guides(tmp_path, cap=500)

    assert "…[truncated]" in body
    # The truncated body should be roughly the cap, not the full 10K.
    assert len(body) < 2000


# ─── _orient_pr_template ───────────────────────────────────────────────────


def test_pr_template_from_directory(tmp_path: Path) -> None:
    tmpl_dir = tmp_path / ".github" / "PULL_REQUEST_TEMPLATE"
    tmpl_dir.mkdir(parents=True)
    (tmpl_dir / "pull_request_template.md").write_text(
        "### Summary\n<short summary>\n\n### Test plan\n<how tested>\n"
    )

    body = run._orient_pr_template(tmp_path)

    assert ".github/PULL_REQUEST_TEMPLATE/pull_request_template.md" in body
    assert "### Summary" in body
    assert "### Test plan" in body


def test_pr_template_from_root(tmp_path: Path) -> None:
    gh_dir = tmp_path / ".github"
    gh_dir.mkdir()
    (gh_dir / "pull_request_template.md").write_text("## Description\nDescribe.\n")

    body = run._orient_pr_template(tmp_path)

    assert ".github/pull_request_template.md" in body
    assert "## Description" in body


def test_pr_template_returns_empty_when_absent(tmp_path: Path) -> None:
    body = run._orient_pr_template(tmp_path)
    assert body == ""


# ─── _orient_tooling_config ────────────────────────────────────────────────


def test_tooling_config_extracts_tool_sections(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1"\n\n'
        '[tool.ruff]\nline-length = 100\nselect = ["E", "F"]\n\n'
        '[tool.mypy]\nstrict = true\n\n'
        '[build-system]\nrequires = ["setuptools"]\n'
    )

    body = run._orient_tooling_config(tmp_path)

    # Should include [tool.X] sections
    assert "[tool.ruff]" in body
    assert "line-length = 100" in body
    assert "[tool.mypy]" in body
    assert "strict = true" in body
    # Should NOT include unrelated sections
    assert "[project]" not in body
    assert "[build-system]" not in body


def test_tooling_config_includes_standalone_configs(tmp_path: Path) -> None:
    (tmp_path / ".ruff.toml").write_text('line-length = 88\n')
    (tmp_path / "mypy.ini").write_text("[mypy]\nstrict = True\n")

    body = run._orient_tooling_config(tmp_path)

    assert "`.ruff.toml`" in body
    assert "line-length = 88" in body
    assert "`mypy.ini`" in body
    assert "strict = True" in body


def test_tooling_config_makefile_targets(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text(
        "format:\n\truff format\n\n"
        "lint:\n\truff check\n\n"
        "typecheck:\n\tmypy .\n\n"
        "tests:\n\tpytest\n\n"
        "unrelated_target:\n\techo unrelated\n"
    )

    body = run._orient_tooling_config(tmp_path)

    assert "format:" in body
    assert "lint:" in body
    assert "typecheck:" in body
    assert "tests:" in body
    # Truly-unrelated targets should be filtered out (heuristic by keyword)
    assert "unrelated_target" not in body


def test_tooling_config_returns_empty_when_no_configs(tmp_path: Path) -> None:
    body = run._orient_tooling_config(tmp_path)
    assert body == ""


# ─── _detect_verification_stack ───────────────────────────────────────────


def test_detect_uv_from_lockfile(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("# uv lock file")
    pkg, _ = run._detect_verification_stack(tmp_path)
    assert pkg == "uv"


def test_detect_poetry_from_lockfile(tmp_path: Path) -> None:
    (tmp_path / "poetry.lock").write_text("# poetry lock file")
    pkg, _ = run._detect_verification_stack(tmp_path)
    assert pkg == "poetry"


def test_detect_pip_pyproject_fallback(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[build-system]\nrequires = ["setuptools"]\n')
    pkg, _ = run._detect_verification_stack(tmp_path)
    assert pkg == "pip+pyproject"


def test_detect_commands_from_makefile(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text(
        "format:\n\truff format\n\nlint:\n\truff check\n\n"
        "typecheck:\n\tmypy .\n\ntests:\n\tpytest\n"
    )
    _, commands = run._detect_verification_stack(tmp_path)
    assert "make format" in commands
    assert "make lint" in commands
    assert "make typecheck" in commands
    assert "make tests" in commands


def test_detect_commands_falls_back_to_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\nline-length = 100\n\n'
        '[tool.mypy]\nstrict = true\n\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    _, commands = run._detect_verification_stack(tmp_path)
    # Should detect ruff + mypy + pytest from the tool sections
    assert any("ruff" in c for c in commands)
    assert any("mypy" in c for c in commands)
    assert any("pytest" in c for c in commands)


def test_detect_commands_falls_back_to_tox(tmp_path: Path) -> None:
    (tmp_path / "tox.ini").write_text("[tox]\nenvlist = py310\n")
    _, commands = run._detect_verification_stack(tmp_path)
    assert "tox" in commands


def test_detect_no_signals_returns_empty_command_list(tmp_path: Path) -> None:
    pkg, commands = run._detect_verification_stack(tmp_path)
    assert pkg == "pip"
    assert commands == []


# ─── _orient_nearby_files ──────────────────────────────────────────────────


def test_nearby_files_lists_modules_with_docstrings(tmp_path: Path) -> None:
    pkg = tmp_path / "demo_pkg"
    pkg.mkdir()
    (pkg / "first.py").write_text('"""First module — does the first thing."""\nimport os\n')
    (pkg / "second.py").write_text('"""Second module — does the second thing."""\n')
    (pkg / "no_doc.py").write_text("import os\n")

    body = run._orient_nearby_files(tmp_path, "demo_pkg")

    assert "demo_pkg/first.py" in body
    assert "First module — does the first thing." in body
    assert "demo_pkg/second.py" in body
    assert "demo_pkg/no_doc.py" in body


def test_nearby_files_returns_empty_when_pkg_missing(tmp_path: Path) -> None:
    body = run._orient_nearby_files(tmp_path, "nonexistent_pkg")
    assert body == ""


# ─── _orient_nearby_tests ──────────────────────────────────────────────────


def test_nearby_tests_lists_files_and_samples_one(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_alpha.py").write_text(
        '"""Alpha tests."""\nimport pytest\n\ndef test_one():\n    assert True\n'
    )
    (tests / "test_beta.py").write_text(
        '"""Beta tests."""\ndef test_two():\n    assert 1 + 1 == 2\n'
    )

    body = run._orient_nearby_tests(tmp_path)

    assert "tests/test_alpha.py" in body
    assert "tests/test_beta.py" in body
    # The first file should be included as a sample
    assert "import pytest" in body or "def test_one" in body


def test_nearby_tests_returns_empty_when_no_tests_dir(tmp_path: Path) -> None:
    body = run._orient_nearby_tests(tmp_path)
    assert body == ""


# ─── _collect_repo_orientation ─────────────────────────────────────────────


def _target(repo: str = "owner/repo") -> Target:
    return Target(repo=repo, interest_id="iid")


def test_collect_returns_empty_when_repo_has_no_signals(tmp_path: Path) -> None:
    """A blank repo with no contributor guides, no pyproject, no tests,
    and no recent PRs (mocked) returns an empty string so the caller
    can skip writing ORIENTATION.md entirely."""
    with patch.object(run, "_orient_recent_merged_prs", return_value=""):
        body = run._collect_repo_orientation(tmp_path, _target(""), "demo_pkg")
    assert body == ""


def test_collect_assembles_full_orientation_block(tmp_path: Path) -> None:
    """A realistic-shaped repo with multiple convention signals produces
    the full orientation markdown block with each section rendered."""
    # Contributor guide
    (tmp_path / "CLAUDE.md").write_text("# Claude Guide\nverify with `make tests`\n")
    # PR template
    tmpl_dir = tmp_path / ".github" / "PULL_REQUEST_TEMPLATE"
    tmpl_dir.mkdir(parents=True)
    (tmpl_dir / "pull_request_template.md").write_text("### Summary\n### Test plan\n")
    # Tooling
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\nline-length = 100\n\n[tool.mypy]\nstrict = true\n'
    )
    # Makefile
    (tmp_path / "Makefile").write_text(
        "format:\n\truff format\nlint:\n\truff check\ntests:\n\tpytest\n"
    )
    # Package dir with a module
    pkg = tmp_path / "demo_pkg"
    pkg.mkdir()
    (pkg / "core.py").write_text('"""Core demo module."""\n')
    # Tests dir
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text('"""Tests for core."""\ndef test_x():\n    pass\n')

    with patch.object(run, "_orient_recent_merged_prs", return_value="- #1 (by @x): docs: fix typo"):
        body = run._collect_repo_orientation(tmp_path, _target(), "demo_pkg")

    # Each section should be present
    assert "Contributor guides" in body
    assert "PR template" in body
    assert "Recent merged PRs" in body
    assert "Tooling and lint/type config" in body
    assert "Detected verification stack" in body
    assert "Existing modules in `demo_pkg/`" in body
    assert "Existing tests" in body
    # Specific content should propagate through
    assert "verify with `make tests`" in body
    assert "### Summary" in body
    assert "line-length = 100" in body
    assert "make format" in body  # from verification stack detection
    assert "demo_pkg/core.py" in body
    assert "tests/test_core.py" in body
    assert "docs: fix typo" in body


def test_collect_omits_missing_sections(tmp_path: Path) -> None:
    """When some sections are empty, the resulting block has only the
    populated subsections — graceful partial degradation."""
    # Only a contributor guide; no pyproject, no tests, no package
    (tmp_path / "CONTRIBUTING.md").write_text("# Contributing\nbe nice\n")
    with patch.object(run, "_orient_recent_merged_prs", return_value=""):
        body = run._collect_repo_orientation(tmp_path, _target(""), "demo_pkg")

    assert "Contributor guides" in body
    assert "be nice" in body
    # Missing sections should not appear as section headers
    assert "## PR template" not in body
    assert "## Tooling and lint/type config" not in body
    assert "## Existing modules" not in body
