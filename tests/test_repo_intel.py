"""Tests for the per-fork repo-intel read path.

Covers:
  * ``_load_fork_repo_intel`` — git-show fetch + YAML parse + schema check
  * ``_render_repo_intel_md`` — YAML dict → REPO_INTEL.md markdown
  * ``write_spec_bundle`` integration — REPO_INTEL.md emitted iff
    INPUT_MAINTAIN_STATE=true AND the fork has a valid repo_intel.yaml
  * ``_INVOCATION_MD_TEMPLATE`` — REPO_INTEL.md ref conditionally
    interpolated
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# --- _load_fork_repo_intel --------------------------------------------------

def _fake_git_show(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr="",
    )


def test_load_fork_repo_intel_reads_directly_from_workdir_first(tmp_path):
    # Under actions/checkout@v4 the workdir IS main HEAD — file is on disk directly.
    yaml_body = """\
schema_version: 1
fork: smellslikeml/ag2
observed_landing_zones:
  - path: autogen/beta/tools/
    shape_tags: [library-shape-public-api]
    confirmed_by:
      - {arxiv: "2607.07321v1", mode: "Mode 2", branch: "from-atomic-actions-..."}
rejected_shapes: []
"""
    (tmp_path / ".remyx").mkdir()
    (tmp_path / ".remyx" / "repo_intel.yaml").write_text(yaml_body)
    # git subprocess should NOT be called when direct read succeeds
    with patch.object(run.subprocess, "run", side_effect=AssertionError("git subprocess called")):
        intel = run._load_fork_repo_intel(tmp_path)
    assert intel is not None
    assert intel["schema_version"] == 1
    assert intel["fork"] == "smellslikeml/ag2"
    assert len(intel["observed_landing_zones"]) == 1


def test_load_fork_repo_intel_falls_back_to_git_show_when_file_absent(tmp_path):
    # Filesystem miss → git show fallback (for bare-clone or non-main-HEAD setups)
    yaml_body = "schema_version: 1\nfork: smellslikeml/ag2\n"
    with patch.object(run.subprocess, "run",
                      return_value=_fake_git_show(yaml_body)):
        intel = run._load_fork_repo_intel(tmp_path)
    assert intel is not None
    assert intel["fork"] == "smellslikeml/ag2"


def test_load_fork_repo_intel_returns_none_when_file_absent(tmp_path):
    # git show returns non-zero when the file doesn't exist on origin/main
    with patch.object(run.subprocess, "run",
                      return_value=_fake_git_show("", returncode=128)):
        intel = run._load_fork_repo_intel(tmp_path)
    assert intel is None


def test_load_fork_repo_intel_direct_read_falls_back_when_file_empty(tmp_path):
    # File exists but is empty — should fall back to git show
    (tmp_path / ".remyx").mkdir()
    (tmp_path / ".remyx" / "repo_intel.yaml").write_text("")
    yaml_body = "schema_version: 1\nfork: x\n"
    with patch.object(run.subprocess, "run",
                      return_value=_fake_git_show(yaml_body)):
        intel = run._load_fork_repo_intel(tmp_path)
    assert intel is not None
    assert intel["fork"] == "x"


def test_load_fork_repo_intel_returns_none_on_empty_stdout(tmp_path):
    with patch.object(run.subprocess, "run",
                      return_value=_fake_git_show("")):
        assert run._load_fork_repo_intel(tmp_path) is None


def test_load_fork_repo_intel_returns_none_on_malformed_yaml(tmp_path):
    with patch.object(run.subprocess, "run",
                      return_value=_fake_git_show("this is: not: valid: yaml: because:::")):
        assert run._load_fork_repo_intel(tmp_path) is None


def test_load_fork_repo_intel_returns_none_when_schema_version_wrong(tmp_path):
    yaml_body = "schema_version: 99\nfork: smellslikeml/ag2\n"
    with patch.object(run.subprocess, "run",
                      return_value=_fake_git_show(yaml_body)):
        assert run._load_fork_repo_intel(tmp_path) is None


def test_load_fork_repo_intel_returns_none_when_not_a_mapping(tmp_path):
    # Top-level is a list, not a dict
    with patch.object(run.subprocess, "run",
                      return_value=_fake_git_show("- foo\n- bar\n")):
        assert run._load_fork_repo_intel(tmp_path) is None


def test_load_fork_repo_intel_returns_none_on_git_timeout(tmp_path):
    with patch.object(run.subprocess, "run",
                      side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30)):
        assert run._load_fork_repo_intel(tmp_path) is None


def test_load_fork_repo_intel_returns_none_on_oserror(tmp_path):
    with patch.object(run.subprocess, "run", side_effect=OSError("git missing")):
        assert run._load_fork_repo_intel(tmp_path) is None


def test_load_fork_repo_intel_returns_none_on_missing_workdir():
    assert run._load_fork_repo_intel(Path("/does/not/exist/anywhere")) is None


# --- _render_repo_intel_md --------------------------------------------------

def test_render_repo_intel_md_includes_confirmed_landing_zones():
    intel = {
        "schema_version": 1,
        "fork": "smellslikeml/ag2",
        "observed_landing_zones": [
            {
                "path": "autogen/beta/tools/",
                "shape_tags": ["library-shape-public-api", "decorator-hook"],
                "confirmed_by": [
                    {"arxiv": "2607.07321v1", "mode": "Mode 2",
                     "branch": "from-atomic-actions-...",
                     "call_site_specifics": "public API via __all__ export"},
                ],
            },
        ],
    }
    md = run._render_repo_intel_md(intel)
    assert "autogen/beta/tools/" in md
    assert "library-shape-public-api" in md
    assert "2607.07321v1" in md
    assert "Mode 2" in md
    assert "from-atomic-actions-..." in md
    assert "public API via __all__ export" in md


def test_render_repo_intel_md_includes_rejected_shapes_with_caveats():
    intel = {
        "schema_version": 1,
        "rejected_shapes": [
            {
                "shape_tag": "reranker-decision-layer",
                "reason_code": "no_public_middleware_surface",
                "reason_summary": "reranking operates on inputs from another layer ag2 doesn't natively expose",
                "when_this_penalty_should_NOT_apply": [
                    "candidate proposes adding a public middleware surface",
                    "reranking targets an EXISTING public surface",
                ],
                "observed": [
                    {"arxiv": "2607.06283v1", "timestamp": "2026-07-13"},
                ],
            },
        ],
    }
    md = run._render_repo_intel_md(intel)
    assert "reranker-decision-layer" in md
    assert "reranking operates on inputs from another layer" in md
    assert "candidate proposes adding a public middleware surface" in md
    assert "reranking targets an EXISTING public surface" in md
    assert "2607.06283v1" in md


def test_render_repo_intel_md_includes_coordination_signals():
    intel = {
        "schema_version": 1,
        "coordination_signals": [
            {"source": "https://x.com/pybeebee/status/xyz",
             "topic_tags": ["rlmf", "author-thread"]},
        ],
    }
    md = run._render_repo_intel_md(intel)
    assert "https://x.com/pybeebee/status/xyz" in md
    assert "rlmf" in md


def test_render_repo_intel_md_includes_exploration_budget():
    intel = {"schema_version": 1, "exploration_budget": {"novel_shape_fraction": 0.25}}
    md = run._render_repo_intel_md(intel)
    assert "25%" in md
    assert "novel shapes" in md.lower()


def test_render_repo_intel_md_includes_mode_history():
    intel = {"schema_version": 1,
             "mode_history": {"mode_1_count": 0, "mode_2_count": 2, "mode_3_count": 1}}
    md = run._render_repo_intel_md(intel)
    assert "Mode 2" in md
    assert "Mode 3" in md
    # Numeric totals surfaced
    assert "=2" in md or "= 2" in md


def test_render_repo_intel_md_empty_intel_still_yields_valid_markdown():
    md = run._render_repo_intel_md({"schema_version": 1, "fork": "empty/fork"})
    # Header always present
    assert "Cross-run learning" in md
    assert md.startswith("---")


def test_render_repo_intel_md_handles_pr_anchor_not_branch():
    intel = {
        "schema_version": 1,
        "observed_landing_zones": [
            {"path": "x/", "confirmed_by": [{"arxiv": "1", "mode": "Mode 3", "pr": 14}]},
        ],
    }
    md = run._render_repo_intel_md(intel)
    assert "PR #14" in md


# --- write_spec_bundle integration ------------------------------------------

def _minimal_rec():
    r = MagicMock()
    r.paper_title = "Test paper"
    r.arxiv_id = "0000.00000v1"
    r.tier = "moderate"
    r.relevance_score = 0.8
    r.interest_name = "test"
    r.interest_context = ""
    r.reasoning = "why"
    r.suggested_experiment = "do the thing"
    r.paper_abstract = "abstract"
    r.experiment_history = ""
    return r


def _minimal_target():
    t = MagicMock()
    t.repo = "org/repo"
    return t


def _bundle_setup(workdir: Path):
    """Make workdir look like a cloned repo so write_spec_bundle can run."""
    workdir.mkdir(parents=True, exist_ok=True)


def test_write_spec_bundle_emits_repo_intel_when_maintain_state_on(tmp_path, monkeypatch):
    workdir = tmp_path / "wd"
    _bundle_setup(workdir)

    intel = {
        "schema_version": 1,
        "fork": "smellslikeml/ag2",
        "observed_landing_zones": [
            {"path": "autogen/beta/tools/",
             "shape_tags": ["library-shape-public-api"],
             "confirmed_by": [{"arxiv": "2607.07321v1", "mode": "Mode 2",
                               "branch": "from-atomic-actions-..."}]},
        ],
    }

    monkeypatch.setenv("INPUT_MAINTAIN_STATE", "true")

    with patch.object(run, "_load_fork_repo_intel", return_value=intel), \
         patch.object(run, "_collect_repo_orientation", return_value=""):
        run.write_spec_bundle(workdir, _minimal_target(), _minimal_rec(), package="mypkg")

    repo_intel_path = workdir / run.BUNDLE_DIR_NAME / "REPO_INTEL.md"
    assert repo_intel_path.exists()
    md = repo_intel_path.read_text()
    assert "autogen/beta/tools/" in md
    # INVOCATION.md references REPO_INTEL.md
    inv = (workdir / run.BUNDLE_DIR_NAME / "INVOCATION.md").read_text()
    assert "REPO_INTEL.md" in inv


def test_write_spec_bundle_skips_repo_intel_when_maintain_state_off(tmp_path, monkeypatch):
    workdir = tmp_path / "wd"
    _bundle_setup(workdir)
    monkeypatch.delenv("INPUT_MAINTAIN_STATE", raising=False)
    with patch.object(run, "_load_fork_repo_intel", return_value={"schema_version": 1}), \
         patch.object(run, "_collect_repo_orientation", return_value=""):
        run.write_spec_bundle(workdir, _minimal_target(), _minimal_rec(), package="mypkg")

    assert not (workdir / run.BUNDLE_DIR_NAME / "REPO_INTEL.md").exists()
    inv = (workdir / run.BUNDLE_DIR_NAME / "INVOCATION.md").read_text()
    assert "REPO_INTEL.md" not in inv


def test_write_spec_bundle_skips_repo_intel_when_load_returns_none(tmp_path, monkeypatch):
    workdir = tmp_path / "wd"
    _bundle_setup(workdir)
    monkeypatch.setenv("INPUT_MAINTAIN_STATE", "true")
    with patch.object(run, "_load_fork_repo_intel", return_value=None), \
         patch.object(run, "_collect_repo_orientation", return_value=""):
        run.write_spec_bundle(workdir, _minimal_target(), _minimal_rec(), package="mypkg")
    # No REPO_INTEL.md written; INVOCATION.md doesn't reference it
    assert not (workdir / run.BUNDLE_DIR_NAME / "REPO_INTEL.md").exists()
    inv = (workdir / run.BUNDLE_DIR_NAME / "INVOCATION.md").read_text()
    assert "REPO_INTEL.md" not in inv


def test_write_spec_bundle_accepts_all_maintain_state_truthy_values(tmp_path, monkeypatch):
    for val in ("true", "TRUE", "1", "yes", "YES"):
        workdir = tmp_path / f"wd-{val}"
        _bundle_setup(workdir)
        monkeypatch.setenv("INPUT_MAINTAIN_STATE", val)
        with patch.object(run, "_load_fork_repo_intel", return_value={"schema_version": 1, "fork": "x"}), \
             patch.object(run, "_collect_repo_orientation", return_value=""):
            run.write_spec_bundle(workdir, _minimal_target(), _minimal_rec(), package="p")
        assert (workdir / run.BUNDLE_DIR_NAME / "REPO_INTEL.md").exists(), (
            f"maintain-state={val!r} should have produced REPO_INTEL.md"
        )


# --- _render_repo_intel_for_selection --------------------------------------

def test_render_repo_intel_for_selection_returns_empty_when_none():
    assert run._render_repo_intel_for_selection(None) == ""
    assert run._render_repo_intel_for_selection({}) == ""


def test_render_repo_intel_for_selection_includes_confirmed_zones():
    intel = {
        "observed_landing_zones": [
            {"path": "autogen/beta/tools/",
             "shape_tags": ["library-shape-public-api", "decorator-hook"],
             "confirmed_by": [
                 {"arxiv": "2607.07321v1", "mode": "Mode 2"},
                 {"arxiv": "2503.14432v2", "mode": "Mode 3"},
             ]},
        ],
    }
    out = run._render_repo_intel_for_selection(intel)
    assert "autogen/beta/tools/" in out
    assert "library-shape-public-api" in out
    assert "2607.07321v1" in out
    assert "PRIORS (not filters)" in out


def test_render_repo_intel_for_selection_includes_rejected_with_caveats():
    intel = {
        "rejected_shapes": [
            {"shape_tag": "reranker-decision-layer",
             "reason_code": "no_public_middleware_surface",
             "reason_summary": "reranking operates on inputs from another layer",
             "when_this_penalty_should_NOT_apply": [
                 "candidate proposes to ADD a public middleware surface",
             ]},
        ],
    }
    out = run._render_repo_intel_for_selection(intel)
    assert "reranker-decision-layer" in out
    assert "reranking operates on inputs" in out
    assert "Caveat: candidate proposes to ADD" in out


def test_render_repo_intel_for_selection_includes_exploration_budget():
    intel = {"exploration_budget": {"novel_shape_fraction": 0.25}}
    out = run._render_repo_intel_for_selection(intel)
    assert "25%" in out
    assert "novel shapes" in out
    assert "don't hard-filter" in out


# --- select_recommendation integration --------------------------------------

def test_selection_prompt_threads_repo_intel_when_maintain_state_on(tmp_path, monkeypatch):
    """Selection prompt gets __REPO_INTEL__ populated when maintain-state=true
    AND fork has repo_intel.yaml. Verifies the wire-in from _load_fork_repo_intel
    into the selection prompt template."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / ".remyx").mkdir()
    (workdir / ".remyx" / "repo_intel.yaml").write_text(
        "schema_version: 1\nfork: smellslikeml/ag2\n"
        "observed_landing_zones:\n"
        "  - path: autogen/beta/tools/\n"
        "    shape_tags: [library-shape-public-api]\n"
        "    confirmed_by:\n"
        "      - {arxiv: \"2607.07321v1\", mode: \"Mode 2\"}\n"
    )
    monkeypatch.setenv("INPUT_MAINTAIN_STATE", "true")

    candidates = [
        MagicMock(paper_title="a", arxiv_id="1", relevance_score=0.9,
                  paper_abstract="", reasoning="", tier="high"),
        MagicMock(paper_title="b", arxiv_id="2", relevance_score=0.85,
                  paper_abstract="", reasoning="", tier="high"),
    ]

    captured: dict = {}

    def fake_streaming(wd, prompt, timeout, max_turns=25):
        captured["prompt"] = prompt
        # Return a valid JSON response so selection completes
        return True, '{"chosen_index": 0, "reasoning": "test"}', []

    target = MagicMock()
    target.repo = "org/repo"
    target.claude_timeout_s = 480
    target.pin_arxiv = ""
    target.search_method = ""

    with patch.object(run, "_repo_layout_manifest", return_value="(layout)"), \
         patch.object(run, "_render_candidate_brief", return_value="(candidates)"), \
         patch.object(run, "_run_claude_oneshot_streaming", side_effect=fake_streaming):
        run.select_recommendation(workdir, "pkg", candidates, target=target)

    prompt = captured["prompt"]
    assert "autogen/beta/tools/" in prompt, "repo_intel confirmed-zone should thread into prompt"
    assert "library-shape-public-api" in prompt
    assert "arxiv:2607.07321v1" in prompt
    assert "PRIORS (not filters)" in prompt


def test_selection_prompt_omits_repo_intel_when_maintain_state_off(tmp_path, monkeypatch):
    workdir = tmp_path / "wd"
    workdir.mkdir()
    monkeypatch.delenv("INPUT_MAINTAIN_STATE", raising=False)

    candidates = [
        MagicMock(paper_title="a", arxiv_id="1", relevance_score=0.9,
                  paper_abstract="", reasoning="", tier="high"),
        MagicMock(paper_title="b", arxiv_id="2", relevance_score=0.85,
                  paper_abstract="", reasoning="", tier="high"),
    ]
    captured: dict = {}

    def fake_streaming(wd, prompt, timeout, max_turns=25):
        captured["prompt"] = prompt
        return True, '{"chosen_index": 0, "reasoning": "test"}', []

    target = MagicMock()
    target.repo = "org/repo"
    target.claude_timeout_s = 480
    target.pin_arxiv = ""
    target.search_method = ""

    with patch.object(run, "_repo_layout_manifest", return_value="(layout)"), \
         patch.object(run, "_render_candidate_brief", return_value="(candidates)"), \
         patch.object(run, "_load_fork_repo_intel") as load_mock, \
         patch.object(run, "_run_claude_oneshot_streaming", side_effect=fake_streaming):
        run.select_recommendation(workdir, "pkg", candidates, target=target)
    prompt = captured["prompt"]
    # No repo_intel block should appear
    assert "PRIORS (not filters)" not in prompt
    # Loader should not have been called
    load_mock.assert_not_called()
    # Template placeholder should be fully substituted (no leftover token)
    assert "__REPO_INTEL__" not in prompt


def test_selection_prompt_omits_repo_intel_when_maintain_state_on_but_no_yaml(tmp_path, monkeypatch):
    """maintain-state=true but no .remyx/repo_intel.yaml → intel_block stays empty,
    selection prompt runs unchanged."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    monkeypatch.setenv("INPUT_MAINTAIN_STATE", "true")

    candidates = [
        MagicMock(paper_title="a", arxiv_id="1", relevance_score=0.9,
                  paper_abstract="", reasoning="", tier="high"),
        MagicMock(paper_title="b", arxiv_id="2", relevance_score=0.85,
                  paper_abstract="", reasoning="", tier="high"),
    ]
    captured: dict = {}
    def fake_streaming(wd, prompt, timeout, max_turns=25):
        captured["prompt"] = prompt
        return True, '{"chosen_index": 0, "reasoning": "test"}', []

    target = MagicMock()
    target.repo = "org/repo"
    target.claude_timeout_s = 480

    with patch.object(run, "_repo_layout_manifest", return_value="(layout)"), \
         patch.object(run, "_render_candidate_brief", return_value="(candidates)"), \
         patch.object(run, "_run_claude_oneshot_streaming", side_effect=fake_streaming):
        run.select_recommendation(workdir, "pkg", candidates, target=target)
    prompt = captured["prompt"]
    assert "PRIORS (not filters)" not in prompt
    assert "__REPO_INTEL__" not in prompt


def test_write_spec_bundle_treats_falsy_maintain_state_as_off(tmp_path, monkeypatch):
    for val in ("false", "FALSE", "0", "no", ""):
        workdir = tmp_path / f"wd-off-{val or 'empty'}"
        _bundle_setup(workdir)
        monkeypatch.setenv("INPUT_MAINTAIN_STATE", val)
        with patch.object(run, "_load_fork_repo_intel", return_value={"schema_version": 1}), \
             patch.object(run, "_collect_repo_orientation", return_value=""):
            run.write_spec_bundle(workdir, _minimal_target(), _minimal_rec(), package="p")
        assert not (workdir / run.BUNDLE_DIR_NAME / "REPO_INTEL.md").exists(), (
            f"maintain-state={val!r} should NOT have produced REPO_INTEL.md"
        )
