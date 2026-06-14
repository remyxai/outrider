"""Tests for the v1.4.8 selection-prompt dedup-awareness:

  - `_arxiv_id_from_issue_body` pulls a versionless id from an Issue body
  - `_discharged_index` builds an arxiv-id -> {number, state, title} map
    from an all-state issue list and de-dupes by arxiv id
  - `_render_discharged_papers` renders the "Already filed by Outrider"
    section when there are prior Issues, returns "" when there are not
  - The section caps at top-N most-recent and reports a footer
  - `_render_candidate_brief` inlines an "✗ already filed: #NN (state)"
    annotation on candidates whose arxiv id matches the discharge set
  - The selection prompt body explains the discharged-set rule (so the
    LLM applies it to out-of-pool picks too)
  - End-to-end: the discharge section is byte-stable empty when there
    are no prior Issues — regression guard for the v1.4.6/v1.4.7 behavior

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Recommendation  # noqa: E402


def _rec(arxiv_id: str, title: str = "Sample Paper",
         relevance: float = 0.8) -> Recommendation:
    return Recommendation(
        paper_title=title, arxiv_id=arxiv_id, tier="high",
        z_score=0.0, spec_md="", paper_abstract="abstract",
        domain_summary="", raw_paper_md="",
        relevance_score=relevance, reasoning="why",
        interest_name="x",
    )


def _issue(number: int, arxiv: str, state: str = "open",
           title: str = "Sample Title") -> dict:
    return {
        "number": number,
        "state": state,
        "title": f"[Remyx Recommendation] {title}",
        "body": f"Paper: https://arxiv.org/abs/{arxiv}\nOther stuff.",
    }


# ─── _arxiv_id_from_issue_body ────────────────────────────────────────────


def test_arxiv_from_body_extracts_versionless_id():
    body = "see https://arxiv.org/abs/2605.26102v2 for details"
    assert run._arxiv_id_from_issue_body(body) == "2605.26102"


def test_arxiv_from_body_returns_none_when_absent():
    assert run._arxiv_id_from_issue_body("no link here") is None
    assert run._arxiv_id_from_issue_body("") is None
    assert run._arxiv_id_from_issue_body(None) is None


def test_arxiv_from_body_takes_first_when_multiple():
    body = (
        "first: arxiv.org/abs/2605.26102v1\n"
        "second: arxiv.org/abs/2412.18404"
    )
    assert run._arxiv_id_from_issue_body(body) == "2605.26102"


# ─── _discharged_index ────────────────────────────────────────────────────


def test_discharged_index_keys_by_versionless_arxiv():
    issues = [
        _issue(88, "2605.26102", state="closed", title="Paper Alpha"),
        _issue(94, "2607.07321", state="open", title="Paper Gamma"),
    ]
    idx = run._discharged_index(issues)
    assert set(idx) == {"2605.26102", "2607.07321"}
    assert idx["2605.26102"]["number"] == 88
    assert idx["2605.26102"]["state"] == "closed"
    assert idx["2607.07321"]["state"] == "open"


def test_discharged_index_first_write_wins():
    """When two Outrider Issues reference the same paper, the more-recent
    one (first in the list — GitHub returns most-recent-first) wins."""
    issues = [
        _issue(99, "2605.26102", state="open", title="Recent"),
        _issue(88, "2605.26102", state="closed", title="Older"),
    ]
    idx = run._discharged_index(issues)
    assert idx["2605.26102"]["number"] == 99
    assert idx["2605.26102"]["state"] == "open"


def test_discharged_index_skips_issues_without_arxiv():
    """OPEN_AS_ISSUE downgrades that don't reference an arxiv id in the
    body must not crash the index — they're just ignored (the dedup
    pathway for those uses title-match, not arxiv-match)."""
    issues = [
        {"number": 50, "state": "closed", "title": "[Remyx Recommendation] X",
         "body": "no arxiv link"},
        _issue(88, "2605.26102"),
    ]
    idx = run._discharged_index(issues)
    assert set(idx) == {"2605.26102"}


# ─── _render_discharged_papers ────────────────────────────────────────────


def test_render_discharged_empty_returns_empty_string():
    """Regression guard for new-install / no-prior-Issues case. An empty
    section means the selection prompt is byte-identical to its
    pre-v1.4.8 shape."""
    assert run._render_discharged_papers([]) == ""


def test_render_discharged_includes_header_and_bullets():
    issues = [
        _issue(88, "2605.26102", state="closed", title="Paper Alpha"),
        _issue(94, "2607.07321", state="open",
               title="Paper Gamma"),
    ]
    out = run._render_discharged_papers(issues)
    # New header copy in v1.5.0 (was "Already filed by Outrider")
    assert "Already in the team's attention" in out
    assert "do NOT re-pick" in out
    # Both bullets present with arxiv + Issue # + state
    assert "arxiv 2605.26102" in out
    assert "Issue #88 (closed)" in out
    assert "arxiv 2607.07321" in out
    assert "Issue #94 (open)" in out
    # Source tag — defaults to [Outrider] when _remyx_source isn't set
    # (v1.4.8 callers).
    assert "[Outrider]" in out
    # Re-engagement lever is documented inside the section
    assert "reopen the issue" in out.lower()


def test_render_discharged_strips_remyx_prefix_from_title():
    """Title prefix is contextually obvious from the section header —
    rendering it on every bullet wastes prompt tokens."""
    issues = [_issue(88, "2605.26102", title="The Real Title")]
    out = run._render_discharged_papers(issues)
    assert "The Real Title" in out
    assert "[Remyx Recommendation]" not in out


def test_render_discharged_truncates_long_titles():
    long_title = "A very long paper title that goes on and on " * 5
    issues = [_issue(88, "2605.26102", title=long_title)]
    out = run._render_discharged_papers(issues)
    assert "…" in out
    # No line over ~120 chars wide (bullet prefix + capped title)
    title_line = next(
        line for line in out.splitlines() if "arxiv 2605.26102" in line
    )
    assert len(title_line) <= 140


def test_render_discharged_caps_at_n_with_footer():
    issues = [
        _issue(100 - i, f"2605.{i:05d}", state="open")
        for i in range(75)
    ]
    out = run._render_discharged_papers(issues, cap=50)
    # First entry kept (most recent)
    assert "arxiv 2605.00000" in out
    # 50th entry kept; 51st should be omitted
    assert "arxiv 2605.00049" in out
    assert "arxiv 2605.00050" not in out
    # Footer reports the truncated count
    assert "…and 25 older Issue(s) omitted" in out


def test_render_discharged_returns_empty_when_no_bullets_pass_filter():
    """If every Issue in the list lacks an arxiv link, the section
    collapses to empty rather than rendering just the header."""
    issues = [
        {"number": 1, "state": "open", "title": "[Remyx Recommendation] X",
         "body": "no arxiv reference at all"},
    ]
    assert run._render_discharged_papers(issues) == ""


# ─── _render_candidate_brief inline annotation ────────────────────────────


def test_candidate_brief_annotates_matching_arxiv():
    candidates = [
        _rec("2605.26102", "Paper Alpha"),
        _rec("2412.18404", "Paper Beta"),
    ]
    discharged = {
        "2605.26102": {"number": 88, "state": "closed", "title": "X"},
    }
    out = run._render_candidate_brief(candidates, discharged=discharged)
    # First candidate carries the annotation; second does not.
    instructsam_line = next(
        line for line in out.splitlines() if "Paper Alpha" in line
    )
    photoflow_line = next(
        line for line in out.splitlines() if "Paper Beta" in line
    )
    assert "already filed: Issue #88 (closed)" in instructsam_line
    assert "do NOT pick" in instructsam_line
    assert "already filed" not in photoflow_line


def test_candidate_brief_matches_versionless_arxiv():
    """Discharge keys are versionless; candidate arxiv ids may carry a
    version suffix. The annotation must still fire."""
    candidates = [_rec("2605.26102v3", "Paper Alpha v3")]
    discharged = {
        "2605.26102": {"number": 88, "state": "closed", "title": "X"},
    }
    out = run._render_candidate_brief(candidates, discharged=discharged)
    assert "already filed: Issue #88" in out


def test_candidate_brief_unchanged_when_discharged_empty():
    """Regression guard: when no discharge set is passed, the candidate
    brief is byte-identical to the pre-v1.4.8 shape."""
    candidates = [_rec("2605.26102", "Paper Alpha")]
    out_with = run._render_candidate_brief(candidates, discharged={})
    out_without = run._render_candidate_brief(candidates)
    assert out_with == out_without
    assert "already filed" not in out_with


# ─── Selection prompt template wiring ─────────────────────────────────────


def test_selection_prompt_body_mentions_discharged_rule():
    """The prompt body must reference the discharge set so the LLM
    applies it to out-of-pool picks too — annotating in-pool candidates
    alone isn't enough to suppress an external pick of a discharged
    paper. Phrases are searched against the un-wrapped prompt because
    the template wraps at ~70 cols."""
    prompt = run._SELECTION_PROMPT_TEMPLATE
    unwrapped = " ".join(prompt.split())
    # v1.5.0: section header renamed from "Already filed by Outrider"
    # to "Already in the team's attention"
    assert "Already in the team's attention" in unwrapped
    assert "must not be re-picked" in unwrapped
    # The placeholder for the section itself is present (un-wrapped).
    assert "__DISCHARGED_PAPERS__" in prompt


def test_selection_prompt_renders_byte_stable_when_no_issues(monkeypatch, tmp_path):
    """End-to-end: with no prior Issues for the target, the prompt
    rendering must not include any discharge section content. Customers
    on their first run see the pre-v1.4.8 prompt shape."""
    captured_prompts: list[str] = []

    def fake_oneshot(workdir, prompt, timeout_s, max_turns=None):
        captured_prompts.append(prompt)
        return True, '{"chosen_index": 0, "reasoning": "test"}', []

    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", fake_oneshot)
    monkeypatch.setattr(run, "_repo_layout_manifest", lambda wd, pkg: "(layout)")

    candidates = [_rec("2605.26102", "A"), _rec("2412.18404", "B")]
    run.select_recommendation(
        tmp_path, "pkg", candidates,
        target=run.Target(repo="example/repo", interest_id="iid"),
        discharged_issues=[],
    )
    prompt = captured_prompts[0]
    # No discharge section content when there are no prior Issues —
    # neither header variant should leak.
    assert "Already in the team's attention" not in prompt
    assert "Already filed by Outrider" not in prompt
    assert "already filed: Issue" not in prompt
    # Placeholder must be substituted — should never leak into the
    # rendered prompt.
    assert "__DISCHARGED_PAPERS__" not in prompt


def test_selection_prompt_renders_discharge_section_when_issues_present(
    monkeypatch, tmp_path,
):
    """End-to-end: with prior Issues, both the section and the in-pool
    annotation appear in the actual rendered prompt."""
    captured_prompts: list[str] = []

    def fake_oneshot(workdir, prompt, timeout_s, max_turns=None):
        captured_prompts.append(prompt)
        return True, '{"chosen_index": 1, "reasoning": "test"}', []

    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", fake_oneshot)
    monkeypatch.setattr(run, "_repo_layout_manifest", lambda wd, pkg: "(layout)")

    candidates = [
        _rec("2605.26102", "Paper Alpha"),
        _rec("2412.18404", "Paper Beta"),
    ]
    run.select_recommendation(
        tmp_path, "pkg", candidates,
        target=run.Target(repo="example/repo", interest_id="iid"),
        discharged_issues=[
            _issue(88, "2605.26102", state="closed", title="Paper Alpha"),
        ],
    )
    prompt = captured_prompts[0]
    # Standalone section — v1.5.0 changed the header
    assert "Already in the team's attention" in prompt
    assert "Issue #88 (closed)" in prompt
    # In-pool inline annotation
    assert "already filed: Issue #88 (closed)" in prompt
    assert "do NOT pick" in prompt
    # Placeholder must be substituted
    assert "__DISCHARGED_PAPERS__" not in prompt
