"""Tests for the cadence guard `open_remyx_artifact_exists`.

The guard's new semantic (2026-06-15): skip a run iff an *open* Remyx
PR or Issue from a prior run still exists on the target. Engagement
(merge or close) releases the gate. The prior sliding-window
("opened within N days") behavior is gone; the `rate_limit_days`
field on Target is reinterpreted as an on/off bit (>0 enables, <=0
disables) so existing workflow files continue to work.

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Target  # noqa: E402


def _pr(ref: str, html_url: str = "https://github.com/r/x/pull/1") -> dict:
    return {"head": {"ref": ref}, "html_url": html_url}


def _issue(title: str = "", body: str = "",
           html_url: str = "https://github.com/r/x/issues/1",
           is_pr: bool = False) -> dict:
    out = {"title": title, "body": body, "html_url": html_url}
    if is_pr:
        out["pull_request"] = {"url": "..."}
    return out


def _target(rate_limit_days: int = 7) -> Target:
    return Target(
        repo="r/x",
        interest_id="iid",
        min_confidence="moderate",
        draft_mode="always",
        rate_limit_days=rate_limit_days,
    )


# ─── happy path: gate fires when an open Remyx artifact exists ─────────────


def test_open_remyx_pr_fires_the_gate(monkeypatch):
    """A Remyx PR (`remyx-recommendation/*` branch) that's open blocks
    the next run — that's the whole point of the guard."""
    def fake_gh_api(method, path, body=None):
        if "/pulls?" in path:
            return [_pr(ref="remyx-recommendation/2606.06460v1")]
        return []
    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run.open_remyx_artifact_exists(_target()) is True


def test_open_remyx_issue_fires_the_gate(monkeypatch):
    """A Remyx Issue (identified by title prefix or body marker) that's
    open blocks the next run."""
    def fake_gh_api(method, path, body=None):
        if "/pulls?" in path:
            return []
        if "/issues?" in path:
            return [_issue(title="[Remyx Recommendation] foo")]
        return []
    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run.open_remyx_artifact_exists(_target()) is True


# ─── engagement releases the gate ──────────────────────────────────────────


def test_only_merged_or_closed_prs_do_not_fire(monkeypatch):
    """Once a Remyx PR is merged or closed, the gate releases — the
    GitHub API's `state=open` filter never surfaces them, so this is
    the simplest test: only open PRs come back, and if there are
    none, the gate is clear."""
    state_param: list[str] = []

    def fake_gh_api(method, path, body=None):
        # Capture the query so we can prove we ask only for state=open.
        if "/pulls?" in path:
            state_param.append(path)
            return []  # merged/closed don't satisfy state=open
        return []
    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run.open_remyx_artifact_exists(_target()) is False
    assert any("state=open" in p for p in state_param), \
        "gate must query only open PRs, not state=all"


# ─── on/off bit ────────────────────────────────────────────────────────────


def test_rate_limit_days_zero_disables_the_gate(monkeypatch):
    """`rate-limit-days: 0` in the workflow input disables the gate
    entirely — the guard returns False even if a Remyx PR is open."""
    def fake_gh_api(method, path, body=None):
        # Should never be reached when the gate is disabled.
        raise AssertionError("gate must not call gh_api when disabled")
    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run.open_remyx_artifact_exists(_target(rate_limit_days=0)) is False


# ─── non-Remyx artifacts don't fire the gate ───────────────────────────────


def test_non_remyx_open_pr_does_not_fire(monkeypatch):
    """A maintainer's own open PR (not on a `remyx-recommendation/*`
    branch) is irrelevant to the cadence guard."""
    def fake_gh_api(method, path, body=None):
        if "/pulls?" in path:
            return [_pr(ref="feat/some-maintainer-work")]
        return []
    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run.open_remyx_artifact_exists(_target()) is False


def test_open_issue_without_remyx_marker_does_not_fire(monkeypatch):
    """A random open Issue with no Remyx title prefix or body marker
    is irrelevant — the guard counts only Remyx-authored Issues."""
    def fake_gh_api(method, path, body=None):
        if "/pulls?" in path:
            return []
        if "/issues?" in path:
            return [_issue(title="Bug: foo broken", body="unrelated")]
        return []
    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run.open_remyx_artifact_exists(_target()) is False


def test_issues_endpoint_returns_prs_which_are_filtered(monkeypatch):
    """GitHub's /issues endpoint returns PRs too (they carry a
    'pull_request' key). The guard's Issue scan must ignore those so
    PRs aren't double-counted — they're already handled by the
    /pulls scan."""
    def fake_gh_api(method, path, body=None):
        if "/pulls?" in path:
            return []
        if "/issues?" in path:
            # Looks like a Remyx Issue by title, but it's actually a PR.
            return [_issue(title="[Remyx Recommendation] X",
                           body="...", is_pr=True)]
        return []
    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    # The PR-like Issue is filtered; no Remyx artifact reported.
    assert run.open_remyx_artifact_exists(_target()) is False
