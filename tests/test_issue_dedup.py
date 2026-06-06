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
        paper_abstract="", domain_summary="", raw_paper_md="",
        relevance_score=0.9, reasoning="", suggested_experiment="",
        recommendation_id="", interest_name="VQASynth", interest_context="",
        experiment_history="",
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


# ─── arxiv version-string normalization ────────────────────────────────────

def test_match_versioned_candidate_against_versionless_body():
    """The bug that produced VQASynth #87: engine-pool candidate carries
    ``2605.26102v1`` while the existing Issue body has ``2605.26102``
    (filed via broadening-search). Substring match in the original
    direction missed because ``…/abs/2605.26102v1`` is not in
    ``…/abs/2605.26102``."""
    ours = [{"title": "[Remyx Recommendation] InstructSAM",
             "body": "see arxiv.org/abs/2605.26102 — surfaced via broadening-search"}]
    assert run.issue_for_paper(ours, _rec("2605.26102v1", "InstructSAM")) is not None


def test_match_versionless_candidate_against_versioned_body():
    """Reverse direction: broadening-search candidate ``2605.26102``
    against an open Issue body containing ``2605.26102v1``. Substring
    match was already working in this direction (prefix match); locking
    it down with an explicit test."""
    ours = [{"title": "[Remyx Recommendation] InstructSAM",
             "body": "see arxiv.org/abs/2605.26102v1"}]
    assert run.issue_for_paper(ours, _rec("2605.26102", "InstructSAM")) is not None


def test_arxiv_versionless_helper_strips_only_trailing_version():
    """Pure helper. Only trailing ``v<digits>`` is dropped — embedded
    sequences that happen to look like a version aren't touched."""
    assert run._arxiv_versionless("2605.26102v1") == "2605.26102"
    assert run._arxiv_versionless("2605.26102v17") == "2605.26102"
    assert run._arxiv_versionless("2605.26102") == "2605.26102"
    assert run._arxiv_versionless("") == ""
    assert run._arxiv_versionless("v1abc") == "v1abc"  # not a trailing suffix


def test_versioned_candidate_no_match_for_unrelated_paper():
    """The fix shouldn't widen matches across distinct papers — a
    versioned candidate for paper A shouldn't match an Issue for paper B
    even if both share a common arxiv prefix accidentally."""
    ours = [{"title": "[Remyx Recommendation] OtherPaper",
             "body": "see arxiv.org/abs/2605.99999v1"}]
    assert run.issue_for_paper(ours, _rec("2605.26102v1", "InstructSAM")) is None


# ─── external-pick dedup ───────────────────────────────────────────────────
# The external (broadening-search) branch of run_one_target constructs a
# synthetic Recommendation via _resolve_external_candidate and routes
# straight to _open_downgrade_issue. The viability gate above filters
# engine-pool candidates only; the external branch bypasses it. We now
# call issue_for_paper(open_issues, rec) in that branch — these tests pin
# the matching behaviour for the shape of Recommendation the external
# branch produces.


def _external_rec(arxiv, title, query="InstructSAM"):
    """Mirrors the synthetic Recommendation _resolve_external_candidate builds
    when the selection pass surfaces an out-of-pool candidate (chosen_index=-2).
    Same dataclass; just constructed with the via-broadening-search defaults."""
    return Recommendation(
        paper_title=title, arxiv_id=arxiv, tier="high", z_score=0.0, spec_md="",
        paper_abstract="", domain_summary="", raw_paper_md="",
        relevance_score=0.0, reasoning=f"surfaced via remyxai search {query!r}",
        suggested_experiment="", recommendation_id="",
        interest_name="(via broadening-search)", interest_context="",
        experiment_history="",
    )


def test_external_pick_matches_prior_external_issue():
    """VQASynth#88's bug: an external pick at arxiv 2605.26102 was filed
    two days after VQASynth#86 (also external) for the same paper. The
    dedup helper sees the prior Issue's body and matches."""
    ours = [{"title": "[Remyx Recommendation] InstructSAM",
             "body": "External pick surfaced via remyxai search query"
                     " 'InstructSAM' — arxiv.org/abs/2605.26102"}]
    rec = _external_rec("2605.26102", "InstructSAM")
    assert run.issue_for_paper(ours, rec) is not None


def test_external_pick_matches_prior_engine_pool_issue():
    """The other direction of the same bug: an external pick at
    2605.26102 should match an existing engine-pool Issue body that
    contains 2605.26102v1. (The version-stripped needle from Commit 1
    handles this when the existing body has the version.)"""
    ours = [{"title": "[Remyx Recommendation] InstructSAM: Segment Any Instance",
             "body": "**Recommended paper**: arxiv.org/abs/2605.26102v1"}]
    rec = _external_rec("2605.26102", "InstructSAM")
    assert run.issue_for_paper(ours, rec) is not None


def test_external_pick_no_match_for_distinct_paper():
    """An external pick for paper A shouldn't be deduped against an
    existing Issue for paper B even if both came from broadening-search."""
    ours = [{"title": "[Remyx Recommendation] Other paper",
             "body": "External pick surfaced via remyxai search query "
                     "'other' — arxiv.org/abs/2605.99999v1"}]
    rec = _external_rec("2605.26102", "InstructSAM")
    assert run.issue_for_paper(ours, rec) is None
