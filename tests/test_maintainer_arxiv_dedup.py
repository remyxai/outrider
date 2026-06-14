"""Tests for v1.5.0 maintainer-arxiv discharge tier (v1.5.0 maintainer-Issue dedup):

  - `_arxiv_linked_issues` returns Issues with arxiv-link bodies,
    regardless of title prefix
  - Filters out PRs (consistent with `_remyx_issues`)
  - `_all_discharge_issues` merges Outrider + arxiv-linked sets without
    double-counting (Outrider Issues that also link arxiv aren't
    counted twice)
  - `_all_discharge_issues` annotates each merged issue with
    `_remyx_source` ∈ {"outrider", "maintainer"}
  - `_discharged_index` propagates `source` from the merged set
  - `_render_discharged_papers` tags bullets with [Outrider]/[Maintainer]
  - In-pool candidate annotation carries the source tag
  - Step summary differentiated copy still works against
    maintainer-tagged dedup hits (open vs closed semantics unchanged)
  - Regression: Outrider-only path still works when no maintainer
    arxiv-linked Issues exist (v1.4.7 semantics preserved)

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Recommendation, Target  # noqa: E402


def _outrider_issue(number: int, arxiv: str, state: str = "open",
                    title: str | None = None) -> dict:
    return {
        "number": number,
        "state": state,
        "title": title or f"[Remyx Recommendation] Sample {number}",
        "body": f"Paper: https://arxiv.org/abs/{arxiv}",
    }


def _maintainer_rfc(number: int, arxiv: str, state: str = "open",
                    title: str | None = None) -> dict:
    return {
        "number": number,
        "state": state,
        "title": title or f"[RFC] Some proposal {number}",
        "body": (
            f"Considering adding capability X — referencing "
            f"[the paper](https://arxiv.org/abs/{arxiv}) for details."
        ),
    }


def _rec(arxiv_id: str, title: str = "Sample", relevance: float = 0.8) -> Recommendation:
    return Recommendation(
        paper_title=title, arxiv_id=arxiv_id, tier="high",
        z_score=0.0, spec_md="", paper_abstract="abstract",
        domain_summary="", raw_paper_md="",
        relevance_score=relevance, reasoning="why",
        interest_name="x",
    )


# ─── _arxiv_linked_issues ────────────────────────────────────────────────


def test_arxiv_linked_returns_maintainer_rfcs(monkeypatch):
    """An [RFC]-titled maintainer-opened Issue with arxiv in body must
    be returned — that's the whole point of v1.5.0 maintainer-Issue dedup."""
    def fake_gh_api(method, path, body=None):
        assert "state=all" in path
        return [
            _maintainer_rfc(95, "2605.26004", title="[RFC] Paper Alpha proposal"),
        ]

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    out = run._arxiv_linked_issues(Target(repo="r/x", interest_id="iid"))
    assert len(out) == 1
    assert out[0]["number"] == 95


def test_arxiv_linked_filters_out_prs(monkeypatch):
    def fake_gh_api(method, path, body=None):
        return [
            {"number": 1, "state": "open",
             "title": "[RFC] x",
             "body": "https://arxiv.org/abs/2605.26004",
             "pull_request": {"url": "..."}},
            _maintainer_rfc(2, "2605.26004"),
        ]

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    out = run._arxiv_linked_issues(Target(repo="r/x", interest_id="iid"))
    numbers = [i["number"] for i in out]
    assert 1 not in numbers   # PR filtered
    assert 2 in numbers


def test_arxiv_linked_skips_issues_without_arxiv_body_link(monkeypatch):
    """An Issue with no arxiv link in body isn't a dedup signal — must
    not be returned (defensive against fishing for "any [RFC]")."""
    def fake_gh_api(method, path, body=None):
        return [
            {"number": 50, "state": "open",
             "title": "[RFC] General architecture discussion",
             "body": "Should we refactor module X? No paper reference."},
        ]

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    out = run._arxiv_linked_issues(Target(repo="r/x", interest_id="iid"))
    assert out == []


def test_arxiv_linked_swallows_fetch_error(monkeypatch):
    def fake_gh_api(method, path, body=None):
        raise RuntimeError("simulated 503")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    assert run._arxiv_linked_issues(Target(repo="r/x", interest_id="iid")) == []


# ─── _all_discharge_issues merge ──────────────────────────────────────────


def test_all_discharge_merges_outrider_and_maintainer(monkeypatch):
    """When the repo has both Outrider-prefixed Issues and maintainer
    RFCs that link arxiv, the merged set contains both with the right
    source tags."""
    def fake_gh_api(method, path, body=None):
        # Both helpers call the same endpoint shape — return all Issues.
        return [
            _outrider_issue(88, "2605.26102", state="closed",
                            title="[Remyx Recommendation] Alpha"),
            _maintainer_rfc(95, "2605.26004", title="[RFC] Beta proposal"),
        ]

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    out = run._all_discharge_issues(Target(repo="r/x", interest_id="iid"))
    by_number = {i["number"]: i for i in out}
    assert set(by_number) == {88, 95}
    assert by_number[88]["_remyx_source"] == "outrider"
    assert by_number[95]["_remyx_source"] == "maintainer"


def test_all_discharge_does_not_double_count_outrider_issues(monkeypatch):
    """If an Outrider Issue also matches the arxiv-link filter (which
    they always do, since they always carry an arxiv link), the merge
    must dedupe by number — not return two copies."""
    def fake_gh_api(method, path, body=None):
        return [_outrider_issue(88, "2605.26102")]

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    out = run._all_discharge_issues(Target(repo="r/x", interest_id="iid"))
    assert len(out) == 1
    assert out[0]["number"] == 88
    assert out[0]["_remyx_source"] == "outrider"


def test_all_discharge_outrider_only_when_no_maintainer_rfcs(monkeypatch):
    """Regression guard for v1.4.7 semantics: when the repo has only
    Outrider Issues (the pre-v1.5.0 world), behavior is identical."""
    def fake_gh_api(method, path, body=None):
        return [
            _outrider_issue(88, "2605.26102", state="closed"),
            _outrider_issue(94, "2607.07321", state="open"),
        ]

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    out = run._all_discharge_issues(Target(repo="r/x", interest_id="iid"))
    assert len(out) == 2
    assert all(it["_remyx_source"] == "outrider" for it in out)


# ─── _discharged_index propagates source ──────────────────────────────────


def test_discharged_index_carries_source_from_merged_set():
    issues = [
        {"number": 88, "state": "closed",
         "title": "[Remyx Recommendation] Alpha",
         "body": "https://arxiv.org/abs/2605.26102",
         "_remyx_source": "outrider"},
        {"number": 95, "state": "open",
         "title": "[RFC] Paper Alpha proposal",
         "body": "https://arxiv.org/abs/2605.26004",
         "_remyx_source": "maintainer"},
    ]
    idx = run._discharged_index(issues)
    assert idx["2605.26102"]["source"] == "outrider"
    assert idx["2605.26004"]["source"] == "maintainer"


def test_discharged_index_defaults_source_to_outrider_when_unset():
    """v1.4.7/v1.4.8 callers didn't set _remyx_source — must default to
    outrider so back-compat behavior is preserved."""
    issues = [{"number": 88, "state": "closed",
               "title": "[Remyx Recommendation] X",
               "body": "https://arxiv.org/abs/2605.26102"}]
    idx = run._discharged_index(issues)
    assert idx["2605.26102"]["source"] == "outrider"


# ─── _render_discharged_papers source tag rendering ───────────────────────


def test_render_discharged_tags_outrider_and_maintainer():
    issues = [
        {"number": 88, "state": "closed",
         "title": "[Remyx Recommendation] Alpha",
         "body": "https://arxiv.org/abs/2605.26102",
         "_remyx_source": "outrider"},
        {"number": 95, "state": "open",
         "title": "[RFC] Paper Alpha proposal",
         "body": "https://arxiv.org/abs/2605.26004",
         "_remyx_source": "maintainer"},
    ]
    out = run._render_discharged_papers(issues)
    # New header copy (v1.5.0)
    assert "Already in the team's attention" in out
    # Both bullets present with their respective source tags
    assert "Issue #88 (closed) [Outrider]" in out
    assert "Issue #95 (open) [Maintainer]" in out
    # Section body explains the rule
    assert "Maintainer]-tagged paper is a STRONGER stay-away signal" in out


def test_render_discharged_defaults_unset_source_to_outrider():
    """Back-compat: an issue with no _remyx_source key renders as
    [Outrider]. v1.4.7/v1.4.8 issues were Outrider-only by construction.
    Check the bullet line specifically (the section body always mentions
    both tags as part of explaining the rule)."""
    issues = [{"number": 88, "state": "closed",
               "title": "[Remyx Recommendation] X",
               "body": "https://arxiv.org/abs/2605.26102"}]
    out = run._render_discharged_papers(issues)
    bullet_line = next(
        line for line in out.splitlines() if "arxiv 2605.26102" in line
    )
    assert "[Outrider]" in bullet_line
    assert "[Maintainer]" not in bullet_line


# ─── In-pool candidate annotation source tag ──────────────────────────────


def test_candidate_brief_annotates_outrider_source():
    discharged = {
        "2605.26102": {"number": 88, "state": "closed",
                       "title": "Alpha", "source": "outrider"},
    }
    out = run._render_candidate_brief([_rec("2605.26102", "Alpha")],
                                       discharged=discharged)
    assert "already filed: Issue #88 (closed) [Outrider]" in out
    assert "do NOT pick" in out


def test_candidate_brief_annotates_maintainer_source():
    discharged = {
        "2605.26004": {"number": 95, "state": "open",
                       "title": "Paper Alpha", "source": "maintainer"},
    }
    out = run._render_candidate_brief([_rec("2605.26004", "Paper Alpha pool entry")],
                                       discharged=discharged)
    assert "already filed: Issue #95 (open) [Maintainer]" in out


# ─── End-to-end ───────────────────────────────────────────────────────────


def test_end_to_end_maintainer_rfc_discharges_pool_candidate(monkeypatch, tmp_path):
    """The full v1.5.0 promise: a maintainer-opened RFC linking a paper
    that's also in the engine's recommendation pool causes the in-pool
    candidate to be annotated as [Maintainer]-discharged when the
    selection prompt is rendered."""
    captured_prompts: list[str] = []

    def fake_oneshot(workdir, prompt, timeout_s, max_turns=None):
        captured_prompts.append(prompt)
        return True, '{"chosen_index": 0, "reasoning": "test"}', []

    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", fake_oneshot)
    monkeypatch.setattr(run, "_repo_layout_manifest", lambda wd, pkg: "(layout)")

    # Pool has the paper at index 0; maintainer RFC has it discharged
    candidates = [_rec("2605.26004", "Paper Alpha pool entry")]
    discharge_set = [
        {"number": 95, "state": "open",
         "title": "[RFC] Paper Alpha proposal",
         "body": "https://arxiv.org/abs/2605.26004",
         "_remyx_source": "maintainer"},
    ]
    # Selection bails early on single-candidate pool, so add a filler
    candidates.append(_rec("9999.99999", "Filler"))
    run.select_recommendation(
        tmp_path, "pkg", candidates,
        target=Target(repo="example/repo", interest_id="iid"),
        discharged_issues=discharge_set,
    )
    prompt = captured_prompts[0]
    # The candidate brief inline annotation includes [Maintainer]
    assert "already filed: Issue #95 (open) [Maintainer]" in prompt
    # Discharge section also lists it with [Maintainer]
    assert "Issue #95 (open) [Maintainer]" in prompt
