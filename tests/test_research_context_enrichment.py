"""Tests for the HF Hub + sibling-library research-phase context enrichment.

Both signals are pre-fetched deterministically in Python and interpolated
into the research prompt (rather than surfacing as Claude tool calls). The
tests verify the render shape stays stable across signal-present /
signal-absent / failure branches, and that the pre-fetches happen inside
``write_research_invocation`` — not lazily.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


def _rec(**overrides):
    defaults = dict(
        paper_title="Example Paper on Widget Structures",
        arxiv_id="2412.99999v1",
        tier="high",
        z_score=0.0,
        spec_md="",
        paper_abstract="",
        domain_summary="",
        raw_paper_md="",
    )
    defaults.update(overrides)
    return run.Recommendation(**defaults)


def _target():
    return run.Target(
        repo="example-org/example-repo",
        interest_id="00000000-0000-0000-0000-000000000000",
    )


# ── HF linkage rendering ──────────────────────────────────────────────────


def test_hf_linkage_block_renders_indexed_paper_with_models(monkeypatch):
    """Paper indexed on HF with linked models — block lists top model IDs
    and hints that the model card carries the canonical call-site pattern."""
    fake_linkage = {
        "title": "Example Paper",
        "linked_models": [
            {"id": "microsoft/example-model", "downloads": 2300},
            {"id": "example-user/example-fork"},
            {"id": "another-user/another-fork"},
            {"id": "fourth/fork"},  # capped at top-3 in output
        ],
        "linked_datasets": [],
        "linked_spaces": [],
    }
    with patch.object(run, "_fetch_hf_paper_linkage", return_value=fake_linkage):
        block = run._render_hf_linkage_block("2412.99999v1")
    assert "paper indexed" in block
    assert "microsoft/example-model" in block
    assert "example-user/example-fork" in block
    assert "another-user/another-fork" in block
    assert "fourth/fork" not in block  # capped
    assert "call-site pattern" in block


def test_hf_linkage_block_renders_indexed_paper_with_datasets(monkeypatch):
    """Datasets rendered alongside models when both are populated."""
    fake_linkage = {
        "title": "Example Paper",
        "linked_models": [],
        "linked_datasets": [{"id": "example-org/dataset-x"}],
        "linked_spaces": [],
    }
    with patch.object(run, "_fetch_hf_paper_linkage", return_value=fake_linkage):
        block = run._render_hf_linkage_block("2412.99999v1")
    assert "example-org/dataset-x" in block
    assert "Linked datasets" in block
    assert "Linked models" in block  # even when empty, block-shape stable


def test_hf_linkage_block_renders_no_signal_when_paper_not_indexed(monkeypatch):
    """Paper not on HF Hub — block emits stable "no signal" text so
    downstream readers can tell "looked up, empty" from "not looked up"."""
    with patch.object(run, "_fetch_hf_paper_linkage", return_value=None):
        block = run._render_hf_linkage_block("2412.99999v1")
    assert "not indexed" in block
    assert "no signal" in block


# ── Sibling implementations rendering ─────────────────────────────────────


def test_sibling_impls_block_renders_top_hits(monkeypatch):
    """Well-known-org hits render as a bulleted list with stars + description
    + coordination-signal framing."""
    fake_hits = [
        {"full_name": "huggingface/peft", "stars": 15234,
         "description": "State-of-the-art Parameter-Efficient Fine-Tuning.",
         "why_relevant": "huggingface is a well-known ML-library org"},
        {"full_name": "EleutherAI/lm-eval-harness", "stars": 8102,
         "description": "A framework for few-shot evaluation.",
         "why_relevant": "EleutherAI is a well-known ML-library org"},
    ]
    with patch.object(run, "_fetch_sibling_implementations", return_value=fake_hits):
        block = run._render_sibling_impls_block(
            "2412.99999v1", "Example Paper", "smellslikeml/target",
        )
    assert "huggingface/peft" in block
    assert "15234" in block
    assert "EleutherAI/lm-eval-harness" in block
    assert "coordination signals" in block


def test_sibling_impls_block_renders_no_signal_when_no_hits(monkeypatch):
    """Empty hit list — block emits stable "none found" framing that
    suggests a fallback (search-method probe)."""
    with patch.object(run, "_fetch_sibling_implementations", return_value=[]):
        block = run._render_sibling_impls_block(
            "2412.99999v1", "Example Paper", "smellslikeml/target",
        )
    assert "none found" in block.lower()
    assert "search-method-shaped probe" in block


# ── Sibling-impl fetch — org filter + target-repo exclusion ──────────────


class _FakeUrlOpen:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_fetch_sibling_impls_filters_to_wellknown_orgs(monkeypatch):
    """The search returns many repos; only those owned by well-known ML-library
    orgs pass the filter. Unknown-org repos + fork-noise are excluded."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    run._SIBLING_IMPL_CACHE.clear()
    payload = {
        "items": [
            {"full_name": "huggingface/peft", "stargazers_count": 15234,
             "description": "PEFT lib"},
            {"full_name": "randomuser/some-fork", "stargazers_count": 3,
             "description": "just a fork"},  # excluded: not a wellknown org
            {"full_name": "EleutherAI/lm-eval-harness", "stargazers_count": 8102,
             "description": "eval harness"},
            {"full_name": "another-random/experiment", "stargazers_count": 12,
             "description": "unrelated"},  # excluded
        ]
    }
    with patch("run.urllib.request.urlopen", return_value=_FakeUrlOpen(payload)):
        hits = run._fetch_sibling_implementations(
            "2412.99999v1", "Example Paper", "smellslikeml/target",
        )
    names = [h["full_name"] for h in hits]
    assert "huggingface/peft" in names
    assert "EleutherAI/lm-eval-harness" in names
    assert "randomuser/some-fork" not in names
    assert "another-random/experiment" not in names


def test_fetch_sibling_impls_excludes_target_repo(monkeypatch):
    """The target repo itself, if it matches a wellknown-org filter, must be
    excluded from its own sibling list — avoids self-referencing."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    run._SIBLING_IMPL_CACHE.clear()
    payload = {
        "items": [
            {"full_name": "huggingface/peft", "stargazers_count": 15234,
             "description": "PEFT lib"},
        ]
    }
    with patch("run.urllib.request.urlopen", return_value=_FakeUrlOpen(payload)):
        # Target repo IS huggingface/peft — should be excluded
        hits = run._fetch_sibling_implementations(
            "2412.99999v1", "Example Paper", "huggingface/peft",
        )
    assert not hits  # target self-excluded, no other wellknown-org hits


def test_fetch_sibling_impls_returns_empty_on_network_failure(monkeypatch):
    """Any network failure → empty list, cached negative so we don't retry."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    run._SIBLING_IMPL_CACHE.clear()
    with patch("run.urllib.request.urlopen",
               side_effect=OSError("network unreachable")):
        hits = run._fetch_sibling_implementations(
            "2412.99999v1", "Example Paper", "smellslikeml/target",
        )
    assert hits == []


def test_fetch_sibling_impls_caches_result(monkeypatch):
    """Second call for same (arxiv_id, target_repo) hits cache, not network."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    run._SIBLING_IMPL_CACHE.clear()
    payload = {
        "items": [
            {"full_name": "huggingface/peft", "stargazers_count": 15234,
             "description": "PEFT lib"},
        ]
    }
    calls = {"n": 0}

    def counted_urlopen(*args, **kwargs):
        calls["n"] += 1
        return _FakeUrlOpen(payload)

    with patch("run.urllib.request.urlopen", side_effect=counted_urlopen):
        h1 = run._fetch_sibling_implementations(
            "2412.99999v1", "Example Paper", "smellslikeml/target",
        )
        h2 = run._fetch_sibling_implementations(
            "2412.99999v1", "Example Paper", "smellslikeml/target",
        )
    assert h1 == h2
    assert calls["n"] == 1  # network hit only once


# ── Integration: write_research_invocation interpolates both blocks ──────


def test_write_research_invocation_interpolates_hf_and_sibling_blocks(tmp_path):
    """The two enrichment blocks appear in the rendered RESEARCH_INVOCATION.md
    when write_research_invocation runs — pre-fetched, not lazy."""
    with patch.object(run, "_fetch_hf_paper_linkage", return_value={
        "title": "Example",
        "linked_models": [{"id": "org/example-checkpoint"}],
        "linked_datasets": [],
        "linked_spaces": [],
    }), patch.object(run, "_fetch_sibling_implementations", return_value=[
        {"full_name": "huggingface/example-lib", "stars": 100,
         "description": "adjacent impl", "why_relevant": "wellknown org"},
    ]):
        run.write_research_invocation(tmp_path, _rec(), _target())
    content = (tmp_path / ".remyx-recommendation" / "RESEARCH_INVOCATION.md").read_text()
    assert "org/example-checkpoint" in content  # HF block interpolated
    assert "huggingface/example-lib" in content  # sibling block interpolated
    assert "Pre-fetched signals" in content
