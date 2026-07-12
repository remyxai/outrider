"""Tests for the staged-synthesis two-stage dispatch flow.

Covers:

- ``write_research_invocation`` writes ``RESEARCH_INVOCATION.md`` with paper /
  target-repo / prior-attempt substitutions filled in.
- ``invoke_research_phase`` returns success + logs a byte count when
  ``web_findings.json`` is produced; returns soft-failure when the
  invocation completes but no artifact appears.
- ``write_spec_bundle`` conditionally includes the research-findings ref
  block in INVOCATION.md based on ``web_findings.json`` presence.
- INPUT_STAGED_SYNTHESIS gating: only truthy values trigger the research
  phase; unset / empty / 'false' preserve the classic flow.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


def _rec(**overrides):
    """Recommendation fixture — minimum fields for template substitution."""
    defaults = dict(
        paper_title="Example Paper",
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
    """Target fixture — minimum fields for the research prompt substitution."""
    return run.Target(
        repo="example-org/example-repo",
        interest_id="00000000-0000-0000-0000-000000000000",
    )


# ── write_research_invocation ────────────────────────────────────────────


def test_write_research_invocation_substitutes_paper_and_target(tmp_path, monkeypatch):
    """The template fills paper title, arxiv ID, and target repo verbatim."""
    monkeypatch.delenv("INPUT_START_FROM_REF", raising=False)
    run.write_research_invocation(tmp_path, _rec(), _target())
    content = (tmp_path / ".remyx-recommendation" / "RESEARCH_INVOCATION.md").read_text()
    assert "Example Paper" in content
    assert "arxiv:2412.99999v1" in content
    assert "example-org/example-repo" in content
    # Prior-attempt hint reflects no start-from-ref set
    assert "no prior-attempt branch" in content


def test_write_research_invocation_surfaces_start_from_ref(tmp_path, monkeypatch):
    """When start-from-ref is set, the research prompt names the baseline branch."""
    monkeypatch.setenv("INPUT_START_FROM_REF", "previous-attempt-branch")
    run.write_research_invocation(tmp_path, _rec(), _target())
    content = (tmp_path / ".remyx-recommendation" / "RESEARCH_INVOCATION.md").read_text()
    assert "previous-attempt-branch" in content
    assert "an earlier dispatch produced the branch" in content


def test_write_research_invocation_includes_web_findings_schema(tmp_path):
    """The prompt shows the coding session's expected schema — call_site_candidates,
    coordination_signals, scope_recommendations, provenance."""
    run.write_research_invocation(tmp_path, _rec(), _target())
    content = (tmp_path / ".remyx-recommendation" / "RESEARCH_INVOCATION.md").read_text()
    for key in [
        "call_site_candidates",
        "coordination_signals",
        "scope_recommendations",
        "mode_hint",
        "provenance",
        "web_findings.json",
    ]:
        assert key in content, f"expected {key} in research prompt template"


def test_write_research_invocation_prescribes_parallelism(tmp_path):
    """Prompt explicitly names the parallelism prescription — informed by the
    prototype's observation that soft "prefer breadth" isn't enough to hit
    5+ parallel tool calls per turn."""
    run.write_research_invocation(tmp_path, _rec(), _target())
    content = (tmp_path / ".remyx-recommendation" / "RESEARCH_INVOCATION.md").read_text()
    assert "at least 5 concurrent tool calls per turn" in content


# ── invoke_research_phase ────────────────────────────────────────────────


def test_invoke_research_phase_success_when_findings_written(tmp_path, monkeypatch):
    """The invocation reports success + logs a byte count when web_findings.json
    lands in the workspace root."""
    (tmp_path / ".remyx-recommendation").mkdir()
    (tmp_path / ".remyx-recommendation" / "RESEARCH_INVOCATION.md").write_text(
        "---\ntype: research_invocation\n---\n\nDo research."
    )

    def fake_claude(cmd, prompt, workdir, timeout_s):
        # Simulate the research invocation writing the artifact
        (workdir / ".remyx-recommendation" / "web_findings.json").write_text(json.dumps({"paper": {"title": "x"}}))
        return True, "research succeeded"

    with patch.object(run, "_run_claude_json", side_effect=fake_claude):
        ok, tail = run.invoke_research_phase(tmp_path, timeout_s=60)
    assert ok
    assert (tmp_path / ".remyx-recommendation" / "web_findings.json").exists()


def test_invoke_research_phase_soft_fail_when_artifact_missing(tmp_path):
    """If the CLI exits 0 but no web_findings.json is written, this is a soft
    failure — caller should fall through to classic single-invocation flow
    rather than aborting the dispatch."""
    (tmp_path / ".remyx-recommendation").mkdir()
    (tmp_path / ".remyx-recommendation" / "RESEARCH_INVOCATION.md").write_text(
        "---\ntype: research_invocation\n---\n\nDo research."
    )

    def fake_claude(cmd, prompt, workdir, timeout_s):
        # CLI succeeded but the agent didn't write the artifact
        return True, "session ended without producing web_findings.json"

    with patch.object(run, "_run_claude_json", side_effect=fake_claude):
        ok, _ = run.invoke_research_phase(tmp_path, timeout_s=60)
    assert not ok  # Soft failure
    assert not (tmp_path / ".remyx-recommendation" / "web_findings.json").exists()


def test_invoke_research_phase_hard_fail(tmp_path):
    """Hard CLI failure (e.g. timeout, credit exhaustion) surfaces as (False, tail)
    with the failure text; workspace has no web_findings.json artifact."""
    (tmp_path / ".remyx-recommendation").mkdir()
    (tmp_path / ".remyx-recommendation" / "RESEARCH_INVOCATION.md").write_text(
        "---\ntype: research_invocation\n---\n\nDo research."
    )

    with patch.object(
        run, "_run_claude_json",
        return_value=(False, "claude CLI timed out after 60s"),
    ):
        ok, tail = run.invoke_research_phase(tmp_path, timeout_s=60)
    assert not ok
    assert "timed out" in tail


def test_invoke_research_phase_uses_research_max_turns_env(tmp_path, monkeypatch):
    """REMYX_RESEARCH_MAX_TURNS caps the research invocation's turn count.
    Default is 8 per the prompt's bounded-budget instruction."""
    (tmp_path / ".remyx-recommendation").mkdir()
    (tmp_path / ".remyx-recommendation" / "RESEARCH_INVOCATION.md").write_text(
        "---\ntype: research_invocation\n---\n\nDo research."
    )
    captured_cmd = {}

    def fake_claude(cmd, prompt, workdir, timeout_s):
        captured_cmd["cmd"] = list(cmd)
        (workdir / ".remyx-recommendation" / "web_findings.json").write_text("{}")
        return True, ""

    monkeypatch.setenv("REMYX_RESEARCH_MAX_TURNS", "12")
    with patch.object(run, "_run_claude_json", side_effect=fake_claude):
        run.invoke_research_phase(tmp_path, timeout_s=60)
    assert "--max-turns" in captured_cmd["cmd"]
    assert captured_cmd["cmd"][captured_cmd["cmd"].index("--max-turns") + 1] == "12"


# ── INVOCATION.md conditional research-findings ref ─────────────────────


def _make_workdir_with_bundle_prereqs(tmp_path):
    """Enough of the workdir shape to let write_spec_bundle succeed with
    minimum context (no real repo clone required for this template test)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("")
    return tmp_path


def test_invocation_md_omits_findings_ref_when_no_web_findings(tmp_path, monkeypatch):
    """Classic flow: no research phase ran → no web_findings.json → INVOCATION.md
    omits the research-context ref block."""
    monkeypatch.delenv("INPUT_STAGED_SYNTHESIS", raising=False)
    wd = _make_workdir_with_bundle_prereqs(tmp_path)
    run.write_spec_bundle(wd, _target(), _rec(), "src", env_body="")
    content = (wd / ".remyx-recommendation" / "INVOCATION.md").read_text()
    assert "web_findings.json" not in content


def test_invocation_md_includes_findings_ref_when_web_findings_present(tmp_path):
    """Staged flow: research phase produced web_findings.json → INVOCATION.md
    references it so the coding session reads it as another bundle context file."""
    wd = _make_workdir_with_bundle_prereqs(tmp_path)
    (wd / ".remyx-recommendation").mkdir(exist_ok=True)
    (wd / ".remyx-recommendation" / "web_findings.json").write_text(json.dumps({"paper": {"title": "x"}}))
    run.write_spec_bundle(wd, _target(), _rec(), "src", env_body="")
    content = (wd / ".remyx-recommendation" / "INVOCATION.md").read_text()
    assert "web_findings.json" in content
    assert "structured research context" in content
    assert "call_site_candidates" in content
