"""Tests for pool-composition + license-distribution telemetry:

  - `_pool_composition` counts broad vs refine candidates via the
    per-candidate `refine_query` provenance marker
  - `_asset_to_recommendation` stamps that marker on refine candidates
  - `_license_class_counts` tallies the pool's license classes
  - `_format_license_class_counts` renders one line in canonical class
    order, appends unexpected classes, omits zero counts
  - Step summary renders the "Candidate pool" + "License gate (pool)"
    lines when the result fields are populated and degrades silently
    when they're absent

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Recommendation  # noqa: E402


def _rec(**kw):
    base = dict(
        paper_title="Sample Paper", arxiv_id="2601.00001", tier="high",
        z_score=0.0, spec_md="", paper_abstract="", domain_summary="",
        raw_paper_md="",
    )
    base.update(kw)
    return Recommendation(**base)


def _capture(result, tmp_path, monkeypatch) -> str:
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    run._write_step_summary(result)
    return summary_file.read_text()


# ─── _pool_composition ────────────────────────────────────────────────────


def test_pool_composition_counts_by_refine_marker():
    pool = [
        _rec(arxiv_id="2601.00001"),
        _rec(arxiv_id="2601.00002"),
        _rec(arxiv_id="2601.00003", refine_query="depth estimation successor"),
    ]
    assert run._pool_composition(pool) == (2, 1)


def test_pool_composition_empty_pool():
    assert run._pool_composition([]) == (0, 0)


def test_asset_to_recommendation_stamps_refine_query():
    rec = run._asset_to_recommendation(
        {"arxiv_id": "2602.11111", "title": "T", "abstract": "A"},
        refine_query="single pass joint depth intrinsics",
        fallback_interest_name="i", interest_context="", experiment_history="",
    )
    assert rec.refine_query == "single pass joint depth intrinsics"


def test_broad_candidates_default_to_empty_refine_query():
    assert _rec().refine_query == ""


# ─── _license_class_counts / _format_license_class_counts ────────────────


def test_license_class_counts_tallies_pool():
    pool = [
        _rec(license_class="permissive"),
        _rec(license_class="permissive"),
        _rec(license_class="nc"),
        _rec(),  # default "unknown"
    ]
    assert run._license_class_counts(pool) == {
        "permissive": 2, "nc": 1, "unknown": 1,
    }


def test_format_counts_canonical_order():
    line = run._format_license_class_counts(
        {"missing": 30, "permissive": 4, "nc": 1}
    )
    # permissive before nc before missing, regardless of dict order.
    assert line == "permissive: 4 · nc: 1 · missing: 30"


def test_format_counts_appends_unexpected_class():
    line = run._format_license_class_counts({"permissive": 1, "weird": 2})
    assert line.startswith("permissive: 1")
    assert "weird: 2" in line


def test_format_counts_includes_no_code_link():
    # `no-code-link` dominated real pools 
    line = run._format_license_class_counts({"no-code-link": 7})
    assert line == "no-code-link: 7"


def test_format_counts_empty():
    assert run._format_license_class_counts({}) == "(no candidates)"


# ─── Step summary rendering ───────────────────────────────────────────────


def test_summary_renders_pool_composition_line(tmp_path, monkeypatch):
    out = _capture({
        "status": "skipped_by_selection_verification",
        "broad_pool_size": 25,
        "refine_pool_size": 11,
    }, tmp_path, monkeypatch)
    assert (
        "**Candidate pool**: 25 broad + 11 refine candidate(s) "
        "considered (after dedup)"
    ) in out


def test_summary_omits_pool_line_when_fields_absent(tmp_path, monkeypatch):
    out = _capture({"status": "pr_opened_draft"}, tmp_path, monkeypatch)
    assert "Candidate pool" not in out


def test_summary_omits_pool_line_when_pool_empty(tmp_path, monkeypatch):
    out = _capture({
        "status": "error", "broad_pool_size": 0, "refine_pool_size": 0,
    }, tmp_path, monkeypatch)
    assert "Candidate pool" not in out


def test_summary_renders_license_distribution_line(tmp_path, monkeypatch):
    out = _capture({
        "status": "pr_opened_draft",
        "license_class_counts": {"permissive": 4, "missing": 30, "nc": 1},
    }, tmp_path, monkeypatch)
    assert (
        "**License gate (pool)**: permissive: 4 · nc: 1 · missing: 30"
    ) in out


def test_summary_omits_license_distribution_when_absent(tmp_path, monkeypatch):
    out = _capture({"status": "pr_opened_draft"}, tmp_path, monkeypatch)
    assert "License gate (pool)" not in out


def test_summary_pool_lines_render_above_cost(tmp_path, monkeypatch):
    out = _capture({
        "status": "skipped_by_selection_verification",
        "broad_pool_size": 3,
        "refine_pool_size": 1,
        "license_class_counts": {"permissive": 4},
        "cost_usd": 1.0,
    }, tmp_path, monkeypatch)
    assert out.index("Candidate pool") < out.index("Cost & tokens")
    assert out.index("License gate (pool)") < out.index("Cost & tokens")
