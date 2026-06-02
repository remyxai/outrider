"""Tests for the deepresearch veto pre-flight hook (REMYX-78).

The engine endpoint is patched — we assert:
  - the mode env var controls invocation
  - missing API key falls through (None)
  - HTTP errors fall through (None) — never blocks publishing
  - successful responses are returned with the mode tagged
  - the action's process_target consumes a 'low' verdict in enforce mode
    as `skipped_by_deepresearch`

Run with: pytest tests/test_deepresearch_veto.py -q
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Recommendation, deepresearch_veto_check  # noqa: E402


def _make_rec(arxiv_id="2605.25734v1", score=1.0):
    return Recommendation(
        paper_title="Test paper",
        arxiv_id=arxiv_id,
        tier="high",
        z_score=0.0,
        spec_md="",
        paper_abstract="Test abstract",
        domain_summary="",
        raw_paper_md="",
        relevance_score=score,
        reasoning="x",
        suggested_experiment="y",
        recommendation_id="rec-1",
        interest_name="test",
    )


def test_veto_off_by_default(monkeypatch):
    monkeypatch.delenv("OUTRIDER_DEEPRESEARCH_VETO", raising=False)
    monkeypatch.setenv("REMYX_API_KEY", "sk-test")
    rec = _make_rec()
    # Should return None without ever attempting urllib
    with patch("urllib.request.urlopen", side_effect=AssertionError("must not call")):
        result = deepresearch_veto_check(rec, "https://github.com/o/r")
    assert result is None


def test_veto_skips_without_api_key(monkeypatch):
    monkeypatch.setenv("OUTRIDER_DEEPRESEARCH_VETO", "enforce")
    monkeypatch.delenv("REMYX_API_KEY", raising=False)
    monkeypatch.delenv("REMYXAI_API_KEY", raising=False)
    rec = _make_rec()
    with patch("urllib.request.urlopen", side_effect=AssertionError("must not call")):
        result = deepresearch_veto_check(rec, "https://github.com/o/r")
    assert result is None


def test_veto_skips_without_arxiv_id(monkeypatch):
    monkeypatch.setenv("OUTRIDER_DEEPRESEARCH_VETO", "enforce")
    monkeypatch.setenv("REMYX_API_KEY", "sk-test")
    rec = _make_rec(arxiv_id=None)
    with patch("urllib.request.urlopen", side_effect=AssertionError("must not call")):
        result = deepresearch_veto_check(rec, "https://github.com/o/r")
    assert result is None


def test_veto_returns_verdict_in_shadow_mode(monkeypatch):
    monkeypatch.setenv("OUTRIDER_DEEPRESEARCH_VETO", "shadow")
    monkeypatch.setenv("REMYX_API_KEY", "sk-test")
    rec = _make_rec()

    response_body = json.dumps({
        "confidence": "low",
        "rationale": "Different problem class.",
        "code_hits": [],
        "synth_excerpt": "",
        "elapsed_s": 42.0,
        "log_id": "log-1",
    }).encode("utf-8")
    fake_resp = MagicMock()
    fake_resp.read.return_value = response_body
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False

    with patch("urllib.request.urlopen", return_value=fake_resp):
        result = deepresearch_veto_check(rec, "https://github.com/o/r")
    assert result is not None
    assert result["confidence"] == "low"
    assert result["_mode"] == "shadow"


def test_veto_falls_through_on_http_error(monkeypatch):
    """A failed deepresearch check must never block publishing — fail-soft
    same as preflight_routing's convention."""
    import urllib.error
    monkeypatch.setenv("OUTRIDER_DEEPRESEARCH_VETO", "enforce")
    monkeypatch.setenv("REMYX_API_KEY", "sk-test")
    rec = _make_rec()
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            "u", 503, "Service Unavailable", {}, None
        ),
    ):
        result = deepresearch_veto_check(rec, "https://github.com/o/r")
    assert result is None


def test_step_summary_renders_deepresearch_assessment(monkeypatch, tmp_path):
    """When result carries deepresearch_* fields, _write_step_summary should
    render a 'Deepresearch assessment' block with confidence + rationale +
    excerpt."""
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    result = {
        "status": "issue_opened_preflight",
        "paper": "Test Paper",
        "arxiv": "2605.25734v1",
        "tier": "high",
        "issue_url": "https://github.com/o/r/issues/3",
        "deepresearch_confidence": "low",
        "deepresearch_rationale": "Different problem class — biomedical vs. PPL.",
        "deepresearch_synth_excerpt": "The paper applies Stein identities to a supervised encoder; the repo uses them for posterior inference.",
        "deepresearch_mode": "enforce",
        "cost_usd": 0.0,
    }
    run._write_step_summary(result)
    written = summary_file.read_text()
    assert "Deepresearch assessment" in written
    assert "`low`" in written
    assert "Different problem class" in written
    assert "Excerpt from the analysis" in written
    assert "supervised encoder" in written
    assert "mode: enforce" in written


def test_step_summary_omits_deepresearch_section_when_absent(monkeypatch, tmp_path):
    """When deepresearch didn't run (mode=off, default), the summary should
    not include the assessment block."""
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    result = {
        "status": "pr_opened_draft",
        "paper": "T",
        "arxiv": "2605.25734v1",
        "pr_url": "https://github.com/o/r/pull/4",
        "cost_usd": 0.0,
    }
    run._write_step_summary(result)
    written = summary_file.read_text()
    assert "Deepresearch assessment" not in written


def test_augment_selection_rejected_with_existing_entries():
    """LLM-enumerated rejections stay at the head; remaining viable
    candidates fill the tail with a neutral marker."""
    viable = [_make_rec(arxiv_id="A", score=0.99),  # selected
              _make_rec(arxiv_id="B", score=0.98),  # already enumerated
              _make_rec(arxiv_id="C", score=0.95),  # extra
              _make_rec(arxiv_id="D", score=0.91)]  # extra
    existing = [{"arxiv_id": "B", "title": "B-paper", "reason": "wrong layer"}]
    out = run._augment_selection_rejected(
        existing=existing, viable=viable, selected_arxiv="A"
    )
    assert [r["arxiv_id"] for r in out] == ["B", "C", "D"]
    assert out[0]["reason"] == "wrong layer"
    assert "not flagged by selection" in out[1]["reason"]
    assert "0.95" in out[1]["reason"]


def test_augment_selection_rejected_when_selection_unavailable():
    """No LLM-enumerated rejections (single-candidate pool / fallback
    path) → every other viable candidate becomes an extra."""
    viable = [_make_rec(arxiv_id="A", score=0.99),  # selected
              _make_rec(arxiv_id="B", score=0.96),
              _make_rec(arxiv_id="C", score=0.93)]
    out = run._augment_selection_rejected(
        existing=[], viable=viable, selected_arxiv="A"
    )
    assert [r["arxiv_id"] for r in out] == ["B", "C"]
    for entry in out:
        assert "not flagged by selection" in entry["reason"]


def test_augment_selection_rejected_caps_total():
    """Combined list (LLM-rejected + extras) is capped to keep the
    workflow step summary readable."""
    viable = [_make_rec(arxiv_id=str(i), score=0.99 - i * 0.01) for i in range(10)]
    out = run._augment_selection_rejected(
        existing=[], viable=viable, selected_arxiv="0", cap=5
    )
    assert len(out) == 5
    # Should be 1, 2, 3, 4, 5 — the next 5 after the selected "0"
    assert [r["arxiv_id"] for r in out] == ["1", "2", "3", "4", "5"]


def test_augment_selection_rejected_skips_selected_candidate():
    """The selected candidate must never appear in the alternatives list."""
    viable = [_make_rec(arxiv_id="A", score=0.99),
              _make_rec(arxiv_id="B", score=0.97)]
    out = run._augment_selection_rejected(
        existing=[], viable=viable, selected_arxiv="A"
    )
    assert all(r["arxiv_id"] != "A" for r in out)


def test_veto_sends_expected_payload(monkeypatch):
    """The endpoint receives arxiv_url + github_url + ranker_score +
    candidate_id + preflight_summary, with Bearer auth from REMYX_API_KEY."""
    monkeypatch.setenv("OUTRIDER_DEEPRESEARCH_VETO", "enforce")
    monkeypatch.setenv("REMYX_API_KEY", "sk-test-bearer")
    rec = _make_rec()

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        m = MagicMock()
        m.read.return_value = json.dumps({
            "confidence": "high",
            "rationale": "Clean fit.",
            "elapsed_s": 30.0,
        }).encode("utf-8")
        m.__enter__.return_value = m
        m.__exit__.return_value = False
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = deepresearch_veto_check(
            rec,
            "https://github.com/pyro-ppl/numpyro",
            candidate_id="cand-xyz",
            preflight_summary="hedged on call-site",
        )
    assert "/api/v1.0/deepresearch/paper-vs-repo" in captured["url"]
    # Header keys are normalized — case-insensitive lookup
    headers_lc = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers_lc.get("authorization") == "Bearer sk-test-bearer"
    assert captured["body"]["arxiv_url"] == "https://arxiv.org/abs/2605.25734v1"
    assert captured["body"]["github_url"] == "https://github.com/pyro-ppl/numpyro"
    assert captured["body"]["ranker_score"] == 1.0
    assert captured["body"]["candidate_id"] == "cand-xyz"
    assert captured["body"]["preflight_summary"] == "hedged on call-site"
    assert result["confidence"] == "high"
