"""Tests for the implementation-diff preservation in downgrade-Issue
bodies.

When a downgrade fires after the coding agent wrote real code, the
diff Claude produced should land inside the Issue body so the
maintainer can review and apply it without re-deriving from the paper.

Covers:
  - `_capture_implementation_diff` against a real tempdir with staged
    and unstaged changes, plus untracked files
  - Truncation behavior on diffs larger than max_bytes
  - Graceful empty-string return when the workdir isn't a git repo
  - `_render_implementation_diff_section` markdown shape (collapse +
    diff fence + line-count summary), empty when input is empty
  - `_open_downgrade_issue` body wiring (section appears when diff is
    passed, absent when it isn't)

Run with: pytest tests/ -q
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Recommendation, Target  # noqa: E402


def _init_git_repo(path: Path) -> None:
    """Minimal git repo with one committed file so `git diff` has a HEAD
    to diff against. Run config is local-only — no user-global writes."""
    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=path, check=True, capture_output=True,
        )
    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test")
    _git("config", "commit.gpgsign", "false")
    (path / "existing.py").write_text("def existing():\n    return 1\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "seed")


# ─── _capture_implementation_diff ─────────────────────────────────────────


def test_capture_diff_includes_unstaged_modifications(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "existing.py").write_text(
        "def existing():\n    return 2  # modified\n"
    )

    diff = run._capture_implementation_diff(tmp_path)

    assert "diff --git" in diff
    assert "existing.py" in diff
    assert "-    return 1" in diff
    assert "+    return 2  # modified" in diff


def test_capture_diff_includes_untracked_new_files(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "new_module.py").write_text(
        "def new_function():\n    return 'hello'\n"
    )

    diff = run._capture_implementation_diff(tmp_path)

    # Untracked files only show up in `git diff --staged` after `git add` —
    # the helper stages first, so they should be visible.
    assert "new_module.py" in diff
    assert "+def new_function():" in diff


def test_capture_diff_truncates_oversize_input(tmp_path):
    _init_git_repo(tmp_path)
    # Write a single large file — easily exceeds the max_bytes budget.
    big = "# " + ("x" * 80 + "\n") * 2000  # ~160KB raw
    (tmp_path / "big.py").write_text(big)

    diff = run._capture_implementation_diff(tmp_path, max_bytes=5_000)

    assert len(diff) <= 5_000 + 200, "should fit within max_bytes + footer"
    assert "[diff truncated at" in diff
    assert "bytes; original was" in diff


def test_capture_diff_returns_empty_on_non_git_workdir(tmp_path):
    """A workdir without a `.git` directory shouldn't crash — the helper
    swallows the git failure and returns "" so the downgrade Issue is
    still opened."""
    (tmp_path / "anything.py").write_text("pass\n")

    diff = run._capture_implementation_diff(tmp_path)

    assert diff == ""


def test_capture_diff_returns_empty_when_no_changes(tmp_path):
    _init_git_repo(tmp_path)
    diff = run._capture_implementation_diff(tmp_path)
    assert diff == ""


def test_capture_diff_excludes_remyx_recommendation_scratchpad(tmp_path):
    """The orchestrator writes internal agent-facing prompts under
    `.remyx-recommendation/` (CONTEXT.md, GUARDRAILS.md, etc.). These
    must NOT appear in the diff embedded in downgrade-Issue bodies —
    they leak orchestrator phrasing into a maintainer-facing artifact
    (REMYX-112). The actual code contribution alongside them MUST still
    appear."""
    _init_git_repo(tmp_path)
    # Real code contribution the agent wrote
    (tmp_path / "new_module.py").write_text(
        "def cooperative_guardrail():\n    return 'ok'\n"
    )
    # Orchestrator scratchpad files that must be stripped
    scratchpad = tmp_path / ".remyx-recommendation"
    scratchpad.mkdir()
    (scratchpad / "CONTEXT.md").write_text(
        "# Team's recent shipping history\n- Some experiment\n"
    )
    (scratchpad / "GUARDRAILS.md").write_text(
        "# Honesty rules\n- Don't scaffold\n"
    )
    (scratchpad / "PAPER.md").write_text("# Recommended paper\nDoesn't matter\n")

    diff = run._capture_implementation_diff(tmp_path)

    # Real code is included
    assert "new_module.py" in diff
    assert "+def cooperative_guardrail()" in diff
    # Scratchpad files are NOT
    assert ".remyx-recommendation" not in diff
    assert "CONTEXT.md" not in diff
    assert "GUARDRAILS.md" not in diff
    assert "Honesty rules" not in diff
    assert "Team's recent shipping history" not in diff


# ─── _render_implementation_diff_section ──────────────────────────────────


def test_render_diff_section_empty_when_no_diff():
    assert run._render_implementation_diff_section("") == ""
    assert run._render_implementation_diff_section("   \n  ") == ""


def test_render_diff_section_shape():
    diff = (
        "diff --git a/new.py b/new.py\n"
        "+def new_function():\n"
        "+    return 1\n"
    )
    section = run._render_implementation_diff_section(diff)

    assert "## Proposed implementation" in section
    assert "git apply" in section
    assert "<details>" in section and "</details>" in section
    assert "```diff" in section
    assert "diff --git a/new.py" in section
    # Line count appears in the summary so the maintainer can size the
    # change before expanding.
    assert "lines)" in section


def test_render_diff_section_uses_diff_fence_for_github_coloring():
    """GitHub renders ```diff blocks with +/- coloring — required for the
    readability claim in the ticket. A plain ``` fence loses that."""
    diff = "diff --git a/x b/x\n+a\n-b\n"
    section = run._render_implementation_diff_section(diff)
    assert "```diff\n" in section
    assert section.count("```") == 2   # opening + closing fence, no more


# ─── _open_downgrade_issue wiring ─────────────────────────────────────────


def _rec():
    return Recommendation(
        paper_title="Sample Paper", arxiv_id="2601.00001", tier="high",
        z_score=0.0, spec_md="",
        paper_abstract="abstract text", domain_summary="", raw_paper_md="",
        relevance_score=0.92,
        reasoning="anchors on the localize stage",
        suggested_experiment="enable the new flag",
        interest_name="ExampleInterest",
    )


def test_downgrade_body_includes_diff_section_when_passed(monkeypatch):
    captured: dict = {}

    def fake_open_issue(target, title, body, **kw):
        captured["title"] = title
        captured["body"] = body
        return "https://github.com/example/repo/issues/123"

    monkeypatch.setattr(run, "open_issue", fake_open_issue)

    url = run._open_downgrade_issue(
        target=Target(repo="example/repo", interest_id="iid"),
        rec=_rec(),
        reason="Self-review judged the new code an orphan",
        detail="reachability gap — flag defaults False",
        implementation_diff=(
            "diff --git a/vqasynth/alignment_scorer.py b/vqasynth/alignment_scorer.py\n"
            "new file mode 100644\n"
            "+def score():\n"
            "+    return 1\n"
        ),
    )

    assert url == "https://github.com/example/repo/issues/123"
    body = captured["body"]
    assert "## Proposed implementation" in body
    assert "```diff" in body
    assert "def score():" in body
    # The downgrade reason still renders below — section ordering check.
    assert body.index("## Proposed implementation") < body.index(
        "## Why the orchestrator opened an Issue instead of a PR"
    )


def test_downgrade_body_omits_diff_section_when_not_passed(monkeypatch):
    """Backwards-compatible default — call sites that haven't been
    updated (preflight, etc.) should produce identical bodies to the
    pre-feature behavior."""
    captured: dict = {}

    def fake_open_issue(target, title, body, **kw):
        captured["body"] = body
        return "https://github.com/example/repo/issues/124"

    monkeypatch.setattr(run, "open_issue", fake_open_issue)

    run._open_downgrade_issue(
        target=Target(repo="example/repo", interest_id="iid"),
        rec=_rec(),
        reason="Pre-flight routed to Issue before implementation",
        detail="no call site fits",
    )

    body = captured["body"]
    assert "## Proposed implementation" not in body
    assert "```diff" not in body


def test_downgrade_body_omits_diff_section_when_diff_is_empty_string(monkeypatch):
    """When the capture helper returns "" (no changes, or non-git
    workdir), the call site passes that through — body must not render
    an empty `## Proposed implementation` section."""
    captured: dict = {}
    monkeypatch.setattr(
        run, "open_issue",
        lambda target, title, body, **kw: captured.update({"body": body}) or "url",
    )

    run._open_downgrade_issue(
        target=Target(repo="example/repo", interest_id="iid"),
        rec=_rec(),
        reason="x", detail="y",
        implementation_diff="",
    )

    assert "## Proposed implementation" not in captured["body"]
