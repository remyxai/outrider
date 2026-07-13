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


def test_load_fork_repo_intel_returns_dict_on_valid_yaml(tmp_path):
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
    with patch.object(run.subprocess, "run",
                      return_value=_fake_git_show(yaml_body)):
        intel = run._load_fork_repo_intel(tmp_path)
    assert intel is not None
    assert intel["schema_version"] == 1
    assert intel["fork"] == "smellslikeml/ag2"
    assert len(intel["observed_landing_zones"]) == 1


def test_load_fork_repo_intel_returns_none_when_file_absent(tmp_path):
    # git show returns non-zero when the file doesn't exist on origin/main
    with patch.object(run.subprocess, "run",
                      return_value=_fake_git_show("", returncode=128)):
        intel = run._load_fork_repo_intel(tmp_path)
    assert intel is None


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
