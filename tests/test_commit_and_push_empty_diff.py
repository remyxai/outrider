"""``commit_and_push`` must survive a session that changed nothing.

A warm-start refinement session can review the curated branch, judge it
already correct, and make zero edits — a valid (best-case) outcome. The
staged diff is then empty and ``git commit`` exits 1; an unconditional
``check=True`` commit turned that into a crashed run (status ``error``)
before push/PR, even though the branch differs from main by the entire
implementation and pushing it as-is works fine.

Covers:

- empty diff → no commit, branch still pushed to origin at the ref's
  SHA, and the API re-author step is skipped (it would otherwise pin an
  empty bot commit on top).
- non-empty diff → the classic path is unchanged: a commit lands on the
  pushed branch and the API re-author runs with the base as parent.

Run with: pytest tests/test_commit_and_push_empty_diff.py -q
"""
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# ── Setup: a bare "remote" plus a workdir cloned on the refinement ref ──


@pytest.fixture
def warm_start_workdir(tmp_path, monkeypatch):
    """Clone of a fake fork, checked out on the refinement ref.

    Mirrors what ``prepare_workdir`` leaves behind for a start-from-ref
    session: HEAD on ``refine-me`` == ``origin/refine-me``, identity
    configured. Returns (workdir, bare path, refine-me SHA).
    """
    seed = tmp_path / "seed"
    # git init -b needs git >= 2.28; symbolic-ref names the unborn
    # branch on older gits too.
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )
    (seed / "README.md").write_text("baseline\n")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "baseline"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "checkout", "-q", "-b", "refine-me"],
        check=True,
    )
    (seed / "impl.py").write_text("print('the entire implementation')\n")
    subprocess.run(["git", "-C", str(seed), "add", "impl.py"], check=True)
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

    bare = tmp_path / "fork.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)], check=True,
    )

    workdir = tmp_path / "workdir"
    subprocess.run(["git", "clone", "-q", str(bare), str(workdir)], check=True)
    subprocess.run(
        ["git", "-C", str(workdir), "config", "user.email", "bot@remyx.ai"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workdir), "config", "user.name", "remyx-ai[bot]"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workdir), "checkout", "-q", "refine-me"], check=True,
    )
    monkeypatch.setenv("INPUT_START_FROM_REF", "refine-me")
    return workdir, bare, refine_sha


def _push_branch(workdir, bare, recommit_calls):
    """Run commit_and_push with network-touching pieces neutralized.

    An empty token skips the remote set-url rewrite, so pushes land on
    the local bare; the API re-author is recorded instead of executed.
    """
    with patch.object(run, "_github_token", lambda: ""), \
         patch.object(
             run, "_recommit_via_api",
             lambda *a, **kw: recommit_calls.append((a, kw)),
         ):
        run.commit_and_push(
            workdir, "refine-me-refined", "refine: widget method",
            repo="owner/repo", base_branch="main",
        )


# ── empty diff: push proceeds, no commit, no API re-author ──────────────


def test_empty_diff_pushes_branch_without_commit(warm_start_workdir):
    """Zero edits must not crash; the branch lands on origin at the ref's SHA."""
    workdir, bare, refine_sha = warm_start_workdir
    recommit_calls = []

    _push_branch(workdir, bare, recommit_calls)

    pushed_sha = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "refs/heads/refine-me-refined"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert pushed_sha == refine_sha
    # No new commit → nothing to re-author; wrapping would only add an
    # empty commit on top of the base.
    assert recommit_calls == []


# ── non-empty diff: classic path unchanged ──────────────────────────────


def test_nonempty_diff_still_commits_and_reauthors(warm_start_workdir):
    """Edits present → commit lands on the pushed branch, re-author runs."""
    workdir, bare, refine_sha = warm_start_workdir
    (workdir / "impl.py").write_text("print('a refined implementation')\n")
    recommit_calls = []

    _push_branch(workdir, bare, recommit_calls)

    pushed_sha = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "refs/heads/refine-me-refined"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert pushed_sha != refine_sha
    parent_sha = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "refs/heads/refine-me-refined^"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert parent_sha == refine_sha
    assert len(recommit_calls) == 1
    args, kwargs = recommit_calls[0]
    assert kwargs.get("parent_sha") == refine_sha
