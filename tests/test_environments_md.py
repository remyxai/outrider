"""Tests for the workflow-authored ENVIRONMENTS.md convention.

The workflow author can leave an ENVIRONMENTS.md (or ENVIRONMENT.md) file
at the workflow workspace root (`$GITHUB_WORKSPACE`) or the target
workdir. Outrider reads it, strips OKF/YAML frontmatter, caps size, and
writes the body into the recommendation bundle as ENVIRONMENT.md so the
agent sees workflow-attached tooling as first-class context alongside
SPEC/PAPER/CONTEXT/GUARDRAILS.

Run with: pytest tests/test_environments_md.py -q
"""
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# ─── _load_environments_md: file lookup, frontmatter stripping, size cap ──


def test_returns_empty_when_no_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    assert run._load_environments_md(tmp_path) == ""


def test_reads_environments_md_from_workdir(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    (tmp_path / "ENVIRONMENTS.md").write_text(
        "# my env\n\nccc is available for AST search."
    )
    body = run._load_environments_md(tmp_path)
    assert "# my env" in body
    assert "ccc is available" in body


def test_strips_okf_yaml_frontmatter(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    (tmp_path / "ENVIRONMENTS.md").write_text(textwrap.dedent("""\
        ---
        type: Workflow Environment
        title: cocoindex available
        description: cocoindex-code AST search installed
        resource: https://example.com/workflow.yml
        tags: [outrider, cocoindex]
        timestamp: 2026-07-01T02:00:00Z
        ---

        # Tooling

        AST search via `ccc`. Prefer over Read for large modules.
        """))
    body = run._load_environments_md(tmp_path)
    # Frontmatter (metadata the agent doesn't need) is stripped
    assert "type: Workflow Environment" not in body
    assert "timestamp:" not in body
    # Markdown body is preserved
    assert "# Tooling" in body
    assert "AST search via `ccc`" in body


def test_no_frontmatter_body_still_returned(tmp_path, monkeypatch):
    """Files without frontmatter are returned as-is; the convention is
    encouraged but not required."""
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    (tmp_path / "ENVIRONMENTS.md").write_text("plain content, no frontmatter\n")
    assert run._load_environments_md(tmp_path) == "plain content, no frontmatter"


def test_size_cap_truncates_over_max(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    big = "x" * 8000
    (tmp_path / "ENVIRONMENTS.md").write_text(big)
    body = run._load_environments_md(tmp_path, max_bytes=1024)
    assert len(body.encode("utf-8")) <= 1024 + len("\n\n... (truncated at 1024 bytes)")
    assert "truncated" in body


def test_singular_environment_md_also_works(tmp_path, monkeypatch):
    """ENVIRONMENT.md (singular) is a fallback for the same convention;
    matches CONTRIBUTING.md / LICENSE.md naming."""
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    (tmp_path / "ENVIRONMENT.md").write_text("singular works too")
    assert run._load_environments_md(tmp_path) == "singular works too"


def test_workspace_takes_precedence_over_workdir(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "ENVIRONMENTS.md").write_text("from workspace")
    (tmp_path / "ENVIRONMENTS.md").write_text("from workdir")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(workspace))
    body = run._load_environments_md(tmp_path)
    assert body == "from workspace"


def test_plural_takes_precedence_over_singular(tmp_path, monkeypatch):
    """When both are present, ENVIRONMENTS.md (the primary spelling)
    wins over ENVIRONMENT.md."""
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    (tmp_path / "ENVIRONMENTS.md").write_text("plural")
    (tmp_path / "ENVIRONMENT.md").write_text("singular")
    assert run._load_environments_md(tmp_path) == "plural"


def test_empty_body_after_frontmatter_returns_empty(tmp_path, monkeypatch):
    """A file that's all frontmatter and no body doesn't produce a bundle
    entry — treat it as if the workflow didn't provide any environment
    hints."""
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    (tmp_path / "ENVIRONMENTS.md").write_text("---\ntype: x\n---\n\n\n")
    assert run._load_environments_md(tmp_path) == ""
