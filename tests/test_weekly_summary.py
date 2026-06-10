"""Tests for weekly Discussion summary mode:

  - `gh_graphql` posts to the GraphQL endpoint, returns `data`, and
    raises on GraphQL-level errors (same error shape as gh_api)
  - `_resolve_discussion_id` passes node IDs through and resolves
    Discussion numbers via one GraphQL query
  - `_extract_run_summary` parses the RUN SUMMARY JSON out of
    timestamp-prefixed Actions log text
  - `_aggregate_week` sums verified costs, merges license-class counts,
    preserves selection-reasoning quotes verbatim, and counts
    retention gaps instead of dropping them
  - `_compose_weekly_markdown` renders the digest: verbatim blockquote,
    honest retention rows, next-action column, patterns only when the
    LLM call succeeded
  - `run_weekly_summary` skips cleanly without REMYX_WEEKLY_DISCUSSION_ID
    and posts + threads the comment URL when configured

Run with: pytest tests/ -q
"""
import datetime as dt
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Target  # noqa: E402


def _target():
    return Target(repo="example/repo", interest_id="iid")


# ─── gh_graphql ───────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_gh_graphql_returns_data(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    seen = {}

    def fake_urlopen(req, timeout=30):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        seen["auth"] = req.headers.get("Authorization")
        return _FakeResponse({"data": {"ok": True}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    data = run.gh_graphql("query { viewer { login } }", {"a": 1})
    assert data == {"ok": True}
    assert seen["url"] == "https://api.github.com/graphql"
    assert seen["body"]["query"].startswith("query")
    assert seen["body"]["variables"] == {"a": 1}
    assert seen["auth"] == "Bearer t0ken"


def test_gh_graphql_raises_on_graphql_errors(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=30: _FakeResponse(
            {"errors": [{"message": "Could not resolve to a node"}]}
        ),
    )
    with pytest.raises(RuntimeError, match="GraphQL errors"):
        run.gh_graphql("query { x }")


def test_gh_graphql_requires_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("INPUT_GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        run.gh_graphql("query { x }")


# ─── _resolve_discussion_id ───────────────────────────────────────────────


def test_resolve_discussion_id_passes_node_id_through(monkeypatch):
    monkeypatch.setattr(run, "gh_graphql", lambda *a, **k: pytest.fail(
        "node IDs must not trigger a GraphQL lookup"
    ))
    assert run._resolve_discussion_id(_target(), "D_kwDOAbc123") == "D_kwDOAbc123"


def test_resolve_discussion_id_resolves_number(monkeypatch):
    def fake_graphql(query, variables=None):
        assert variables == {"owner": "example", "name": "repo", "number": 19}
        return {"repository": {"discussion": {"id": "D_resolved"}}}

    monkeypatch.setattr(run, "gh_graphql", fake_graphql)
    assert run._resolve_discussion_id(_target(), "19") == "D_resolved"


def test_resolve_discussion_id_raises_when_number_missing(monkeypatch):
    monkeypatch.setattr(
        run, "gh_graphql",
        lambda *a, **k: {"repository": {"discussion": None}},
    )
    with pytest.raises(RuntimeError, match="not found"):
        run._resolve_discussion_id(_target(), "404")


# ─── _extract_run_summary ─────────────────────────────────────────────────


_SUMMARY = {
    "repo": "example/repo",
    "status": "skipped_by_selection_verification",
    "selection_reasoning": "Every in-pool candidate is a VLM architecture.",
    "cost_usd": 1.01,
    "license_class_counts": {"permissive": 4, "missing": 30},
    "refine_queries": ["depth successor single pass"],
}


def _log_text(summary: dict) -> str:
    body = "\n".join(
        f"2026-06-08T14:00:0{i % 10}.1234567Z {line}"
        for i, line in enumerate(
            ["some earlier output", "=== RUN SUMMARY ==="]
            + json.dumps(summary, indent=2).splitlines()
            + ["trailing line"]
        )
    )
    return body


def test_extract_run_summary_parses_timestamped_log():
    assert run._extract_run_summary(_log_text(_SUMMARY)) == _SUMMARY


def test_extract_run_summary_none_without_marker():
    assert run._extract_run_summary("2026-06-08T14:00:00Z hello\n") is None


def test_extract_run_summary_none_on_truncated_json():
    text = _log_text(_SUMMARY)
    truncated = text[: text.index('"cost_usd"')]
    assert run._extract_run_summary(truncated) is None


# ─── _aggregate_week ──────────────────────────────────────────────────────


def _entry(summary, created="2026-06-08T14:00:00Z", conclusion="success"):
    return {
        "run": {"id": 1, "created_at": created, "conclusion": conclusion},
        "summary": summary,
    }


def test_aggregate_week_sums_and_merges():
    entries = [
        _entry(_SUMMARY),
        _entry({
            "status": "issue_opened_preflight",
            "issue_url": "https://github.com/example/repo/issues/92",
            "cost_usd": 2.5,
            "license_class_counts": {"permissive": 1, "nc": 2},
        }, created="2026-06-07T14:00:00Z"),
        _entry(None, created="2026-06-02T14:00:00Z", conclusion="failure"),
    ]
    agg = run._aggregate_week(entries)
    assert agg["n_runs"] == 3
    assert agg["n_success"] == 2
    assert agg["n_failed"] == 1
    assert agg["verified_cost"] == pytest.approx(3.51)
    assert agg["unverified_runs"] == 1
    assert agg["license_class_counts"] == {
        "permissive": 5, "missing": 30, "nc": 2,
    }
    # Verbatim quote captured from the verification-skip run.
    assert agg["selection_quotes"] == [
        "Every in-pool candidate is a VLM architecture."
    ]
    assert agg["refine_queries"] == ["depth successor single pass"]
    # Artifact URLs render as #-links; skips as "No artifact"; retention
    # gaps as an explicit placeholder row.
    outputs = [r["output"] for r in agg["rows"]]
    assert "No artifact" in outputs
    assert "[#92](https://github.com/example/repo/issues/92)" in outputs
    assert "—" in outputs


# ─── _compose_weekly_markdown ─────────────────────────────────────────────


def _compose(agg=None, open_issues=None, drafted=None):
    agg = agg or run._aggregate_week([_entry(_SUMMARY)])
    start = dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 6, 9, tzinfo=dt.timezone.utc)
    return run._compose_weekly_markdown(
        start, end, agg, open_issues or [], drafted,
    )


def test_compose_preserves_selection_quote_verbatim():
    body = _compose()
    assert "> Every in-pool candidate is a VLM architecture." in body


def test_compose_renders_retention_gap_row():
    agg = run._aggregate_week([_entry(None, conclusion="failure")])
    body = _compose(agg=agg)
    assert "outside log retention — details unavailable" in body


def test_compose_patterns_only_when_drafted():
    no_draft = _compose(drafted=None)
    assert "Patterns worth attention" not in no_draft
    drafted = _compose(drafted={
        "patterns": ["The dedup gate fired twice — review Issue #87."],
    })
    assert "### Patterns worth attention" in drafted
    assert "1. The dedup gate fired twice — review Issue #87." in drafted


def test_compose_open_issue_next_action_column():
    body = _compose(
        open_issues=[{
            "number": 93, "title": "CLIP4DM",
            "html_url": "https://github.com/example/repo/issues/93",
        }],
        drafted={"next_actions": {"93": "flip the default-False flag"}},
    )
    assert "| [#93](https://github.com/example/repo/issues/93) " in body
    assert "flip the default-False flag" in body


def test_compose_open_issue_without_next_action_gets_dash():
    body = _compose(open_issues=[{
        "number": 85, "title": "Beyond 3D VQAs",
        "html_url": "https://github.com/example/repo/issues/85",
    }])
    assert "| [#85](https://github.com/example/repo/issues/85) | Beyond 3D VQAs | — |" in body


def test_compose_license_table_canonical_order():
    body = _compose()
    perm_idx = body.index("| permissive | 4 |")
    missing_idx = body.index("| missing | 30 |")
    assert perm_idx < missing_idx


def test_compose_flags_unverified_cost():
    agg = run._aggregate_week([_entry(_SUMMARY), _entry(None)])
    body = _compose(agg=agg)
    assert "1 run(s) outside log retention not counted" in body


# ─── run_weekly_summary ───────────────────────────────────────────────────


def test_weekly_skips_cleanly_without_discussion_id(monkeypatch):
    monkeypatch.delenv("REMYX_WEEKLY_DISCUSSION_ID", raising=False)
    result = run.run_weekly_summary(_target())
    assert result["status"] == "weekly_summary_skipped_no_discussion_id"


def test_weekly_posts_and_threads_url(monkeypatch):
    monkeypatch.setenv("REMYX_WEEKLY_DISCUSSION_ID", "D_node")
    posted = {}

    monkeypatch.setattr(run, "_fetch_week_runs",
                        lambda target, since: [_entry(_SUMMARY)])
    monkeypatch.setattr(run, "_remyx_issues", lambda target, state: [])
    monkeypatch.setattr(run, "_draft_weekly_narrative", lambda agg, oi: None)

    def fake_post(discussion_id, body):
        posted["id"] = discussion_id
        posted["body"] = body
        return "https://github.com/example/repo/discussions/19#comment-1"

    monkeypatch.setattr(run, "_post_discussion_comment", fake_post)
    result = run.run_weekly_summary(_target())
    assert result["status"] == "weekly_summary_posted"
    assert result["discussion_comment_url"].endswith("#comment-1")
    assert result["runs_aggregated"] == 1
    assert posted["id"] == "D_node"
    assert "## Outrider weekly summary" in posted["body"]
    # Data-only degrade: the digest still posted without drafted sections.
    assert "Patterns worth attention" not in posted["body"]


# ─── _fetch_run_log_text (zip handling) ───────────────────────────────────


class _FakeZipResponse:
    def __init__(self, blob: bytes):
        self._blob = blob

    def read(self):
        return self._blob

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_run_log_text_finds_summary_member(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("0_other.txt", "no marker here")
        z.writestr("1_recommend.txt", _log_text(_SUMMARY))
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=60: _FakeZipResponse(buf.getvalue()),
    )
    text = run._fetch_run_log_text("example/repo", 42)
    assert text is not None and "=== RUN SUMMARY ===" in text


def test_fetch_run_log_text_none_on_http_error(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")

    def boom(req, timeout=60):
        raise urllib.error.HTTPError(req.full_url, 410, "Gone", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert run._fetch_run_log_text("example/repo", 42) is None
