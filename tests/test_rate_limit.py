"""Tests for the cadence guard `open_remyx_artifact_exists`.

Semantic (2026-06-29): time-decayed. Skip a run only if
the most recently opened Remyx PR/Issue on the target is younger than
``rate_limit_days``. Older open artifacts age out of the throttle
window and stop blocking — recognizing that real maintainers leave
Issues open for weeks without active triage, and Outrider should
resume cadence rather than mute the repo indefinitely.

History:
- pre-2026-06-15: sliding-window "opened within N days" (numeric).
- 2026-06-15: simplified to on/off (any open Remyx artifact blocks
  indefinitely until engagement). Found too restrictive in practice.
- 2026-06-29 (this revision): restored the sliding window, but now
  computed against the *most recent* open artifact's age — same on/off
  escape hatch (`rate_limit_days <= 0` disables).

Run with: pytest tests/ -q
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Target  # noqa: E402


def _iso_days_ago(days: int) -> str:
    """ISO-8601 timestamp ``days`` ago, GitHub-style ``Z`` suffix."""
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pr(ref: str, html_url: str = "https://github.com/r/x/pull/1",
        created_at: str | None = None) -> dict:
    return {
        "head": {"ref": ref},
        "html_url": html_url,
        "created_at": created_at or _iso_days_ago(0),
    }


def _issue(title: str = "", body: str = "",
           html_url: str = "https://github.com/r/x/issues/1",
           is_pr: bool = False,
           created_at: str | None = None) -> dict:
    out = {
        "title": title, "body": body, "html_url": html_url,
        "created_at": created_at or _iso_days_ago(0),
    }
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


# ─── happy path: a recent open Remyx artifact fires the gate ─────────────


def test_open_remyx_pr_fires_the_gate_when_recent(monkeypatch):
    """A Remyx PR (`remyx-recommendation/*` branch) opened today blocks
    the next run — within the throttle window."""
    def fake_gh_api(method, path, body=None):
        if "/pulls?" in path:
            return [_pr(ref="remyx-recommendation/2606.06460v1",
                        created_at=_iso_days_ago(0))]
        return []
    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run.open_remyx_artifact_exists(_target()) is True


def test_open_remyx_issue_fires_the_gate_when_recent(monkeypatch):
    """A Remyx Issue (identified by title prefix or body marker) opened
    today blocks the next run."""
    def fake_gh_api(method, path, body=None):
        if "/pulls?" in path:
            return []
        if "/issues?" in path:
            return [_issue(title="[Remyx Recommendation] foo",
                           created_at=_iso_days_ago(0))]
        return []
    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run.open_remyx_artifact_exists(_target()) is True


# ─── time-decay: stale open artifacts no longer block ──────────────────────


def test_stale_open_pr_ages_out_of_the_throttle(monkeypatch):
    """A Remyx PR opened 30 days ago no longer blocks runs under the
    default 7-day window — the core time-decay behavior. Real maintainers
    leave PRs open for weeks; the action should resume cadence rather
    than mute the repo indefinitely."""
    def fake_gh_api(method, path, body=None):
        if "/pulls?" in path:
            return [_pr(ref="remyx-recommendation/2606.06460v1",
                        created_at=_iso_days_ago(30))]
        return []
    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run.open_remyx_artifact_exists(_target(rate_limit_days=7)) is False


def test_stale_open_issue_ages_out_of_the_throttle(monkeypatch):
    """Same as above, for Issues."""
    def fake_gh_api(method, path, body=None):
        if "/pulls?" in path:
            return []
        if "/issues?" in path:
            return [_issue(title="[Remyx Recommendation] foo",
                           created_at=_iso_days_ago(21))]
        return []
    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run.open_remyx_artifact_exists(_target(rate_limit_days=7)) is False


def test_strict_window_still_blocks_a_recent_artifact(monkeypatch):
    """A higher `rate_limit_days` value extends the throttle window —
    `rate-limit-days: 30` still blocks on a 5-day-old open Issue."""
    def fake_gh_api(method, path, body=None):
        if "/pulls?" in path:
            return []
        if "/issues?" in path:
            return [_issue(title="[Remyx Recommendation] foo",
                           created_at=_iso_days_ago(5))]
        return []
    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run.open_remyx_artifact_exists(_target(rate_limit_days=30)) is True


def test_youngest_artifact_drives_the_gate(monkeypatch):
    """When multiple open Remyx artifacts exist, the *most recent* one
    drives the throttle decision — older co-existing artifacts don't
    matter for cadence purposes. The per-paper discharge filter handles
    same-paper retries independently."""
    def fake_gh_api(method, path, body=None):
        if "/pulls?" in path:
            return [
                _pr(ref="remyx-recommendation/old",
                    created_at=_iso_days_ago(30)),  # aged out
                _pr(ref="remyx-recommendation/new",
                    created_at=_iso_days_ago(2)),   # within window
            ]
        return []
    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run.open_remyx_artifact_exists(_target(rate_limit_days=7)) is True


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


# ─── on/off escape hatch ───────────────────────────────────────────────────


def test_rate_limit_days_zero_disables_the_gate(monkeypatch):
    """`rate-limit-days: 0` in the workflow input disables the gate
    entirely — the guard returns False even if a Remyx PR is open. No
    API calls are made (cheap-circuit)."""
    def fake_gh_api(method, path, body=None):
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


# ─── age helper ────────────────────────────────────────────────────────────


def test_age_helper_returns_smallest_age(monkeypatch):
    """`_most_recent_open_artifact_age_days` returns the smallest age
    when multiple open Remyx artifacts exist — that's the artifact the
    throttle cares about."""
    def fake_gh_api(method, path, body=None):
        if "/pulls?" in path:
            return [
                _pr(ref="remyx-recommendation/old",
                    created_at=_iso_days_ago(30)),
                _pr(ref="remyx-recommendation/new",
                    created_at=_iso_days_ago(2)),
            ]
        return []
    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run._most_recent_open_artifact_age_days(_target()) == 2


def test_age_helper_returns_none_when_no_artifacts(monkeypatch):
    """When no open Remyx artifact exists, the helper returns None
    (signal for the caller to not fire the gate at all)."""
    def fake_gh_api(method, path, body=None):
        return []
    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run._most_recent_open_artifact_age_days(_target()) is None
