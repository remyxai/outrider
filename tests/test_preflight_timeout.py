"""Tests for the preflight-call timeout configurability.

The implementation-call timeout (`claude-timeout`, default 900s) now
also governs the preflight routing call. Customers who bumped the
timeout for a large monorepo or a slower non-default backend get the
same headroom on the preflight pass without needing to know about a
separate knob.

These tests verify the timeout parameter is honored end-to-end through
`preflight_routing` — that the underlying `_run_claude_oneshot` call
receives whatever timeout the caller passed.

Run with: pytest tests/test_preflight_timeout.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# ─── preflight_routing passes timeout through to _run_claude_oneshot ──


def _fake_workdir_with_spec(tmp_path):
    """The preflight function reads SPEC.md from the bundle dir; create
    a minimal one so the function reaches its agent call."""
    bundle = tmp_path / run.BUNDLE_DIR_NAME
    bundle.mkdir()
    (bundle / "SPEC.md").write_text("# minimal spec for test")
    return tmp_path


def test_preflight_passes_default_timeout(monkeypatch, tmp_path):
    """Direct callers (tests, ad-hoc) that don't pass a timeout get
    the documented 180s default — kept for backwards compatibility."""
    captured = {}

    def fake_oneshot(workdir, prompt, timeout_s):
        captured["timeout_s"] = timeout_s
        return (False, "")  # forces None return; we just care about timeout

    monkeypatch.setattr(run, "_run_claude_oneshot", fake_oneshot)
    monkeypatch.setattr(run, "_repo_layout_manifest", lambda *a, **k: "")

    run.preflight_routing(_fake_workdir_with_spec(tmp_path), "pkg")
    assert captured["timeout_s"] == 180


def test_preflight_honors_custom_timeout(monkeypatch, tmp_path):
    """When the caller passes a larger ceiling (mirroring what
    process_target does with target.claude_timeout_s), it flows through
    to the agent call unchanged."""
    captured = {}

    def fake_oneshot(workdir, prompt, timeout_s):
        captured["timeout_s"] = timeout_s
        return (False, "")

    monkeypatch.setattr(run, "_run_claude_oneshot", fake_oneshot)
    monkeypatch.setattr(run, "_repo_layout_manifest", lambda *a, **k: "")

    run.preflight_routing(
        _fake_workdir_with_spec(tmp_path), "pkg", timeout_s=1500,
    )
    assert captured["timeout_s"] == 1500


def test_preflight_short_timeout_for_tight_budgets(monkeypatch, tmp_path):
    """A customer capping cost can pass a small timeout; the value
    flows through verbatim (the CLI rejects sub-60s at the boundary,
    but the action itself doesn't second-guess what it receives)."""
    captured = {}

    def fake_oneshot(workdir, prompt, timeout_s):
        captured["timeout_s"] = timeout_s
        return (False, "")

    monkeypatch.setattr(run, "_run_claude_oneshot", fake_oneshot)
    monkeypatch.setattr(run, "_repo_layout_manifest", lambda *a, **k: "")

    run.preflight_routing(
        _fake_workdir_with_spec(tmp_path), "pkg", timeout_s=90,
    )
    assert captured["timeout_s"] == 90
