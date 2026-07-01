"""Unit tests for the license + code-availability gate.

Covers pure logic: SPDX classification, license-compat scoring,
GitHub-URL extraction from paper text, and per-class rendering of the
"License & code availability" PR/Issue section. The GitHub-fetch path
(``_fetch_repo_license``) is exercised indirectly via monkeypatching of
``gh_api`` since it's the only network call.

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Recommendation, Target  # noqa: E402


# ─── _extract_github_urls ─────────────────────────────────────────────────


def test_extract_github_urls_single_match():
    out = run._extract_github_urls("see https://github.com/foo/bar for code")
    assert out == ["foo/bar"]


def test_extract_github_urls_dedup_and_order_preserving():
    text = (
        "https://github.com/foo/bar and again https://github.com/foo/bar, "
        "then https://github.com/baz/qux"
    )
    assert run._extract_github_urls(text) == ["foo/bar", "baz/qux"]


def test_extract_github_urls_strips_dot_git_and_trailing_path():
    text = "code at https://github.com/foo/bar.git/tree/main/sub"
    # .git stripped; everything after the / cut.
    assert run._extract_github_urls(text) == ["foo/bar"]


def test_extract_github_urls_skips_non_repo_owners():
    text = (
        "https://github.com/orgs/remyxai and "
        "https://github.com/topics/vlm should be skipped; "
        "https://github.com/foo/bar should be kept"
    )
    assert run._extract_github_urls(text) == ["foo/bar"]


def test_extract_github_urls_handles_empty_and_none_safely():
    assert run._extract_github_urls("", None, "  ") == []


def test_extract_github_urls_multiple_texts():
    out = run._extract_github_urls(
        "abstract mentions https://github.com/a/one",
        "reasoning mentions https://github.com/b/two",
    )
    assert out == ["a/one", "b/two"]


# ─── _classify_license ────────────────────────────────────────────────────


def test_classify_license_permissive():
    for spdx in ["Apache-2.0", "MIT", "BSD-3-Clause", "ISC", "CC0-1.0"]:
        assert run._classify_license(spdx) == "permissive", spdx


def test_classify_license_copyleft():
    for spdx in ["GPL-3.0", "GPL-2.0", "AGPL-3.0", "MPL-2.0", "CC-BY-SA-4.0"]:
        assert run._classify_license(spdx) == "copyleft", spdx


def test_classify_license_nc():
    for spdx in ["CC-BY-NC-4.0", "CC-BY-NC-SA-4.0", "CC-BY-ND-4.0",
                 "CC-BY-NC-SA-3.0"]:
        assert run._classify_license(spdx) == "nc", spdx


def test_classify_license_missing_when_empty():
    assert run._classify_license("") == "missing"
    assert run._classify_license("   ") == "missing"


def test_classify_license_unknown_for_unrecognized():
    # GitHub returns NOASSERTION when it can't parse — _fetch_repo_license
    # collapses that to "", but in case a different caller passes it
    # through, treat it as unknown rather than crashing.
    assert run._classify_license("Custom-License-1.0") == "unknown"
    assert run._classify_license("NOASSERTION") == "unknown"


# ─── _license_compat_score ────────────────────────────────────────────────


def test_compat_permissive_paper_into_anything():
    # Permissive paper code can be adopted regardless of target license.
    for target in ["permissive", "copyleft", "nc", "missing", "unknown"]:
        assert run._license_compat_score("permissive", target) == 1.0


def test_compat_missing_is_blocking():
    # No LICENSE = no permission. Worst-case score everywhere.
    for target in ["permissive", "copyleft", "unknown"]:
        assert run._license_compat_score("missing", target) == 0.0


def test_compat_nc_is_near_zero():
    # NC stays adoption-blocking — visible (non-zero) but should sort
    # below every other class.
    assert run._license_compat_score("nc", "permissive") == 0.1
    assert run._license_compat_score("nc", "copyleft") == 0.1


def test_compat_copyleft_depends_on_target():
    # Copyleft into copyleft: fine. Into permissive: license-compat
    # discussion needed — yellow flag, not red.
    assert run._license_compat_score("copyleft", "copyleft") == 0.7
    assert run._license_compat_score("copyleft", "permissive") == 0.5
    assert run._license_compat_score("copyleft", "unknown") == 0.5


def test_compat_unknown_lands_mid():
    # Unknown should be visible (non-zero) but penalized vs permissive.
    assert run._license_compat_score("unknown", "permissive") == 0.5


# ─── _paper_to_recommendation github URL extraction ──────────────────────


def test_paper_to_recommendation_picks_github_url_from_resource_key():
    rec = run._paper_to_recommendation(
        {
            "title": "X",
            "resource_id": "2601.00001v1",
            "relevance_score": 0.9,
            "resource": {
                "abstract": "no link in here",
                "github_url": "https://github.com/foo/bar",
            },
        },
        fallback_interest_name="x",
        interest_context="",
        experiment_history="",
    )
    assert rec.paper_github_url == "https://github.com/foo/bar"


def test_paper_to_recommendation_falls_back_to_text_scrape():
    rec = run._paper_to_recommendation(
        {
            "title": "X",
            "resource_id": "2601.00002v1",
            "relevance_score": 0.9,
            "reasoning": "code at https://github.com/baz/qux/tree/main",
            "resource": {"abstract": ""},
        },
        fallback_interest_name="x",
        interest_context="",
        experiment_history="",
    )
    assert rec.paper_github_url == "https://github.com/baz/qux"


def test_paper_to_recommendation_no_url_leaves_field_empty():
    rec = run._paper_to_recommendation(
        {
            "title": "X",
            "resource_id": "2601.00003v1",
            "relevance_score": 0.9,
            "resource": {"abstract": "no links anywhere"},
        },
        fallback_interest_name="x",
        interest_context="",
        experiment_history="",
    )
    assert rec.paper_github_url == ""
    # Dataclass defaults intact when no enrichment has run.
    assert rec.paper_license == ""
    assert rec.license_class == "unknown"
    assert rec.license_compat == 0.0


# ─── _enrich_candidate_licenses (mocked gh_api) ──────────────────────────


def test_enrich_candidates_populates_fields_and_compat(monkeypatch):
    # Mock gh_api: target repo is Apache-2.0; one candidate repo is
    # CC-BY-NC-SA-4.0, another has no LICENSE (404), a third has no
    # code URL at all. Verify each lands in the right bucket.
    calls: list[str] = []

    def fake_gh_api(method, path, body=None):
        calls.append(path)
        if path == "/repos/example/target-repo/license":
            return {"license": {"spdx_id": "Apache-2.0"}}
        if path == "/repos/example/nc-paper/license":
            return {"license": {"spdx_id": "CC-BY-NC-SA-4.0"}}
        if path == "/repos/example/no-license-paper/license":
            # 404 surfaces as a RuntimeError from gh_api.
            raise RuntimeError(
                "GitHub GET /repos/example/no-license-paper/license "
                "→ HTTP 404"
            )
        raise AssertionError(f"unexpected gh_api path: {path}")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    monkeypatch.setattr(run, "_LICENSE_CACHE", {})
    # Block the arxiv-page fallback so the NoCodePaper case doesn't hit
    # the network during a unit test.
    monkeypatch.setattr(
        run, "_fetch_arxiv_abstract_page_urls", lambda arxiv_id: ([], []),
    )
    # Block the arxiv HTML retry (fires on unfavorable buckets) so it
    # doesn't hit the network during a unit test either.
    monkeypatch.setattr(
        run, "_fetch_arxiv_html_urls", lambda arxiv_id: [],
    )

    target = Target(repo="example/target-repo", interest_id="iid")
    candidates = [
        Recommendation(
            paper_title="NCPaper", arxiv_id="2512.02541", tier="high",
            z_score=0.0, spec_md="", paper_abstract="", domain_summary="",
            raw_paper_md="",
            paper_github_url="https://github.com/example/nc-paper",
        ),
        Recommendation(
            paper_title="NoLicensePaper", arxiv_id="2511.01010",
            tier="high", z_score=0.0, spec_md="", paper_abstract="",
            domain_summary="", raw_paper_md="",
            paper_github_url="https://github.com/example/no-license-paper",
        ),
        Recommendation(
            paper_title="NoCodePaper", arxiv_id="2510.00100",
            tier="moderate", z_score=0.0, spec_md="", paper_abstract="",
            domain_summary="", raw_paper_md="",
            paper_github_url="",   # no code link surfaced
        ),
    ]

    run._enrich_candidate_licenses(candidates, target)

    # NC license → adoption-blocking score.
    assert candidates[0].paper_license == "CC-BY-NC-SA-4.0"
    assert candidates[0].license_class == "nc"
    assert candidates[0].license_compat == 0.1

    # 404 from GitHub → empty paper_license, classified "missing" so
    # the downstream report surfaces a loud red flag rather than a
    # silent "unknown" — the helper's contract is that no parseable
    # LICENSE is the worst signal we can give.
    assert candidates[1].paper_license == ""
    assert candidates[1].license_class == "missing"
    assert candidates[1].license_compat == 0.0

    # No URL at all (after arxiv-page fallback also produced nothing) →
    # "no-code-link" (yellow flag), distinct from "missing" (red flag,
    # reserved for "we did fetch a LICENSE endpoint and got nothing").
    assert candidates[2].license_class == "no-code-link"
    assert candidates[2].license_compat == 0.3

    # Target license was fetched exactly once (cached).
    assert calls.count("/repos/example/target-repo/license") == 1


# ─── arxiv HTML retry (license fallback discovery) ────────────────────────


def test_rank_slugs_by_title_overlap_picks_paper_repo():
    """Given multiple github refs, the one whose repo name substrings
    match the paper title ranks first — that's the paper's own repo,
    not a citation to a competitor."""
    slugs = ["cloneofsimo/lora", "pilancilab/Riemannian_Preconditioned_LoRA"]
    ranked = run._rank_slugs_by_title_overlap(
        slugs, "Riemannian Preconditioned LoRA for Fine-Tuning Foundation Models"
    )
    assert ranked[0] == "pilancilab/Riemannian_Preconditioned_LoRA"


def test_rank_slugs_by_title_overlap_handles_compound_repo_names():
    """Compound repo names like `EntityBindingFailures` (no separators)
    still match via substring against title words."""
    slugs = ["arxiv/html_feedback", "R-Suresh/EntityBindingFailures"]
    ranked = run._rank_slugs_by_title_overlap(
        slugs, "Entity Binding Failures in Tool-Augmented Agents"
    )
    assert ranked[0] == "R-Suresh/EntityBindingFailures"


def test_retry_via_arxiv_html_skips_favorable_buckets(monkeypatch):
    """When license_class is already `permissive` (or any non-unfavorable),
    the retry never runs — never overrides successful classification."""
    called = {"n": 0}
    def fake_fetch_urls(arxiv_id):
        called["n"] += 1
        return ["should/not/matter"]
    monkeypatch.setattr(run, "_fetch_arxiv_html_urls", fake_fetch_urls)

    c = Recommendation(
        paper_title="Foo", arxiv_id="2410.12345", tier="high",
        z_score=0.0, spec_md="", paper_abstract="", domain_summary="",
        raw_paper_md="",
    )
    c.license_class = "permissive"
    c.license_compat = 1.0
    result = run._retry_license_via_arxiv_html(c, target_class="permissive")
    assert result is False
    assert called["n"] == 0
    # Fields unchanged.
    assert c.license_class == "permissive"
    assert c.license_compat == 1.0


def test_retry_via_arxiv_html_recovers_discovery_gap(monkeypatch):
    """Candidate with `license_class=no-code-link` gets recovered when
    arxiv HTML discovers a github URL that returns MIT — exactly the
    LettuceDetect / EntityBindingFailures failure shape."""
    monkeypatch.setattr(
        run, "_fetch_arxiv_html_urls",
        lambda arxiv_id: ["KRLabsOrg/LettuceDetect"],
    )
    monkeypatch.setattr(
        run, "_fetch_repo_license", lambda slug: "MIT",
    )

    c = Recommendation(
        paper_title="LettuceDetect: A Hallucination Detection Framework for RAG",
        arxiv_id="2502.17125v1", tier="high", z_score=0.0, spec_md="",
        paper_abstract="", domain_summary="", raw_paper_md="",
    )
    c.license_class = "no-code-link"
    c.license_compat = 0.3

    result = run._retry_license_via_arxiv_html(c, target_class="permissive")
    assert result is True
    assert c.paper_license == "MIT"
    assert c.paper_github_url == "https://github.com/KRLabsOrg/LettuceDetect"
    assert c.license_class == "permissive"
    assert c.license_compat == 1.0
    assert c.license_source == "arxiv_html_retry"


def test_retry_via_arxiv_html_recovers_timing_gap(monkeypatch):
    """Candidate with `license_class=unknown` (URL was known but classifier
    returned NOASSERTION or similar) gets recovered on retry — the
    Riemannian LoRA / LightMem2 failure shape."""
    monkeypatch.setattr(
        run, "_fetch_arxiv_html_urls",
        lambda arxiv_id: ["pilancilab/Riemannian_Preconditioned_LoRA"],
    )
    monkeypatch.setattr(
        run, "_fetch_repo_license", lambda slug: "MIT",
    )

    c = Recommendation(
        paper_title="Riemannian Preconditioned LoRA for Fine-Tuning Foundation Models",
        arxiv_id="2402.02347v3", tier="high", z_score=0.0, spec_md="",
        paper_abstract="", domain_summary="", raw_paper_md="",
        paper_github_url="https://github.com/pilancilab/Riemannian_Preconditioned_LoRA",
    )
    c.paper_license = "NOASSERTION"  # what the primary path landed on
    c.license_class = "unknown"
    c.license_compat = 0.0

    result = run._retry_license_via_arxiv_html(c, target_class="permissive")
    assert result is True
    assert c.paper_license == "MIT"
    assert c.license_class == "permissive"
    # The URL was already set — retry doesn't clobber it.
    assert c.paper_github_url == "https://github.com/pilancilab/Riemannian_Preconditioned_LoRA"


def test_retry_via_arxiv_html_skips_noassertion_from_retry(monkeypatch):
    """If the retry also finds a URL but that URL's license fetch returns
    NOASSERTION (GitHub classifier still lagging), keep the original
    unfavorable class rather than falsely upgrading."""
    monkeypatch.setattr(
        run, "_fetch_arxiv_html_urls",
        lambda arxiv_id: ["some/repo"],
    )
    monkeypatch.setattr(
        run, "_fetch_repo_license", lambda slug: "NOASSERTION",
    )

    c = Recommendation(
        paper_title="Foo", arxiv_id="2410.12345", tier="high",
        z_score=0.0, spec_md="", paper_abstract="", domain_summary="",
        raw_paper_md="",
    )
    c.license_class = "unknown"
    c.license_compat = 0.0

    result = run._retry_license_via_arxiv_html(c, target_class="permissive")
    assert result is False
    assert c.license_class == "unknown"


def test_retry_via_arxiv_html_tries_multiple_urls_until_match(monkeypatch):
    """When ranked URLs are tried in order, the first one that returns a
    real SPDX wins; NOASSERTION responses skip to the next candidate."""
    monkeypatch.setattr(
        run, "_fetch_arxiv_html_urls",
        lambda arxiv_id: ["some/citation", "real/paper_repo"],
    )
    responses = {"some/citation": "NOASSERTION", "real/paper_repo": "MIT"}
    monkeypatch.setattr(
        run, "_fetch_repo_license", lambda slug: responses[slug],
    )

    c = Recommendation(
        paper_title="paper repo", arxiv_id="2410.12345", tier="high",
        z_score=0.0, spec_md="", paper_abstract="", domain_summary="",
        raw_paper_md="",
    )
    c.license_class = "no-code-link"
    c.license_compat = 0.3

    result = run._retry_license_via_arxiv_html(c, target_class="permissive")
    assert result is True
    assert c.paper_license == "MIT"
    # Title-overlap ranking put real/paper_repo first (title "paper repo"
    # substring-matches "paper_repo" in slug name).
    assert c.paper_github_url == "https://github.com/real/paper_repo"


def test_fetch_repo_license_caches(monkeypatch):
    """Same owner/repo should hit gh_api at most once per process."""
    hits = {"n": 0}

    def fake_gh_api(method, path, body=None):
        hits["n"] += 1
        return {"license": {"spdx_id": "MIT"}}

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    monkeypatch.setattr(run, "_LICENSE_CACHE", {})

    a = run._fetch_repo_license("foo/bar")
    b = run._fetch_repo_license("foo/bar")
    c = run._fetch_repo_license("foo/bar")
    assert a == b == c == "MIT"
    assert hits["n"] == 1


def test_fetch_repo_license_noassertion_with_unrecognized_content_stays_noassertion(monkeypatch):
    """NOASSERTION from GitHub with a LICENSE file the sniffer can't
    classify should land as "NOASSERTION" so the upstream classifier
    buckets it as "unknown" (yellow flag) — never as "missing" (red
    flag, reserved for "no LICENSE file at all")."""
    import base64 as _b64
    custom_text = _b64.b64encode(b"My fully custom academic license\n"
                                 b"No standard FOSS/CC fingerprints here.").decode()
    def fake_gh_api(method, path, body=None):
        return {
            "license": {"spdx_id": "NOASSERTION"},
            "content": custom_text, "encoding": "base64",
        }

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    monkeypatch.setattr(run, "_LICENSE_CACHE", {})

    assert run._fetch_repo_license("foo/bar") == "NOASSERTION"
    assert run._classify_license("NOASSERTION") == "unknown"


def test_fetch_repo_license_sniffs_cc_by_nc_when_github_punts(monkeypatch):
    """The headline regression case from the v1.3.8 ship:
    UniDepth (lpiccinelli-eth/UniDepth) is CC-BY-NC-4.0 but GitHub's
    classifier returns NOASSERTION. The sniffer must catch this so
    the gate flags the NC restriction instead of silently reporting
    "missing"."""
    import base64 as _b64
    cc_by_nc = _b64.b64encode(
        b"Attribution-NonCommercial 4.0 International\n\n"
        b"Creative Commons Corporation ...\n"
    ).decode()
    def fake_gh_api(method, path, body=None):
        return {
            "license": {"spdx_id": "NOASSERTION"},
            "content": cc_by_nc, "encoding": "base64",
        }

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    monkeypatch.setattr(run, "_LICENSE_CACHE", {})

    spdx = run._fetch_repo_license("foo/bar")
    assert spdx == "CC-BY-NC-4.0"
    assert run._classify_license(spdx) == "nc"


def test_sniff_license_from_content_creative_commons_variants():
    """The full Creative Commons matrix is the headline case for the
    sniffer. NC > NC-SA > NC-ND priority ordering must not collapse
    composites into the simpler parent class."""
    import base64 as _b64

    def b64(text: str) -> str:
        return _b64.b64encode(text.encode()).decode()

    cases = [
        ("Attribution-NonCommercial-ShareAlike 4.0", "CC-BY-NC-SA-4.0"),
        ("Attribution-NonCommercial-ShareAlike 3.0", "CC-BY-NC-SA-3.0"),
        ("Attribution-NonCommercial-NoDerivatives 4.0", "CC-BY-NC-ND-4.0"),
        ("Attribution-NonCommercial 4.0", "CC-BY-NC-4.0"),
        ("Attribution-NoDerivatives 4.0", "CC-BY-ND-4.0"),
        ("Attribution-ShareAlike 4.0", "CC-BY-SA-4.0"),
        ("Attribution 4.0 International", "CC-BY-4.0"),
    ]
    for needle, expected_spdx in cases:
        content = b64(f"{needle}\n\nCreative Commons preamble ...")
        assert run._sniff_license_from_content(content) == expected_spdx, needle


def test_sniff_license_from_content_standard_foss_licenses():
    import base64 as _b64

    def b64(text: str) -> str:
        return _b64.b64encode(text.encode()).decode()

    assert run._sniff_license_from_content(
        b64("Apache License\nVersion 2.0, January 2004\n...")
    ) == "Apache-2.0"
    assert run._sniff_license_from_content(
        b64("MIT License\n\nPermission is hereby granted, "
            "free of charge, to any person ...")
    ) == "MIT"
    assert run._sniff_license_from_content(
        b64("BSD 3-Clause License\n\nRedistribution and use in source "
            "and binary forms, with or without modification, ...")
    ) == "BSD-3-Clause"
    assert run._sniff_license_from_content(
        b64("GNU GENERAL PUBLIC LICENSE\nVersion 3 ...")
    ) == "GPL-3.0"
    assert run._sniff_license_from_content(
        b64("GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3 ...")
    ) == "AGPL-3.0"
    assert run._sniff_license_from_content(
        b64("Mozilla Public License Version 2.0 ...")
    ) == "MPL-2.0"


def test_sniff_license_from_content_returns_empty_on_no_match():
    import base64 as _b64
    custom = _b64.b64encode(
        b"Proprietary research license, all rights reserved.\n"
        b"Contact the authors for terms."
    ).decode()
    assert run._sniff_license_from_content(custom) == ""


def test_sniff_license_from_content_handles_empty_and_invalid_safely():
    assert run._sniff_license_from_content("") == ""
    assert run._sniff_license_from_content("not-valid-base64!@#$") == ""


def test_sniff_license_from_content_prefers_specific_over_general():
    """NC-SA contains both 'NonCommercial' and 'ShareAlike' substrings;
    must match NC-SA not plain NC or plain SA."""
    import base64 as _b64
    content = _b64.b64encode(
        b"Attribution-NonCommercial-ShareAlike 4.0 International"
    ).decode()
    assert run._sniff_license_from_content(content) == "CC-BY-NC-SA-4.0"


def test_fetch_repo_license_swallows_errors(monkeypatch):
    """Any exception from gh_api must return "" — license fetch must
    never block the pipeline."""
    def fake_gh_api(method, path, body=None):
        raise RuntimeError("simulated 503")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    monkeypatch.setattr(run, "_LICENSE_CACHE", {})

    assert run._fetch_repo_license("foo/bar") == ""


# ─── _render_license_section ──────────────────────────────────────────────


def _rec_with(license_class, paper_license="", url="", compat=0.0):
    return Recommendation(
        paper_title="X", arxiv_id="1234.5678", tier="high",
        z_score=0.0, spec_md="", paper_abstract="", domain_summary="",
        raw_paper_md="",
        paper_github_url=url, paper_license=paper_license,
        license_class=license_class, license_compat=compat,
    )


def test_render_license_section_permissive_safe_to_adopt():
    s = run._render_license_section(_rec_with(
        "permissive", "Apache-2.0", "https://github.com/foo/bar", 1.0,
    ))
    assert "License & code availability" in s
    assert "Apache-2.0" in s
    assert "Permissive" in s
    assert "🟢" in s


def test_render_license_section_nc_is_adoption_blocked():
    s = run._render_license_section(_rec_with(
        "nc", "CC-BY-NC-SA-4.0", "https://github.com/avggt/code", 0.1,
    ))
    assert "adoption blocked" in s.lower()
    assert "CC-BY-NC-SA-4.0" in s
    assert "🔴" in s


def test_render_license_section_missing_is_loud():
    s = run._render_license_section(_rec_with("missing", "", "", 0.0))
    assert "No LICENSE file detected" in s
    assert "blocking" in s.lower()
    assert "🔴" in s


def test_render_license_section_empty_when_no_enrichment():
    # Pristine Recommendation (env opt-out or caller that bypasses
    # query_remyx_candidates) — render to "\n" so PR-body formatting
    # doesn't change.
    rec = Recommendation(
        paper_title="X", arxiv_id="1", tier="high", z_score=0.0,
        spec_md="", paper_abstract="", domain_summary="", raw_paper_md="",
    )
    assert run._render_license_section(rec) == "\n"


# ─── _render_candidate_brief surfaces license info ───────────────────────


def test_candidate_brief_includes_license_line():
    rec = _rec_with("nc", "CC-BY-NC-SA-4.0",
                    "https://github.com/avggt/code", 0.1)
    brief = run._render_candidate_brief([rec])
    assert "code/license:" in brief
    assert "avggt/code" in brief
    assert "CC-BY-NC-SA-4.0" in brief
    assert "(nc, compat=0.10)" in brief


def test_candidate_brief_omits_license_line_when_unset():
    rec = Recommendation(
        paper_title="X", arxiv_id="1", tier="high", z_score=0.0,
        spec_md="", paper_abstract="abstract here",
        domain_summary="", raw_paper_md="",
        reasoning="why", relevance_score=0.9,
    )
    brief = run._render_candidate_brief([rec])
    # Default Recommendation has license_class="unknown", no URL, no license —
    # the brief should suppress the license line entirely.
    assert "code/license:" not in brief
