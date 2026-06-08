"""Tests for the v1.4.4 identity-coverage work:

  - HuggingFace URL extraction + license fetch
  - Arxiv abstract-page fallback
  - "no-code-link" license class (distinct from "missing")
  - License-source preference (HF over GitHub) + mismatch detection
  - Family coalescing for paper-version siblings
  - issue_for_paper sibling-paper dedup

Run with: pytest tests/ -q
"""
import json
import sys
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Recommendation, Target  # noqa: E402


# ─── _extract_huggingface_urls ────────────────────────────────────────────


def test_extract_hf_urls_basic():
    text = "Model card at https://huggingface.co/example/my-model is here."
    assert run._extract_huggingface_urls(text) == ["example/my-model"]


def test_extract_hf_urls_skips_platform_paths():
    text = (
        "See https://huggingface.co/spaces/foo/demo and "
        "https://huggingface.co/datasets/foo/data — but the model is at "
        "https://huggingface.co/example/real-model."
    )
    assert run._extract_huggingface_urls(text) == ["example/real-model"]


def test_extract_hf_urls_dedupes_repeated_mentions():
    text = (
        "https://huggingface.co/x/y appears twice: "
        "https://huggingface.co/x/y and once more."
    )
    assert run._extract_huggingface_urls(text) == ["x/y"]


def test_extract_hf_urls_strips_trailing_path():
    text = "https://huggingface.co/owner/model/tree/main/configs is a sub-page"
    assert run._extract_huggingface_urls(text) == ["owner/model"]


def test_extract_hf_urls_empty_input():
    assert run._extract_huggingface_urls("") == []
    assert run._extract_huggingface_urls(None, "  ") == []


# ─── _fetch_hf_license (mocked HTTP) ──────────────────────────────────────


def _patch_urlopen(monkeypatch, payload: dict | bytes, status: int = 200):
    """Patch urllib.request.urlopen for a single test. Payload may be
    a dict (JSON-encoded) or raw bytes."""
    if isinstance(payload, dict):
        body = json.dumps(payload).encode()
    else:
        body = payload

    class FakeResp:
        def __init__(self, body):
            self._body = body
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        if status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, status, "err", {}, BytesIO(body),
            )
        return FakeResp(body)

    monkeypatch.setattr(run.urllib.request, "urlopen", fake_urlopen)


def test_fetch_hf_license_returns_card_license(monkeypatch):
    _patch_urlopen(monkeypatch, {"cardData": {"license": "cc-by-nc-4.0"}})
    monkeypatch.setattr(run, "_HF_LICENSE_CACHE", {})

    assert run._fetch_hf_license("example/some-model") == "cc-by-nc-4.0"


def test_fetch_hf_license_returns_empty_on_404(monkeypatch):
    _patch_urlopen(monkeypatch, b"not found", status=404)
    monkeypatch.setattr(run, "_HF_LICENSE_CACHE", {})

    assert run._fetch_hf_license("nonexistent/model") == ""


def test_fetch_hf_license_returns_empty_when_field_missing(monkeypatch):
    """Model card present but no license declared — common case for
    proprietary or unreleased weights."""
    _patch_urlopen(monkeypatch, {"cardData": {}})
    monkeypatch.setattr(run, "_HF_LICENSE_CACHE", {})

    assert run._fetch_hf_license("example/no-license-field") == ""


def test_fetch_hf_license_handles_list_license_value(monkeypatch):
    """HF allows multi-license declarations as a list — we take the first."""
    _patch_urlopen(monkeypatch, {
        "cardData": {"license": ["apache-2.0", "cc-by-nc-4.0"]},
    })
    monkeypatch.setattr(run, "_HF_LICENSE_CACHE", {})

    assert run._fetch_hf_license("example/multi-license") == "apache-2.0"


def test_fetch_hf_license_caches(monkeypatch):
    hits = {"n": 0}

    def fake_urlopen(req, timeout=None):
        hits["n"] += 1
        class R:
            def read(self): return b'{"cardData": {"license": "mit"}}'
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()

    monkeypatch.setattr(run.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(run, "_HF_LICENSE_CACHE", {})

    assert run._fetch_hf_license("foo/bar") == "mit"
    assert run._fetch_hf_license("foo/bar") == "mit"
    assert run._fetch_hf_license("foo/bar") == "mit"
    assert hits["n"] == 1


# ─── _fetch_arxiv_abstract_page_urls (mocked HTTP) ────────────────────────


def test_arxiv_page_fallback_extracts_both_url_types(monkeypatch):
    page_html = (
        b'<html><body>'
        b'<a href="https://github.com/owner/repo">Code</a> '
        b'<a href="https://huggingface.co/owner/model">Model</a>'
        b'</body></html>'
    )
    _patch_urlopen(monkeypatch, page_html)
    monkeypatch.setattr(run, "_ARXIV_PAGE_CACHE", {})

    gh, hf = run._fetch_arxiv_abstract_page_urls("2502.20110")

    assert gh == ["owner/repo"]
    assert hf == ["owner/model"]


def test_arxiv_page_fallback_returns_empty_on_failure(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("DNS failure")

    monkeypatch.setattr(run.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(run, "_ARXIV_PAGE_CACHE", {})

    assert run._fetch_arxiv_abstract_page_urls("2502.20110") == ([], [])


def test_arxiv_page_fallback_caches(monkeypatch):
    hits = {"n": 0}

    def fake_urlopen(req, timeout=None):
        hits["n"] += 1
        class R:
            def read(self):
                return b'<a href="https://github.com/o/r">x</a>'
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()

    monkeypatch.setattr(run.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(run, "_ARXIV_PAGE_CACHE", {})

    run._fetch_arxiv_abstract_page_urls("2502.20110")
    run._fetch_arxiv_abstract_page_urls("2502.20110")
    run._fetch_arxiv_abstract_page_urls("2502.20110")
    assert hits["n"] == 1


def test_arxiv_page_fallback_empty_arxiv_id_short_circuits(monkeypatch):
    """Empty arxiv id must not even attempt a fetch."""
    def fake_urlopen(req, timeout=None):
        raise AssertionError("should not be called for empty arxiv_id")

    monkeypatch.setattr(run.urllib.request, "urlopen", fake_urlopen)
    assert run._fetch_arxiv_abstract_page_urls("") == ([], [])
    assert run._fetch_arxiv_abstract_page_urls("   ") == ([], [])


# ─── "no-code-link" classification ────────────────────────────────────────


def test_no_code_link_compat_score():
    """Yellow flag — distinct from missing (red) and unknown (mid)."""
    assert run._license_compat_score("no-code-link", "permissive") == 0.3


def test_render_license_section_no_code_link_emoji():
    rec = Recommendation(
        paper_title="X", arxiv_id="1", tier="moderate",
        z_score=0.0, spec_md="", paper_abstract="", domain_summary="",
        raw_paper_md="",
        license_class="no-code-link", license_compat=0.3,
    )
    section = run._render_license_section(rec)
    assert "🟡" in section
    assert "No code repository surfaced" in section
    assert "no-code-link" in section


# ─── _enrich_candidate_licenses — HF preferred + mismatch ─────────────────


def test_enrichment_prefers_hf_over_github(monkeypatch):
    """HF model card carries authoritative weight licensing — must win
    over the GitHub LICENSE classifier when both are present."""
    monkeypatch.setattr(
        run, "gh_api",
        lambda m, p, b=None: {"license": {"spdx_id": "Apache-2.0"}},
    )
    monkeypatch.setattr(run, "_LICENSE_CACHE", {})
    monkeypatch.setattr(run, "_HF_LICENSE_CACHE", {})
    monkeypatch.setattr(
        run, "_fetch_hf_license", lambda owner_model: "cc-by-nc-4.0",
    )
    monkeypatch.setattr(
        run, "_fetch_arxiv_abstract_page_urls", lambda a: ([], []),
    )

    target = Target(repo="example/permissive-target", interest_id="iid")
    rec = Recommendation(
        paper_title="DualSourcePaper", arxiv_id="2502.20110",
        tier="high", z_score=0.0, spec_md="", paper_abstract="",
        domain_summary="", raw_paper_md="",
        paper_github_url="https://github.com/example/code-says-apache",
        paper_huggingface_url="https://huggingface.co/example/model-says-cc",
    )
    run._enrich_candidate_licenses([rec], target)

    # HF result wins; license_source records the provenance.
    assert rec.paper_license == "cc-by-nc-4.0"
    assert rec.license_source == "huggingface"
    assert rec.license_class == "nc"
    assert rec.license_compat == 0.1


def test_enrichment_falls_back_to_github_when_hf_empty(monkeypatch):
    monkeypatch.setattr(
        run, "gh_api",
        lambda m, p, b=None: {"license": {"spdx_id": "MIT"}},
    )
    monkeypatch.setattr(run, "_LICENSE_CACHE", {})
    monkeypatch.setattr(run, "_HF_LICENSE_CACHE", {})
    monkeypatch.setattr(
        run, "_fetch_hf_license", lambda owner_model: "",
    )
    monkeypatch.setattr(
        run, "_fetch_arxiv_abstract_page_urls", lambda a: ([], []),
    )

    target = Target(repo="example/perm", interest_id="iid")
    rec = Recommendation(
        paper_title="X", arxiv_id="1", tier="moderate",
        z_score=0.0, spec_md="", paper_abstract="", domain_summary="",
        raw_paper_md="",
        paper_github_url="https://github.com/example/repo",
        paper_huggingface_url="https://huggingface.co/example/no-license-field",
    )
    run._enrich_candidate_licenses([rec], target)

    assert rec.paper_license == "MIT"
    assert rec.license_source == "github"
    assert rec.license_class == "permissive"


def test_enrichment_arxiv_page_fallback_populates_urls(monkeypatch):
    """When the envelope has no URLs, the arxiv-page fallback should
    populate them and enable the GitHub license fetch downstream."""
    fetched_paths = []

    def fake_gh_api(method, path, body=None):
        fetched_paths.append(path)
        if path == "/repos/example/perm/license":
            return {"license": {"spdx_id": "Apache-2.0"}}
        if path == "/repos/scraped/from-arxiv/license":
            return {"license": {"spdx_id": "BSD-3-Clause"}}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    monkeypatch.setattr(run, "_LICENSE_CACHE", {})
    monkeypatch.setattr(run, "_HF_LICENSE_CACHE", {})
    monkeypatch.setattr(
        run, "_fetch_arxiv_abstract_page_urls",
        lambda arxiv_id: (["scraped/from-arxiv"], []),
    )
    monkeypatch.setattr(run, "_fetch_hf_license", lambda owner_model: "")

    target = Target(repo="example/perm", interest_id="iid")
    rec = Recommendation(
        paper_title="X", arxiv_id="2502.20110", tier="moderate",
        z_score=0.0, spec_md="", paper_abstract="", domain_summary="",
        raw_paper_md="",
        paper_github_url="",        # envelope had nothing
        paper_huggingface_url="",
    )
    run._enrich_candidate_licenses([rec], target)

    # URL got populated from the arxiv-page fallback.
    assert rec.paper_github_url == "https://github.com/scraped/from-arxiv"
    # License was fetched against that scraped URL.
    assert rec.paper_license == "BSD-3-Clause"
    assert rec.license_class == "permissive"


def test_enrichment_no_url_anywhere_lands_no_code_link(monkeypatch):
    """Envelope empty, arxiv-page returns nothing — must land in
    no-code-link (yellow), not missing (red)."""
    monkeypatch.setattr(
        run, "gh_api",
        lambda m, p, b=None: {"license": {"spdx_id": "Apache-2.0"}},
    )
    monkeypatch.setattr(run, "_LICENSE_CACHE", {})
    monkeypatch.setattr(
        run, "_fetch_arxiv_abstract_page_urls", lambda a: ([], []),
    )

    target = Target(repo="example/perm", interest_id="iid")
    rec = Recommendation(
        paper_title="X", arxiv_id="2502.20110", tier="moderate",
        z_score=0.0, spec_md="", paper_abstract="", domain_summary="",
        raw_paper_md="",
        paper_github_url="", paper_huggingface_url="",
    )
    run._enrich_candidate_licenses([rec], target)

    assert rec.license_class == "no-code-link"
    assert rec.license_compat == 0.3


# ─── _coalesce_candidate_families ─────────────────────────────────────────


def _broad_rec(arxiv: str, title: str, github: str, relevance: float):
    return Recommendation(
        paper_title=title, arxiv_id=arxiv, tier="high",
        z_score=0.0, spec_md="", paper_abstract="", domain_summary="",
        raw_paper_md="", relevance_score=relevance,
        paper_github_url=github,
    )


def test_coalesce_merges_siblings_sharing_a_repo():
    """UniDepth + UniDepthV2 share github.com/owner/UniDepth → collapse
    to one representative (highest relevance)."""
    recs = [
        _broad_rec("2403.18913", "UniDepth", "https://github.com/owner/repo", 0.65),
        _broad_rec("2502.20110", "UniDepthV2", "https://github.com/owner/repo", 0.72),
        _broad_rec("2510.00100", "UnrelatedPaper", "https://github.com/x/y", 0.80),
    ]

    out = run._coalesce_candidate_families(recs)

    # Family collapsed; unrelated paper passes through.
    assert len(out) == 2
    titles = [r.paper_title for r in out]
    assert "UniDepthV2" in titles    # highest-relevance sibling kept
    assert "UnrelatedPaper" in titles
    assert "UniDepth" not in titles or "UniDepthV2" not in titles
    # Representative carries family_summary describing the merge.
    rep = next(r for r in out if r.paper_title == "UniDepthV2")
    assert "Coalesced from 2" in rep.family_summary
    assert "UniDepth" in rep.family_summary


def test_coalesce_does_not_merge_candidates_with_no_github_url():
    """Candidates with no code URL must not collide with each other —
    they're each their own family."""
    recs = [
        _broad_rec("2403.18913", "PaperA", "", 0.7),
        _broad_rec("2502.20110", "PaperB", "", 0.8),
    ]
    out = run._coalesce_candidate_families(recs)
    assert len(out) == 2
    assert all(r.family_summary == "" for r in out)


def test_coalesce_passes_through_when_no_families():
    """No two candidates share a repo → unchanged list."""
    recs = [
        _broad_rec("2403.18913", "A", "https://github.com/a/b", 0.9),
        _broad_rec("2502.20110", "B", "https://github.com/c/d", 0.8),
    ]
    out = run._coalesce_candidate_families(recs)
    assert len(out) == 2
    assert [r.paper_title for r in out] == ["A", "B"]
    assert all(r.family_summary == "" for r in out)


def test_coalesce_keeps_highest_relevance_as_representative():
    recs = [
        _broad_rec("p1", "Low", "https://github.com/o/r", 0.55),
        _broad_rec("p2", "Med", "https://github.com/o/r", 0.70),
        _broad_rec("p3", "High", "https://github.com/o/r", 0.91),
    ]
    out = run._coalesce_candidate_families(recs)
    assert len(out) == 1
    assert out[0].paper_title == "High"
    assert "Coalesced from 3" in out[0].family_summary


def test_coalesce_empty_or_single_passes_through():
    assert run._coalesce_candidate_families([]) == []
    solo = [_broad_rec("p1", "Solo", "https://github.com/o/r", 0.9)]
    assert run._coalesce_candidate_families(solo) == solo


def test_coalesce_is_case_insensitive_on_github_slug():
    """GitHub URLs resolve case-insensitively for owner/repo. Different
    upstream envelopes occasionally supply the URL in different cases
    (one envelope might canonicalize to lowercase, another preserves
    CamelCase) — family dedup must catch that."""
    recs = [
        _broad_rec("p1", "UniDepth",
                   "https://github.com/lpiccinelli-eth/unidepth", 0.65),
        _broad_rec("p2", "UniDepthV2",
                   "https://github.com/lpiccinelli-eth/UniDepth", 0.72),
    ]
    out = run._coalesce_candidate_families(recs)
    assert len(out) == 1
    assert out[0].paper_title == "UniDepthV2"
    assert "Coalesced from 2" in out[0].family_summary


# ─── issue_for_paper sibling-paper dedup ──────────────────────────────────


def test_issue_for_paper_matches_sibling_via_github_url():
    """An existing open Issue for arxiv X that mentions github.com/o/r
    should also match a new candidate for arxiv Y that points to the
    same code repo — sibling-paper dedup."""
    open_issues = [
        {
            "title": "[Remyx Recommendation] UniDepth",
            "body": (
                "**Recommended paper**: UniDepth\n"
                "arxiv.org/abs/2403.18913\n"
                "**Code**: https://github.com/lpiccinelli-eth/UniDepth"
            ),
        },
    ]
    new_candidate = Recommendation(
        paper_title="UniDepthV2", arxiv_id="2502.20110", tier="high",
        z_score=0.0, spec_md="", paper_abstract="", domain_summary="",
        raw_paper_md="",
        paper_github_url="https://github.com/lpiccinelli-eth/UniDepth",
    )
    match = run.issue_for_paper(open_issues, new_candidate)
    assert match is not None
    assert match["title"] == "[Remyx Recommendation] UniDepth"


def test_issue_for_paper_matches_sibling_via_huggingface_url():
    """Same idea via huggingface.co reference in the existing Issue."""
    open_issues = [
        {
            "title": "[Remyx Recommendation] BERT",
            "body": (
                "arxiv.org/abs/1810.04805\n"
                "Model card: https://huggingface.co/google-bert/bert-base"
            ),
        },
    ]
    sibling = Recommendation(
        paper_title="Sibling", arxiv_id="9999.99999", tier="high",
        z_score=0.0, spec_md="", paper_abstract="", domain_summary="",
        raw_paper_md="",
        paper_huggingface_url="https://huggingface.co/google-bert/bert-base",
    )
    assert run.issue_for_paper(open_issues, sibling) is not None


def test_issue_for_paper_no_match_when_urls_differ():
    open_issues = [
        {
            "title": "[Remyx Recommendation] X",
            "body": "arxiv.org/abs/A\nhttps://github.com/o/repo-x",
        },
    ]
    unrelated = Recommendation(
        paper_title="Different", arxiv_id="9999.0", tier="high",
        z_score=0.0, spec_md="", paper_abstract="", domain_summary="",
        raw_paper_md="",
        paper_github_url="https://github.com/o/repo-y",
    )
    assert run.issue_for_paper(open_issues, unrelated) is None
