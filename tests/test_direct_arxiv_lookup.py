"""Tests for the v1.4.6 direct arxiv-id resolution path:

  - `_remyx_get_asset(arxiv_id)` returns the asset dict on success
  - Returns None on 404 / network failure / empty input
  - Never raises (even when the engine flakes)
  - Selection prompt template now includes the direct-lookup guidance
  - `Tools available` list mentions `remyxai search info <arxiv_id>`

The point of this feature: when a maintainer thread names a specific
arxiv id, the keyword search endpoint may miss the asset (the
InstructSAM 2605.26102 case), but direct id lookup retrieves it from
the catalog regardless of keyword-search retrieval quality.

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# ─── _remyx_get_asset ────────────────────────────────────────────────────


def test_get_asset_returns_dict_on_success(monkeypatch):
    """Direct lookup returns the asset envelope as-is when the engine
    has the asset (the InstructSAM-shaped case).
    """
    asset = {
        "arxiv_id": "2605.26102",
        "title": "InstructSAM: Segment Any Instance with Any Instructions",
        "abstract": "In this paper, we introduce InstructSAM ...",
        "categories": ["cs.CV"],
    }

    def fake_get(path, params=None):
        assert path == "/api/v1.0/search/assets/2605.26102"
        return asset

    monkeypatch.setattr(run, "_remyx_get", fake_get)

    out = run._remyx_get_asset("2605.26102")
    assert out is not None
    assert out["arxiv_id"] == "2605.26102"
    assert "InstructSAM" in out["title"]


def test_get_asset_returns_none_on_404(monkeypatch):
    """Engine returns 404 → helper returns None silently. Must not
    raise — broadening search has to keep flowing."""
    def fake_get(path, params=None):
        raise RuntimeError(
            "Remyx API GET /api/v1.0/search/assets/9999.99999 → HTTP 404"
        )

    monkeypatch.setattr(run, "_remyx_get", fake_get)
    assert run._remyx_get_asset("9999.99999") is None


def test_get_asset_returns_none_on_network_flake(monkeypatch):
    """Any unexpected exception from the engine collapses to None."""
    def fake_get(path, params=None):
        raise ConnectionError("simulated socket reset")

    monkeypatch.setattr(run, "_remyx_get", fake_get)
    assert run._remyx_get_asset("2605.26102") is None


def test_get_asset_returns_none_on_empty_or_whitespace_id():
    """Defensive — never even attempt the network call for an empty id."""
    assert run._remyx_get_asset("") is None
    assert run._remyx_get_asset("   ") is None
    assert run._remyx_get_asset(None) is None


def test_get_asset_returns_none_when_envelope_lacks_identity(monkeypatch):
    """Tolerate the response shape changing — a dict with no arxiv_id or
    title is not a recognizable asset; treat it as a miss rather than
    pretending we found something."""
    def fake_get(path, params=None):
        return {"some_other_field": "value"}

    monkeypatch.setattr(run, "_remyx_get", fake_get)
    assert run._remyx_get_asset("2605.26102") is None


def test_get_asset_accepts_title_only_envelope(monkeypatch):
    """The CLI's `search info` envelope sometimes returns title without
    a duplicated arxiv_id field — treat title presence as enough to
    identify the response as a real asset."""
    def fake_get(path, params=None):
        return {"title": "Some Paper", "abstract": "..."}

    monkeypatch.setattr(run, "_remyx_get", fake_get)
    out = run._remyx_get_asset("2605.26102")
    assert out is not None
    assert out["title"] == "Some Paper"


# ─── Selection prompt instruction ────────────────────────────────────────


def test_selection_prompt_documents_direct_id_lookup():
    """The selection prompt must instruct the model to try direct
    arxiv-id resolution before keyword search when a paper is named
    by id. Without this, the model defaults to keyword broadening and
    hits the InstructSAM-shaped retrieval gap."""
    prompt = run._SELECTION_PROMPT_TEMPLATE
    assert "remyxai search info" in prompt
    assert "<arxiv_id>" in prompt
    # The guidance must point out the FIRST-attempt position when an id
    # is named, not bury it as a later fallback.
    assert "use this\n    FIRST" in prompt or "FIRST when a maintainer" in prompt
    # The explanation needs to reference the keyword-retrieval gap so
    # the model can reason about when to use which tool.
    assert (
        "keyword search endpoint occasionally misses" in prompt
        or "tokenize cleanly" in prompt
    )


def test_selection_prompt_tools_list_includes_search_info():
    """The Tools-available list must explicitly expose `search info`
    so the agentic flow knows the subcommand is allowed."""
    prompt = run._SELECTION_PROMPT_TEMPLATE
    tools_section = prompt.split("Tools available:")[-1]
    assert "remyxai search info" in tools_section


def test_selection_prompt_preserves_existing_keyword_search_path():
    """Direct lookup is *additive* — the keyword `search query` path
    must still be documented for cases when no arxiv id is named.
    Regression-guards against the new guidance accidentally displacing
    the old."""
    prompt = run._SELECTION_PROMPT_TEMPLATE
    assert "remyxai search query" in prompt
    assert "keyword" in prompt.lower()
