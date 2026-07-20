"""Pre-PR fidelity gate + branch-patch stage tests.

Prototype for the fidelity-before-PR-publication redesign: fidelity runs
on the local branch, and if flagged, a single Claude Code patch attempt
tries to resolve the deviations before deciding whether to publish.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# --- _local_git_diff --------------------------------------------------------

def test_local_git_diff_returns_stdout_on_success(tmp_path):
    fake = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="diff --git a/x b/x\n+added\n", stderr="",
    )
    with patch.object(run.subprocess, "run", return_value=fake):
        got = run._local_git_diff(tmp_path, "main")
    assert "added" in got


def test_local_git_diff_returns_empty_on_nonzero(tmp_path):
    fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="err")
    with patch.object(run.subprocess, "run", return_value=fake):
        got = run._local_git_diff(tmp_path, "main")
    assert got == ""


def test_local_git_diff_returns_empty_on_subprocess_error(tmp_path):
    with patch.object(run.subprocess, "run", side_effect=OSError("boom")):
        got = run._local_git_diff(tmp_path, "main")
    assert got == ""


# --- _run_pre_pr_fidelity_check ---------------------------------------------

def _make_rec(arxiv_id="2606.30560v1"):
    """Minimal Recommendation-shape stub for tests."""
    rec = MagicMock()
    rec.arxiv_id = arxiv_id
    rec.paper_title = "Test Paper"
    return rec


def _make_target():
    t = MagicMock()
    t.repo = "org/repo"
    t.claude_timeout_s = 900
    return t


def test_pre_pr_fidelity_skips_when_no_reference_url(tmp_path):
    """No reference URL in the body → skip cleanly, no needs_judgment."""
    rec = _make_rec()
    target = _make_target()
    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("2606.30560v1", "")):
        verdict = run._run_pre_pr_fidelity_check(
            rec, target, tmp_path, "PR title", "PR body with no ref", "main",
        )
    assert verdict["needs_judgment"] is False
    assert verdict["status"] == "pre_pr_fidelity_skipped_no_reference"


def test_pre_pr_fidelity_returns_needs_judgment_when_matrix_flags(tmp_path):
    rec = _make_rec()
    target = _make_target()
    fake_matrix = {
        "items": [
            {"name": "algo mismatch", "status": "deviation (needs-judgment)",
             "rationale": "..."},
        ],
        "needs_judgment": True,
    }
    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("2606.30560v1", "https://github.com/x/y")), \
         patch.object(run, "_clone_reference_repo",
                      return_value=(True, tmp_path / "ref", "")), \
         patch.object(run, "_score_reference_confidence",
                      return_value=("high", {})), \
         patch.object(run, "_local_git_diff", return_value="fake diff"), \
         patch.object(run, "_run_claude_oneshot", return_value=(True, "{...}")), \
         patch.object(run, "_extract_json_object", return_value=fake_matrix), \
         patch.object(run, "_render_coverage_matrix", return_value="## Coverage\n..."):
        verdict = run._run_pre_pr_fidelity_check(
            rec, target, tmp_path, "T", "B", "main",
        )
    assert verdict["needs_judgment"] is True
    assert verdict["items_count"] == 1
    assert verdict["status"] == "pre_pr_fidelity_needs_judgment"
    assert verdict["matrix"] == fake_matrix


def test_pre_pr_fidelity_returns_clean_when_matrix_not_flagged(tmp_path):
    rec = _make_rec()
    target = _make_target()
    fake_matrix = {"items": [{"name": "x", "status": "covered"}], "needs_judgment": False}
    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("2606.30560v1", "https://github.com/x/y")), \
         patch.object(run, "_clone_reference_repo",
                      return_value=(True, tmp_path / "ref", "")), \
         patch.object(run, "_score_reference_confidence",
                      return_value=("high", {})), \
         patch.object(run, "_local_git_diff", return_value="fake diff"), \
         patch.object(run, "_run_claude_oneshot", return_value=(True, "{...}")), \
         patch.object(run, "_extract_json_object", return_value=fake_matrix), \
         patch.object(run, "_render_coverage_matrix", return_value=""):
        verdict = run._run_pre_pr_fidelity_check(
            rec, target, tmp_path, "T", "B", "main",
        )
    assert verdict["needs_judgment"] is False
    assert verdict["status"] == "pre_pr_fidelity_clean"


def test_pre_pr_fidelity_degrades_when_reference_clone_fails(tmp_path):
    rec = _make_rec()
    target = _make_target()
    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("2606.30560v1", "https://github.com/x/y")):
        with patch.object(run, "_clone_reference_repo",
                          return_value=(False, None, "network error")):
            verdict = run._run_pre_pr_fidelity_check(
                rec, target, tmp_path, "T", "B", "main",
            )
    assert verdict["needs_judgment"] is False
    assert verdict["status"] == "pre_pr_fidelity_failed_clone"


def test_pre_pr_fidelity_degrades_when_diff_empty(tmp_path):
    rec = _make_rec()
    target = _make_target()
    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("2606.30560v1", "https://github.com/x/y")), \
         patch.object(run, "_clone_reference_repo",
                      return_value=(True, tmp_path / "ref", "")), \
         patch.object(run, "_score_reference_confidence",
                      return_value=("high", {})), \
         patch.object(run, "_local_git_diff", return_value=""):
        verdict = run._run_pre_pr_fidelity_check(
            rec, target, tmp_path, "T", "B", "main",
        )
    assert verdict["needs_judgment"] is False
    assert verdict["status"] == "pre_pr_fidelity_failed_no_diff"


def test_pre_pr_fidelity_degrades_when_claude_returns_unparseable(tmp_path):
    rec = _make_rec()
    target = _make_target()
    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("2606.30560v1", "https://github.com/x/y")), \
         patch.object(run, "_clone_reference_repo",
                      return_value=(True, tmp_path / "ref", "")), \
         patch.object(run, "_score_reference_confidence",
                      return_value=("high", {})), \
         patch.object(run, "_local_git_diff", return_value="diff"), \
         patch.object(run, "_run_claude_oneshot", return_value=(True, "garbage")), \
         patch.object(run, "_extract_json_object", return_value=None):
        verdict = run._run_pre_pr_fidelity_check(
            rec, target, tmp_path, "T", "B", "main",
        )
    assert verdict["needs_judgment"] is False
    assert verdict["status"] == "pre_pr_fidelity_failed_claude"


# --- _attempt_pre_pr_fidelity_patch -----------------------------------------

def test_patch_returns_false_when_no_flagged_items(tmp_path):
    matrix = {"items": [{"name": "x", "status": "covered"}]}
    assert run._attempt_pre_pr_fidelity_patch(
        tmp_path, matrix, "https://github.com/x/y",
    ) is False


def test_patch_returns_false_when_matrix_empty(tmp_path):
    assert run._attempt_pre_pr_fidelity_patch(
        tmp_path, {}, "https://github.com/x/y",
    ) is False


def test_patch_returns_false_when_claude_fails(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("class Foo:\n    pass\n")
    matrix = {
        "items": [
            {"name": "algo mismatch", "status": "deviation (needs-judgment)",
             "rationale": "..."},
        ],
    }
    with patch.object(run, "invoke_claude_code", return_value=(False, "err")):
        got = run._attempt_pre_pr_fidelity_patch(
            tmp_path, matrix, "https://github.com/x/y",
        )
    assert got is False


def test_patch_returns_false_when_no_files_touched(tmp_path):
    (tmp_path / "src").mkdir()
    src_file = tmp_path / "src" / "a.py"
    src_file.write_text("class Foo:\n    pass\n")
    matrix = {
        "items": [
            {"name": "algo mismatch", "status": "deviation (needs-judgment)"},
        ],
    }
    # Claude "succeeds" but doesn't actually modify any .py files
    with patch.object(run, "invoke_claude_code", return_value=(True, "no edits")):
        got = run._attempt_pre_pr_fidelity_patch(
            tmp_path, matrix, "https://github.com/x/y",
        )
    assert got is False


def test_patch_returns_true_when_files_touched(tmp_path):
    import time
    (tmp_path / "src").mkdir()
    src_file = tmp_path / "src" / "a.py"
    src_file.write_text("class Foo:\n    pass\n")

    matrix = {
        "items": [
            {"name": "algo mismatch", "status": "deviation (needs-judgment)"},
        ],
    }
    def fake_claude(workdir, timeout_s=None):
        time.sleep(0.05)  # ensure mtime moves
        src_file.write_text("class Foo:\n    def new_method(self): pass\n")
        return (True, "edited")
    with patch.object(run, "invoke_claude_code", side_effect=fake_claude):
        got = run._attempt_pre_pr_fidelity_patch(
            tmp_path, matrix, "https://github.com/x/y",
        )
    assert got is True
    # Brief is written to INVOCATION.md (what invoke_claude_code reads at startup).
    invoc = tmp_path / ".remyx-recommendation" / "INVOCATION.md"
    assert invoc.exists()
    assert "Fidelity remediation brief" in invoc.read_text()


# --- mode-aware fidelity gate ------------------------------------------------

def test_classify_mode_cited_reads_mode_1():
    assert run._classify_mode_cited({"mode_cited": "Mode 1 (direct port)"}) == "mode-1"
    assert run._classify_mode_cited({"mode_cited": "mode 1"}) == "mode-1"
    assert run._classify_mode_cited({"mode_cited": "direct port"}) == "mode-1"


def test_classify_mode_cited_reads_mode_2():
    assert run._classify_mode_cited({"mode_cited": "Mode 2 (adapted port)"}) == "mode-2"
    assert run._classify_mode_cited({"mode_cited": "Mode 2"}) == "mode-2"
    assert run._classify_mode_cited({"mode_cited": "adapted port"}) == "mode-2"


def test_classify_mode_cited_reads_mode_3():
    assert run._classify_mode_cited({"mode_cited": "Mode 3 (inspired experiment)"}) == "mode-3"
    assert run._classify_mode_cited({"mode_cited": "Mode 3"}) == "mode-3"
    assert run._classify_mode_cited({"mode_cited": "inspired experiment"}) == "mode-3"
    assert run._classify_mode_cited({"mode_cited": "inspired adaptation"}) == "mode-3"


def test_classify_mode_cited_returns_empty_on_missing_or_unknown():
    assert run._classify_mode_cited(None) == ""
    assert run._classify_mode_cited({}) == ""
    assert run._classify_mode_cited({"mode_cited": ""}) == ""
    assert run._classify_mode_cited({"mode_cited": "something else"}) == ""


def test_build_fidelity_audit_prompt_mode_2_injects_substitutions():
    """Mode 2 fidelity prompt should list the substitutions so Claude
    treats those deltas as defensible, not needs-judgment."""
    prompt = run._build_fidelity_audit_prompt(
        pr_title="Test", pr_body="body", pr_diff="diff",
        arxiv_id="2606.27025", reference_url="https://github.com/x/y",
        reference_root=Path("/tmp/ref"),
        mode="mode-2",
        substitutions=["paper's learned MI estimator replaced by vocab-overlap proxy"],
    )
    assert "Mode 2" in prompt
    assert "vocab-overlap proxy" in prompt
    assert "defensible" in prompt


def test_build_fidelity_audit_prompt_scoped_out_lists_deferred_items():
    """Scoped-out items should appear in the prompt so the gate downgrades
    them from needs-judgment to deferred."""
    prompt = run._build_fidelity_audit_prompt(
        pr_title="Test", pr_body="body", pr_diff="diff",
        arxiv_id="2606.27025", reference_url="https://github.com/x/y",
        reference_root=Path("/tmp/ref"),
        mode="mode-1",
        scoped_out=["adaptive-decay coefficient tuning (deferred as follow-up)"],
    )
    assert "Deliberately scoped out" in prompt
    assert "adaptive-decay" in prompt
    assert "deferred" in prompt


def test_build_fidelity_audit_prompt_mode_1_no_mode_guidance_when_no_scoped():
    """Mode 1 with no scoped_out and no substitutions produces the original prompt shape."""
    prompt = run._build_fidelity_audit_prompt(
        pr_title="Test", pr_body="body", pr_diff="diff",
        arxiv_id="x", reference_url="u", reference_root=Path("/tmp/ref"),
        mode="mode-1", substitutions=[], scoped_out=[],
    )
    assert "Mode 2" not in prompt
    assert "Deliberately scoped out" not in prompt


def test_pre_pr_fidelity_routes_mode_3_to_insight_check(tmp_path):
    """Mode 3 self-review skips reference-clone and runs insight-preservation."""
    rec = _make_rec()
    target = _make_target()
    review = {
        "mode_cited": "Mode 3 (inspired experiment)",
        "reframed_insight": "value overestimation → SAC critic-ensemble spread",
        "honest_summary": "Inspired adaptation.",
        "delivered": ["per-token weight from critic spread"],
    }
    fake_diff = "diff --git a/x b/x\n+new"
    fake_matrix = {
        "summary": "insight preserved",
        "needs_judgment": False,
        "items": [{"name": "insight", "status": "covered",
                   "draft_location": "x", "reference_location": None,
                   "deviation_class": None, "rationale": "yes"}],
        "insight_check": {
            "docstring_cites_paper": "pass",
            "docstring_frames_as_inspired": "pass",
            "code_embodies_insight": "pass",
        },
    }
    with patch.object(run, "_local_git_diff", return_value=fake_diff), \
         patch.object(run, "_run_claude_oneshot",
                      return_value=(True, __import__("json").dumps(fake_matrix))), \
         patch.object(run, "_extract_reference_url_from_pr_body") as ref_url_mock:
        verdict = run._run_pre_pr_fidelity_check(
            rec, target, tmp_path, "title", "body", "main",
            self_review=review,
        )
    # Reference URL should NEVER be looked up on Mode-3.
    assert not ref_url_mock.called
    assert verdict["status"] == "pre_pr_fidelity_mode3_insight_preserved"
    assert verdict["needs_judgment"] is False
    assert verdict["mode_cited"] == "mode-3"


def test_pre_pr_fidelity_mode_3_flags_needs_judgment_when_insight_fails(tmp_path):
    """Insight-check failures escalate to needs_judgment even if item list looks clean."""
    rec = _make_rec()
    target = _make_target()
    review = {
        "mode_cited": "Mode 3",
        "reframed_insight": "some insight",
        "honest_summary": "s",
        "delivered": [],
    }
    fake_matrix = {
        "summary": "code doesn't embody insight",
        "needs_judgment": False,
        "items": [],
        "insight_check": {
            "docstring_cites_paper": "pass",
            "docstring_frames_as_inspired": "pass",
            "code_embodies_insight": "fail",
        },
    }
    with patch.object(run, "_local_git_diff", return_value="diff"), \
         patch.object(run, "_run_claude_oneshot",
                      return_value=(True, __import__("json").dumps(fake_matrix))):
        verdict = run._run_pre_pr_fidelity_check(
            rec, target, tmp_path, "title", "body", "main",
            self_review=review,
        )
    assert verdict["status"] == "pre_pr_fidelity_mode3_needs_judgment"
    assert verdict["needs_judgment"] is True


def test_pre_pr_fidelity_mode_3_skips_when_no_reframed_insight(tmp_path):
    """When Claude omits reframed_insight, skip cleanly rather than falsely pass."""
    rec = _make_rec()
    target = _make_target()
    review = {"mode_cited": "Mode 3", "reframed_insight": ""}
    verdict = run._run_pre_pr_fidelity_check(
        rec, target, tmp_path, "title", "body", "main",
        self_review=review,
    )
    assert verdict["status"] == "pre_pr_fidelity_mode3_skipped_no_insight"


def test_pre_pr_fidelity_mode_defaults_to_mode_1_when_review_missing(tmp_path):
    """Pre-v1.7.14 self-reviews without mode_cited still work — assume Mode 1."""
    rec = _make_rec()
    target = _make_target()
    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("x", "")):
        verdict = run._run_pre_pr_fidelity_check(
            rec, target, tmp_path, "title", "body", "main",
            self_review={"delivered": ["x"]},  # no mode_cited
        )
    assert verdict["mode_cited"] == "mode-1"


def test_render_coverage_matrix_insight_anchor():
    """Insight-anchor rendering explains Mode-3 shape to the reviewer."""
    matrix = {
        "summary": "insight preserved",
        "needs_judgment": False,
        "items": [{"name": "x", "status": "covered", "draft_location": "y",
                   "reference_location": None, "rationale": "ok"}],
    }
    md = run._render_coverage_matrix(matrix, audit_anchor="insight")
    assert "Mode 3" in md
    assert "insight" in md.lower()


# --- Remediation-pass mode-context regression ------------------------------
#
# process_target runs _run_pre_pr_fidelity_check twice when the first pass
# flags fabrication: once before the patch attempt (with self_review), and
# once after the patch (was: without self_review, hence Mode-1 default).
# The second call MUST pass the same self_review as the first so Mode-2
# refinements' substitutions + scope-outs stay honored on the remediation
# pass — otherwise the fidelity gate systematically over-flags legitimate
# Mode-2 diffs on remediation and downgrades runs that should have shipped.


def test_classify_mode_cited_defaults_to_none_on_missing_self_review():
    """No self_review → no mode signal, downstream defaults to mode-1.
    This is the bug's blast radius: without self_review, every downstream
    Mode-2 substitution appears as "fabrication" instead of "defensible"."""
    assert not run._classify_mode_cited(None)  # falsy — empty str / None
    assert not run._classify_mode_cited({})


def test_classify_mode_cited_reads_mode_2_from_self_review():
    """With a Mode-2 self_review present, downstream evaluates as Mode 2
    and honors substitutions + scoped_out annotations."""
    self_review = {
        "mode_cited": "Mode 2 (adapted port)",
        "substitutions": ["paper's trainer-level → reward-level shaping"],
        "scoped_out": ["trainer-level GRPO advantage computation"],
    }
    assert run._classify_mode_cited(self_review) == "mode-2"


def test_process_target_source_passes_self_review_to_both_fidelity_calls():
    """Both `_run_pre_pr_fidelity_check` calls inside `process_target` must
    receive `self_review=review` — otherwise the remediation pass defaults
    to Mode 1 and over-flags Mode-2 substitutions.

    Source-code regression check: guards against dropping the kwarg on the
    second call site during future refactors. Cheaper than mocking the
    1000+-line process_target flow end-to-end while still catching the
    exact regression class this test file names.
    """
    src_path = Path(__file__).resolve().parent.parent / "src" / "run.py"
    text = src_path.read_text()
    # Find every _run_pre_pr_fidelity_check invocation site (both call sites
    # in process_target) — each must have `self_review=` in its argument list.
    # We look at the call-site window (up to the closing paren) rather than
    # the whole function to keep the check narrow.
    import re
    call_windows = re.findall(
        r"_run_pre_pr_fidelity_check\s*\((?:[^()]|\([^()]*\))*?\)",
        text,
    )
    # Filter to invocation sites (not the def itself). The def has arg names
    # like `rec: "Recommendation"` — invocations have kwargs or bare rec.
    invocations = [
        w for w in call_windows if not w.startswith("_run_pre_pr_fidelity_check(\n    rec: ")
    ]
    assert len(invocations) >= 2, (
        f"expected ≥ 2 invocation sites in process_target; got {len(invocations)}"
    )
    for i, window in enumerate(invocations):
        assert "self_review=" in window, (
            f"invocation {i} at {window[:80]!r} is missing self_review= — "
            "the remediation-pass fidelity check must inherit the coding session's "
            "mode context from the first-pass check. Bug: without self_review, "
            "the check defaults to Mode 1 with no substitutions honored."
        )


# --- reference-impl confidence scoring (Option A + B) -----------------------
#
# Motivating case: paper 2503.14432v2 (PLAY2PROMPT) had a Remyx catalog
# reference URL of `microsoft/JARVIS`, but that repo self-identifies with
# arxiv 2303.17580 (HuggingGPT). Comparing PLAY2PROMPT code against
# HuggingGPT flagged every legitimate feature as fabrication and triggered
# a false-positive `skipped_fidelity_fabrication_after_patch`.

def test_arxiv_bare_id_strips_v_suffix():
    assert run._arxiv_bare_id("2503.14432v2") == "2503.14432"
    assert run._arxiv_bare_id("2606.30560v1") == "2606.30560"
    assert run._arxiv_bare_id("  2503.14432V3  ") == "2503.14432"
    assert run._arxiv_bare_id("2503.14432") == "2503.14432"
    assert run._arxiv_bare_id("") == ""
    assert run._arxiv_bare_id(None) == ""


def test_arxiv_id_signals_in_text_finds_bare_and_v_suffixed_forms():
    text = "Reference implementation of 2503.14432 (accepted at NAACL)."
    assert run._arxiv_id_signals_in_text(text, "2503.14432v2") is True

    text2 = "See paper at arxiv.org/abs/2503.14432v2 for details."
    assert run._arxiv_id_signals_in_text(text2, "2503.14432") is True

    text3 = "https://arxiv.org/pdf/2503.14432 is the preprint URL."
    assert run._arxiv_id_signals_in_text(text3, "2503.14432v2") is True


def test_arxiv_id_signals_in_text_ignores_unrelated_ids():
    text = "See our paper arxiv:2303.17580 (JARVIS/HuggingGPT)."
    assert run._arxiv_id_signals_in_text(text, "2503.14432v2") is False


def test_arxiv_id_signals_in_text_returns_false_on_empty():
    assert run._arxiv_id_signals_in_text("", "2503.14432") is False
    assert run._arxiv_id_signals_in_text("some text", "") is False
    assert run._arxiv_id_signals_in_text(None, "2503.14432") is False


def _write_ref_repo(root: Path, *, readme: str = "", citation: str = "",
                    py_files: dict[str, str] | None = None) -> Path:
    """Build a fake reference-repo directory for confidence-scoring tests."""
    root.mkdir(parents=True, exist_ok=True)
    if readme:
        (root / "README.md").write_text(readme, encoding="utf-8")
    if citation:
        (root / "CITATION.cff").write_text(citation, encoding="utf-8")
    for name, body in (py_files or {}).items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


def test_score_reference_confidence_high_on_readme_arxiv(tmp_path):
    ref = _write_ref_repo(
        tmp_path / "ref",
        readme="# PLAY2PROMPT\n\n[arXiv:2503.14432](https://arxiv.org/abs/2503.14432)",
    )
    tier, signals = run._score_reference_confidence(
        ref, "2503.14432v2", "PLAY2PROMPT: Zero-shot Tool Instruction Optimization",
    )
    assert tier == "high"
    assert signals["readme_arxiv_id"] is True


def test_score_reference_confidence_high_on_citation_cff(tmp_path):
    ref = _write_ref_repo(
        tmp_path / "ref",
        readme="# Unrelated title",
        citation="cff-version: 1.2.0\nidentifiers:\n  - value: '2503.14432'\n    type: other\n",
    )
    tier, signals = run._score_reference_confidence(
        ref, "2503.14432v2", "some paper title",
    )
    assert tier == "high"
    assert signals["citation_arxiv_id"] is True


def test_score_reference_confidence_high_on_code_file_arxiv_mention(tmp_path):
    ref = _write_ref_repo(
        tmp_path / "ref",
        readme="# A repo with no citation",
        py_files={
            "src/main.py": "# Implementation of arxiv 2503.14432v2\n" + ("x = 1\n" * 200),
        },
    )
    tier, signals = run._score_reference_confidence(
        ref, "2503.14432v2", "paper title",
    )
    assert tier == "high"
    assert signals["code_arxiv_id"] is True


def test_score_reference_confidence_medium_on_title_tokens_only(tmp_path):
    ref = _write_ref_repo(
        tmp_path / "ref",
        readme=(
            "# Zero-shot Tool Instruction Optimization\n\n"
            "This repository provides zero-shot instruction optimization for tool play."
        ),
    )
    tier, signals = run._score_reference_confidence(
        ref, "2503.14432v2", "PLAY2PROMPT: Zero-shot Tool Instruction Optimization for LLM Agents via Tool Play",
    )
    assert tier == "medium"
    assert signals["readme_title_tokens"] is True
    assert signals["title_tokens_matched"] >= 3


def test_score_reference_confidence_low_on_jarvis_case(tmp_path):
    # The exact regression: JARVIS README self-identifies with 2303.17580,
    # not 2503.14432. Our sanity check must return "low" here.
    ref = _write_ref_repo(
        tmp_path / "ref",
        readme=(
            "# JARVIS\n\n"
            "[arXiv:2303.17580](https://arxiv.org/abs/2303.17580)\n\n"
            "The mission of JARVIS is to explore AGI and deliver research."
        ),
    )
    tier, signals = run._score_reference_confidence(
        ref, "2503.14432v2", "PLAY2PROMPT: Zero-shot Tool Instruction Optimization for LLM Agents via Tool Play",
    )
    assert tier == "low"
    assert signals["readme_arxiv_id"] is False
    assert signals["readme_title_tokens"] is False


def test_score_reference_confidence_low_on_missing_dir(tmp_path):
    tier, signals = run._score_reference_confidence(
        tmp_path / "does-not-exist", "2503.14432v2", "some title",
    )
    assert tier == "low"


def test_score_reference_confidence_low_on_empty_arxiv_id(tmp_path):
    ref = _write_ref_repo(tmp_path / "ref", readme="anything")
    tier, _ = run._score_reference_confidence(ref, "", "title")
    assert tier == "low"


def test_score_reference_confidence_stopwords_do_not_produce_medium(tmp_path):
    # A README that just uses generic ML vocabulary should NOT hit the
    # ≥3 title-token threshold — stopwords are filtered out first.
    ref = _write_ref_repo(
        tmp_path / "ref",
        readme="# A large language model framework paper with novel approach",
    )
    tier, _ = run._score_reference_confidence(
        ref, "2503.14432", "Large Language Model Framework Paper With Novel Approach",
    )
    assert tier == "low"


# --- _sniff_reference_confidence_remote (Option B) --------------------------

def _fake_urlopen(readme_bytes: bytes, status: int = 200):
    """Build a fake urllib.urlopen context manager returning the given bytes."""
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return readme_bytes
    return _Resp()


def test_sniff_reference_confidence_remote_high_on_arxiv_id():
    readme = b"# PLAY2PROMPT\n\nSee arxiv 2503.14432 for the paper."
    with patch.object(run.urllib.request, "urlopen", return_value=_fake_urlopen(readme)):
        tier, signals = run._sniff_reference_confidence_remote(
            "https://github.com/somebody/play2prompt",
            "2503.14432v2",
            "PLAY2PROMPT: Zero-shot Tool Instruction Optimization",
        )
    assert tier == "high"
    assert signals["readme_arxiv_id"] is True


def test_sniff_reference_confidence_remote_medium_on_title_tokens():
    readme = b"# Zero-shot Tool Instruction Optimization\n\nRepo for zero-shot optimization tooling."
    with patch.object(run.urllib.request, "urlopen", return_value=_fake_urlopen(readme)):
        tier, signals = run._sniff_reference_confidence_remote(
            "https://github.com/some/repo",
            "2503.14432v2",
            "PLAY2PROMPT: Zero-shot Tool Instruction Optimization for LLM Agents via Tool Play",
        )
    assert tier == "medium"
    assert signals["readme_title_tokens"] is True


def test_sniff_reference_confidence_remote_low_on_unrelated_readme():
    readme = b"# JARVIS\n\n[arXiv:2303.17580]\n\nMission of JARVIS: explore AGI."
    with patch.object(run.urllib.request, "urlopen", return_value=_fake_urlopen(readme)):
        tier, _ = run._sniff_reference_confidence_remote(
            "https://github.com/microsoft/JARVIS",
            "2503.14432v2",
            "PLAY2PROMPT: Zero-shot Tool Instruction Optimization for LLM Agents via Tool Play",
        )
    assert tier == "low"


def test_sniff_reference_confidence_remote_unknown_on_bad_url():
    tier, _ = run._sniff_reference_confidence_remote(
        "not a url at all", "2503.14432", "title",
    )
    assert tier == "unknown"


def test_sniff_reference_confidence_remote_unknown_on_network_error():
    import urllib.error
    with patch.object(run.urllib.request, "urlopen",
                      side_effect=urllib.error.URLError("network down")):
        tier, _ = run._sniff_reference_confidence_remote(
            "https://github.com/some/repo", "2503.14432", "title",
        )
    assert tier == "unknown"


def test_sniff_reference_confidence_remote_strips_git_suffix():
    readme = b"# repo\nSee 2503.14432 for the paper."
    captured_url: dict = {}
    def fake_urlopen(req, timeout=None):
        captured_url["url"] = req.full_url
        return _fake_urlopen(readme)
    with patch.object(run.urllib.request, "urlopen", side_effect=fake_urlopen):
        run._sniff_reference_confidence_remote(
            "https://github.com/some/repo.git", "2503.14432", "title",
        )
    # .git suffix stripped so the /readme endpoint resolves
    assert "some/repo/readme" in captured_url["url"]
    assert ".git" not in captured_url["url"].split("/repos/")[1].split("/")[1]


# --- _run_pre_pr_fidelity_check integration ---------------------------------

def test_pre_pr_fidelity_soft_skips_on_low_confidence_reference(tmp_path):
    """The JARVIS regression: low-confidence reference triggers a soft-skip
    with `pre_pr_fidelity_reference_mismatch` and does NOT invoke the
    expensive fidelity Claude one-shot."""
    workdir = tmp_path / "wd"; workdir.mkdir()
    ref_dir = tmp_path / "wd/reference"; ref_dir.mkdir()
    (ref_dir / "README.md").write_text(
        "# JARVIS\n\n[arXiv:2303.17580]\n\nMission of JARVIS: explore AGI.\n",
        encoding="utf-8",
    )
    rec = _make_rec("2503.14432v2")
    rec.paper_title = "PLAY2PROMPT: Zero-shot Tool Instruction Optimization for LLM Agents via Tool Play"

    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("", "https://github.com/microsoft/JARVIS")), \
         patch.object(run, "_clone_reference_repo",
                      return_value=(True, ref_dir, "")), \
         patch.object(run, "_run_claude_oneshot") as claude_mock:
        verdict = run._run_pre_pr_fidelity_check(
            rec, _make_target(), workdir, "PR title", "PR body",
            base_branch="main", self_review={"mode_cited": "Mode 2 (adapted port)"},
        )

    assert verdict["status"] == "pre_pr_fidelity_reference_mismatch"
    assert verdict["reference_confidence"] == "low"
    assert verdict["needs_judgment"] is False
    # Critical: Claude one-shot must NOT be invoked when reference is low-confidence
    claude_mock.assert_not_called()


def test_pre_pr_fidelity_advisory_on_medium_confidence_reference(tmp_path):
    """Medium-confidence reference: fidelity runs but reports advisory-only.
    Even if the Claude one-shot flags items, needs_judgment must stay False
    so publication proceeds."""
    workdir = tmp_path / "wd"; workdir.mkdir()
    ref_dir = tmp_path / "wd/reference"; ref_dir.mkdir()
    (ref_dir / "README.md").write_text(
        "# Zero-shot Tool Instruction Optimization\n\n"
        "Repository for zero-shot instruction optimization on tool play.",
        encoding="utf-8",
    )
    rec = _make_rec("2503.14432v2")
    rec.paper_title = "PLAY2PROMPT: Zero-shot Tool Instruction Optimization for LLM Agents via Tool Play"

    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("", "https://github.com/some/repo")), \
         patch.object(run, "_clone_reference_repo",
                      return_value=(True, ref_dir, "")), \
         patch.object(run, "_local_git_diff", return_value="diff --git a/x b/x\n+a\n"), \
         patch.object(run, "_run_claude_oneshot",
                      return_value=(True, '{"items":[{"status":"deviation","description":"x"}],"needs_judgment":true}')):
        verdict = run._run_pre_pr_fidelity_check(
            rec, _make_target(), workdir, "PR title", "PR body",
            base_branch="main", self_review={"mode_cited": "Mode 2 (adapted port)"},
        )

    assert verdict["reference_confidence"] == "medium"
    assert verdict["advisory_only"] is True
    assert verdict["status"] == "pre_pr_fidelity_advisory"
    # Flags surfaced but non-blocking:
    assert verdict["items_count"] == 1
    assert verdict["needs_judgment"] is False


def test_pre_pr_fidelity_full_gate_on_high_confidence_reference(tmp_path):
    """High-confidence reference (arxiv ID in README): fidelity gate runs
    normally and needs_judgment is preserved from the Claude verdict."""
    workdir = tmp_path / "wd"; workdir.mkdir()
    ref_dir = tmp_path / "wd/reference"; ref_dir.mkdir()
    (ref_dir / "README.md").write_text(
        "# PLAY2PROMPT\n\n[arXiv:2503.14432](https://arxiv.org/abs/2503.14432)",
        encoding="utf-8",
    )
    rec = _make_rec("2503.14432v2")
    rec.paper_title = "PLAY2PROMPT"

    with patch.object(run, "_extract_reference_url_from_pr_body",
                      return_value=("", "https://github.com/real/play2prompt")), \
         patch.object(run, "_clone_reference_repo",
                      return_value=(True, ref_dir, "")), \
         patch.object(run, "_local_git_diff", return_value="diff --git a/x b/x\n+a\n"), \
         patch.object(run, "_run_claude_oneshot",
                      return_value=(True, '{"items":[{"status":"deviation","description":"x"}],"needs_judgment":true}')):
        verdict = run._run_pre_pr_fidelity_check(
            rec, _make_target(), workdir, "PR title", "PR body",
            base_branch="main", self_review={"mode_cited": "Mode 2 (adapted port)"},
        )

    assert verdict["reference_confidence"] == "high"
    assert verdict["advisory_only"] is False
    assert verdict["status"] == "pre_pr_fidelity_needs_judgment"
    assert verdict["needs_judgment"] is True
