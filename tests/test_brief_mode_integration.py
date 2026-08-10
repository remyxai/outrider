"""Integration test for ``run_brief_mode`` — real filesystem, real git
push, mocked only at network boundaries.

Scope: everything downstream of ``prepare_workdir`` runs against real
disk and a local bare git "remote". What's covered:

  - ``write_spec_bundle`` renders SPEC.md / INVOCATION.md on disk from
    the brief-mode templates, correctly, with the brief content
    interpolated in.
  - ``PAPER.md`` is NOT written (paper-less path invariant).
  - The ``.git/info/exclude`` mark is applied so downstream ``git
    add -A`` skips the bundle.
  - A simulated coding-agent edit passes ``validate_changes``.
  - ``format_pr_title`` returns the brief-derived title when no
    PR_TITLE.txt is written.
  - ``commit_and_push`` produces a real commit + branch push to the
    local bare remote, and the bundle directory does NOT land in the
    committed diff.
  - ``build_pr_body`` renders the brief-variant body against the
    committed state.

Mocked (network boundaries only):
  - ``prepare_workdir`` — the test constructs the workdir directly by
    cloning a local bare repo; the real function hardcodes GitHub URLs.
  - ``_fetch_interest_context`` — no engine round-trip.
  - ``invoke_claude_code`` — the coding-agent call is simulated by
    writing a small file into the workdir before validation runs.
  - ``_recommit_via_api`` — the final GitHub-API bot re-author step.
  - ``open_pr`` — the final GitHub-API PR creation.

Not covered (out of scope for this integration test):
  - What the real ``claude`` CLI produces from a brief-mode INVOCATION.md.
    That's the live-dispatch integration test (VQASynth issue #53 or
    similar), which needs the branch merged + a real GitHub Action run.

Run with: pytest tests/test_brief_mode_integration.py -q -s
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# ─── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def bare_remote(tmp_path):
    """Create a local bare "GitHub remote" with a seed commit on main."""
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )
    (seed / "README.md").write_text("# Test target repo\n")
    (seed / "mypkg").mkdir()
    (seed / "mypkg" / "__init__.py").write_text("")
    (seed / "mypkg" / "core.py").write_text("def existing_function():\n    pass\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "seed"],
        check=True,
    )
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)],
        check=True,
    )
    return bare


@pytest.fixture
def workdir(tmp_path, bare_remote):
    """Clone the local bare remote as the coding-agent workdir; set
    identity so commit_and_push can create commits without hitting
    global git config."""
    wd = tmp_path / "workdir"
    subprocess.run(
        ["git", "clone", "-q", str(bare_remote), str(wd)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(wd), "config", "user.email", "bot@test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(wd), "config", "user.name", "test-bot"],
        check=True,
    )
    return wd


@pytest.fixture
def brief_rec():
    """A brief-mode Recommendation as ``_recommendation_from_brief``
    would produce for an issue-style brief."""
    return run._recommendation_from_brief(
        "# Add orientation-query support\n\n"
        "Extend the annotation stage to support orientation queries. "
        "Motivating reference: SpatialVision/Orient-Anything.",
        interest_context="Focus: VQA dataset synthesis with spatial reasoning.",
    )


@pytest.fixture
def target_from_bare(bare_remote):
    """Target with repo pointing at a fake org/name; commit_and_push
    references this string in remote URL rewrite, which we skip by
    not providing a token."""
    return run.Target(repo="testorg/testrepo", interest_id="")


# ─── Real bundle-write, real disk ───────────────────────────────────


def test_bundle_renders_brief_variants_on_disk(workdir, target_from_bare, brief_rec):
    """write_spec_bundle produces the brief-mode SPEC.md (paper-less
    template) + INVOCATION.md (no PAPER.md reference) + no PAPER.md
    on disk. Interpolations are correct."""
    run.write_spec_bundle(
        workdir, target_from_bare, brief_rec, package="mypkg",
        selection_note="", env_body="",
    )
    bundle = workdir / run.BUNDLE_DIR_NAME
    assert bundle.exists()

    # SPEC.md exists and renders the brief variant.
    spec = (bundle / "SPEC.md").read_text()
    assert "mode: brief" in spec
    assert "Design brief" in spec
    assert "orientation queries" in spec
    assert "arxiv_id:" not in spec  # paper-less
    assert "Focus: VQA dataset synthesis" in spec

    # PAPER.md is NOT written (no paper to describe).
    assert not (bundle / "PAPER.md").exists()

    # INVOCATION.md renders the brief variant.
    invocation = (bundle / "INVOCATION.md").read_text()
    assert "brief IS the spec" in invocation
    assert "PAPER.md" not in invocation  # not in the read list
    assert "Mode 1 (direct port)" not in invocation  # paper-only framing


def test_bundle_marks_gitignore_exclude(workdir, target_from_bare, brief_rec):
    """After bundle write, ``git add -A`` on a workdir with the
    bundle skips the bundle files — the leak this fixes is why
    ``.git/info/exclude`` gained the entry."""
    run.write_spec_bundle(
        workdir, target_from_bare, brief_rec, package="mypkg",
        selection_note="", env_body="",
    )
    exclude = (workdir / ".git" / "info" / "exclude").read_text()
    assert "/.remyx-recommendation/" in exclude


# ─── Real git push against the local bare remote ────────────────────


def test_commit_and_push_lands_branch_without_bundle(
    workdir, target_from_bare, brief_rec, bare_remote, monkeypatch
):
    """Composed flow with real git operations:

      1. write_spec_bundle produces the bundle on disk.
      2. Simulated coding agent edits mypkg/orient.py.
      3. validate_changes accepts the diff.
      4. commit_and_push commits + pushes the branch to the local bare.
      5. The pushed branch contains the coding-agent edit but NOT the
         bundle files.
    """
    # 1. Bundle
    run.write_spec_bundle(
        workdir, target_from_bare, brief_rec, package="mypkg",
        selection_note="", env_body="",
    )

    # 2. Simulate coding agent
    (workdir / "mypkg" / "orient.py").write_text(
        "def orientation_query(image, obj):\n"
        "    \"\"\"Stub for orientation-query annotation stage.\"\"\"\n"
        "    return None\n"
    )

    # 3. Validate
    allowlisted, violations = run.validate_changes(
        workdir, target_from_bare, package="mypkg",
    )
    assert allowlisted, f"path violations: {violations}"

    # 4. Push — mock _recommit_via_api (GitHub API) but let the real
    # git push against the local bare remote run.
    monkeypatch.setattr(run, "_recommit_via_api", lambda *a, **kw: None)
    monkeypatch.setattr(run, "_github_token", lambda: "")  # skip URL rewrite
    branch = "remyx-recommendation/add-orientation-query-support"
    run.commit_and_push(
        workdir, branch, "[Remyx Recommendation] Add orientation-query support",
        target_from_bare.repo, base_branch="main",
    )

    # 5. Verify the branch landed on the bare remote and its diff is
    # ONLY the coding-agent file — bundle got scrubbed.
    ls_remote = subprocess.run(
        ["git", "-C", str(bare_remote), "ls-remote", "--heads", "origin"],
        capture_output=True, text=True, check=False,
    )
    # Bare repos don't have an "origin" — read refs directly instead.
    refs = subprocess.run(
        ["git", "-C", str(bare_remote), "for-each-ref",
         "--format=%(refname:short)", "refs/heads/"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert branch in refs, f"branch not on remote; refs={refs}"

    # Inspect the diff between main and the pushed branch.
    diff_names = subprocess.run(
        ["git", "-C", str(bare_remote), "diff", "--name-only",
         f"main..{branch}"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "mypkg/orient.py" in diff_names
    assert not any(f.startswith(".remyx-recommendation/") for f in diff_names), (
        f"bundle leaked into commit: {diff_names}"
    )


# ─── Real PR body rendering ─────────────────────────────────────────


def test_pr_body_renders_brief_variant_with_real_rec(target_from_bare, brief_rec):
    """build_pr_body renders the brief variant against a real
    Recommendation from the factory — no ``Implements arXiv``
    attribution, no license section, ``Design brief`` collapse
    carrying the actual brief content."""
    body = run.build_pr_body(
        target_from_bare, brief_rec,
        tests_status="unvalidated",
        test_output="pytest not run in this integration test",
    )
    assert "arxiv.org/abs/" not in body
    assert "Discovery context" not in body
    assert "Design brief" in body
    assert "orientation queries" in body
    assert "SpatialVision/Orient-Anything" in body
    assert "Drafted from a design brief supplied at dispatch time" in body


# ─── Full run_brief_mode composition, real leaves except network ─────


def test_run_brief_mode_end_to_end_against_local_bare(
    workdir, bare_remote, target_from_bare, monkeypatch
):
    """Full ``run_brief_mode`` flow — all real leaves except
    ``prepare_workdir`` (bypassed with the pre-cloned local workdir),
    ``invoke_claude_code`` (simulated agent edit), and the two
    GitHub-API touches (``_recommit_via_api``, ``open_pr``).

    Confirms the composition end-to-end: brief → bundle → agent →
    validate → push → PR body → open_pr, and the branch lands on the
    local bare with a clean diff.
    """
    monkeypatch.setenv(
        "INPUT_LEAD_CONTENT",
        "# Add orientation-query support\n\n"
        "Extend the annotation stage to support orientation queries. "
        "Motivating reference: SpatialVision/Orient-Anything.",
    )
    # No engine round-trip.
    monkeypatch.setattr(run, "_fetch_interest_context", lambda i: ("", "", ""))
    # Bypass prepare_workdir — use the pre-cloned local workdir.
    monkeypatch.setattr(run, "prepare_workdir", lambda t: workdir)

    # Simulate the coding agent editing a real file inside the workdir.
    def fake_invoke_claude(w, timeout_s):
        (w / "mypkg" / "orient.py").write_text(
            "def orientation_query(image, obj):\n"
            "    return {'yaw': 0.0, 'pitch': 0.0, 'roll': 0.0}\n"
        )
        return (True, "simulated agent edit")
    monkeypatch.setattr(run, "invoke_claude_code", fake_invoke_claude)

    # Skip the GitHub-API bot re-author + skip URL rewrite (no token).
    monkeypatch.setattr(run, "_recommit_via_api", lambda *a, **kw: None)
    monkeypatch.setattr(run, "_github_token", lambda: "")

    # Capture what would have been PR-opened.
    opened = {}
    def fake_open_pr(target, branch, title, body, draft, base="main"):
        opened.update(
            branch=branch, title=title, body=body, draft=draft, base=base,
        )
        return ("https://example.test/pr/1", 1)
    monkeypatch.setattr(run, "open_pr", fake_open_pr)
    monkeypatch.setattr(run, "_check_canary_ack_file", lambda *a: True)

    # Prevent the finally-block from wiping our fixture workdir before we
    # can inspect it.
    monkeypatch.setenv("DEBUG_KEEP_WORKDIR", "1")

    result = run.run_brief_mode(target_from_bare)

    # Result shape
    assert result["status"] == "pr_opened_brief", result
    assert result["pr_url"] == "https://example.test/pr/1"
    assert result["pr_number"] == 1
    assert result["package"] == "mypkg"
    assert result["base_branch"] == "main"
    assert result["branch"].startswith("remyx-recommendation/")

    # PR body is the brief variant
    assert "Design brief" in opened["body"]
    assert "arxiv.org/abs/" not in opened["body"]
    assert "orientation queries" in opened["body"]

    # Branch is really on the bare remote
    refs = subprocess.run(
        ["git", "-C", str(bare_remote), "for-each-ref",
         "--format=%(refname:short)", "refs/heads/"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert opened["branch"] in refs

    # Diff on the branch is exactly the agent's edit; no bundle.
    diff_names = subprocess.run(
        ["git", "-C", str(bare_remote), "diff", "--name-only",
         f"main..{opened['branch']}"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert diff_names == ["mypkg/orient.py"], diff_names
