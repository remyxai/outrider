"""Tests for Issue dedup in the candidate viability filter.

The candidate filter drops a paper that already has an open Remyx Issue
(treating it as "in flight", like an open PR), so a sticky top candidate
that keeps routing to Issue isn't re-selected and reopened every run over a
longer lookback window. issue_for_paper is the pure matcher; the fetch +
"is this one of ours" filtering lives in open_remyx_issues.

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Recommendation  # noqa: E402


def _rec(arxiv="2605.22536v1", title="SpaceDG"):
    return Recommendation(
        paper_title=title, arxiv_id=arxiv, tier="high", z_score=0.0, spec_md="",
        paper_abstract="", team_context="", domain_summary="", raw_paper_md="",
        relevance_score=0.9, reasoning="", suggested_experiment="",
        recommendation_id="", interest_name="VQASynth", interest_context="",
    )


def _filter_ours(raw):
    # Mirrors the keep-only-ours logic in open_remyx_issues (the network
    # part), so we can test it without hitting GitHub.
    return [
        i for i in raw
        if not i.get("pull_request")
        and ((i.get("title") or "").startswith(run.PR_TITLE_PREFIX)
             or "Remyx Recommendation" in (i.get("body") or ""))
    ]


def test_open_issue_filter_keeps_only_remyx_non_prs():
    raw = [
        {"title": "[Remyx Recommendation] SpaceDG",
         "body": "arxiv.org/abs/2605.22536v1 ... Remyx Recommendation"},
        {"title": "Some PR", "body": "x", "pull_request": {"url": "..."}},
        {"title": "Unrelated bug", "body": "nothing here"},
        {"title": "custom title",
         "body": "opened by Remyx Recommendation; arxiv.org/abs/2605.10887v1"},
    ]
    ours = _filter_ours(raw)
    assert len(ours) == 2
    assert {i["title"] for i in ours} == {"[Remyx Recommendation] SpaceDG",
                                          "custom title"}


def test_match_by_arxiv_in_body_prefixed_title():
    ours = [{"title": "[Remyx Recommendation] SpaceDG",
             "body": "see arxiv.org/abs/2605.22536v1"}]
    assert run.issue_for_paper(ours, _rec("2605.22536v1")) is not None


def test_match_by_arxiv_in_body_custom_title():
    # The OPEN_AS_ISSUE path gives a Claude-authored title; body still links arxiv.
    ours = [{"title": "Add a degradation eval harness",
             "body": "opened by Remyx Recommendation; arxiv.org/abs/2605.10887v1"}]
    assert run.issue_for_paper(ours, _rec("2605.10887v1")) is not None


def test_no_match_for_paper_without_open_issue():
    ours = [{"title": "[Remyx Recommendation] SpaceDG",
             "body": "arxiv.org/abs/2605.22536v1"}]
    assert run.issue_for_paper(ours, _rec("9999.99999v1")) is None


def test_title_fallback_when_no_arxiv():
    ours = [{"title": "[Remyx Recommendation] CoolPaper",
             "body": "opened by Remyx Recommendation (no arxiv link)"}]
    assert run.issue_for_paper(ours, _rec(arxiv="", title="CoolPaper")) is not None
    assert run.issue_for_paper(ours, _rec(arxiv="", title="OtherPaper")) is None


def test_empty_open_issue_list_never_matches():
    assert run.issue_for_paper([], _rec()) is None
