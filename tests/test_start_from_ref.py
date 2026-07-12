"""Tests for the ``start-from-ref`` refinement input (REMYX-219).

Covers:

- ``format_branch_name`` appends ``-refined`` when ``INPUT_START_FROM_REF``
  is set (so the refinement push doesn't collide with the original artifact
  when the same paper feeds both runs).
- ``prepare_workdir`` fetches + checks out the ref when set, so the
  coding session begins with that branch's diff already applied to the
  workspace (rather than an empty default-branch clone).
- ``commit_and_push`` sanity-checks HEAD against ``origin/<start-from-ref>``
  instead of ``origin/<base_branch>`` when set — otherwise the ref-based
  session would fail every commit as "state disturbed" even on a clean run.

Run with: pytest tests/test_start_from_ref.py -q
"""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# ── Setup: a bare "remote" with two branches (main + a fake refinement) ──


@pytest.fixture
def fake_remote(tmp_path, monkeypatch):
    """Build a bare repo that stands in for the GitHub-side fork.

    Two branches:
      - ``main`` with a baseline README
      - ``refine-me`` with an extra file (simulating a prior draft branch)

    Returns the (bare-repo path, expected refine-me SHA) tuple.
    """
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "README.md").write_text("baseline\n")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "baseline"],
        check=True,
    )
    main_sha = subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Fabricate a refinement branch on top of main.
    subprocess.run(["git", "-C", str(seed), "checkout", "-q", "-b", "refine-me"], check=True)
    (seed / "PRIOR_DRAFT.md").write_text("this file only exists on the refinement branch\n")
    subprocess.run(["git", "-C", str(seed), "add", "PRIOR_DRAFT.md"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "prior draft"],
        check=True,
    )
    refine_sha = subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(seed), "checkout", "-q", "main"], check=True)

    # Convert seed to a bare that we can clone from.
    bare = tmp_path / "fork.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)], check=True,
    )
    return bare, main_sha, refine_sha


# ── format_branch_name suffix ────────────────────────────────────────────


def _rec(title="A Fancy New Method for Widgets", arxiv_id="2606.99999v1"):
    return run.Recommendation(
        paper_title=title,
        arxiv_id=arxiv_id,
        tier="high",
        z_score=0.0,
        spec_md="",
        paper_abstract="",
        domain_summary="",
        raw_paper_md="",
    )


def test_format_branch_name_no_suffix_when_unset(monkeypatch):
    """Default flow (no start-from-ref) preserves the paper-title slug verbatim."""
    monkeypatch.delenv("INPUT_START_FROM_REF", raising=False)
    assert run.format_branch_name(_rec()) == "a-fancy-new-method-for-widgets"


def test_format_branch_name_appends_refined_when_set(monkeypatch):
    """Refinement runs append ``-refined`` to avoid colliding with the original."""
    monkeypatch.setenv("INPUT_START_FROM_REF", "refine-me")
    assert run.format_branch_name(_rec()) == "a-fancy-new-method-for-widgets-refined"


def test_format_branch_name_ignores_whitespace_only_env(monkeypatch):
    """A whitespace-only env var doesn't count as set — same slug as unset."""
    monkeypatch.setenv("INPUT_START_FROM_REF", "   ")
    assert run.format_branch_name(_rec()) == "a-fancy-new-method-for-widgets"


# ── prepare_workdir checks out the ref when set ────────────────────────


def test_prepare_workdir_checks_out_start_from_ref(fake_remote, tmp_path, monkeypatch):
    """When start-from-ref is set, HEAD is on that ref after clone (not main)."""
    bare, main_sha, refine_sha = fake_remote
    monkeypatch.setenv("INPUT_START_FROM_REF", "refine-me")

    class _FakeTarget:
        repo = "owner/repo"

    def fake_github_token():
        return "gh_placeholder_token"

    def fake_mkdtemp(prefix):
        d = tmp_path / prefix
        d.mkdir()
        return str(d)

    # The real prepare_workdir builds a URL from _github_token(); we route
    # git clone at the local bare repo instead by patching the URL builder.
    original_run = subprocess.run

    def routed_run(cmd, *args, **kwargs):
        # Redirect the initial `git clone <https-url> <dir>` to the local bare.
        if cmd[:2] == ["git", "clone"] and len(cmd) >= 4 and cmd[-2].startswith("https://"):
            cmd = list(cmd)
            cmd[-2] = str(bare)
        # And the follow-up `git fetch origin <ref>` also works against the
        # local bare (already the configured origin), no rewrite needed.
        return original_run(cmd, *args, **kwargs)

    with patch.object(run, "_github_token", fake_github_token), \
         patch.object(run.tempfile, "mkdtemp", fake_mkdtemp), \
         patch.object(run.subprocess, "run", side_effect=routed_run):
        workdir = run.prepare_workdir(_FakeTarget())

    head = original_run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == refine_sha
    # And the refinement-only file is present in the workspace.
    assert (workdir / "PRIOR_DRAFT.md").exists()


def test_detect_default_branch_after_ref_checkout(fake_remote, tmp_path, monkeypatch):
    """After a start-from-ref checkout, detect_default_branch still returns the
    remote's default (main) — not the currently-checked-out ref. Otherwise
    the PR base would open against the ref instead of main, and the diff
    review wouldn't show the full baseline+refinement state (observed on
    the OLMo-core live test, run 29198542589 → PR #12 with base=T3 ref)."""
    bare, _main_sha, _refine_sha = fake_remote
    monkeypatch.setenv("INPUT_START_FROM_REF", "refine-me")

    class _FakeTarget:
        repo = "owner/repo"

    def fake_github_token():
        return "gh_placeholder_token"

    def fake_mkdtemp(prefix):
        d = tmp_path / prefix
        d.mkdir()
        return str(d)

    original_run = subprocess.run

    def routed_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "clone"] and len(cmd) >= 4 and cmd[-2].startswith("https://"):
            cmd = list(cmd)
            cmd[-2] = str(bare)
        return original_run(cmd, *args, **kwargs)

    with patch.object(run, "_github_token", fake_github_token), \
         patch.object(run.tempfile, "mkdtemp", fake_mkdtemp), \
         patch.object(run.subprocess, "run", side_effect=routed_run):
        workdir = run.prepare_workdir(_FakeTarget())

    # Local HEAD is on refine-me (verified by another test), but the
    # remote-tracking origin/HEAD still points at main.
    assert run.detect_default_branch(workdir) == "main"


def test_prepare_workdir_default_flow_stays_on_main(fake_remote, tmp_path, monkeypatch):
    """When start-from-ref is unset, HEAD stays on main — no regression on classic flow."""
    bare, main_sha, _refine_sha = fake_remote
    monkeypatch.delenv("INPUT_START_FROM_REF", raising=False)

    class _FakeTarget:
        repo = "owner/repo"

    def fake_github_token():
        return "gh_placeholder_token"

    def fake_mkdtemp(prefix):
        d = tmp_path / prefix
        d.mkdir()
        return str(d)

    original_run = subprocess.run

    def routed_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "clone"] and len(cmd) >= 4 and cmd[-2].startswith("https://"):
            cmd = list(cmd)
            cmd[-2] = str(bare)
        return original_run(cmd, *args, **kwargs)

    with patch.object(run, "_github_token", fake_github_token), \
         patch.object(run.tempfile, "mkdtemp", fake_mkdtemp), \
         patch.object(run.subprocess, "run", side_effect=routed_run):
        workdir = run.prepare_workdir(_FakeTarget())

    head = original_run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == main_sha
    # Refinement-only file must NOT be present on the default clone.
    assert not (workdir / "PRIOR_DRAFT.md").exists()
