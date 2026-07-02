"""Tests for the HF Hub checkpoint-availability preflight.

The preflight uses HF Hub's canonical arxiv-paper linkage API
(``GET /api/papers/{arxiv_id}``) rather than heuristic author/name
matching, so we have three signals to test:
- paper indexed + linked models present → 🟢 list them
- paper indexed + no linked models      → 🟡 declare absence
- paper NOT indexed on HF               → no signal, emit nothing
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# --- _is_architecture_add_shape ---------------------------------------------

def test_arch_shape_matches_new_transformer_model():
    body = (
        "A faithful, registered DiG would mean a new DiGTransformer2DModel "
        "mirroring DiTTransformer2DModel."
    )
    assert run._is_architecture_add_shape(body) is True


def test_arch_shape_matches_attention_processor():
    body = "A new GLA attention processor alongside SanaLinearAttnProcessor2_0."
    assert run._is_architecture_add_shape(body) is True


def test_arch_shape_matches_new_tuner():
    body = "Add a new PEFT tuner implementing AdaMoLE."
    assert run._is_architecture_add_shape(body) is True


def test_arch_shape_matches_model_class_no_weights():
    body = "A model class with no weights and no pipeline that instantiates it."
    assert run._is_architecture_add_shape(body) is True


def test_arch_shape_matches_hf_diffusers_template():
    body = "**Model/Pipeline/Scheduler description**\n\nDiG (Diffusion..."
    assert run._is_architecture_add_shape(body) is True


def test_arch_shape_rejects_pure_algorithm_improvement():
    body = (
        "Add GD²PO advantage estimator behind --algo.advantage.estimator "
        "gd2po. Extends the existing dispatch."
    )
    assert run._is_architecture_add_shape(body) is False


def test_arch_shape_rejects_dataset_or_benchmark_add():
    body = "Add a new benchmark that evaluates existing models on X."
    assert run._is_architecture_add_shape(body) is False


def test_arch_shape_rejects_empty_body():
    assert run._is_architecture_add_shape("") is False


# --- _fetch_hf_paper_linkage ------------------------------------------------

def _fake_resp(payload):
    class Resp:
        def __init__(self, data): self._data = data
        def read(self): return json.dumps(self._data).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass
    return Resp(payload)


def test_paper_linkage_indexed_with_linked_models():
    run._HF_PAPER_CACHE.clear()
    payload = {
        "title": "SomePaper: Doing Something",
        "linkedModels": [
            {"id": "org/somepaper-checkpoint", "downloads": 42},
            {"id": "org/somepaper-large"},
        ],
        "linkedDatasets": [],
        "linkedSpaces": [],
    }
    with patch.object(run.urllib.request, "urlopen", return_value=_fake_resp(payload)):
        got = run._fetch_hf_paper_linkage("2405.18428")
    assert got is not None
    assert got["title"] == "SomePaper: Doing Something"
    assert len(got["linked_models"]) == 2


def test_paper_linkage_indexed_but_no_linked_models():
    """This is the DiG case — paper indexed on HF, but no checkpoint linked."""
    run._HF_PAPER_CACHE.clear()
    payload = {
        "title": "DiG: Scalable and Efficient Diffusion Models with Gated Linear Attention",
        "linkedModels": [],
        "linkedDatasets": [],
        "linkedSpaces": [],
    }
    with patch.object(run.urllib.request, "urlopen", return_value=_fake_resp(payload)):
        got = run._fetch_hf_paper_linkage("2405.18428")
    assert got is not None
    assert got["title"].startswith("DiG")
    assert got["linked_models"] == []


def test_paper_linkage_not_indexed_returns_none():
    """HF returns title=null when the paper isn't on huggingface.co/papers."""
    run._HF_PAPER_CACHE.clear()
    payload = {"title": None, "linkedModels": [], "linkedDatasets": []}
    with patch.object(run.urllib.request, "urlopen", return_value=_fake_resp(payload)):
        got = run._fetch_hf_paper_linkage("2606.99999")
    assert got is None


def test_paper_linkage_network_failure_returns_none():
    run._HF_PAPER_CACHE.clear()
    with patch.object(run.urllib.request, "urlopen", side_effect=OSError("dns")):
        assert run._fetch_hf_paper_linkage("2405.18428") is None


def test_paper_linkage_caches_per_arxiv_id():
    run._HF_PAPER_CACHE.clear()
    payload = {"title": "X", "linkedModels": []}
    calls = [0]
    def counting(*a, **kw):
        calls[0] += 1
        return _fake_resp(payload)
    with patch.object(run.urllib.request, "urlopen", side_effect=counting):
        run._fetch_hf_paper_linkage("2405.18428")
        run._fetch_hf_paper_linkage("2405.18428")
        run._fetch_hf_paper_linkage("2405.18428v2")  # version stripped in cache key
    assert calls[0] == 1


def test_paper_linkage_empty_arxiv_id_returns_none():
    assert run._fetch_hf_paper_linkage("") is None


# --- _format_hf_checkpoint_section ------------------------------------------

def test_format_section_omits_entirely_when_linkage_is_none():
    """No-signal case: paper isn't indexed on HF, so we say nothing.
    Emitting a false 'not found' would be worse than staying silent."""
    assert run._format_hf_checkpoint_section(None) == ""


def test_format_section_lists_linked_models_when_present():
    linkage = {
        "title": "X",
        "linked_models": [
            {"id": "hustvl/DiG-XL-2-256"},
            {"id": "hustvl/DiG-S-2"},
        ],
    }
    out = run._format_hf_checkpoint_section(linkage)
    assert "🟢" in out
    assert "hustvl/DiG-XL-2-256" in out
    assert "hustvl/DiG-S-2" in out
    assert "huggingface.co/hustvl/DiG-XL-2-256" in out
    assert "2 public checkpoint" in out


def test_format_section_declares_absence_when_paper_indexed_no_models():
    linkage = {"title": "DiG: ...", "linked_models": []}
    out = run._format_hf_checkpoint_section(linkage)
    assert "🟡" in out
    assert "indexed on huggingface.co/papers" in out
    assert "no linked" in out


def test_format_section_skips_models_without_id():
    linkage = {
        "title": "X",
        "linked_models": [{"id": "org/valid"}, {"no_id": True}, "not_a_dict"],
    }
    out = run._format_hf_checkpoint_section(linkage)
    assert "org/valid" in out
    assert "no_id" not in out
