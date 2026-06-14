"""Tests for v1.5.0 "extension" as fourth integration shape (v1.5.0 extension shape):

  - Prompt template documents the fourth shape with its four gates
  - Prompt schema lists `extension` as a legal value alongside
    addition / replacement / simplification
  - Schema requires `team_direction_signal` + `proposed_call_site`
    for extension picks; absent on other shapes
  - Tie-break ordering: simplification > replacement > addition >
    extension (extension is last-resort)
  - `select_recommendation` rejects extension picks missing the
    required fields (treats as malformed → falls back to skip)
  - Extension floor: tier=high AND relevance >= 0.85 required for
    in-pool extension picks; below-floor picks are rejected
    (recalibrated from 0.90 in v1.5.1 to admit candidates in the
    0.85-0.90 boundary band that the old hard cut was over-rejecting)
  - Extension picks thread through `selection_team_direction_signal`
    and `selection_proposed_call_site` in the result dict (for
    downgrade Issue body + step summary consumption)

Run with: pytest tests/ -q
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Recommendation, Target  # noqa: E402


def _rec(arxiv_id: str, title: str = "Sample", tier: str = "high",
         relevance: float = 0.93) -> Recommendation:
    return Recommendation(
        paper_title=title, arxiv_id=arxiv_id, tier=tier,
        z_score=0.0, spec_md="", paper_abstract="abstract",
        domain_summary="", raw_paper_md="",
        relevance_score=relevance, reasoning="why",
        interest_name="x",
    )


# ─── Prompt template documents the fourth shape ───────────────────────────


def test_prompt_documents_extension_shape():
    prompt = run._SELECTION_PROMPT_TEMPLATE
    unwrapped = " ".join(prompt.split())
    # Updated headline acknowledges four shapes
    assert "Four legitimate integration shapes" in unwrapped
    # Extension shape is named and defined
    assert "extension" in unwrapped
    assert "NEW capability the repo currently lacks" in unwrapped


def test_prompt_lists_four_extension_gates():
    """All four gates must be documented in the prompt for the model
    to apply them as constraints, not as suggestions."""
    prompt = run._SELECTION_PROMPT_TEMPLATE
    unwrapped = " ".join(prompt.split())
    # Gate 1 — pipeline-compatible I/O
    assert "Pipeline-compatible I/O contract" in unwrapped
    # Gate 2 — stated team-direction signal
    assert "Stated team-direction signal in the repo" in unwrapped
    assert "RFC-fishing" in unwrapped
    # Gate 3 — no existing implementation
    assert "No existing implementation in the repo" in unwrapped
    # Gate 4 — higher relevance + tier bar
    assert "Higher relevance + interest-alignment bar than addition" in unwrapped


def test_prompt_tie_break_ordering_includes_extension_last():
    """Extension must be last in the tie-break preference order, so the
    model defaults to it only when other shapes fail."""
    prompt = run._SELECTION_PROMPT_TEMPLATE
    unwrapped = " ".join(prompt.split())
    assert "simplification > replacement > addition > extension" in unwrapped
    # Explicit acknowledgment that extension is last-resort
    assert "Extension is LAST-RESORT" in unwrapped


def test_prompt_schema_lists_extension_as_legal_value():
    prompt = run._SELECTION_PROMPT_TEMPLATE
    unwrapped = " ".join(prompt.split())
    # Schema enumerates the four shape strings
    assert '"addition" | "replacement" | "simplification" | "extension"' in unwrapped


def test_prompt_schema_documents_extension_specific_fields():
    """team_direction_signal + proposed_call_site are required for
    extension picks. Both must be in the schema with "REQUIRED when
    integration_shape = `extension`" language."""
    prompt = run._SELECTION_PROMPT_TEMPLATE
    unwrapped = " ".join(prompt.split())
    assert "team_direction_signal" in unwrapped
    assert "proposed_call_site" in unwrapped
    # Both fields are explicitly marked REQUIRED for extension
    assert unwrapped.count("REQUIRED when integration_shape = `extension`") >= 2


# ─── Schema validation in select_recommendation ───────────────────────────


def _run_selection(monkeypatch, tmp_path, candidates, claude_response):
    """Invoke select_recommendation with a mocked Claude response."""
    def fake_oneshot(workdir, prompt, timeout_s, max_turns=None):
        return True, json.dumps(claude_response), []

    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", fake_oneshot)
    monkeypatch.setattr(run, "_repo_layout_manifest", lambda wd, pkg: "(layout)")

    return run.select_recommendation(
        tmp_path, "pkg", candidates,
        target=Target(repo="example/repo", interest_id="iid"),
    )


def test_extension_pick_missing_team_direction_signal_rejected(
    monkeypatch, tmp_path,
):
    """An extension pick without team_direction_signal is malformed —
    selection must fall back to skip-by-verification (chosen_index=-1),
    not silently accept the pick."""
    result = _run_selection(
        monkeypatch, tmp_path,
        candidates=[_rec("2605.26004", "Paper Alpha"), _rec("9999.99999", "Filler")],
        claude_response={
            "chosen_index": 0,
            "integration_shape": "extension",
            "proposed_call_site": "after stage_a, before publish",
            # team_direction_signal MISSING
            "reasoning": "tempting but should be rejected for missing field",
        },
    )
    assert result["chosen_index"] == -1


def test_extension_pick_missing_proposed_call_site_rejected(
    monkeypatch, tmp_path,
):
    result = _run_selection(
        monkeypatch, tmp_path,
        candidates=[_rec("2605.26004", "Paper Alpha"), _rec("9999.99999", "Filler")],
        claude_response={
            "chosen_index": 0,
            "integration_shape": "extension",
            "team_direction_signal": "Issue #95 names this paper",
            # proposed_call_site MISSING
            "reasoning": "tempting but should be rejected for missing field",
        },
    )
    assert result["chosen_index"] == -1


def test_extension_pick_below_relevance_floor_rejected(monkeypatch, tmp_path):
    """Gate 4: tier=high AND relevance >= 0.85. A pick at relevance 0.60
    fails gate 4 and must be rejected even if the other fields are
    present."""
    result = _run_selection(
        monkeypatch, tmp_path,
        candidates=[
            _rec("2605.26004", "Paper Alpha", relevance=0.60),
            _rec("9999.99999", "Filler"),
        ],
        claude_response={
            "chosen_index": 0,
            "integration_shape": "extension",
            "team_direction_signal": "Issue #95 names this paper",
            "proposed_call_site": "after stage_a, before publish",
            "reasoning": "below-floor relevance — must reject",
        },
    )
    assert result["chosen_index"] == -1


def test_extension_pick_at_0p85_floor_passes(monkeypatch, tmp_path):
    """Recalibrated v1.5.1 boundary: relevance exactly at 0.85 passes
    gate 4 (the comparison is `< 0.85`, not `<=`). A pick at 0.87 — the
    target boundary-band score regime the recalibration was designed to
    admit — must also pass."""
    result = _run_selection(
        monkeypatch, tmp_path,
        candidates=[
            _rec("2605.26004", "Paper Alpha", tier="high", relevance=0.85),
            _rec("9999.99999", "Filler"),
        ],
        claude_response={
            "chosen_index": 0,
            "integration_shape": "extension",
            "team_direction_signal": "Issue #95 names this paper",
            "proposed_call_site": "after stage_a, before publish",
            "reasoning": "at-floor boundary-band pick — must pass",
        },
    )
    assert result["chosen_index"] == 0

    result = _run_selection(
        monkeypatch, tmp_path,
        candidates=[
            _rec("2605.26004", "Paper Beta", tier="high", relevance=0.87),
            _rec("9999.99999", "Filler"),
        ],
        claude_response={
            "chosen_index": 0,
            "integration_shape": "extension",
            "team_direction_signal": "Issue #95 names this paper",
            "proposed_call_site": "after stage_a, before publish",
            "reasoning": "common boundary-band score — must pass",
        },
    )
    assert result["chosen_index"] == 0


def test_extension_pick_below_0p85_floor_rejected(monkeypatch, tmp_path):
    """Recalibrated v1.5.1 boundary: 0.84 must fail (one tick below the
    new floor). Pinning this boundary protects against accidental
    recalibration drift."""
    result = _run_selection(
        monkeypatch, tmp_path,
        candidates=[
            _rec("2605.26004", "Paper Alpha", relevance=0.84),
            _rec("9999.99999", "Filler"),
        ],
        claude_response={
            "chosen_index": 0,
            "integration_shape": "extension",
            "team_direction_signal": "Issue #95 names this paper",
            "proposed_call_site": "after stage_a, before publish",
            "reasoning": "just-below-floor — must reject",
        },
    )
    assert result["chosen_index"] == -1


def test_extension_pick_below_tier_floor_rejected(monkeypatch, tmp_path):
    """Gate 4 also requires tier=high. A medium-tier extension pick is
    rejected."""
    result = _run_selection(
        monkeypatch, tmp_path,
        candidates=[
            _rec("2605.26004", "Paper Alpha", tier="medium", relevance=0.93),
            _rec("9999.99999", "Filler"),
        ],
        claude_response={
            "chosen_index": 0,
            "integration_shape": "extension",
            "team_direction_signal": "Issue #95 names this paper",
            "proposed_call_site": "after stage_a, before publish",
            "reasoning": "below-tier-floor — must reject",
        },
    )
    assert result["chosen_index"] == -1


def test_extension_pick_with_all_fields_passes(monkeypatch, tmp_path):
    """Happy path: all four gates pass at the schema level, the pick
    threads through with chosen_index intact."""
    result = _run_selection(
        monkeypatch, tmp_path,
        candidates=[
            _rec("2605.26004", "Paper Alpha", tier="high", relevance=0.93),
            _rec("9999.99999", "Filler"),
        ],
        claude_response={
            "chosen_index": 0,
            "integration_shape": "extension",
            "team_direction_signal": "Issue #95 names this paper",
            "proposed_call_site": "after stage_a, before publish",
            "reasoning": "in-pool extension; all gates pass",
        },
    )
    assert result["chosen_index"] == 0
    assert result["integration_shape"] == "extension"


def test_other_shapes_dont_require_extension_fields(monkeypatch, tmp_path):
    """Regression — addition / replacement / simplification picks must
    NOT require team_direction_signal or proposed_call_site. Those
    fields are extension-only."""
    result = _run_selection(
        monkeypatch, tmp_path,
        candidates=[_rec("2605.26004", "X"), _rec("9999.99999", "Y")],
        claude_response={
            "chosen_index": 0,
            "integration_shape": "addition",
            "chosen_call_site": "example_pkg/stage.py:Component.run",
            "reasoning": "in-pool addition",
        },
    )
    assert result["chosen_index"] == 0


def test_external_extension_pick_passes_without_pool_floor(monkeypatch, tmp_path):
    """Out-of-pool extension picks (chosen_index=-2) don't have a pool
    candidate to apply the tier/relevance gate to. Schema fields are
    still required, but the floor check is skipped (it doesn't apply)."""
    result = _run_selection(
        monkeypatch, tmp_path,
        candidates=[_rec("AAAA", "A"), _rec("BBBB", "B")],
        claude_response={
            "chosen_index": -2,
            "integration_shape": "extension",
            "external_arxiv_id": "2605.26004",
            "external_title": "Paper Alpha",
            "external_query_used": "remyxai search info 2605.26004",
            "team_direction_signal": "Issue #95 names this paper",
            "proposed_call_site": "after stage_a, before publish",
            "reasoning": "external extension pick — pool floor doesn't apply",
        },
    )
    assert result["chosen_index"] == -2
    assert result["integration_shape"] == "extension"
