"""Tests for the v1.4.7 symmetric-discharge behavior:

  - `_remyx_issues(state="open")` returns only open Outrider Issues
    (back-compat with v1.4.6)
  - `_remyx_issues(state="all")` returns open + closed
  - `_all_remyx_issues()` convenience wrapper uses state=all
  - `open_remyx_issues()` legacy callers still get open-only
  - `issue_for_paper` matches against closed Issues when included in
    the input set — the dedup logic is independent of state
  - `open_issue` default footer includes the re-engagement note
  - Step summary differentiates "Already in flight" (open) vs
    "Already addressed" (closed)

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Recommendation, Target  # noqa: E402


# ─── _remyx_issues / _all_remyx_issues ───────────────────────────────────


def _issue(title, body, state="open"):
    return {"title": title, "body": body, "state": state}


def test_remyx_issues_filters_state_open(monkeypatch):
    """Default state=open passes through to the GitHub API."""
    calls: list[str] = []

    def fake_gh_api(method, path, body=None):
        calls.append(path)
        return [
            _issue("[Remyx Recommendation] X",
                   "arxiv.org/abs/2502.20110", "open"),
        ]

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    out = run._remyx_issues(Target(repo="r/x", interest_id="iid"))
    assert "state=open" in calls[0]
    assert len(out) == 1
    assert out[0]["title"] == "[Remyx Recommendation] X"


def test_remyx_issues_state_all_includes_closed(monkeypatch):
    """state=all returns both open and closed Outrider Issues."""
    def fake_gh_api(method, path, body=None):
        assert "state=all" in path
        return [
            _issue("[Remyx Recommendation] Open A",
                   "arxiv.org/abs/2502.20110", "open"),
            _issue("[Remyx Recommendation] Closed B",
                   "arxiv.org/abs/2412.18404", "closed"),
        ]

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    out = run._remyx_issues(
        Target(repo="r/x", interest_id="iid"), state="all",
    )
    assert len(out) == 2
    titles = [i["title"] for i in out]
    states = [i["state"] for i in out]
    assert "[Remyx Recommendation] Open A" in titles
    assert "[Remyx Recommendation] Closed B" in titles
    assert sorted(states) == ["closed", "open"]


def test_all_remyx_issues_wrapper(monkeypatch):
    captured = {}

    def fake_gh_api(method, path, body=None):
        captured["path"] = path
        return []

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    run._all_remyx_issues(Target(repo="r/x", interest_id="iid"))
    assert "state=all" in captured["path"]


def test_open_remyx_issues_still_filters_to_open(monkeypatch):
    """Back-compat: legacy callers of open_remyx_issues get open-only.
    Any caller that genuinely wants only-open behavior must keep working
    unchanged."""
    captured = {}

    def fake_gh_api(method, path, body=None):
        captured["path"] = path
        return []

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    run.open_remyx_issues(Target(repo="r/x", interest_id="iid"))
    assert "state=open" in captured["path"]
    assert "state=all" not in captured["path"]


def test_remyx_issues_swallows_fetch_error(monkeypatch):
    """A flaky GitHub API call must return [] rather than raising —
    the dedup gate falls back to "no prior Issues" which is the
    safe default."""
    def fake_gh_api(method, path, body=None):
        raise RuntimeError("simulated 503")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run._remyx_issues(
        Target(repo="r/x", interest_id="iid"), state="all"
    ) == []


def test_remyx_issues_filters_out_prs(monkeypatch):
    """GitHub's /issues endpoint returns PRs too; they must be excluded."""
    def fake_gh_api(method, path, body=None):
        return [
            {"title": "[Remyx Recommendation] X",
             "body": "arxiv.org/abs/X", "state": "open"},
            {"title": "[Remyx Recommendation] PR Y",
             "body": "arxiv.org/abs/Y", "state": "open",
             "pull_request": {"url": "..."}},  # PR — must skip
            {"title": "Unrelated", "body": "x", "state": "open"},
        ]

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    out = run._remyx_issues(
        Target(repo="r/x", interest_id="iid"), state="all"
    )
    titles = [i["title"] for i in out]
    assert "[Remyx Recommendation] X" in titles
    assert "[Remyx Recommendation] PR Y" not in titles
    assert "Unrelated" not in titles


# ─── issue_for_paper dedup against closed Issues ─────────────────────────


def _rec(arxiv_id="2605.26102", title="Sample Paper"):
    return Recommendation(
        paper_title=title, arxiv_id=arxiv_id, tier="high",
        z_score=0.0, spec_md="", paper_abstract="",
        domain_summary="", raw_paper_md="",
        relevance_score=0.9, reasoning="",
        interest_name="x",
    )


def test_issue_for_paper_matches_closed_issue_via_arxiv():
    """When the dedup set includes a closed Outrider Issue mentioning
    the candidate's arxiv id, issue_for_paper still matches it. The
    dedup logic doesn't care about state — the caller chose which
    state to fetch."""
    issues_all_state = [
        {"title": "[Remyx Recommendation] Sample Paper",
         "body": "arxiv.org/abs/2605.26102v1\nResearch interest: x",
         "state": "closed"},
    ]
    match = run.issue_for_paper(issues_all_state, _rec("2605.26102"))
    assert match is not None
    assert match["state"] == "closed"


def test_issue_for_paper_returns_none_when_no_match():
    """Regression — when the all-state set carries unrelated Outrider
    Issues, the candidate isn't dedup'd."""
    issues_all_state = [
        {"title": "[Remyx Recommendation] Unrelated",
         "body": "arxiv.org/abs/9999.00001", "state": "closed"},
    ]
    match = run.issue_for_paper(issues_all_state, _rec("2605.26102"))
    assert match is None


# ─── open_issue default footer carries the re-engagement note ────────────


def test_open_issue_default_footer_includes_reengage_note(monkeypatch):
    captured = {}

    def fake_gh_api(method, path, body=None):
        captured["body"] = body["body"]
        return {"html_url": "https://github.com/example/repo/issues/1", "number": 1}

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    run.open_issue(
        Target(repo="example/repo", interest_id="iid"),
        title="[Remyx Recommendation] Sample",
        body="## Body content",
    )
    body = captured["body"]
    assert "Reopen this Issue" in body
    assert "re-engage" in body or "re-recommend" in body
    # The default footer (coding-agent elected Issue-mode) should also
    # still be present alongside the re-engage note.
    assert "elected Issue-mode" in body


def test_open_issue_footer_override_keeps_reengage_note(monkeypatch):
    """Even when the call site passes a custom footer (preflight,
    self-review, etc.), the re-engagement note still renders. The
    note is about Outrider's discharge behavior, not about the
    routing reason."""
    captured = {}
    monkeypatch.setattr(
        run, "gh_api",
        lambda m, p, b=None: captured.update({"body": b["body"]})
        or {"html_url": "https://github.com/example/repo/issues/1", "number": 1},
    )
    run.open_issue(
        Target(repo="example/repo", interest_id="iid"),
        title="x", body="body",
        footer_override="_Custom footer for preflight downgrade._",
    )
    body = captured["body"]
    assert "Custom footer for preflight downgrade" in body
    assert "Reopen this Issue" in body


# ─── Step summary differentiates open vs closed dedup hit ────────────────


def _capture_step_summary(monkeypatch, result, tmp_path):
    """Write _write_step_summary's output to a tempfile and return the
    captured text."""
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    run._write_step_summary(result)
    return summary_file.read_text()


def test_step_summary_renders_open_existing_issue_inline(monkeypatch, tmp_path):
    """When dedup catches an open Issue, step summary surfaces
    "Already in flight" inline with the Issue link."""
    out = _capture_step_summary(monkeypatch, {
        "status": "skipped_external_issue_exists",
        "existing_issue_url": "https://github.com/example/repo/issues/87",
        "existing_issue_state": "open",
    }, tmp_path)
    assert "Already in flight" in out
    assert "https://github.com/example/repo/issues/87" in out
    assert "open — re-validated" in out


def test_step_summary_renders_closed_existing_issue_inline(monkeypatch, tmp_path):
    """When dedup catches a closed Issue, step summary surfaces
    "Already addressed" with the reopen-to-re-engage prompt."""
    out = _capture_step_summary(monkeypatch, {
        "status": "skipped_external_issue_exists",
        "existing_issue_url": "https://github.com/example/repo/issues/87",
        "existing_issue_state": "closed",
    }, tmp_path)
    assert "Already addressed" in out
    assert "team has resolved" in out
    assert "Reopen the Issue" in out
    assert "https://github.com/example/repo/issues/87" in out


def test_step_summary_omits_existing_issue_when_absent(monkeypatch, tmp_path):
    """Backwards-compat: status outcomes without an existing-issue
    context don't render the new section."""
    out = _capture_step_summary(monkeypatch, {
        "status": "pr_opened_draft",
        "paper": "Sample Paper",
        "arxiv": "2601.00001",
    }, tmp_path)
    assert "Already in flight" not in out
    assert "Already addressed" not in out


def test_step_summary_has_emoji_for_skipped_external_issue_exists(monkeypatch, tmp_path):
    """skipped_external_issue_exists wasn't in the emoji map before
    v1.4.7 — this test pins it so the status renders consistently."""
    out = _capture_step_summary(monkeypatch, {
        "status": "skipped_external_issue_exists",
        "existing_issue_url": "https://github.com/example/repo/issues/87",
        "existing_issue_state": "open",
    }, tmp_path)
    # Headline shows skip emoji ⏭️ not the fallback ℹ️
    assert "⏭️" in out.split("\n")[0]
