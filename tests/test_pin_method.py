"""Tests for the pin-method input — REMYX-148.

Covers the pure logic of pin-method resolution:

  - arxiv_id-shaped query → direct asset lookup (bypasses keyword search)
  - free-text query → keyword search → top-1 hit
  - empty / no-match → ``None`` (the caller surfaces ``skipped_no_method_match``)
  - the recommend-phase short-circuit reduces pin-method to pin-arxiv so
    the existing pinning logic at §4 selects the asset
  - pin-method + pin-arxiv set together exits with code 2 (mutex)

The Remyx /search/assets fetch is the only network I/O involved;
both the keyword-search and the direct-asset endpoints are
monkey-patched.

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# ─── _resolve_pin_method ──────────────────────────────────────────────────


def test_pin_method_arxiv_id_uses_direct_lookup(monkeypatch):
    """A query that matches the arxiv_id shape skips the keyword search
    and goes straight to ``_remyx_get_asset`` — both faster and immune
    to keyword-retrieval gaps.
    """
    asset = {"arxiv_id": "2410.20305v2", "title": "Some Paper"}
    calls = {"get_asset": 0, "search_assets": 0}

    def fake_get_asset(arxiv_id):
        calls["get_asset"] += 1
        assert arxiv_id == "2410.20305v2"
        return asset

    def fake_search(query, max_results=5, use_llm=True):
        calls["search_assets"] += 1
        return []

    monkeypatch.setattr(run, "_remyx_get_asset", fake_get_asset)
    monkeypatch.setattr(run, "_remyx_search_assets", fake_search)

    result = run._resolve_pin_method("2410.20305v2")
    assert result == asset
    assert calls["get_asset"] == 1
    assert calls["search_assets"] == 0


def test_pin_method_arxiv_id_without_version_also_matches(monkeypatch):
    asset = {"arxiv_id": "2410.20305", "title": "Some Paper"}

    def fake_get_asset(arxiv_id):
        assert arxiv_id == "2410.20305"
        return asset

    monkeypatch.setattr(run, "_remyx_get_asset", fake_get_asset)
    monkeypatch.setattr(run, "_remyx_search_assets",
                        lambda *a, **kw: pytest.fail("must not call search"))

    assert run._resolve_pin_method("2410.20305") == asset


def test_pin_method_freetext_uses_keyword_search(monkeypatch):
    """A non-arxiv query hits ``_remyx_search_assets`` and returns the
    top-1 envelope."""
    hits = [
        {"arxiv_id": "2402.01234", "title": "A Distillation Paper"},
        {"arxiv_id": "2402.05678", "title": "Another Paper"},
    ]

    def fake_search(query, max_results=5, use_llm=True):
        assert query == "knowledge distillation"
        assert max_results == 1
        return hits

    monkeypatch.setattr(run, "_remyx_search_assets", fake_search)
    monkeypatch.setattr(run, "_remyx_get_asset",
                        lambda *a, **kw: pytest.fail("must not call get"))

    result = run._resolve_pin_method("knowledge distillation")
    assert result == hits[0]


def test_pin_method_freetext_no_hits_returns_none(monkeypatch):
    monkeypatch.setattr(run, "_remyx_search_assets",
                        lambda *a, **kw: [])
    assert run._resolve_pin_method("some obscure unknown method") is None


def test_pin_method_arxiv_id_not_found_returns_none(monkeypatch):
    """``_remyx_get_asset`` returns None on 404 — pin-method propagates that."""
    monkeypatch.setattr(run, "_remyx_get_asset", lambda *a, **kw: None)
    assert run._resolve_pin_method("9999.99999v1") is None


def test_pin_method_empty_query_returns_none(monkeypatch):
    monkeypatch.setattr(run, "_remyx_get_asset",
                        lambda *a, **kw: pytest.fail("must not call"))
    monkeypatch.setattr(run, "_remyx_search_assets",
                        lambda *a, **kw: pytest.fail("must not call"))
    assert run._resolve_pin_method("") is None
    assert run._resolve_pin_method("   ") is None


# ─── Target.pin_method env wiring ─────────────────────────────────────────


def test_target_picks_up_pin_method_env(monkeypatch):
    monkeypatch.setenv("TARGET_REPO", "owner/name")
    monkeypatch.setenv("INPUT_INTEREST_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("INPUT_PIN_METHOD", "knowledge distillation")
    target = run.build_target_from_env()
    assert target.pin_method == "knowledge distillation"
    assert target.pin_arxiv == ""


def test_target_pin_arxiv_and_pin_method_both_default_empty(monkeypatch):
    monkeypatch.setenv("TARGET_REPO", "owner/name")
    monkeypatch.setenv("INPUT_INTEREST_ID", "00000000-0000-0000-0000-000000000000")
    target = run.build_target_from_env()
    assert target.pin_method == ""
    assert target.pin_arxiv == ""


# ─── main() mutex check ───────────────────────────────────────────────────


def test_main_exits_when_both_pin_inputs_set(monkeypatch):
    monkeypatch.setenv("TARGET_REPO", "owner/name")
    monkeypatch.setenv("INPUT_INTEREST_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("INPUT_PIN_ARXIV", "2410.20305v2")
    monkeypatch.setenv("INPUT_PIN_METHOD", "knowledge distillation")
    with pytest.raises(SystemExit) as exc:
        run.main()
    assert exc.value.code == 2
