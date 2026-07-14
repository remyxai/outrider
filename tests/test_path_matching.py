"""Tests for the guardrails path checks — permissive allowlist + narrow
blocklist as of v1.7.24.

The old default (`*.py` + `.remyx-recommendation/**` + `**/*.md`) surfaced
in production as the top false-negative: canonical extension points (task
YAML, adapter JSON, .gitignore, lockfiles) were rejected while human
review remained the actual safety layer. Path allowlisting is now
permissive-by-default; only `.github/workflows/**` is blocked as a fixed
guard against agent-authored edits that could silently expand this run's
own future agency. Teams that want tighter blocking add entries via
`guardrails-blocklist`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


ALLOW = [g.format(package="vqasynth") for g in run.DEFAULT_ALLOWLIST_GLOBS]


def test_default_allowlist_is_permissive():
    """Every reasonable path matches the default allowlist."""
    for p in (
        "tests/test_visual_degradation.py",
        "tests/sub/test_x.py",
        "vqasynth/foo.py",
        "vqasynth/sub/bar.py",
        ".remyx-recommendation/SPEC.md",
        "README.md",
        "examples/agent_patterns/README.md",
        "lm_eval/tasks/holmes/holmes.yaml",
        "lm_eval/tasks/holmes/_holmes_template_yaml",
        "method_comparison/experiments/rank8/adapter_config.json",
        ".gitignore",
        ".dockerignore",
        "uv.lock",
        "setup.py",
        "pyproject.toml",
        "Dockerfile",
    ):
        assert run.path_matches_glob(p, ALLOW), f"expected {p!r} in allowlist"


def test_default_blocklist_is_workflow_only():
    """The fixed ALWAYS_BLOCKED covers exactly the one agency-expansion risk."""
    assert run.ALWAYS_BLOCKED == [".github/workflows/**"]
    assert run.path_matches_glob(".github/workflows/ci.yml", run.ALWAYS_BLOCKED)
    assert run.path_matches_glob(".github/workflows/outrider.yml", run.ALWAYS_BLOCKED)
    # Everything else that used to be in ALWAYS_BLOCKED (Dockerfile, lockfiles,
    # setup.py, requirements.txt, *.sh) is no longer blocked by default —
    # human review is the safety layer for those.
    for p in (
        "Dockerfile",
        "docker/eval/Dockerfile",
        "requirements.txt",
        "setup.py",
        "pyproject.toml",
        "uv.lock",
        "deep/nested/poetry.lock",
        "scripts/run.sh",
        ".github/dependabot.yml",  # non-workflow .github/ file
    ):
        assert not run.path_matches_glob(p, run.ALWAYS_BLOCKED), (
            f"expected {p!r} not in default blocklist"
        )


def test_effective_blocklist_extends_defaults():
    """User guardrails-blocklist is appended to ALWAYS_BLOCKED."""
    t = run.Target(repo="o/r", guardrails_blocklist=["secrets/**", "*.lock"])
    blk = run.effective_blocklist(t)
    assert ".github/workflows/**" in blk
    assert "secrets/**" in blk
    assert "*.lock" in blk


def test_effective_blocklist_empty_is_workflow_only():
    """No blocklist configured = only the fixed workflow guard."""
    assert run.effective_blocklist(run.Target(repo="o/r")) == list(run.ALWAYS_BLOCKED)


def test_effective_allowlist_still_appends_deprecated_input():
    """guardrails-allowlist is a no-op semantically (default already matches
    everything) but the input still round-trips through effective_allowlist
    for backwards compat until v2 removes it."""
    base = [g.format(package="pkg") for g in run.DEFAULT_ALLOWLIST_GLOBS]
    t = run.Target(repo="o/r", guardrails_allowlist=["docs/**", "*.cfg"])
    eff = run.effective_allowlist(t, "pkg")
    for g in base:
        assert g in eff
    assert "docs/**" in eff and "*.cfg" in eff


def test_effective_allowlist_empty_is_defaults():
    base = [g.format(package="pkg") for g in run.DEFAULT_ALLOWLIST_GLOBS]
    assert run.effective_allowlist(run.Target(repo="o/r"), "pkg") == base


def test_case_insensitive_matching_preserved():
    """Case-insensitive matcher stayed — earlier regression rejected README.MD."""
    assert run.path_matches_glob("README.MD", ALLOW)
    assert run.path_matches_glob("readme.md", ALLOW)
    assert run.path_matches_glob(".GITHUB/WORKFLOWS/CI.YML", run.ALWAYS_BLOCKED)
