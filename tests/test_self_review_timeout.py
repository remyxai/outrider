"""Tests for the self-review-call timeout configurability.

The implementation-call timeout (`claude-timeout`, default 900s) now
also governs the self-review pass over the produced diff. Customers
who bumped the timeout for a large monorepo or a slower non-default
backend get the same headroom on review without needing to know about
a separate knob — completing the per-stage consolidation started in
v1.6.28-30 (preflight + audit + selection).

These tests verify the timeout parameter is honored end-to-end through
`self_review_diff` — that the underlying `_run_claude_oneshot` call
receives whatever timeout the caller passed.

Run with: pytest tests/test_self_review_timeout.py -q
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# ─── self_review_diff passes timeout through to _run_claude_oneshot ──


def _fake_workdir(tmp_path, monkeypatch):
    """Self-review reads `git diff HEAD` from the workdir. Stub the
    subprocess call so the function reaches its agent call without
    needing a real git repo."""
    class _Proc:
        stdout = "diff --git a/x b/x\n+++ b/x\n+new line\n"
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _Proc(),
    )
    return tmp_path


def test_self_review_passes_default_timeout(monkeypatch, tmp_path):
    """Direct callers (tests, ad-hoc) that don't pass a timeout get
    the documented 180s default — kept for backwards compatibility."""
    captured = {}

    def fake_oneshot(workdir, prompt, timeout_s):
        captured["timeout_s"] = timeout_s
        return (False, "")  # forces None return; we just care about timeout

    monkeypatch.setattr(run, "_run_claude_oneshot", fake_oneshot)

    run.self_review_diff(_fake_workdir(tmp_path, monkeypatch))
    assert captured["timeout_s"] == 180


def test_self_review_honors_custom_timeout(monkeypatch, tmp_path):
    """When the caller passes a larger ceiling (mirroring what
    process_target does with target.claude_timeout_s), it flows
    through to the agent call unchanged."""
    captured = {}

    def fake_oneshot(workdir, prompt, timeout_s):
        captured["timeout_s"] = timeout_s
        return (False, "")

    monkeypatch.setattr(run, "_run_claude_oneshot", fake_oneshot)

    run.self_review_diff(
        _fake_workdir(tmp_path, monkeypatch), timeout_s=1500,
    )
    assert captured["timeout_s"] == 1500


def test_self_review_short_timeout_for_tight_budgets(monkeypatch, tmp_path):
    """A customer capping cost can pass a small timeout; the value
    flows through verbatim (the CLI rejects sub-60s at the boundary,
    but the action itself doesn't second-guess what it receives)."""
    captured = {}

    def fake_oneshot(workdir, prompt, timeout_s):
        captured["timeout_s"] = timeout_s
        return (False, "")

    monkeypatch.setattr(run, "_run_claude_oneshot", fake_oneshot)

    run.self_review_diff(
        _fake_workdir(tmp_path, monkeypatch), timeout_s=90,
    )
    assert captured["timeout_s"] == 90
