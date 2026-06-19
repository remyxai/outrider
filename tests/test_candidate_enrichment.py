"""Tests for _candidate_enrichment — the per-candidate code/model/license
metadata included in the run-telemetry payload.

Run with: pytest tests/test_candidate_enrichment.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Recommendation  # noqa: E402


def _rec(arxiv_id, *, github="", hf="", license_="", source="", lclass="unknown"):
    return Recommendation(
        paper_title=f"Paper {arxiv_id}",
        arxiv_id=arxiv_id,
        tier="high",
        z_score=0.0,
        spec_md="",
        paper_abstract="",
        domain_summary="",
        raw_paper_md="",
        relevance_score=0.9,
        paper_github_url=github,
        paper_huggingface_url=hf,
        paper_license=license_,
        license_source=source,
        license_class=lclass,
        license_compat=0.42,
    )


def test_maps_fields_and_omits_compat():
    out = run._candidate_enrichment([
        _rec("2606.1", github="https://github.com/a/b", license_="MIT",
             source="github", lclass="permissive"),
    ])
    assert out == [{
        "arxiv_id": "2606.1",
        "github": "https://github.com/a/b",
        "huggingface": "",
        "paper_license": "MIT",
        "license_source": "github",
        "license_class": "permissive",
    }]
    # license_compat is target-relative, not a property of the paper.
    assert "license_compat" not in out[0]


def test_excludes_candidates_without_code_or_license():
    out = run._candidate_enrichment([
        _rec("2606.2", lclass="no-code-link"),             # nothing resolved
        _rec("2606.3", hf="https://huggingface.co/o/m"),   # has a model URL
    ])
    assert [c["arxiv_id"] for c in out] == ["2606.3"]


def test_skips_blank_arxiv_id():
    out = run._candidate_enrichment([_rec("", github="https://github.com/a/b")])
    assert out == []
