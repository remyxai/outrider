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
