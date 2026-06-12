"""Tests for the weekly-summary lifecycle-events helpers (REMYX-114).

Covers:
  - `_is_bot_actor` — bot detection across the GitHub user-dict shapes
  - `_is_outrider_artifact` — title prefix + body marker + branch prefix
    identification, including new-format (no title prefix) artifacts
  - `_parse_iso` + `_relative_when` — timestamp helpers
  - `_lifecycle_events_for_outrider_artifacts` — main detector against
    mocked `gh_api` responses (Outrider Issue closed in window, new
    maintainer comments, PR merged, non-Outrider items filtered out)
  - `_render_lifecycle_events_section` — markdown shape per event kind +
    empty-list short-circuit

Run with: pytest tests/test_lifecycle_events.py -q
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Target  # noqa: E402


# ─── _is_bot_actor ────────────────────────────────────────────────────────


def test_is_bot_actor_type_bot() -> None:
    assert run._is_bot_actor({"login": "someone", "type": "Bot"})
    assert run._is_bot_actor({"login": "someone", "type": "bot"})


def test_is_bot_actor_known_bot_logins() -> None:
    assert run._is_bot_actor({"login": "remyx-ai[bot]", "type": "User"})
    assert run._is_bot_actor({"login": "github-actions[bot]", "type": "User"})


def test_is_bot_actor_human_user() -> None:
    assert not run._is_bot_actor({"login": "smellslikeml", "type": "User"})


def test_is_bot_actor_none_or_empty() -> None:
    assert run._is_bot_actor(None)
    assert run._is_bot_actor({})


# ─── _is_outrider_artifact ────────────────────────────────────────────────


def test_is_outrider_artifact_title_prefix() -> None:
    item = {"title": "[Remyx Recommendation] Some Paper", "body": ""}
    assert run._is_outrider_artifact(item)


def test_is_outrider_artifact_body_marker_new_format() -> None:
    # New-format PRs (post-v1.5.4) have no title prefix but the body
    # always contains the marker via the orchestrator-built footer.
    item = {
        "title": "Some Paper Title",
        "body": (
            "## Summary\n...\n\n_Opened by the [Remyx Recommendation]"
            "(https://engine.remyx.ai) orchestrator._"
        ),
    }
    assert run._is_outrider_artifact(item)


def test_is_outrider_artifact_branch_prefix_legacy() -> None:
    # Historical PRs with the legacy branch prefix.
    item = {
        "title": "Some Title", "body": "",
        "pull_request": {"head": {"ref": "remyx-recommendation/2606.06460v1"}},
    }
    assert run._is_outrider_artifact(item)


def test_is_outrider_artifact_unrelated_pr_excluded() -> None:
    item = {
        "title": "Bump dependency X to v2.0", "body": "Standard dep bump",
        "pull_request": {"head": {"ref": "deps/bump-x"}},
    }
    assert not run._is_outrider_artifact(item)


# ─── _parse_iso + _relative_when ──────────────────────────────────────────


def test_parse_iso_handles_z_suffix() -> None:
    when = run._parse_iso("2026-06-12T15:30:00Z")
    assert when is not None
    assert when.year == 2026 and when.month == 6 and when.day == 12


def test_parse_iso_handles_offset_suffix() -> None:
    when = run._parse_iso("2026-06-12T15:30:00+00:00")
    assert when is not None


def test_parse_iso_returns_none_on_empty() -> None:
    assert run._parse_iso("") is None
    assert run._parse_iso(None) is None  # type: ignore[arg-type]
    assert run._parse_iso("not a date") is None


def test_relative_when_labels() -> None:
    now = dt.datetime(2026, 6, 12, 12, 0, 0, tzinfo=dt.timezone.utc)
    assert run._relative_when(now, now) == "today"
    assert (
        run._relative_when(now - dt.timedelta(days=1), now) == "yesterday"
    )
    assert run._relative_when(now - dt.timedelta(days=3), now) == "3 days ago"
    assert run._relative_when(now - dt.timedelta(days=7), now) == "7 days ago"


# ─── _lifecycle_events_for_outrider_artifacts ────────────────────────────


def _target() -> Target:
    return Target(repo="owner/repo", interest_id="iid")


def test_lifecycle_events_picks_up_outrider_artifacts(monkeypatch) -> None:
    """Mock gh_api to return one Outrider Issue with a maintainer comment
    + one unrelated Issue; verify only the Outrider one shows up."""
    window_end = dt.datetime(2026, 6, 12, 12, 0, 0, tzinfo=dt.timezone.utc)
    window_start = window_end - dt.timedelta(days=7)

    def fake_gh_api(method, path):
        if "/comments" in path and "/issues/42/" in path:
            return [
                {
                    "user": {"login": "smellslikeml", "type": "User"},
                    "created_at": "2026-06-10T14:00:00Z",
                    "body": "Looks great — adopting next sprint.",
                }
            ]
        if "/comments" in path and "/issues/" in path:
            return []
        # The issues-list endpoint
        return [
            {
                "number": 42,
                "title": "[Remyx Recommendation] Some Paper",
                "body": "...",
                "html_url": "https://github.com/owner/repo/issues/42",
                "state": "open",
                "created_at": "2026-05-15T10:00:00Z",  # outside window
                "user": {"login": "remyx-ai[bot]"},
                "pull_request": None,
            },
            {
                "number": 99,
                "title": "Unrelated maintainer issue",
                "body": "Regular community bug report",
                "html_url": "https://github.com/owner/repo/issues/99",
                "state": "open",
                "created_at": "2026-06-11T10:00:00Z",
                "user": {"login": "community-member"},
                "pull_request": None,
            },
        ]

    monkeypatch.setattr(run, "gh_api", fake_gh_api)

    events = run._lifecycle_events_for_outrider_artifacts(
        _target(), window_start, window_end,
    )

    # Only the Outrider Issue's maintainer comment should surface
    assert len(events) == 1
    assert events[0]["number"] == 42
    assert events[0]["kind"] == "comment"
    assert events[0]["actor"] == "smellslikeml"
    assert "adopting next sprint" in events[0]["summary"]


def test_lifecycle_events_surfaces_merged_pr(monkeypatch) -> None:
    window_end = dt.datetime(2026, 6, 12, 12, 0, 0, tzinfo=dt.timezone.utc)
    window_start = window_end - dt.timedelta(days=7)

    def fake_gh_api(method, path):
        if "/comments" in path:
            return []
        return [
            {
                "number": 14,
                "title": "[Remyx Recommendation] FlashAttention v3 integration",
                "body": "...",
                "html_url": "https://github.com/owner/repo/pull/14",
                "state": "closed",
                "created_at": "2026-05-20T10:00:00Z",
                "closed_at": "2026-06-10T10:00:00Z",
                "user": {"login": "remyx-ai[bot]"},
                "pull_request": {
                    "merged_at": "2026-06-10T10:00:00Z",
                    "head": {"ref": "remyx-recommendation/2510.99999"},
                },
            },
        ]

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    events = run._lifecycle_events_for_outrider_artifacts(
        _target(), window_start, window_end,
    )

    assert len(events) == 1
    assert events[0]["number"] == 14
    assert events[0]["kind"] == "merged"
    assert events[0]["kind_prefix"] == "PR"


def test_lifecycle_events_filters_bot_comments(monkeypatch) -> None:
    """A comment by github-actions[bot] should NOT show up as a lifecycle
    event — that's our own follow-up, not a maintainer signal."""
    window_end = dt.datetime(2026, 6, 12, 12, 0, 0, tzinfo=dt.timezone.utc)
    window_start = window_end - dt.timedelta(days=7)

    def fake_gh_api(method, path):
        if "/comments" in path:
            return [
                {
                    "user": {"login": "github-actions[bot]", "type": "Bot"},
                    "created_at": "2026-06-11T10:00:00Z",
                    "body": "CI passed",
                }
            ]
        return [{
            "number": 1, "title": "[Remyx Recommendation] Paper", "body": "",
            "html_url": "https://github.com/o/r/issues/1", "state": "open",
            "created_at": "2026-05-01T10:00:00Z",
            "user": {"login": "remyx-ai[bot]"},
            "pull_request": None,
        }]

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    events = run._lifecycle_events_for_outrider_artifacts(
        _target(), window_start, window_end,
    )

    assert events == []


def test_lifecycle_events_handles_gh_api_failure(monkeypatch) -> None:
    """If the issues-list call fails, return [] cleanly — the digest
    should still post without the lifecycle section."""

    def fake_gh_api(method, path):
        raise RuntimeError("simulated 503")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    window_end = dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc)
    events = run._lifecycle_events_for_outrider_artifacts(
        _target(), window_end - dt.timedelta(days=7), window_end,
    )
    assert events == []


def test_lifecycle_events_caps_at_max(monkeypatch) -> None:
    """With many events, the cap is enforced and terminal events
    (priority 0 — merged/closed) are preserved over intermediate ones."""
    window_end = dt.datetime(2026, 6, 12, 12, 0, 0, tzinfo=dt.timezone.utc)
    window_start = window_end - dt.timedelta(days=7)

    # Build many Outrider artifacts — 3 with closed events + many with
    # comment events. The cap should keep the 3 terminal events plus
    # fill with the most recent comments.
    items = []
    for i in range(3):
        items.append({
            "number": 1000 + i,
            "title": "[Remyx Recommendation] Closed paper",
            "body": "", "html_url": f"https://github.com/o/r/issues/{1000 + i}",
            "state": "closed",
            "created_at": "2026-05-01T10:00:00Z",
            "closed_at": f"2026-06-{8 + i:02d}T12:00:00Z",
            "user": {"login": "remyx-ai[bot]"},
            "pull_request": None,
        })
    for i in range(20):
        items.append({
            "number": 2000 + i,
            "title": "[Remyx Recommendation] Open paper with comment",
            "body": "", "html_url": f"https://github.com/o/r/issues/{2000 + i}",
            "state": "open",
            "created_at": "2026-05-01T10:00:00Z",
            "user": {"login": "remyx-ai[bot]"},
            "pull_request": None,
        })

    def fake_gh_api(method, path):
        if "/comments" in path:
            num = int(path.split("/issues/")[1].split("/")[0])
            if num >= 2000:
                return [
                    {
                        "user": {"login": "smellslikeml", "type": "User"},
                        "created_at": "2026-06-11T10:00:00Z",
                        "body": f"comment on {num}",
                    }
                ]
            return []
        return items

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    events = run._lifecycle_events_for_outrider_artifacts(
        _target(), window_start, window_end, max_events=5,
    )

    assert len(events) == 5
    # The 3 closed events (priority 0) should be in the first 3 slots
    closed_count = sum(1 for e in events if e["kind"] == "closed")
    assert closed_count == 3


# ─── _render_lifecycle_events_section ─────────────────────────────────────


def test_render_empty_events_returns_empty_list() -> None:
    """No events -> no section header (skipped entirely per acceptance criteria)."""
    now = dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc)
    assert run._render_lifecycle_events_section([], now) == []


def test_render_merged_pr() -> None:
    now = dt.datetime(2026, 6, 12, 12, 0, 0, tzinfo=dt.timezone.utc)
    events = [{
        "number": 14, "title": "FlashAttention v3 integration",
        "url": "https://github.com/o/r/pull/14",
        "kind_prefix": "PR", "kind": "merged",
        "when": now - dt.timedelta(days=2), "actor": "maintainer",
        "priority": 0,
    }]
    lines = run._render_lifecycle_events_section(events, now)
    body = "\n".join(lines)
    assert "Recent activity on Outrider Issues/PRs" in body
    assert "PR #14" in body
    assert "merged 2 days ago" in body


def test_render_comment_event() -> None:
    now = dt.datetime(2026, 6, 12, 12, 0, 0, tzinfo=dt.timezone.utc)
    events = [{
        "number": 87, "title": "InstructSAM recommendation",
        "url": "https://github.com/o/r/issues/87",
        "kind_prefix": "Issue", "kind": "comment",
        "when": now - dt.timedelta(days=1), "actor": "smellslikeml",
        "summary": "licensing audit showing InstructSAM has no declared license",
        "priority": 1,
    }]
    lines = run._render_lifecycle_events_section(events, now)
    body = "\n".join(lines)
    assert "Issue #87" in body
    assert "@smellslikeml" in body
    assert "yesterday" in body
    assert "licensing audit" in body
