"""Pre-PR fidelity gate + branch-patch stage tests.

Prototype for the fidelity-before-PR-publication redesign: fidelity runs
on the local branch, and if flagged, a single Claude Code patch attempt
tries to resolve the deviations before deciding whether to publish.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# --- _local_git_diff --------------------------------------------------------

def test_local_git_diff_returns_stdout_on_success(tmp_path):
    fake = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="diff --git a/x b/x\n+added\n", stderr="",
    )
    with patch.object(run.subprocess, "run", return_value=fake):
        got = run._local_git_diff(tmp_path, "main")
    assert "added" in got


def test_local_git_diff_returns_empty_on_nonzero(tmp_path):
    fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="err")
    with patch.object(run.subprocess, "run", return_value=fake):
        got = run._local_git_diff(tmp_path, "main")
    assert got == ""


def test_local_git_diff_returns_empty_on_subprocess_error(tmp_path):
    with patch.object(run.subprocess, "run", side_effect=OSError("boom")):
        got = run._local_git_diff(tmp_path, "main")
    assert got == ""


# --- _run_pre_pr_fidelity_check ---------------------------------------------

def _make_rec(arxiv_id="2606.30560v1"):
    """Minimal Recommendation-shape stub for tests."""
    rec = MagicMock()
    rec.arxiv_id = arxiv_id
    rec.paper_title = "Test Paper"
    return rec


def _make_target():
    t = MagicMock()
    t.repo = "org/repo"
    t.claude_timeout_s = 900
    return t


def test_pre_pr_fidelity_skips_when_no_reference_url(tmp_path):
    """No reference URL in the body → skip cleanly, no needs_judgment."""
    rec = _make_rec()
    target = _make_target()
    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("2606.30560v1", "")):
        verdict = run._run_pre_pr_fidelity_check(
            rec, target, tmp_path, "PR title", "PR body with no ref", "main",
        )
    assert verdict["needs_judgment"] is False
    assert verdict["status"] == "pre_pr_fidelity_skipped_no_reference"


def test_pre_pr_fidelity_returns_needs_judgment_when_matrix_flags(tmp_path):
    rec = _make_rec()
    target = _make_target()
    fake_matrix = {
        "items": [
            {"name": "algo mismatch", "status": "deviation (needs-judgment)",
             "rationale": "..."},
        ],
        "needs_judgment": True,
    }
    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("2606.30560v1", "https://github.com/x/y")):
        with patch.object(run, "_clone_reference_repo",
                          return_value=(True, tmp_path / "ref", "")):
            with patch.object(run, "_local_git_diff", return_value="fake diff"):
                with patch.object(run, "_run_claude_oneshot",
                                  return_value=(True, "{...}")):
                    with patch.object(run, "_extract_json_object",
                                      return_value=fake_matrix):
                        with patch.object(run, "_render_coverage_matrix",
                                          return_value="## Coverage\n..."):
                            verdict = run._run_pre_pr_fidelity_check(
                                rec, target, tmp_path, "T", "B", "main",
                            )
    assert verdict["needs_judgment"] is True
    assert verdict["items_count"] == 1
    assert verdict["status"] == "pre_pr_fidelity_needs_judgment"
    assert verdict["matrix"] == fake_matrix


def test_pre_pr_fidelity_returns_clean_when_matrix_not_flagged(tmp_path):
    rec = _make_rec()
    target = _make_target()
    fake_matrix = {"items": [{"name": "x", "status": "covered"}], "needs_judgment": False}
    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("2606.30560v1", "https://github.com/x/y")):
        with patch.object(run, "_clone_reference_repo",
                          return_value=(True, tmp_path / "ref", "")):
            with patch.object(run, "_local_git_diff", return_value="fake diff"):
                with patch.object(run, "_run_claude_oneshot",
                                  return_value=(True, "{...}")):
                    with patch.object(run, "_extract_json_object",
                                      return_value=fake_matrix):
                        with patch.object(run, "_render_coverage_matrix",
                                          return_value=""):
                            verdict = run._run_pre_pr_fidelity_check(
                                rec, target, tmp_path, "T", "B", "main",
                            )
    assert verdict["needs_judgment"] is False
    assert verdict["status"] == "pre_pr_fidelity_clean"


def test_pre_pr_fidelity_degrades_when_reference_clone_fails(tmp_path):
    rec = _make_rec()
    target = _make_target()
    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("2606.30560v1", "https://github.com/x/y")):
        with patch.object(run, "_clone_reference_repo",
                          return_value=(False, None, "network error")):
            verdict = run._run_pre_pr_fidelity_check(
                rec, target, tmp_path, "T", "B", "main",
            )
    assert verdict["needs_judgment"] is False
    assert verdict["status"] == "pre_pr_fidelity_failed_clone"


def test_pre_pr_fidelity_degrades_when_diff_empty(tmp_path):
    rec = _make_rec()
    target = _make_target()
    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("2606.30560v1", "https://github.com/x/y")):
        with patch.object(run, "_clone_reference_repo",
                          return_value=(True, tmp_path / "ref", "")):
            with patch.object(run, "_local_git_diff", return_value=""):
                verdict = run._run_pre_pr_fidelity_check(
                    rec, target, tmp_path, "T", "B", "main",
                )
    assert verdict["needs_judgment"] is False
    assert verdict["status"] == "pre_pr_fidelity_failed_no_diff"


def test_pre_pr_fidelity_degrades_when_claude_returns_unparseable(tmp_path):
    rec = _make_rec()
    target = _make_target()
    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("2606.30560v1", "https://github.com/x/y")):
        with patch.object(run, "_clone_reference_repo",
                          return_value=(True, tmp_path / "ref", "")):
            with patch.object(run, "_local_git_diff", return_value="diff"):
                with patch.object(run, "_run_claude_oneshot",
                                  return_value=(True, "garbage")):
                    with patch.object(run, "_extract_json_object", return_value=None):
                        verdict = run._run_pre_pr_fidelity_check(
                            rec, target, tmp_path, "T", "B", "main",
                        )
    assert verdict["needs_judgment"] is False
    assert verdict["status"] == "pre_pr_fidelity_failed_claude"


# --- _attempt_pre_pr_fidelity_patch -----------------------------------------

def test_patch_returns_false_when_no_flagged_items(tmp_path):
    matrix = {"items": [{"name": "x", "status": "covered"}]}
    assert run._attempt_pre_pr_fidelity_patch(
        tmp_path, matrix, "https://github.com/x/y",
    ) is False


def test_patch_returns_false_when_matrix_empty(tmp_path):
    assert run._attempt_pre_pr_fidelity_patch(
        tmp_path, {}, "https://github.com/x/y",
    ) is False


def test_patch_returns_false_when_claude_fails(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("class Foo:\n    pass\n")
    matrix = {
        "items": [
            {"name": "algo mismatch", "status": "deviation (needs-judgment)",
             "rationale": "..."},
        ],
    }
    with patch.object(run, "invoke_claude_code", return_value=(False, "err")):
        got = run._attempt_pre_pr_fidelity_patch(
            tmp_path, matrix, "https://github.com/x/y",
        )
    assert got is False


def test_patch_returns_false_when_no_files_touched(tmp_path):
    (tmp_path / "src").mkdir()
    src_file = tmp_path / "src" / "a.py"
    src_file.write_text("class Foo:\n    pass\n")
    matrix = {
        "items": [
            {"name": "algo mismatch", "status": "deviation (needs-judgment)"},
        ],
    }
    # Claude "succeeds" but doesn't actually modify any .py files
    with patch.object(run, "invoke_claude_code", return_value=(True, "no edits")):
        got = run._attempt_pre_pr_fidelity_patch(
            tmp_path, matrix, "https://github.com/x/y",
        )
    assert got is False


def test_patch_returns_true_when_files_touched(tmp_path):
    import time
    (tmp_path / "src").mkdir()
    src_file = tmp_path / "src" / "a.py"
    src_file.write_text("class Foo:\n    pass\n")

    matrix = {
        "items": [
            {"name": "algo mismatch", "status": "deviation (needs-judgment)"},
        ],
    }
    def fake_claude(workdir, timeout_s=None):
        time.sleep(0.05)  # ensure mtime moves
        src_file.write_text("class Foo:\n    def new_method(self): pass\n")
        return (True, "edited")
    with patch.object(run, "invoke_claude_code", side_effect=fake_claude):
        got = run._attempt_pre_pr_fidelity_patch(
            tmp_path, matrix, "https://github.com/x/y",
        )
    assert got is True
    # Brief is written to INVOCATION.md (what invoke_claude_code reads at startup).
    invoc = tmp_path / ".remyx-recommendation" / "INVOCATION.md"
    assert invoc.exists()
    assert "Fidelity remediation brief" in invoc.read_text()
