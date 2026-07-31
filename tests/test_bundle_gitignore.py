"""``.remyx-recommendation/`` must not leak into target-repo commits.

The bundle holds scratch briefing files (SPEC.md, PAPER.md,
INVOCATION.md, PR_TITLE.txt) that ``commit_and_push`` deletes before
staging. But refinement runs, dispatcher scripts, and manual punch-ups
do their own git-add-alls without knowing about the bundle — one such
punch-up leaked ``PR_TITLE.txt`` into a real customer-facing PR.

``_mark_bundle_gitignored`` writes ``/.remyx-recommendation/`` into
``.git/info/exclude`` so any downstream ``git add -A`` on the workdir
skips the bundle even without ``commit_and_push``'s explicit cleanup.

Run with: pytest tests/test_bundle_gitignore.py -q
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


def _git(workdir, *args):
    return subprocess.run(
        ["git", "-C", str(workdir), *args], check=True,
        capture_output=True, text=True,
    ).stdout


def _init_repo(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    _git(workdir, "init", "-q")
    _git(workdir, "symbolic-ref", "HEAD", "refs/heads/main")
    (workdir / "README.md").write_text("baseline\n")
    _git(workdir, "add", "README.md")
    _git(workdir, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "baseline")
    return workdir


def test_bundle_files_excluded_from_git_add_all(tmp_path):
    """After marking the bundle ignored, ``git add -A`` on a workdir with
    ``.remyx-recommendation/PR_TITLE.txt`` and real edits stages only
    the real edits."""
    workdir = _init_repo(tmp_path)
    bundle = workdir / run.BUNDLE_DIR_NAME
    bundle.mkdir()
    (bundle / "PR_TITLE.txt").write_text("scratch title\n")
    (bundle / "SPEC.md").write_text("scratch spec\n")
    (workdir / "src.py").write_text("print('real change')\n")

    run._mark_bundle_gitignored(workdir)

    _git(workdir, "add", "-A")
    staged = _git(workdir, "diff", "--cached", "--name-only").splitlines()
    assert "src.py" in staged
    assert not any(f.startswith(run.BUNDLE_DIR_NAME) for f in staged), staged


def test_mark_is_idempotent(tmp_path):
    """Calling twice adds the exclude line exactly once — write_spec_bundle
    fires per-attempt during retries; a duplicate every attempt would
    grow ``.git/info/exclude`` unboundedly."""
    workdir = _init_repo(tmp_path)
    run._mark_bundle_gitignored(workdir)
    run._mark_bundle_gitignored(workdir)
    exclude = (workdir / ".git" / "info" / "exclude").read_text()
    assert exclude.count(f"/{run.BUNDLE_DIR_NAME}/") == 1


def test_no_error_on_non_git_workdir(tmp_path):
    """Best-effort: outside a git repo, the helper returns silently."""
    workdir = tmp_path / "not-a-repo"
    workdir.mkdir()
    run._mark_bundle_gitignored(workdir)  # must not raise


def test_workdir_gitignore_unchanged(tmp_path):
    """The exclude lives in ``.git/info/exclude``, not in the target
    repo's tracked ``.gitignore`` — otherwise the target repo's own
    ``.gitignore`` would gain a Remyx-specific line on every dispatch."""
    workdir = _init_repo(tmp_path)
    (workdir / ".gitignore").write_text("*.log\n")
    _git(workdir, "add", ".gitignore")
    _git(workdir, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "gitignore")

    run._mark_bundle_gitignored(workdir)

    assert (workdir / ".gitignore").read_text() == "*.log\n"
