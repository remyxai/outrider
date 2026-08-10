"""Tests for the ``## Recent architecture Discussions`` orientation block.

The block feeds a lexically-relevant slice of the target repo's (or its
parent's) GitHub Discussions into ORIENTATION.md, so the coding agent
sees the pre-implementation rationale — the failure mode RepoProbe
(2608.04783) names "Edit Bias".

Covers:
  1. ``_paper_keywords`` — tokenization + stopword handling
  2. ``_rank_discussions_by_paper`` — scoring, dedup, empty paths
  3. ``_resolve_discussions_repo`` — target-has-discussions,
     fork-fallback-to-parent, both-disabled
  4. ``_fetch_recent_discussions`` — parses GraphQL response envelope,
     honors ``hasDiscussionsEnabled: false``, graceful on exceptions
  5. ``_orient_recent_discussions`` — end-to-end composed function
  6. ``_collect_repo_orientation`` — new paper_title/abstract kwargs;
     block absent when no signal

Run with: pytest tests/test_discussions_injection.py -q
"""
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# ─── _paper_keywords ──────────────────────────────────────────────────────

def test_keywords_drops_stopwords_and_short_tokens():
    kws = run._paper_keywords(
        "A Paper About Cost-Aware Stopping",
        "This work studies with over these methods and their tradeoffs.",
    )
    # Length ≥ 4, not stopword, not appearing twice. Hyphenated compounds
    # ("cost-aware") are kept as one token — matching those as a phrase
    # against discussion text is the whole point of the ranker.
    assert "cost-aware" in kws
    assert "stopping" in kws
    assert "with" not in kws
    assert "these" not in kws
    assert "their" not in kws
    assert "paper" not in kws  # in the stopword list
    assert "abstract" not in kws
    # No duplicates
    assert len(kws) == len(set(kws))


def test_keywords_orders_title_tokens_first():
    kws = run._paper_keywords(
        "Cost Aware Stopping",
        "The abstract mentions verification later and localization elsewhere.",
    )
    # Title terms must appear before abstract-only terms
    assert kws.index("cost") < kws.index("verification")
    assert kws.index("stopping") < kws.index("localization")


def test_keywords_respects_max():
    kws = run._paper_keywords(
        "alpha beta gamma",
        "delta epsilon zeta eta theta iota kappa lambda",
        max_keywords=5,
    )
    assert len(kws) == 5


def test_keywords_empty_input():
    assert run._paper_keywords("", "") == []


# ─── _rank_discussions_by_paper ───────────────────────────────────────────

def test_ranker_scores_title_hits_twice_body_hits():
    discs = [
        {"title": "stopping", "url": "a", "excerpt": ""},           # 2×1 = 2
        {"title": "unrelated", "url": "b", "excerpt": "stopping"},  # 1×1 = 1
    ]
    ranked = run._rank_discussions_by_paper(
        discs, "Cost-Aware Stopping", "", top_k=5,
    )
    assert len(ranked) == 2
    assert ranked[0]["url"] == "a"
    assert ranked[0]["_overlap"] == 2
    assert ranked[1]["_overlap"] == 1


def test_ranker_drops_zero_score_entries():
    discs = [
        {"title": "stopping", "url": "hit", "excerpt": ""},
        {"title": "totally different topic", "url": "miss", "excerpt": "nothing shared"},
    ]
    ranked = run._rank_discussions_by_paper(discs, "Cost-Aware Stopping", "")
    urls = [d["url"] for d in ranked]
    assert "hit" in urls
    assert "miss" not in urls


def test_ranker_ties_broken_by_input_order():
    # Both have the same score; input order (UPDATED_AT desc) wins
    discs = [
        {"title": "stopping newer", "url": "newer", "excerpt": ""},
        {"title": "stopping older", "url": "older", "excerpt": ""},
    ]
    ranked = run._rank_discussions_by_paper(discs, "stopping", "")
    assert [d["url"] for d in ranked] == ["newer", "older"]


def test_ranker_returns_empty_when_no_paper_metadata():
    discs = [{"title": "anything", "url": "x", "excerpt": ""}]
    assert run._rank_discussions_by_paper(discs, "", "") == []


def test_ranker_returns_empty_on_empty_discussions():
    assert run._rank_discussions_by_paper([], "Cost-Aware Stopping", "abstract") == []


def test_ranker_respects_top_k():
    discs = [
        {"title": "stopping A", "url": "a", "excerpt": ""},
        {"title": "stopping B", "url": "b", "excerpt": ""},
        {"title": "stopping C", "url": "c", "excerpt": ""},
    ]
    ranked = run._rank_discussions_by_paper(discs, "stopping", "", top_k=2)
    assert len(ranked) == 2


# ─── _resolve_discussions_repo ────────────────────────────────────────────

def test_resolve_uses_target_when_discussions_enabled():
    with patch.object(run, "gh_api", return_value={"has_discussions": True}) as m:
        assert run._resolve_discussions_repo("owner/repo") == ("owner/repo", False)
        m.assert_called_once_with("GET", "/repos/owner/repo")


def test_resolve_falls_back_to_parent_when_target_disabled():
    def fake_api(method, path, body=None):
        if path == "/repos/fork_owner/repo":
            return {"has_discussions": False,
                    "parent": {"full_name": "upstream/repo"}}
        if path == "/repos/upstream/repo":
            return {"has_discussions": True}
        raise AssertionError(f"unexpected path: {path}")

    with patch.object(run, "gh_api", side_effect=fake_api):
        result = run._resolve_discussions_repo("fork_owner/repo")
    assert result == ("upstream/repo", True)


def test_resolve_returns_none_when_target_disabled_and_no_parent():
    with patch.object(run, "gh_api", return_value={
        "has_discussions": False, "parent": None,
    }):
        assert run._resolve_discussions_repo("owner/repo") is None


def test_resolve_returns_none_when_both_target_and_parent_disabled():
    def fake_api(method, path, body=None):
        if path == "/repos/fork_owner/repo":
            return {"has_discussions": False,
                    "parent": {"full_name": "upstream/repo"}}
        if path == "/repos/upstream/repo":
            return {"has_discussions": False}
        raise AssertionError(f"unexpected: {path}")

    with patch.object(run, "gh_api", side_effect=fake_api):
        assert run._resolve_discussions_repo("fork_owner/repo") is None


def test_resolve_survives_api_exception():
    with patch.object(run, "gh_api", side_effect=RuntimeError("boom")):
        assert run._resolve_discussions_repo("owner/repo") is None


# ─── _fetch_recent_discussions ────────────────────────────────────────────

def test_fetch_parses_graphql_response():
    fake = {
        "repository": {
            "hasDiscussionsEnabled": True,
            "discussions": {"nodes": [
                {"title": "Design of X",
                 "url": "https://github.com/o/r/discussions/1",
                 "bodyText": "Long body here " * 100,
                 "category": {"name": "RFC"}},
                {"title": "Redesign of Y",
                 "url": "https://github.com/o/r/discussions/2",
                 "bodyText": "short",
                 "category": {"name": "General"}},
            ]},
        },
    }
    with patch.object(run, "gh_graphql", return_value=fake):
        out = run._fetch_recent_discussions("o/r", limit=15)
    assert len(out) == 2
    assert out[0]["title"] == "Design of X"
    assert out[0]["category"] == "RFC"
    assert out[0]["url"].endswith("/discussions/1")
    # Excerpt is capped and marked
    assert "…[truncated]" in out[0]["excerpt"]
    assert "…[truncated]" not in out[1]["excerpt"]


def test_fetch_returns_empty_when_discussions_disabled():
    fake = {"repository": {"hasDiscussionsEnabled": False,
                           "discussions": {"nodes": []}}}
    with patch.object(run, "gh_graphql", return_value=fake):
        assert run._fetch_recent_discussions("o/r") == []


def test_fetch_survives_graphql_exception():
    with patch.object(run, "gh_graphql", side_effect=RuntimeError("boom")):
        assert run._fetch_recent_discussions("o/r") == []


def test_fetch_drops_malformed_nodes():
    fake = {
        "repository": {
            "hasDiscussionsEnabled": True,
            "discussions": {"nodes": [
                {"title": "", "url": "x", "bodyText": "", "category": None},
                {"title": "good", "url": "", "bodyText": "", "category": None},
                {"title": "good", "url": "u", "bodyText": "", "category": None},
            ]},
        },
    }
    with patch.object(run, "gh_graphql", return_value=fake):
        out = run._fetch_recent_discussions("o/r")
    assert len(out) == 1
    assert out[0] == {"title": "good", "url": "u", "category": "", "excerpt": ""}


def test_fetch_returns_empty_on_bad_slug():
    # No owner/repo split → nothing to query.
    assert run._fetch_recent_discussions("no-slash") == []
    assert run._fetch_recent_discussions("") == []


def test_fetch_drops_noise_categories():
    # A survey across peft/trl/torchtune/vllm/DeepSpeed found that
    # Q&A / Show and tell / Announcements / Polls / Help categories
    # consistently carry user-support / listing / spam content that
    # keyword-matches the paper without carrying design rationale.
    # The negative-filter drops them at fetch time.
    fake = {
        "repository": {
            "hasDiscussionsEnabled": True,
            "discussions": {"nodes": [
                {"title": "how do I install this?", "url": "u1",
                 "bodyText": "help", "category": {"name": "Q&A"}},
                {"title": "featured on some listing site", "url": "u2",
                 "bodyText": "listing", "category": {"name": "Show and tell"}},
                {"title": "v2.0 released!", "url": "u3",
                 "bodyText": "release notes", "category": {"name": "Announcements"}},
                {"title": "which formatter should we use?", "url": "u4",
                 "bodyText": "poll", "category": {"name": "Polls"}},
                {"title": "how do I run the tests?", "url": "u5",
                 "bodyText": "help", "category": {"name": "Help"}},
                {"title": "RFC: new checkpoint format", "url": "u6",
                 "bodyText": "design content", "category": {"name": "Ideas"}},
                {"title": "General design discussion", "url": "u7",
                 "bodyText": "architecture debate", "category": {"name": "General"}},
            ]},
        },
    }
    with patch.object(run, "gh_graphql", return_value=fake):
        out = run._fetch_recent_discussions("o/r")
    urls = [d["url"] for d in out]
    # Signal categories retained, noise categories dropped.
    assert urls == ["u6", "u7"]


def test_fetch_keeps_custom_category_names():
    # Repos that name their design surface something other than "Ideas"
    # (e.g. "Development", "Architecture", "RFCs") must still surface —
    # the filter is exclude-only, not include-only.
    fake = {
        "repository": {
            "hasDiscussionsEnabled": True,
            "discussions": {"nodes": [
                {"title": "custom cat 1", "url": "u1", "bodyText": "",
                 "category": {"name": "Development"}},
                {"title": "custom cat 2", "url": "u2", "bodyText": "",
                 "category": {"name": "Architecture"}},
                {"title": "no cat", "url": "u3", "bodyText": "",
                 "category": None},
            ]},
        },
    }
    with patch.object(run, "gh_graphql", return_value=fake):
        out = run._fetch_recent_discussions("o/r")
    assert [d["url"] for d in out] == ["u1", "u2", "u3"]


# ─── _orient_recent_discussions (composed) ────────────────────────────────

def test_orient_end_to_end_target_repo():
    with patch.object(run, "_resolve_discussions_repo",
                      return_value=("o/r", False)), \
         patch.object(run, "_fetch_recent_discussions",
                      return_value=[
                          {"title": "Stopping RFC", "url": "https://x/1",
                           "category": "RFC",
                           "excerpt": "Debate about cost-aware stopping."},
                          {"title": "Unrelated", "url": "https://x/2",
                           "category": "Q&A", "excerpt": "nothing here"},
                      ]):
        block = run._orient_recent_discussions(
            "o/r", "Cost-Aware Stopping", "Stopping tool acquisition.",
        )
    assert "Top 1 recent Discussion(s) from `o/r`" in block
    assert "[Stopping RFC](https://x/1)" in block
    assert "[Unrelated]" not in block   # zero overlap → dropped
    assert "> Debate about cost-aware stopping." in block


def test_orient_end_to_end_parent_fallback_annotation():
    with patch.object(run, "_resolve_discussions_repo",
                      return_value=("upstream/r", True)), \
         patch.object(run, "_fetch_recent_discussions",
                      return_value=[
                          {"title": "Stopping RFC", "url": "https://x/1",
                           "category": "", "excerpt": "on cost stopping"}
                      ]):
        block = run._orient_recent_discussions(
            "fork/r", "Cost-Aware Stopping", "abstract",
        )
    assert "parent repo — target `fork/r` has Discussions disabled" in block
    assert "`upstream/r`" in block


def test_orient_empty_when_no_paper_title():
    with patch.object(run, "_resolve_discussions_repo") as res:
        assert run._orient_recent_discussions("o/r", "", "abstract") == ""
        res.assert_not_called()


def test_orient_empty_when_no_target_repo():
    with patch.object(run, "_resolve_discussions_repo") as res:
        assert run._orient_recent_discussions("", "Title", "abstract") == ""
        res.assert_not_called()


def test_orient_empty_when_resolver_returns_none():
    with patch.object(run, "_resolve_discussions_repo", return_value=None):
        assert run._orient_recent_discussions("o/r", "Title", "abstract") == ""


def test_orient_empty_when_no_discussions_returned():
    with patch.object(run, "_resolve_discussions_repo",
                      return_value=("o/r", False)), \
         patch.object(run, "_fetch_recent_discussions", return_value=[]):
        assert run._orient_recent_discussions("o/r", "Title", "abstract") == ""


def test_orient_empty_when_no_lexical_overlap():
    with patch.object(run, "_resolve_discussions_repo",
                      return_value=("o/r", False)), \
         patch.object(run, "_fetch_recent_discussions",
                      return_value=[{"title": "totally unrelated topic",
                                     "url": "https://x/1", "category": "",
                                     "excerpt": ""}]):
        assert run._orient_recent_discussions(
            "o/r", "Cost-Aware Stopping", "abstract text",
        ) == ""


# ─── _collect_repo_orientation kwargs plumbing ────────────────────────────

def test_collect_orientation_passes_paper_to_discussions_block(tmp_path):
    # Set up a minimal repo tree — no conventions/tests/config, so every
    # other block returns "". If the new discussions block passes, the
    # template will still render (non-empty blocks dict).
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pkg").mkdir()

    captured = {}

    def fake_orient(repo, title, abstract, limit=15, top_k=5):
        captured["repo"] = repo
        captured["title"] = title
        captured["abstract"] = abstract
        return "Some discussions body here."

    target = run.Target(repo="o/r")
    with patch.object(run, "_orient_recent_discussions",
                      side_effect=fake_orient):
        body = run._collect_repo_orientation(
            tmp_path, target, "pkg",
            paper_title="A Paper", paper_abstract="Abstract text",
        )
    assert captured == {"repo": "o/r", "title": "A Paper",
                        "abstract": "Abstract text"}
    assert "Recent architecture Discussions" in body
    assert "Some discussions body here." in body


def test_collect_orientation_omits_discussions_block_when_absent(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pkg").mkdir()
    target = run.Target(repo="o/r")
    with patch.object(run, "_orient_recent_discussions", return_value=""):
        body = run._collect_repo_orientation(
            tmp_path, target, "pkg",
            paper_title="A Paper", paper_abstract="",
        )
    assert "Recent architecture Discussions" not in body
