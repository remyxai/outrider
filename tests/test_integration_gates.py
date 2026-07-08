"""Tests for the role-based guardrails and the §2 invocation check.

Three behaviours, all introduced together:

1. ALWAYS_BLOCKED is role-based (filename/type), not directory-based, so
   `docker/` is no longer blanket-blocked — a Python stage driver under
   docker/ is editable (it's often the real call site), while Dockerfiles /
   shell scripts / dependency manifests stay blocked wherever they live.

2. check_integration requires INVOCATION: at least one newly-added
   function/method/class must be called from another changed file. An
   import alone no longer counts, and methods bolted onto an existing file
   that nothing calls are rejected (the shape that slipped through before).

3. changed_files uses --untracked-files=all so files inside a brand-new
   directory are seen per-file, not collapsed to the directory.

Run with: pytest tests/ -q
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Target  # noqa: E402


# ── 1. role-based path policy ──────────────────────────────────────────────

ALLOW = [g.format(package="vqasynth") for g in run.DEFAULT_ALLOWLIST_GLOBS]


def _decision(path: str) -> str:
    if run.path_matches_glob(path, run.ALWAYS_BLOCKED):
        return "blocked"
    if run.path_matches_glob(path, ALLOW):
        return "allowed"
    return "rejected"


def test_docker_python_driver_is_editable():
    # The whole point: the call site under docker/ is no longer locked out.
    assert _decision("docker/eval_stage/process_eval.py") == "allowed"


def test_source_anywhere_is_editable():
    assert _decision("vqasynth/benchmarks.py") == "allowed"
    assert _decision("tests/test_x.py") == "allowed"
    assert _decision("scripts/run_thing.py") == "allowed"


def test_build_and_ci_files_blocked_by_role_anywhere():
    for p in [
        "Dockerfile",
        "docker/eval_stage/Dockerfile",
        "docker/base_image/Dockerfile.cpu",
        "docker/eval_stage/entrypoint.sh",
        "run.sh",
        "requirements.txt",
        "docker/eval_stage/requirements.txt",
        "setup.py",
        "pyproject.toml",
        "poetry.lock",
        ".github/workflows/ci.yml",
    ]:
        assert _decision(p) == "blocked", p


def test_github_block_overrides_python_allow():
    # A .py under .github must stay blocked even though *.py is allowlisted.
    assert _decision(".github/scripts/helper.py") == "blocked"


def test_prod_yaml_not_editable_via_allowlist():
    # Not blocked by directory anymore, but not allowlisted either.
    assert _decision("pipelines/spatialvqa.yaml") == "rejected"
    assert _decision("config/settings.yaml") == "rejected"


# ── 2. AST helpers + invocation check ──────────────────────────────────────

def test_public_callables_and_called_names():
    src = (
        "class Runner:\n"
        "    def score(self, x):\n        return x\n"
        "    def _private(self):\n        pass\n"
        "def top():\n    return Runner().score(1)\n"
    )
    assert run._public_callables(src) == {"Runner", "score", "top"}
    assert {"Runner", "score"} <= run._called_names(src)
    assert "_private" not in run._public_callables(src)


def _git(wd, *a):
    subprocess.run(["git", *a], cwd=wd, check=True, capture_output=True)


def _base_repo() -> Path:
    wd = Path(tempfile.mkdtemp())
    _git(wd, "init", "-q")
    _git(wd, "config", "user.email", "a@b.c")
    _git(wd, "config", "user.name", "t")
    (wd / "vqasynth").mkdir()
    (wd / "vqasynth" / "__init__.py").write_text("")
    (wd / "vqasynth" / "benchmarks.py").write_text(
        "class BenchmarkRunner:\n    def score(self, x):\n        return x\n"
    )
    (wd / "tests").mkdir()
    (wd / "tests" / "test_base.py").write_text("def test_base():\n    assert True\n")
    _git(wd, "add", "-A")
    _git(wd, "commit", "-qm", "base")
    return wd


TGT = Target(repo="r/x", interest_id="i")


def test_uncalled_new_method_is_rejected():
    wd = _base_repo()
    bp = wd / "vqasynth" / "benchmarks.py"
    bp.write_text(bp.read_text() + "    def run_spacedg(self, imgs):\n        return imgs\n")
    ok, violations = run.check_integration(wd, TGT, "vqasynth")
    assert not ok
    assert any("nothing calls" in v for v in violations)


def test_test_invocation_satisfies_integration():
    wd = _base_repo()
    bp = wd / "vqasynth" / "benchmarks.py"
    bp.write_text(bp.read_text() + "    def run_spacedg(self, imgs):\n        return imgs\n")
    (wd / "tests" / "test_spacedg.py").write_text(
        "from vqasynth.benchmarks import BenchmarkRunner\n"
        "def test_run():\n    BenchmarkRunner().run_spacedg([1])\n"
    )
    ok, _ = run.check_integration(wd, TGT, "vqasynth")
    assert ok


def test_import_without_call_is_rejected():
    wd = _base_repo()
    (wd / "vqasynth" / "newcap.py").write_text("def enhance(x):\n    return x * 2\n")
    (wd / "vqasynth" / "evaluation.py").write_text(
        "from vqasynth.newcap import enhance  # imported, never called\nVALUE = 1\n"
    )
    ok, violations = run.check_integration(wd, TGT, "vqasynth")
    assert not ok


def test_new_module_called_from_modified_file_passes():
    wd = _base_repo()
    (wd / "vqasynth" / "newcap.py").write_text("def enhance(x):\n    return x * 2\n")
    (wd / "vqasynth" / "evaluation.py").write_text(
        "from vqasynth.newcap import enhance\ndef go(x):\n    return enhance(x)\n"
    )
    ok, _ = run.check_integration(wd, TGT, "vqasynth")
    assert ok


def test_pure_edit_with_no_new_callable_is_not_gated():
    # Editing an existing function body (no new public callable) shouldn't
    # trip the invocation check.
    wd = _base_repo()
    bp = wd / "vqasynth" / "benchmarks.py"
    bp.write_text("class BenchmarkRunner:\n    def score(self, x):\n        return x + 1\n")
    ok, _ = run.check_integration(wd, TGT, "vqasynth")
    assert ok


# ── 2b. large edits to existing files pass check_integration ──────────────
#
# The per-existing-file line-count cap was removed after observation that
# it produced false-negatives on legitimate paper implementations —
# large-but-focused rewrites of trainer loss functions and big test
# additions matching a paper's property-test surface. Scope discipline
# now lives downstream in the convention pass (which uses graded signal
# against the repo's own PR history), not in a hardcoded ceiling at the
# integration gate.


def test_large_additive_edit_to_existing_file_passes():
    """A big additive edit to an existing file passes as long as at least
    one newly-added callable is invoked from another changed file."""
    wd = _base_repo()
    bp = wd / "vqasynth" / "benchmarks.py"
    bp.write_text(
        bp.read_text()
        + "    def calibrated_score(self, x):\n        return x\n"
        + "".join(f"    # padding line {i}\n" for i in range(75))
    )
    (wd / "tests" / "test_new.py").write_text(
        "from vqasynth.benchmarks import BenchmarkRunner\n"
        "def test_c():\n    BenchmarkRunner().calibrated_score(1)\n"
    )
    ok, violations = run.check_integration(wd, TGT, "vqasynth")
    assert ok, f"expected pass, got violations: {violations}"


def test_large_inplace_rewrite_of_existing_file_passes():
    """An in-place rewrite (deleted > 0) that's large is not gated — the
    integration check does not enforce a per-file line ceiling. As long
    as the invocation check is satisfied, big rewrites pass."""
    wd = _base_repo()
    bp = wd / "vqasynth" / "benchmarks.py"
    bp.write_text(
        "class BenchmarkRunner:\n"
        "    def score(self, x):\n"
        + "".join(f"        line_{i} = {i}\n" for i in range(55))
        + "        return sum([line_0, line_1, line_2])\n"
        "    def rewritten(self, x):\n        return x + 1\n"
    )
    (wd / "tests" / "test_x.py").write_text(
        "from vqasynth.benchmarks import BenchmarkRunner\n"
        "def test_c():\n    BenchmarkRunner().rewritten(1)\n"
    )
    ok, violations = run.check_integration(wd, TGT, "vqasynth")
    assert ok, f"expected pass, got violations: {violations}"


# ── 3. changed_files sees files in brand-new directories ───────────────────

def test_changed_files_expands_new_directory():
    wd = _base_repo()
    (wd / "brand_new_dir").mkdir()
    (wd / "brand_new_dir" / "probe.py").write_text("x = 1\n")
    assert "brand_new_dir/probe.py" in run.changed_files(wd)


# ── 4. F7: test-integration gate accepts public-API wiring ─────────────────

def test_test_gate_passes_when_wired_into_existing_module():
    # New capability module exported from the pre-existing __init__.py; the
    # new test only self-tests the new module. The capability IS wired into
    # the package's surface, so the gate should pass (was demoted to Issue).
    wd = _base_repo()
    (wd / "vqasynth" / "bcos_layer.py").write_text("def bcos(x):\n    return x\n")
    (wd / "vqasynth" / "__init__.py").write_text(
        "from vqasynth.bcos_layer import bcos\n"
    )
    (wd / "tests" / "test_bcos.py").write_text(
        "from vqasynth.bcos_layer import bcos\n"
        "def test_b():\n    assert bcos(1) == 1\n"
    )
    ok, _ = run.check_tests_touch_existing_modules(wd, "vqasynth")
    assert ok


def test_test_gate_rejects_orphan_self_test():
    # New module + self-test only, nothing existing imports it → still gated.
    wd = _base_repo()
    (wd / "vqasynth" / "bcos_layer.py").write_text("def bcos(x):\n    return x\n")
    (wd / "tests" / "test_bcos.py").write_text(
        "from vqasynth.bcos_layer import bcos\n"
        "def test_b():\n    assert bcos(1) == 1\n"
    )
    ok, _ = run.check_tests_touch_existing_modules(wd, "vqasynth")
    assert not ok


# ── 5. F6: pytest outcome classification ───────────────────────────────────

def test_self_review_renders_value_first():
    # F10: the PR-body section reads as a contribution, not an apology.
    md = run._render_self_review_section({
        "delivered": ["a scorer wired into eval.py"],
        "scoped_out": ["the trained model (needs a trainer)"],
        "call_site": "eval.py:run",
        "honest_summary": "Delivers the metric.",
    })
    assert "What this PR delivers" in md
    assert "Delivers (from the paper)" in md
    assert "Intentionally out of scope" in md
    assert "Stubbed" not in md and "left out" not in md
    # Orphan flag should not appear when is_orphan is absent or false.
    assert "orphan-shaped" not in md
    # Legacy keys still render via the fallback.
    md2 = run._render_self_review_section({"implemented": ["x"], "stubbed": ["y"]})
    assert "- x" in md2 and "- y" in md2


def test_self_review_surfaces_orphan_flag_when_set():
    """When is_orphan is true the PR body carries a prominent warning so
    the maintainer sees the verdict without the pipeline downgrading the
    PR to an Issue on a boolean flag."""
    md = run._render_self_review_section({
        "delivered": ["EncoderHallucinationDetector metric"],
        "scoped_out": ["training and eval sweep"],
        "call_site": "opik.evaluation.metrics",
        "is_orphan": True,
        "honest_summary": "Library API addition — external callers only.",
    })
    assert "orphan-shaped" in md
    assert "review before merging" in md
    # Still shows the value-first sections above the warning.
    assert "What this PR delivers" in md
    assert "EncoderHallucinationDetector" in md


def test_detect_default_branch():
    # F12: PR base + the commit sanity check must use the repo's real
    # default branch, not a hardcoded "main" (broke master-default repos).
    wd = _base_repo()
    _git(wd, "branch", "-M", "master")
    assert run.detect_default_branch(wd) == "master"
    _git(wd, "branch", "-M", "main")
    assert run.detect_default_branch(wd) == "main"


def test_classify_pytest():
    assert run._classify_pytest(0, "5 passed in 0.1s") == "passed"
    # Missing-dep collection error is an env limitation, not a code failure.
    assert run._classify_pytest(
        2, "E   ModuleNotFoundError: No module named 'torch'\nERROR collecting"
    ) == "unvalidated"
    assert run._classify_pytest(5, "no tests ran") == "unvalidated"
    # Genuine failure → failed.
    assert run._classify_pytest(1, "1 failed, 2 passed\nE  AssertionError") == "failed"
    # A real failure alongside an import error must NOT be masked.
    assert run._classify_pytest(1, "1 failed\nModuleNotFoundError: x") == "failed"


# ── 6. Per-run token/cost accumulation ────────────────────────────────────

def test_cost_accumulation():
    run._reset_run_cost()
    run._record_claude_usage({
        "total_cost_usd": 0.05, "num_turns": 3,
        "usage": {"input_tokens": 1000, "output_tokens": 200,
                  "cache_read_input_tokens": 500},
    })
    run._record_claude_usage({
        "total_cost_usd": 0.01, "num_turns": 1,
        "usage": {"input_tokens": 300, "output_tokens": 50},
    })
    assert abs(run._RUN_COST["cost_usd"] - 0.06) < 1e-9
    assert run._RUN_COST["input_tokens"] == 1300
    assert run._RUN_COST["output_tokens"] == 250
    assert run._RUN_COST["cache_read_input_tokens"] == 500
    assert run._RUN_COST["claude_calls"] == 2
    run._reset_run_cost()
    assert run._RUN_COST["cost_usd"] == 0.0 and run._RUN_COST["claude_calls"] == 0


def test_record_usage_tolerates_missing_fields():
    run._reset_run_cost()
    run._record_claude_usage({})       # no cost / usage keys at all
    assert run._RUN_COST["claude_calls"] == 1
    assert run._RUN_COST["cost_usd"] == 0.0
    assert run._RUN_COST["input_tokens"] == 0
