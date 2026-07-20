"""Tests for the hostname-match extension in `_extract_project_page_urls`.

Prior behavior: title-word substring must appear in the URL PATH.
New behavior: match in EITHER path OR hostname (subdomain).

Motivating case: TIPSv2 (arxiv 2604.12012) project page is
https://gdm-tipsv2.github.io/ — hostname carries the paper name,
path is empty. Prior extractor returned [] and the one-hop-to-
github discovery of `google-deepmind/tips` never fired, downgrading
the recommendation from PR to Issue with `no-code-link`.

Run with: pytest tests/test_project_page_hostname_match.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


_TIPSV2_TITLE = (
    "TIPSv2: Advancing Vision-Language Pretraining with Enhanced Patch-Text Alignment"
)


# ─── The bug we're fixing ───────────────────────────────────────────────


def test_hostname_match_catches_paper_named_github_io():
    """The motivating case: `<paper>.github.io/` with empty path was skipped."""
    html = "See the code at https://gdm-tipsv2.github.io/ for details."
    urls = run._extract_project_page_urls(html, _TIPSV2_TITLE)
    assert "https://gdm-tipsv2.github.io/" in urls


def test_hostname_match_paper_named_io_domain():
    """Also common: `<paper>.io/` (or `.ai/`, `.dev/`) with empty or short path."""
    html = 'Project page: <a href="https://dreamerv3.io/">Dreamer V3</a>'
    urls = run._extract_project_page_urls(html, "DreamerV3: World Models for Everything")
    # 'dreamerv' or 'dreamer' should match against 'dreamerv3.io'
    assert any("dreamerv3.io" in u for u in urls)


def test_hostname_match_lab_prefixed_subdomain():
    """Real-world shape: `<lab>-<paper>.github.io` — the paper name is
    embedded in a compound subdomain."""
    html = "https://gdm-tipsv2.github.io/index.html"
    urls = run._extract_project_page_urls(html, _TIPSV2_TITLE)
    assert len(urls) == 1
    assert "gdm-tipsv2.github.io" in urls[0]


# ─── Regression guards — prior path-match behavior preserved ────────────


def test_path_match_still_works():
    """Prior path-only behavior — title-word in the path — must still hit."""
    html = "Details at https://example.com/tipsv2-project/"
    urls = run._extract_project_page_urls(html, _TIPSV2_TITLE)
    assert "https://example.com/tipsv2-project/" in urls


def test_neither_path_nor_host_matches_returns_empty():
    """A URL with no title-word overlap in path or host must still be skipped."""
    html = "Unrelated: https://random-blog.com/post/12345"
    urls = run._extract_project_page_urls(html, _TIPSV2_TITLE)
    assert urls == []


# ─── Exclusion rules still applied ──────────────────────────────────────


def test_github_com_still_excluded_even_when_hostname_matches():
    """github.com is filtered before the hostname check — direct github
    URLs go through _extract_github_urls, not this extractor."""
    html = "Repo: https://github.com/gdm-tipsv2/tips"
    urls = run._extract_project_page_urls(html, _TIPSV2_TITLE)
    assert urls == []


def test_arxiv_still_excluded():
    """arxiv is filtered similarly."""
    html = "https://arxiv.org/abs/2604.12012 tipsv2 paper"
    urls = run._extract_project_page_urls(html, _TIPSV2_TITLE)
    assert urls == []


def test_huggingface_still_excluded_even_with_matching_hostname():
    """huggingface.co is filtered."""
    html = "Model: https://huggingface.co/google/tipsv2-l14"
    urls = run._extract_project_page_urls(html, _TIPSV2_TITLE)
    assert urls == []


# ─── Edge cases ─────────────────────────────────────────────────────────


def test_empty_html_returns_empty():
    assert run._extract_project_page_urls("", _TIPSV2_TITLE) == []


def test_empty_title_returns_empty():
    """No title words → nothing to match against → no false positives."""
    html = "https://gdm-tipsv2.github.io/"
    assert run._extract_project_page_urls(html, "") == []


def test_fanout_cap_respected_with_hostname_match():
    """The 3-URL fanout cap must still apply even when all match by hostname."""
    html = " ".join([
        "https://tipsv2-a.example.com/",
        "https://tipsv2-b.example.com/",
        "https://tipsv2-c.example.com/",
        "https://tipsv2-d.example.com/",
        "https://tipsv2-e.example.com/",
    ])
    urls = run._extract_project_page_urls(html, _TIPSV2_TITLE)
    assert len(urls) == 3


def test_dedup_across_path_and_host_match():
    """Same URL discovered by different match paths dedupes correctly."""
    html = ("https://gdm-tipsv2.github.io/ "
            "https://gdm-tipsv2.github.io/ "  # dup
            "https://example.com/tipsv2-page/")
    urls = run._extract_project_page_urls(html, _TIPSV2_TITLE)
    assert len(urls) == 2
    assert "https://gdm-tipsv2.github.io/" in urls
    assert "https://example.com/tipsv2-page/" in urls
