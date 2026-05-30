"""Tests for path_matches_glob — specifically that `**` matches zero or
more path segments, so a top-level test file is allowlisted.

Regression: `tests/**/*.py` previously rejected `tests/test_foo.py` (a
file directly under tests/) because fnmatch's `*` crosses `/` but the
literal `/` after `**` still required at least one intermediate segment.
The §3 test gate expects tests at `tests/test_*.py`, so top-level test
files must be allowed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


ALLOW = [g.format(package="vqasynth") for g in run.DEFAULT_ALLOWLIST_GLOBS]


def test_top_level_test_file_is_allowed():
    # The exact file that the VQASynth validation run wrongly rejected.
    assert run.path_matches_glob("tests/test_visual_degradation.py", ALLOW)


def test_nested_test_file_still_allowed():
    assert run.path_matches_glob("tests/sub/test_x.py", ALLOW)


def test_package_files_allowed_top_and_nested():
    assert run.path_matches_glob("vqasynth/foo.py", ALLOW)
    assert run.path_matches_glob("vqasynth/sub/bar.py", ALLOW)


def test_bundle_and_readme_allowed():
    assert run.path_matches_glob(".remyx-recommendation/SPEC.md", ALLOW)
    assert run.path_matches_glob("README.md", ALLOW)


def test_out_of_allowlist_paths_rejected():
    # Non-.py infra files aren't covered by the default allowlist globs.
    assert not run.path_matches_glob(".github/workflows/x.yml", ALLOW)
    assert not run.path_matches_glob("Dockerfile", ALLOW)
    # `*.py` is intentionally broad (a wiring edit reaches the call site
    # wherever it lives); files like setup.py match the allowlist but are
    # protected by ALWAYS_BLOCKED precedence in validate_changes instead.
    assert run.path_matches_glob("setup.py", ALLOW)


def test_always_blocked_still_matches():
    # The never-touch list is role-based (filename/type), not directory-based.
    assert run.path_matches_glob(".github/workflows/ci.yml", run.ALWAYS_BLOCKED)
    assert run.path_matches_glob("docker/eval/Dockerfile", run.ALWAYS_BLOCKED)
    assert run.path_matches_glob("requirements.txt", run.ALWAYS_BLOCKED)
    assert run.path_matches_glob("setup.py", run.ALWAYS_BLOCKED)
    assert run.path_matches_glob("deep/nested/poetry.lock", run.ALWAYS_BLOCKED)


def test_case_insensitive_readme():
    # README.MD (uppercase) must match the README.md allowlist entry — the
    # case-sensitive matcher threw away an otherwise-valid PR over this.
    assert run.path_matches_glob("README.MD", ALLOW)
    assert run.path_matches_glob("readme.md", ALLOW)


def test_effective_allowlist_extends_defaults():
    base = [g.format(package="pkg") for g in run.DEFAULT_ALLOWLIST_GLOBS]
    t = run.Target(repo="o/r", guardrails_allowlist=["docs/**", "*.cfg"])
    eff = run.effective_allowlist(t, "pkg")
    for g in base:                 # defaults preserved...
        assert g in eff
    assert "docs/**" in eff and "*.cfg" in eff   # ...extras appended, not replacing


def test_effective_allowlist_empty_is_defaults():
    base = [g.format(package="pkg") for g in run.DEFAULT_ALLOWLIST_GLOBS]
    assert run.effective_allowlist(run.Target(repo="o/r"), "pkg") == base
