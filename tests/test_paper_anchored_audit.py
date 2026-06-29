"""Tests for Phase A paper-anchored fidelity-audit helpers.

Covers the three new surfaces:
  - `_fetch_arxiv_abstract_text` extracts title + abstract from the arxiv
    abstract page HTML (and gracefully returns "" on failures).
  - `_build_fidelity_audit_prompt_paper_anchored` produces a prompt that
    embeds the paper text and instructs Claude to audit against the
    paper's described method (not a cloned reference codebase).
  - `_render_coverage_matrix` surfaces the audit anchor in the rendered
    Coverage section so maintainers can read precision expectations from
    the artifact itself.

Run with: pytest tests/test_paper_anchored_audit.py -q
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


_ARXIV_PAGE_FIXTURE = """\
<html><head><title>2606.11127 — arxiv</title></head><body>
<h1 class="title mathjax">
  <span class="descriptor">Title:</span>
  Provenance-Grounded Gating and Adaptive Recovery in Synthetic Post-Training Data Curation
</h1>
<blockquote class="abstract mathjax">
  <span class="descriptor">Abstract:</span>
  We study quality gating in synthetic data curation pipelines and show
  that a provenance-grounded gate plus an adaptive recovery loop raises
  yield substantially over naive discard. The recovery loop diagnoses
  rejected samples and re-admits geometry near-misses within a bounded
  margin.
</blockquote>
</body></html>
"""


def _fake_urlopen_factory(html: str):
    """Build a context-manager-returning fake for urllib.request.urlopen."""
    class _FakeResp:
        def __init__(self, body):
            self._body = body
        def read(self):
            return self._body.encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    def _fake(*_args, **_kwargs):
        return _FakeResp(html)
    return _fake


# ─── _fetch_arxiv_abstract_text ─────────────────────────────────────────────


def test_fetch_arxiv_abstract_text_extracts_title_and_abstract():
    run._ARXIV_ABSTRACT_TEXT_CACHE.pop("2606.11127v1", None)
    with patch("urllib.request.urlopen", _fake_urlopen_factory(_ARXIV_PAGE_FIXTURE)):
        text = run._fetch_arxiv_abstract_text("2606.11127v1")
    assert "Title: Provenance-Grounded Gating" in text
    assert "Abstract: We study quality gating" in text
    # "Title:" / "Abstract:" descriptor labels are stripped from the start
    # of each section value, not carried as a raw prefix on the content.
    assert "Title: Title:" not in text
    assert "Abstract: Abstract:" not in text


def test_fetch_arxiv_abstract_text_caches_result():
    run._ARXIV_ABSTRACT_TEXT_CACHE.pop("2606.11127v1", None)
    call_count = {"n": 0}
    base_fake = _fake_urlopen_factory(_ARXIV_PAGE_FIXTURE)
    def counting_fake(*a, **kw):
        call_count["n"] += 1
        return base_fake(*a, **kw)
    with patch("urllib.request.urlopen", counting_fake):
        run._fetch_arxiv_abstract_text("2606.11127v1")
        run._fetch_arxiv_abstract_text("2606.11127v1")
    # Second call hits the cache — only one fetch.
    assert call_count["n"] == 1


def test_fetch_arxiv_abstract_text_returns_empty_on_fetch_failure():
    run._ARXIV_ABSTRACT_TEXT_CACHE.pop("9999.99999", None)
    def _raises(*_a, **_k):
        raise OSError("simulated network failure")
    with patch("urllib.request.urlopen", _raises):
        text = run._fetch_arxiv_abstract_text("9999.99999")
    assert text == ""


def test_fetch_arxiv_abstract_text_returns_empty_when_no_blocks_found():
    # Page exists but has neither <h1 class="title"> nor
    # <blockquote class="abstract"> — should produce empty text, not
    # raise. Defensive against arxiv markup drift.
    run._ARXIV_ABSTRACT_TEXT_CACHE.pop("0000.00000", None)
    with patch(
        "urllib.request.urlopen",
        _fake_urlopen_factory("<html><body><p>unrelated</p></body></html>"),
    ):
        text = run._fetch_arxiv_abstract_text("0000.00000")
    assert text == ""


def test_fetch_arxiv_abstract_text_empty_id_short_circuits():
    # No fetch should happen for empty arxiv ids.
    called = {"n": 0}
    def tracker(*_a, **_k):
        called["n"] += 1
        raise AssertionError("urlopen should not have been called for empty id")
    with patch("urllib.request.urlopen", tracker):
        text = run._fetch_arxiv_abstract_text("")
    assert text == ""
    assert called["n"] == 0


# ─── _build_fidelity_audit_prompt_paper_anchored ───────────────────────────


def test_paper_anchored_prompt_embeds_paper_text_and_signals_anchor():
    prompt = run._build_fidelity_audit_prompt_paper_anchored(
        pr_title="Provenance gating in OCRScoringQAStage",
        pr_body="Recommended paper: arxiv:2606.11127v1\nDelivers adaptive recovery.",
        pr_diff="diff --git a/ocr_scoring_qa.py b/ocr_scoring_qa.py\n+def _recover_rejected(): ...",
        arxiv_id="2606.11127v1",
        paper_text="Title: Provenance-Grounded Gating ...\nAbstract: We study quality gating ...",
    )
    # Paper text is in the prompt; the prompt explicitly tells Claude it's
    # auditing without a cloned reference impl.
    assert "Provenance-Grounded Gating" in prompt
    assert "paper-anchored" in prompt.lower()
    assert "abstract" in prompt.lower()
    # The output JSON schema stays compatible with the reference-anchored
    # variant so downstream rendering / status logic doesn't fork.
    assert '"summary"' in prompt
    assert '"needs_judgment"' in prompt
    assert '"items"' in prompt
    assert '"deviation_class"' in prompt


def test_paper_anchored_prompt_truncates_long_diff_inputs():
    # 60 KB cap on the diff + sentinel marker, mirroring the reference-
    # anchored prompt's behavior.
    big_diff = "x" * 200_000
    prompt = run._build_fidelity_audit_prompt_paper_anchored(
        pr_title="t", pr_body="b",
        pr_diff=big_diff,
        arxiv_id="2606.11127v1", paper_text="Title: x\nAbstract: y",
    )
    assert "diff truncated" in prompt
    assert "200000 chars" in prompt


# ─── _render_coverage_matrix audit-anchor surfacing ────────────────────────


_MATRIX_FIXTURE = {
    "summary": "Diff implements the abstract's recovery loop with documented narrowing.",
    "needs_judgment": False,
    "items": [
        {
            "name": "Adaptive recovery loop",
            "draft_location": "ocr_scoring_qa.py::_recover_rejected",
            "reference_location": "abstract: 'adaptive recovery loop diagnoses rejected samples'",
            "status": "covered",
            "deviation_class": None,
            "rationale": "Directly implements the abstract's recover-vs-discard claim.",
        },
    ],
}


def test_render_coverage_matrix_surfaces_paper_anchor():
    out = run._render_coverage_matrix(_MATRIX_FIXTURE, audit_anchor="paper")
    assert "Audit anchor: paper abstract" in out
    assert "less precise" in out


def test_render_coverage_matrix_surfaces_reference_anchor():
    out = run._render_coverage_matrix(_MATRIX_FIXTURE, audit_anchor="reference")
    assert "Audit anchor: reference implementation" in out


def test_render_coverage_matrix_default_anchor_is_reference():
    # Legacy call sites pass no audit_anchor — they were always
    # reference-anchored, so the default preserves that behavior.
    out = run._render_coverage_matrix(_MATRIX_FIXTURE)
    assert "Audit anchor: reference implementation" in out
