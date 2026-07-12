"""Regression test for the refinement-chain second-commit git-plumbing race.

Empirically observed on multiple E2E dispatches (peft draft PR, OLMo-core
refinement, staged-synthesis on lm-evaluation-harness): after the initial
commit_and_push, the fidelity-patch path fails with:

    fatal: ambiguous argument 'origin/<branch>': unknown revision or
    path not in the working tree.

Root cause: ``git fetch origin <branch>`` (without an explicit refspec)
only updates ``FETCH_HEAD``. The subsequent ``git reset --soft
origin/<branch>`` reads ``refs/remotes/origin/<branch>``, which the
plain fetch didn't populate — so the reset fails, the patch commit
never happens, and the workflow's PR-open step gets skipped.

The fix uses an explicit refspec ``+<branch>:refs/remotes/origin/<branch>``
to guarantee the remote-tracking ref is populated locally, same pattern
as the start-from-ref checkout path.
"""
import subprocess
from pathlib import Path

import pytest


def _init_bare(path: Path):
    subprocess.run(["git", "init", "--bare", "-q", str(path)], check=True)


def _run(*args, cwd, check=True, capture=False):
    kwargs = {"cwd": cwd, "check": check}
    if capture:
        kwargs.update({"capture_output": True, "text": True})
    return subprocess.run(list(args), **kwargs)


def _setup_local_with_pushed_branch(tmp_path: Path):
    """Reproduce the state the refinement-chain fidelity-patch path starts from:
    local repo with a branch that's been pushed to origin, and the remote
    head has since diverged (simulating the re-author API commit)."""
    bare = tmp_path / "remote.git"
    _init_bare(bare)
    local = tmp_path / "local"
    local.mkdir()
    _run("git", "init", "-q", "-b", "main", cwd=local)
    _run("git", "config", "user.email", "test@test", cwd=local)
    _run("git", "config", "user.name", "test", cwd=local)
    _run("git", "remote", "add", "origin", str(bare), cwd=local)
    # Initial commit on main
    (local / "README.md").write_text("baseline\n")
    _run("git", "add", "README.md", cwd=local)
    _run("git", "commit", "-qm", "baseline", cwd=local)
    _run("git", "push", "-q", "origin", "main", cwd=local)
    # Feature branch with a commit
    _run("git", "checkout", "-qb", "feat-branch", cwd=local)
    (local / "feature.py").write_text("def foo(): pass\n")
    _run("git", "add", "feature.py", cwd=local)
    _run("git", "commit", "-qm", "feature", cwd=local)
    _run("git", "push", "-q", "origin", "feat-branch", cwd=local)
    # Simulate the remote head being re-authored (out-of-band amend + force-push
    # from another clone) — this mirrors what the GitHub API re-author step
    # does after commit_and_push's initial push.
    other = tmp_path / "other-local"
    _run("git", "clone", "-q", "--branch", "feat-branch", str(bare), str(other), cwd=tmp_path)
    _run("git", "config", "user.email", "bot@bot", cwd=other)
    _run("git", "config", "user.name", "bot", cwd=other)
    (other / "feature.py").write_text("def foo(): pass  # re-authored\n")
    _run("git", "add", "feature.py", cwd=other)
    _run("git", "commit", "--amend", "-qm", "feature (re-authored)", cwd=other)
    _run("git", "push", "-qf", "origin", "feat-branch", cwd=other)
    # Now local's HEAD is BEHIND origin/feat-branch (which was force-updated).
    # This mirrors the post-reauth state the fidelity-patch flow enters in.
    return local, "feat-branch"


def test_plain_fetch_does_not_populate_remote_tracking_ref(tmp_path):
    """Reproduce the bug: `git fetch origin <branch>` alone does not create
    `refs/remotes/origin/<branch>` (only FETCH_HEAD), so a subsequent
    `git reset --soft origin/<branch>` fails.

    This is the buggy sequence removed by the fix — codified so we don't
    regress if someone tries to simplify the refspec back to a bare branch."""
    local, branch = _setup_local_with_pushed_branch(tmp_path)
    # Bare fetch — the buggy pattern
    _run("git", "fetch", "origin", branch, cwd=local)
    # Try to resolve origin/<branch> — this is what git reset --soft would do
    result = _run("git", "rev-parse", f"origin/{branch}",
                  cwd=local, check=False, capture=True)
    # The bug: rev-parse origin/<branch> fails because refs/remotes/origin/<branch>
    # wasn't populated by the plain fetch. Fresh clones DO update remote-tracking
    # refs, but subsequent bare-branch fetches don't.
    if result.returncode == 0:
        # Some git versions may DO populate the ref on subsequent fetches;
        # skip if this environment does it. Not a hard-fail — we only care
        # that the FIX works (explicit refspec always populates).
        pytest.skip(
            "This git version populates refs/remotes/origin/<branch> even without "
            "the explicit refspec — cannot reproduce the bug locally."
        )
    assert "ambiguous argument" in (result.stderr or "") or "unknown revision" in (result.stderr or "")


def test_explicit_refspec_fetch_populates_remote_tracking_ref(tmp_path):
    """The fix: `+<branch>:refs/remotes/origin/<branch>` refspec guarantees
    the remote-tracking ref is populated locally, so subsequent
    `origin/<branch>` references resolve cleanly."""
    local, branch = _setup_local_with_pushed_branch(tmp_path)
    # Explicit refspec — the fix
    _run("git", "fetch", "origin",
         f"+{branch}:refs/remotes/origin/{branch}", cwd=local)
    # Now origin/<branch> resolves — the reset command would succeed
    result = _run("git", "rev-parse", f"origin/{branch}",
                  cwd=local, check=False, capture=True)
    assert result.returncode == 0
    assert len(result.stdout.strip()) == 40  # a full SHA
    # And the actual reset command that used to fail now works:
    _run("git", "reset", "--soft", f"origin/{branch}", cwd=local)


def test_fetch_reset_soft_sequence_end_to_end(tmp_path):
    """End-to-end: the exact sequence the refinement-chain fidelity-patch
    path runs — fetch with explicit refspec, then reset --soft — succeeds
    when applied to the post-push, remote-head-reauthored state."""
    local, branch = _setup_local_with_pushed_branch(tmp_path)
    # Add a local WIP change that would be a "patch" — should survive the reset --soft
    (local / "wip.py").write_text("wip\n")
    _run("git", "add", "wip.py", cwd=local)
    # Sequence from run.py's fidelity-patch flow
    _run("git", "fetch", "origin",
         f"+{branch}:refs/remotes/origin/{branch}", cwd=local)
    _run("git", "reset", "--soft", f"origin/{branch}", cwd=local)
    # Verify the WIP change is preserved (index-only reset), and the HEAD is
    # now at the remote's head SHA
    head_sha = _run("git", "rev-parse", "HEAD",
                    cwd=local, capture=True).stdout.strip()
    origin_sha = _run("git", "rev-parse", f"origin/{branch}",
                      cwd=local, capture=True).stdout.strip()
    assert head_sha == origin_sha
    # The WIP file should still be staged (reset --soft preserves the index)
    status = _run("git", "status", "--short", cwd=local, capture=True).stdout
    assert "wip.py" in status
