"""Tests for the code-override carve-out in the selection-pass.

The override lets the selection-pass agent pick a `no-code-link` candidate
(``license_compat <= 0.30``) under three conditions:

  1. Method is conceptually self-contained
  2. Verified existing call site with a clear contract match
  3. Integration archetype is `addition` or `simplification`

The override fires only when the agent populates the new
``code_override_justification`` field. ``select_recommendation`` validates
the override is restricted to the eligible archetypes and to actual
no-code-link candidates; misuse falls back to ``chosen_index = -1``.

The empirical anchor: PR #47 (Exploration Structure in LLM Agents)
shipped from a no-code paper because its analytical contribution was
conceptually self-contained. Without this carve-out the agent had to
side-step the existing license penalty implicitly; with it the override
is explicit, auditable, and trackable across runs.

Run with: pytest tests/test_selection_code_override.py -q
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

import run  # noqa: E402
from run import Recommendation, Target  # noqa: E402


def _rec(
    arxiv_id: str = "2606.99999v1",
    license_compat: float = 0.3,
    license_class: str = "no-code-link",
    tier: str = "moderate",
    relevance: float = 0.85,
) -> Recommendation:
    """Build a minimal Recommendation for selection-pass plumbing tests."""
    return Recommendation(
        paper_title=f"Test paper {arxiv_id}",
        arxiv_id=arxiv_id,
        tier=tier,
        z_score=0.0,
        spec_md="",
        paper_abstract="abstract",
        domain_summary="domain",
        raw_paper_md="md",
        relevance_score=relevance,
        license_class=license_class,
        license_compat=license_compat,
    )


def _target() -> Target:
    return Target(repo="owner/repo", interest_id="iid")


def _mock_streaming(verdict_json: str):
    """Build a monkeypatch target that returns (ok=True, output, events=[])
    for ``_run_claude_oneshot_streaming``, so ``select_recommendation`` runs
    its post-process validation against synthetic agent output."""
    def _inner(*args, **kwargs):
        return True, verdict_json, []
    return _inner


# ─── Prompt-content regression checks ─────────────────────────────────────


def test_prompt_template_includes_override_carve_out():
    """The override section must be in the template; remove it and the
    no-code-link carve-out has no behavioral contract."""
    assert "Overriding the no-code penalty" in run._SELECTION_PROMPT_TEMPLATE


def test_prompt_template_documents_three_conditions():
    """The carve-out's three conditions must each be named in the prompt
    so the agent has a contract to verify against, not an implicit
    norm to guess at."""
    template = run._SELECTION_PROMPT_TEMPLATE
    assert "conceptually self-contained" in template
    assert "Verified existing call site" in template
    assert "addition` or `simplification" in template


def test_prompt_template_documents_schema_field():
    """The schema must document the new field; an undocumented field is
    indistinguishable from a typo in the model's output."""
    assert "code_override_justification" in run._SELECTION_PROMPT_TEMPLATE


# ─── Validation: override accepted for eligible archetypes ────────────────


def test_override_accepted_for_addition(monkeypatch, tmp_path):
    """No-code candidate + addition archetype + justification → override
    fires; chosen_index is preserved."""
    candidates = [_rec(arxiv_id="2606.00001v1", license_compat=0.3)]
    verdict = (
        '{"chosen_index": 0, "integration_shape": "addition", '
        '"chosen_call_site": "src/x.py:f", '
        '"verification_summary": "verified", '
        '"reasoning": "fits", '
        '"code_override_justification": "Method is a classification scheme '
        'over the event stream; abstract specifies the four labels and the '
        'derivation rule without ambiguity.", '
        '"rejected": []}'
    )
    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", _mock_streaming(verdict))
    monkeypatch.setattr(run, "_repo_layout_manifest", lambda *a, **k: "")
    # Need at least two candidates for select_recommendation to engage
    candidates.append(_rec(arxiv_id="2606.00002v1"))
    result = run.select_recommendation(tmp_path, "pkg", candidates, target=_target())
    assert result is not None
    assert result["chosen_index"] == 0
    assert "code_override_justification" in result
    assert "classification scheme" in result["code_override_justification"]


def test_override_accepted_for_simplification(monkeypatch, tmp_path):
    """Simplification is the second eligible archetype; override accepted."""
    candidates = [
        _rec(arxiv_id="2606.00001v1", license_compat=0.3),
        _rec(arxiv_id="2606.00002v1"),
    ]
    verdict = (
        '{"chosen_index": 0, "integration_shape": "simplification", '
        '"contract_match": "matches", "migration_cost": "1 file", '
        '"verification_summary": "verified", "reasoning": "fits", '
        '"code_override_justification": "Pipeline collapse rule is '
        'arithmetic on existing knobs.", "rejected": []}'
    )
    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", _mock_streaming(verdict))
    monkeypatch.setattr(run, "_repo_layout_manifest", lambda *a, **k: "")
    result = run.select_recommendation(tmp_path, "pkg", candidates, target=_target())
    assert result["chosen_index"] == 0
    assert "code_override_justification" in result


# ─── Validation: override rejected for ineligible archetypes ──────────────


def test_override_rejected_for_replacement(monkeypatch, tmp_path):
    """Replacement touches existing code in production — the bar for
    borrowing from a no-code paper there is higher. Override must
    fail-closed: chosen_index falls back to -1."""
    candidates = [
        _rec(arxiv_id="2606.00001v1", license_compat=0.3),
        _rec(arxiv_id="2606.00002v1"),
    ]
    verdict = (
        '{"chosen_index": 0, "integration_shape": "replacement", '
        '"contract_match": "matches", "migration_cost": "5 files", '
        '"verification_summary": "verified", "reasoning": "swap-in", '
        '"code_override_justification": "Method is clear enough.", '
        '"rejected": []}'
    )
    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", _mock_streaming(verdict))
    monkeypatch.setattr(run, "_repo_layout_manifest", lambda *a, **k: "")
    result = run.select_recommendation(tmp_path, "pkg", candidates, target=_target())
    assert result["chosen_index"] == -1


def test_override_rejected_for_extension(monkeypatch, tmp_path):
    """Extension has no call site at all — combining 'no code' + 'no
    anchoring call site' is speculation, not a contract-anchored
    override. Override falls back to -1."""
    candidates = [
        _rec(
            arxiv_id="2606.00001v1",
            license_compat=0.3,
            tier="high",
            relevance=0.92,
        ),
        _rec(arxiv_id="2606.00002v1"),
    ]
    verdict = (
        '{"chosen_index": 0, "integration_shape": "extension", '
        '"team_direction_signal": "README roadmap item", '
        '"proposed_call_site": "after stage_a", '
        '"verification_summary": "verified", "reasoning": "extension fit", '
        '"code_override_justification": "Empirical method, ports cleanly.", '
        '"rejected": []}'
    )
    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", _mock_streaming(verdict))
    monkeypatch.setattr(run, "_repo_layout_manifest", lambda *a, **k: "")
    result = run.select_recommendation(tmp_path, "pkg", candidates, target=_target())
    assert result["chosen_index"] == -1


# ─── Validation: override ignored on code-bearing candidates ──────────────


def test_override_ignored_on_code_bearing_candidate(monkeypatch, tmp_path):
    """Justification field set on a code-bearing pick (compat > 0.30)
    is dropped silently — the override only applies to actual
    no-code-link candidates. chosen_index stays; justification is gone."""
    candidates = [
        _rec(
            arxiv_id="2606.00001v1",
            license_compat=1.0,
            license_class="permissive",
        ),
        _rec(arxiv_id="2606.00002v1"),
    ]
    verdict = (
        '{"chosen_index": 0, "integration_shape": "addition", '
        '"chosen_call_site": "src/x.py:f", '
        '"verification_summary": "verified", "reasoning": "fits", '
        '"code_override_justification": "Not applicable here.", '
        '"rejected": []}'
    )
    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", _mock_streaming(verdict))
    monkeypatch.setattr(run, "_repo_layout_manifest", lambda *a, **k: "")
    result = run.select_recommendation(tmp_path, "pkg", candidates, target=_target())
    assert result["chosen_index"] == 0
    # Override-not-applicable: the field is dropped, not retained.
    assert "code_override_justification" not in result


# ─── Validation: missing-override path stays compatible ───────────────────


def test_no_override_field_passes_through(monkeypatch, tmp_path):
    """When the agent doesn't populate the override field, existing
    behavior holds — chosen_index is preserved, no validation fires.
    This is the no-code candidate that the agent picked WITHOUT
    explicit override; the existing license penalty applies via the
    relevance ranker / pre-filter but does not auto-fail at the
    selection-pass stage."""
    candidates = [
        _rec(arxiv_id="2606.00001v1", license_compat=0.3),
        _rec(arxiv_id="2606.00002v1"),
    ]
    verdict = (
        '{"chosen_index": 0, "integration_shape": "addition", '
        '"chosen_call_site": "src/x.py:f", '
        '"verification_summary": "verified", "reasoning": "fits", '
        '"rejected": []}'
    )
    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", _mock_streaming(verdict))
    monkeypatch.setattr(run, "_repo_layout_manifest", lambda *a, **k: "")
    result = run.select_recommendation(tmp_path, "pkg", candidates, target=_target())
    assert result["chosen_index"] == 0
    assert "code_override_justification" not in result


def test_empty_override_string_treated_as_absent(monkeypatch, tmp_path):
    """Empty / whitespace-only justification is treated as no override —
    same path as the field being absent. Prevents the agent from
    sneaking the override past validation by populating an empty
    string."""
    candidates = [
        _rec(arxiv_id="2606.00001v1", license_compat=0.3),
        _rec(arxiv_id="2606.00002v1"),
    ]
    verdict = (
        '{"chosen_index": 0, "integration_shape": "replacement", '
        '"contract_match": "matches", "migration_cost": "1 file", '
        '"verification_summary": "verified", "reasoning": "swap", '
        '"code_override_justification": "   ", "rejected": []}'
    )
    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", _mock_streaming(verdict))
    monkeypatch.setattr(run, "_repo_layout_manifest", lambda *a, **k: "")
    result = run.select_recommendation(tmp_path, "pkg", candidates, target=_target())
    # Empty override means the replacement validation should NOT trigger
    # the override-with-wrong-archetype fall-through; the pick proceeds
    # as a normal replacement pick.
    assert result["chosen_index"] == 0
    assert "code_override_justification" not in result
