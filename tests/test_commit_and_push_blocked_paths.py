"""``commit_and_push`` must strip un-pushable ALWAYS_BLOCKED paths.

The branch push authenticates with a token that has no ``workflows``
permission, so a single changed file under ``.github/workflows/**`` makes
GitHub reject the ENTIRE push and lose the whole diff. commit_and_push drops
such paths before staging, pushes the rest, and returns what it dropped so
callers can surface it for human review.

Covers:
- a MODIFIED workflow file → reverted to baseline, legit change still lands.
- a NEW workflow file → removed, not present on the pushed branch.
- a clean diff → returns [] and pushes normally.

Run with: pytest tests/test_commit_and_push_blocked_paths.py -q
"""
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


@pytest.fixture
def fork_workdir(tmp_path):
    """A bare "remote" plus a workdir on main, identity configured.

    The seed carries a pre-existing ``.github/workflows/ci.yml`` so a later
    edit to it is a *modification* (the revert path); new workflow files
    exercise the *removal* path.
    """
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )
    (seed / ".github" / "workflows").mkdir(parents=True)
    (seed / ".github" / "workflows" / "ci.yml").write_text("name: ci\n")
    (seed / "README.md").write_text("baseline\n")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "baseline"],
        check=True,
    )

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
    return workdir, bare


def _run(workdir):
    """commit_and_push with the network/API pieces neutralized (empty token
    pushes to the local bare; API re-author recorded as a no-op)."""
    with patch.object(run, "_github_token", lambda: ""), \
         patch.object(run, "_recommit_via_api", lambda *a, **kw: None):
        return run.commit_and_push(
            workdir, "feature/x", "add feature",
            repo="owner/repo", base_branch="main",
        )


def _pushed_tree(bare):
    return subprocess.run(
        ["git", "-C", str(bare), "ls-tree", "-r", "--name-only",
         "refs/heads/feature/x"],
        capture_output=True, text=True, check=True,
    ).stdout.split()


def test_modified_workflow_file_is_stripped(fork_workdir):
    workdir, bare = fork_workdir
    (workdir / ".github" / "workflows" / "ci.yml").write_text(
        "name: ci\n# agent-injected step\n"
    )
    (workdir / "feature.py").write_text("print('impl')\n")

    dropped = _run(workdir)

    assert ".github/workflows/ci.yml" in dropped
    assert "feature.py" in _pushed_tree(bare)
    # the workflow file is back to baseline, not the injected content
    show = subprocess.run(
        ["git", "-C", str(bare), "show",
         "refs/heads/feature/x:.github/workflows/ci.yml"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "agent-injected" not in show


def test_new_workflow_file_is_removed(fork_workdir):
    workdir, bare = fork_workdir
    (workdir / ".github" / "workflows" / "claude_review.yml").write_text(
        "name: review\n"
    )
    (workdir / "feature.py").write_text("print('impl')\n")

    dropped = _run(workdir)

    assert ".github/workflows/claude_review.yml" in dropped
    tree = _pushed_tree(bare)
    assert "feature.py" in tree
    assert ".github/workflows/claude_review.yml" not in tree


def test_clean_diff_returns_empty_and_pushes(fork_workdir):
    workdir, bare = fork_workdir
    (workdir / "feature.py").write_text("print('impl')\n")

    dropped = _run(workdir)

    assert dropped == []
    assert "feature.py" in _pushed_tree(bare)
