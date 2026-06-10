"""Tests for engineering-vs-license axis split.

An A++ engineering analysis fused with a wrong license flag meant a reader 
could miss either verdict. The fix renders the two as adjacent, independent 
sections in both the downgrade-Issue body and the step summary:

  - `_render_engineering_section` renders shape / contract / migration
    (or the extension-shape fields) and returns "" with no signal
  - `_open_downgrade_issue` places the engineering section immediately
    above the license section
  - `_record_verdict_fields` threads the chosen candidate's license axis
    onto the result, skipping when enrichment never ran
  - Step summary renders "Engineering verdict" + "License verdict"
    adjacently, each degrading silently when its fields are absent

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Recommendation, Target  # noqa: E402


def _rec(**kw):
    base = dict(
        paper_title="Sample Paper", arxiv_id="2601.00001", tier="high",
        z_score=0.0, spec_md="", paper_abstract="abstract",
        domain_summary="", raw_paper_md="",
        relevance_score=0.92,
        reasoning="paper anchors on the depth stage",
        suggested_experiment="swap the backbone",
        interest_name="ExampleInterest",
    )
    base.update(kw)
    return Recommendation(**base)


def _capture_issue(monkeypatch):
    captured: dict = {}

    def fake_open_issue(target, title, body, **kw):
        captured["title"] = title
        captured["body"] = body
        captured["kwargs"] = kw
        return "https://github.com/example/repo/issues/999"

    monkeypatch.setattr(run, "open_issue", fake_open_issue)
    return captured


def _capture_summary(result, tmp_path, monkeypatch) -> str:
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    run._write_step_summary(result)
    return summary_file.read_text()


# ─── _render_engineering_section ──────────────────────────────────────────


def test_engineering_section_renders_contract_fields():
    out = run._render_engineering_section(
        integration_shape="drop-in replacement",
        contract_match="run(PIL) -> (depth, focal_px) preserved",
        migration_cost="depth.py + requirements.txt",
    )
    assert out.startswith("## Engineering verdict")
    assert "- **Integration shape**: drop-in replacement" in out
    assert "- **Contract match**: run(PIL) -> (depth, focal_px) preserved" in out
    assert "- **Migration cost**: depth.py + requirements.txt" in out


def test_engineering_section_renders_extension_fields():
    out = run._render_engineering_section(
        integration_shape="out-of-pool extension (new capability)",
        team_direction_signal="RFC #84 names this capability",
        proposed_call_site="pipeline/localize.py",
    )
    assert "- **Team-direction signal**: RFC #84 names this capability" in out
    assert "- **Proposed call site**: pipeline/localize.py" in out
    assert "Contract match" not in out


def test_engineering_section_empty_without_signal():
    assert run._render_engineering_section() == ""
    assert run._render_engineering_section(
        integration_shape="  ", contract_match="",
    ) == ""


# ─── _open_downgrade_issue placement ──────────────────────────────────────


def test_downgrade_body_engineering_adjacent_above_license(monkeypatch):
    captured = _capture_issue(monkeypatch)
    run._open_downgrade_issue(
        Target(repo="example/repo", interest_id="iid"),
        _rec(paper_license="CC-BY-NC-4.0", license_class="nc",
             license_compat=0.10,
             paper_github_url="https://github.com/x/y"),
        reason="r", detail="d",
        engineering_section=run._render_engineering_section(
            integration_shape="drop-in replacement",
            contract_match="contract preserved",
            migration_cost="two files",
        ),
    )
    body = captured["body"]
    eng_idx = body.index("## Engineering verdict")
    lic_idx = body.index("## License & code availability")
    assert eng_idx < lic_idx
    # Adjacent: no other section heading between the two verdicts.
    between = body[eng_idx:lic_idx]
    assert between.count("\n## ") == 0


def test_downgrade_body_omits_engineering_when_empty(monkeypatch):
    captured = _capture_issue(monkeypatch)
    run._open_downgrade_issue(
        Target(repo="example/repo", interest_id="iid"), _rec(),
        reason="r", detail="d",
    )
    assert "## Engineering verdict" not in captured["body"]


# ─── _record_verdict_fields ───────────────────────────────────────────────


def test_record_verdict_fields_threads_license_axis():
    result: dict = {}
    run._record_verdict_fields(result, _rec(
        paper_license="CC-BY-NC-4.0", license_class="nc",
        license_compat=0.10,
        paper_github_url="https://github.com/x/y",
    ))
    assert result == {
        "license_class": "nc",
        "license_compat": 0.10,
        "paper_license": "CC-BY-NC-4.0",
    }


def test_record_verdict_fields_skips_when_enrichment_never_ran():
    result: dict = {}
    run._record_verdict_fields(result, _rec())  # all defaults
    assert result == {}


# ─── Step summary: adjacent verdict blocks ────────────────────────────────


def test_summary_renders_both_verdicts_adjacent(tmp_path, monkeypatch):
    out = _capture_summary({
        "status": "issue_opened_substitution",
        "paper": "Sample Paper",
        "arxiv": "2601.00001",
        "selection_integration_shape": "replacement",
        "selection_contract_match": "contract preserved",
        "selection_migration_cost": "two files",
        "license_class": "nc",
        "license_compat": 0.10,
        "paper_license": "CC-BY-NC-4.0",
    }, tmp_path, monkeypatch)
    eng_idx = out.index("**Engineering verdict**")
    lic_idx = out.index("**License verdict**")
    assert eng_idx < lic_idx
    assert "- **Contract match**: contract preserved" in out
    assert "🔴 `CC-BY-NC-4.0` (class: `nc`, compat: 0.10)" in out


def test_summary_license_verdict_alone(tmp_path, monkeypatch):
    """License axis renders even when selection produced no engineering
    fields (e.g. plain addition-shape PR run)."""
    out = _capture_summary({
        "status": "pr_opened_draft",
        "license_class": "permissive",
        "license_compat": 1.0,
        "paper_license": "Apache-2.0",
    }, tmp_path, monkeypatch)
    assert "**License verdict**: 🟢 `Apache-2.0`" in out
    assert "Engineering verdict" not in out


def test_summary_engineering_verdict_extension_fields(tmp_path, monkeypatch):
    out = _capture_summary({
        "status": "issue_opened_substitution",
        "selection_integration_shape": "extension",
        "selection_team_direction_signal": "RFC #84",
        "selection_proposed_call_site": "pipeline/localize.py",
    }, tmp_path, monkeypatch)
    assert "- **Team-direction signal**: RFC #84" in out
    assert "- **Proposed call site**: pipeline/localize.py" in out


def test_summary_omits_verdicts_without_fields(tmp_path, monkeypatch):
    out = _capture_summary({
        "status": "pr_opened_draft",
        "paper": "x", "arxiv": "2601.00001",
    }, tmp_path, monkeypatch)
    assert "Engineering verdict" not in out
    assert "License verdict" not in out


def test_summary_shape_alone_does_not_render_engineering(tmp_path, monkeypatch):
    """`selection_integration_shape` is set on every selection run; the
    Engineering verdict block needs at least one substantive field
    (contract / migration / extension signals) to be worth a section."""
    out = _capture_summary({
        "status": "pr_opened_draft",
        "selection_integration_shape": "addition",
    }, tmp_path, monkeypatch)
    assert "Engineering verdict" not in out
