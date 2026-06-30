"""Unit tests for the deep-search retrieval loop.

Covers the pure logic: asset→Recommendation mapping (the /search/assets
envelope shape), dedup of refine results against the broad pool by
arxiv_id (with version-stripped fallback), audit-prompt rendering, and
the orchestration in audit_and_refine_pool with mocked Claude + Remyx
calls. The Claude call and /search/assets fetch are the only network
I/O involved; both are monkey-patched.

Run with: pytest tests/ -q
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Recommendation, Target  # noqa: E402


# ─── _asset_to_recommendation ─────────────────────────────────────────────


def test_asset_to_recommendation_basic_mapping():
    asset = {
        "arxiv_id": "2502.20110",
        "title": "Sample Paper Title",
        "abstract": "Abstract first sentence ...",
        "github_url": "https://github.com/example/repo",
        "categories": ["cs.CV"],
    }
    rec = run._asset_to_recommendation(
        asset,
        refine_query="example refine query terms",
        fallback_interest_name="ExampleInterest",
        interest_context="example team focus",
        experiment_history="",
    )
    assert rec.arxiv_id == "2502.20110"
    assert rec.paper_title == "Sample Paper Title"
    assert rec.paper_abstract.startswith("Abstract first sentence")
    assert rec.paper_github_url == "https://github.com/example/repo"
    assert rec.interest_name == "ExampleInterest"
    # Synthetic moderate tier (≥ 0.60 floor) so the candidate
    # survives the default min_confidence filter ("moderate").
    assert rec.relevance_score == 0.65
    assert rec.tier == "moderate"
    # Reasoning must record the refine-query provenance so the
    # selection pass can see *why* this paper reached the pool.
    assert "deep-search refine query" in rec.reasoning
    assert "example refine query terms" in rec.reasoning


def test_asset_to_recommendation_missing_fields_default_safely():
    rec = run._asset_to_recommendation(
        {"arxiv_id": ""},   # only the bare key
        refine_query="x",
        fallback_interest_name="x",
        interest_context="",
        experiment_history="",
    )
    assert rec.paper_title == "(untitled)"
    assert rec.paper_abstract == ""
    assert rec.paper_github_url == ""


# ─── _render_broad_brief ─────────────────────────────────────────────────


def test_render_broad_brief_includes_title_arxiv_tier_and_abstract():
    recs = [
        Recommendation(
            paper_title="A", arxiv_id="2601.00001", tier="high",
            z_score=0.0, spec_md="", paper_abstract="abc",
            domain_summary="", raw_paper_md="",
        ),
        Recommendation(
            paper_title="B", arxiv_id="", tier="moderate",
            z_score=0.0, spec_md="", paper_abstract="def",
            domain_summary="", raw_paper_md="",
        ),
    ]
    out = run._render_broad_brief(recs)
    assert "[0] A" in out
    assert "arxiv 2601.00001" in out
    assert "tier high" in out
    assert "[1] B" in out
    # Empty arxiv → 'n/a' placeholder so the line stays well-formed.
    assert "arxiv n/a" in out


# ─── audit_and_refine_pool — dedup + merge ────────────────────────────────


def _make_broad(arxiv_ids):
    return [
        Recommendation(
            paper_title=f"broad-{a}", arxiv_id=a, tier="high",
            z_score=0.0, spec_md="",
            paper_abstract="broad abstract", domain_summary="",
            raw_paper_md="",
            relevance_score=0.9,
        )
        for a in arxiv_ids
    ]


def _audit_json(refine_queries):
    """Wrap a refine_queries list as the audit pass's JSON output."""
    return json.dumps({
        "refine_queries": refine_queries,
        "reasoning": "test stub",
    })


def test_audit_and_refine_merges_new_assets_after_broad_pool(monkeypatch):
    broad = _make_broad(["2601.00001v1", "2601.00002v1"])

    def fake_oneshot(workdir, prompt, timeout_s, max_turns=None):
        return True, _audit_json(["example refine query"])

    def fake_search_assets(query, max_results=5, use_llm=True):
        assert query == "example refine query"
        return [
            # Brand-new arxiv id → should be appended.
            {
                "arxiv_id": "2502.20110v1",
                "title": "New Refine Paper",
                "abstract": "refine abstract",
                "github_url": "https://github.com/example/new-repo",
            },
            # Duplicate of a broad pool paper → must be deduped out,
            # even though the version suffix differs (v2 vs v1).
            {
                "arxiv_id": "2601.00001v2",
                "title": "duplicate of broad[0]",
                "abstract": "x",
            },
        ]

    monkeypatch.setattr(run, "_run_claude_oneshot", fake_oneshot)
    monkeypatch.setattr(run, "_remyx_search_assets", fake_search_assets)
    monkeypatch.setattr(run, "_fetch_repo_readme", lambda repo, max_chars=2000: "stub")
    monkeypatch.setattr(run, "_recent_outrider_issue_titles", lambda target, n=8: [])

    target = Target(repo="example/repo", interest_id="iid")
    merged = run.audit_and_refine_pool(
        target, broad,
        interest_name="ExampleInterest", interest_context="ctx",
        experiment_history="",
    )

    # Broad pool comes first, refine candidates appended.
    assert len(merged) == 3, merged
    assert merged[:2] == broad
    assert merged[2].arxiv_id == "2502.20110v1"
    assert merged[2].paper_github_url == "https://github.com/example/new-repo"


def test_audit_with_zero_queries_returns_broad_unchanged(monkeypatch):
    broad = _make_broad(["2601.00001v1"])

    monkeypatch.setattr(
        run, "_run_claude_oneshot",
        lambda *a, **k: (True, _audit_json([])),
    )
    monkeypatch.setattr(run, "_remyx_search_assets",
                        lambda *a, **k: pytest_fail_if_called())
    monkeypatch.setattr(run, "_fetch_repo_readme", lambda repo, max_chars=2000: "")
    monkeypatch.setattr(run, "_recent_outrider_issue_titles", lambda target, n=8: [])

    target = Target(repo="r/x", interest_id="iid")
    merged = run.audit_and_refine_pool(
        target, broad, interest_name="x", interest_context="",
        experiment_history="",
    )
    assert merged == broad


def pytest_fail_if_called(*a, **k):
    raise AssertionError("should not be called when audit returns 0 queries")


def test_audit_failure_falls_back_to_broad(monkeypatch):
    broad = _make_broad(["2601.00001v1"])

    # Audit Claude call fails → should degrade silently.
    monkeypatch.setattr(
        run, "_run_claude_oneshot",
        lambda *a, **k: (False, "claude exited 1"),
    )
    monkeypatch.setattr(run, "_fetch_repo_readme", lambda repo, max_chars=2000: "")
    monkeypatch.setattr(run, "_recent_outrider_issue_titles", lambda target, n=8: [])

    target = Target(repo="r/x", interest_id="iid")
    merged = run.audit_and_refine_pool(
        target, broad, interest_name="x", interest_context="",
        experiment_history="",
    )
    assert merged == broad


def test_audit_parse_failure_falls_back_to_broad(monkeypatch):
    broad = _make_broad(["2601.00001v1"])

    monkeypatch.setattr(
        run, "_run_claude_oneshot",
        lambda *a, **k: (True, "not json at all, just prose"),
    )
    monkeypatch.setattr(run, "_fetch_repo_readme", lambda repo, max_chars=2000: "")
    monkeypatch.setattr(run, "_recent_outrider_issue_titles", lambda target, n=8: [])

    target = Target(repo="r/x", interest_id="iid")
    merged = run.audit_and_refine_pool(
        target, broad, interest_name="x", interest_context="",
        experiment_history="",
    )
    assert merged == broad


def test_audit_caps_at_three_queries_even_if_claude_returns_more(monkeypatch):
    broad = _make_broad(["2601.00001v1"])
    calls: list[str] = []

    monkeypatch.setattr(
        run, "_run_claude_oneshot",
        lambda *a, **k: (
            True,
            _audit_json(["q1", "q2", "q3", "q4", "q5"]),
        ),
    )

    def fake_search(query, max_results=5, use_llm=True):
        calls.append(query)
        return []

    monkeypatch.setattr(run, "_remyx_search_assets", fake_search)
    monkeypatch.setattr(run, "_fetch_repo_readme", lambda repo, max_chars=2000: "")
    monkeypatch.setattr(run, "_recent_outrider_issue_titles", lambda target, n=8: [])

    target = Target(repo="r/x", interest_id="iid")
    run.audit_and_refine_pool(
        target, broad, interest_name="x", interest_context="",
        experiment_history="", max_queries=3,
    )
    assert calls == ["q1", "q2", "q3"]


def test_audit_empty_broad_pool_skips_audit(monkeypatch):
    """No broad candidates = nothing to audit; skip the Claude call
    entirely rather than asking it to invent a candidate pool from
    thin air."""
    sentinel = {"called": False}

    def fake_oneshot(*a, **k):
        sentinel["called"] = True
        return True, _audit_json([])

    monkeypatch.setattr(run, "_run_claude_oneshot", fake_oneshot)

    target = Target(repo="r/x", interest_id="iid")
    merged = run.audit_and_refine_pool(
        target, [], interest_name="x", interest_context="",
        experiment_history="",
    )
    assert merged == []
    assert sentinel["called"] is False


def test_audit_dedup_uses_versionless_form(monkeypatch):
    """Refine result with the same arxiv root but different version
    suffix should not duplicate the broad pool entry."""
    broad = _make_broad(["2601.00001v3"])

    monkeypatch.setattr(
        run, "_run_claude_oneshot",
        lambda *a, **k: (True, _audit_json(["q1"])),
    )
    monkeypatch.setattr(
        run, "_remyx_search_assets",
        lambda *a, **k: [{
            "arxiv_id": "2601.00001v1",
            "title": "older version of broad[0]",
            "abstract": "x",
        }],
    )
    monkeypatch.setattr(run, "_fetch_repo_readme", lambda repo, max_chars=2000: "")
    monkeypatch.setattr(run, "_recent_outrider_issue_titles", lambda target, n=8: [])

    target = Target(repo="r/x", interest_id="iid")
    merged = run.audit_and_refine_pool(
        target, broad, interest_name="x", interest_context="",
        experiment_history="",
    )
    assert len(merged) == 1
    assert merged[0].arxiv_id == "2601.00001v3"


# ─── _remyx_search_assets ────────────────────────────────────────────────


def test_remyx_search_assets_returns_empty_on_failure(monkeypatch):
    def fake_post(path, body):
        raise RuntimeError("simulated 503")
    monkeypatch.setattr(run, "_remyx_post", fake_post)
    assert run._remyx_search_assets("anything") == []


def test_remyx_search_assets_passes_query_through(monkeypatch):
    captured = {}

    def fake_post(path, body):
        captured["path"] = path
        captured["body"] = body
        return {"assets": [{"arxiv_id": "1", "title": "t"}]}

    monkeypatch.setattr(run, "_remyx_post", fake_post)
    out = run._remyx_search_assets("depth", max_results=3, use_llm=False)
    assert captured["path"] == "/api/v1.0/search/assets"
    assert captured["body"] == {
        "query": "depth", "max_results": 3, "use_llm": False,
    }
    assert out == [{"arxiv_id": "1", "title": "t"}]


def test_remyx_search_assets_empty_query_short_circuits(monkeypatch):
    def fake_post(path, body):
        raise AssertionError("should not call _remyx_post on empty query")
    monkeypatch.setattr(run, "_remyx_post", fake_post)
    assert run._remyx_search_assets("") == []
    assert run._remyx_search_assets("   ") == []


def test_remyx_search_assets_caps_max_results_at_50(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        run, "_remyx_post",
        lambda path, body: captured.update(body) or {"assets": []},
    )
    run._remyx_search_assets("q", max_results=9999)
    assert captured["max_results"] == 50


# ─── _recent_outrider_issue_titles ───────────────────────────────────────


def test_recent_issue_titles_filters_to_ours_and_drops_prs(monkeypatch):
    raw = [
        {"title": "[Remyx Recommendation] A", "body": ""},
        {"title": "[Remyx Recommendation] B", "body": "",
         "pull_request": {"url": "x"}},   # PR, drop
        {"title": "unrelated bug", "body": ""},
        {"title": "custom title",
         "body": "Opened by Remyx Recommendation; arxiv.org/abs/X"},
        {"title": "[Remyx Recommendation] C", "body": ""},
    ]
    monkeypatch.setattr(run, "gh_api", lambda method, path, body=None: raw)
    target = Target(repo="r/x", interest_id="iid")
    titles = run._recent_outrider_issue_titles(target, n=8)
    # Order preserved (the GitHub query is already created-desc).
    assert titles == [
        "[Remyx Recommendation] A",
        "custom title",
        "[Remyx Recommendation] C",
    ]


def test_recent_issue_titles_respects_n_limit(monkeypatch):
    raw = [{"title": f"[Remyx Recommendation] {i}", "body": ""}
           for i in range(20)]
    monkeypatch.setattr(run, "gh_api", lambda method, path, body=None: raw)
    target = Target(repo="r/x", interest_id="iid")
    titles = run._recent_outrider_issue_titles(target, n=5)
    assert len(titles) == 5
    assert titles[0] == "[Remyx Recommendation] 0"


def test_recent_issue_titles_returns_empty_on_fetch_failure(monkeypatch):
    def fake(method, path, body=None):
        raise RuntimeError("403 rate limit")
    monkeypatch.setattr(run, "gh_api", fake)
    assert run._recent_outrider_issue_titles(
        Target(repo="r/x", interest_id="iid"),
    ) == []


# ─── _fetch_repo_readme ──────────────────────────────────────────────────


def test_fetch_repo_readme_decodes_base64(monkeypatch):
    import base64 as _b64
    payload = _b64.b64encode(b"# Title\n\nBody text").decode()
    monkeypatch.setattr(
        run, "gh_api",
        lambda m, p, b=None: {"content": payload, "encoding": "base64"},
    )
    assert run._fetch_repo_readme("r/x") == "# Title\n\nBody text"


def test_fetch_repo_readme_truncates(monkeypatch):
    import base64 as _b64
    long_body = "x" * 5000
    payload = _b64.b64encode(long_body.encode()).decode()
    monkeypatch.setattr(
        run, "gh_api",
        lambda m, p, b=None: {"content": payload, "encoding": "base64"},
    )
    out = run._fetch_repo_readme("r/x", max_chars=100)
    assert len(out) <= 100 + len("\n…[truncated]")
    assert out.endswith("…[truncated]")


def test_fetch_repo_readme_returns_empty_on_failure(monkeypatch):
    def fake(m, p, b=None):
        raise RuntimeError("404")
    monkeypatch.setattr(run, "gh_api", fake)
    assert run._fetch_repo_readme("r/x") == ""


# ─── audit timeout inherits target.claude_timeout_s ────────────────────


def test_audit_timeout_defaults_to_target_claude_timeout_s(monkeypatch):
    """When REMYX_AUDIT_TIMEOUT_S is unset, the audit pass uses
    target.claude_timeout_s — so a customer who bumped `claude-timeout`
    for a slower backend (e.g. GLM on a large monorepo) gets the same
    headroom on the audit pass without needing to know about a separate
    env knob. Same pattern as preflight in v1.6.28."""
    monkeypatch.delenv("REMYX_AUDIT_TIMEOUT_S", raising=False)
    broad = _make_broad(["2601.00001v1"])
    captured = {}

    def fake_oneshot(workdir, prompt, timeout_s, max_turns=None):
        captured["timeout_s"] = timeout_s
        return (True, _audit_json([]))

    monkeypatch.setattr(run, "_run_claude_oneshot", fake_oneshot)
    monkeypatch.setattr(run, "_fetch_repo_readme", lambda *a, **k: "")
    monkeypatch.setattr(run, "_recent_outrider_issue_titles", lambda *a, **k: [])

    target = Target(repo="example/repo", interest_id="iid", claude_timeout_s=1500)
    run.audit_and_refine_pool(
        target, broad,
        interest_name="X", interest_context="ctx", experiment_history="",
    )
    assert captured["timeout_s"] == 1500


def test_audit_timeout_env_var_overrides_target(monkeypatch):
    """REMYX_AUDIT_TIMEOUT_S, if set, takes precedence — kept as an
    escape hatch for CI scenarios that want a tighter audit ceiling
    while leaving the implementation timeout large."""
    monkeypatch.setenv("REMYX_AUDIT_TIMEOUT_S", "300")
    broad = _make_broad(["2601.00001v1"])
    captured = {}

    def fake_oneshot(workdir, prompt, timeout_s, max_turns=None):
        captured["timeout_s"] = timeout_s
        return (True, _audit_json([]))

    monkeypatch.setattr(run, "_run_claude_oneshot", fake_oneshot)
    monkeypatch.setattr(run, "_fetch_repo_readme", lambda *a, **k: "")
    monkeypatch.setattr(run, "_recent_outrider_issue_titles", lambda *a, **k: [])

    target = Target(repo="example/repo", interest_id="iid", claude_timeout_s=1500)
    run.audit_and_refine_pool(
        target, broad,
        interest_name="X", interest_context="ctx", experiment_history="",
    )
    assert captured["timeout_s"] == 300
