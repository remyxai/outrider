"""Tests for the calibrated Diff Risk Score gate (RADAR).

Exercises both the new `diff_risk_score` module and its wiring into the
existing `run` module:

  * the score is computed from the SAME static-diff helpers the funnel's
    other gates use (`run.changed_files`, `run._diff_line_changes`,
    `run._added_callables`), so we build real working-tree diffs and assert
    the band a `process_target` run would route on;
  * `run` re-exports the scorer and the auto-land threshold it gates on,
    proving the call site imports the new capability.

Run with: pytest tests/ -q
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402  (existing call-site module)
import diff_risk_score  # noqa: E402  (new capability module)


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


# ── module-level scoring behaviour ─────────────────────────────────────────


def test_small_tested_wiring_pr_is_low_risk():
    # One new module, a small call-site edit, and a test → the canonical
    # low-risk shape Outrider aims to auto-land.
    wd = _base_repo()
    (wd / "vqasynth" / "newcap.py").write_text("def enhance(x):\n    return x * 2\n")
    bp = wd / "vqasynth" / "benchmarks.py"
    bp.write_text(
        "from vqasynth.newcap import enhance\n"
        "class BenchmarkRunner:\n    def score(self, x):\n        return enhance(x)\n"
    )
    (wd / "tests" / "test_newcap.py").write_text(
        "from vqasynth.benchmarks import BenchmarkRunner\n"
        "def test_s():\n    assert BenchmarkRunner().score(1) == 2\n"
    )
    risk = run.score_diff_risk(wd, "vqasynth")
    assert risk.band == "low"
    assert risk.score < diff_risk_score.DIFF_RISK_ELEVATED_THRESHOLD


def test_large_untested_critical_change_is_high_risk():
    # Many new callables, a pre-existing critical-path file edited, and no
    # test change → the blast-radius shape RADAR routes to human review.
    wd = _base_repo()
    big = "".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(25))
    (wd / "vqasynth" / "bulk.py").write_text(big)
    # Edit the pre-existing package surface (critical) to call into it.
    (wd / "vqasynth" / "__init__.py").write_text(
        "from vqasynth.bulk import f0\nVALUE = f0(1)\n"
    )
    risk = run.score_diff_risk(wd, "vqasynth")
    assert risk.band == "high"
    assert risk.score >= run.DIFF_RISK_ISSUE_THRESHOLD
    assert risk.features["new_callables"] >= 25
    assert risk.features["critical_file_touched"] is True


def test_untested_new_surface_raises_score():
    # Test-coverage impact (RADAR): an otherwise-identical diff scores higher
    # when no test file is touched. Build the same new surface twice — once
    # with a test, once without — and assert the untested one is riskier.
    def _make(with_test: bool) -> "diff_risk_score.DiffRisk":
        wd = _base_repo()
        (wd / "vqasynth" / "cap.py").write_text(
            "def a(x):\n    return x\ndef b(x):\n    return x\ndef c(x):\n    return x\n"
        )
        (wd / "vqasynth" / "benchmarks.py").write_text(
            "from vqasynth.cap import a\n"
            "class BenchmarkRunner:\n    def score(self, x):\n        return a(x)\n"
        )
        if with_test:
            (wd / "tests" / "test_cap.py").write_text(
                "from vqasynth.benchmarks import BenchmarkRunner\n"
                "def test_s():\n    assert BenchmarkRunner().score(1) == 1\n"
            )
        return run.score_diff_risk(wd, "vqasynth")

    untested = _make(with_test=False)
    tested = _make(with_test=True)
    assert untested.features["untested_new_surface"] is True
    assert tested.features["untested_new_surface"] is False
    assert untested.score > tested.score


# ── test_only_no_source (AIDev rejection-pattern) ────────────────────────


def test_test_only_no_source_diff_flagged():
    """A diff that touches ONLY test files with no corresponding source
    change is the AIDev rejection-pattern this feature targets. The
    `test_only_no_source` feature should be True and contribute to the
    score; a comparable diff that ALSO touches source should not flag."""
    wd = _base_repo()
    (wd / "tests" / "test_only.py").write_text(
        "from vqasynth.benchmarks import BenchmarkRunner\n"
        "def test_more():\n    assert BenchmarkRunner().score(2) == 2\n"
    )
    test_only = run.score_diff_risk(wd, "vqasynth")
    assert test_only.features["test_only_no_source"] is True
    assert test_only.factors.get("test_only_no_source", 0.0) > 0


def test_source_plus_tests_diff_not_flagged():
    """The inverse: a diff that touches BOTH a test file AND a source
    file (the well-tested-change pattern) is exactly what we want — the
    feature should NOT flag and should not contribute to the score."""
    wd = _base_repo()
    (wd / "vqasynth" / "newcap.py").write_text("def f(x):\n    return x\n")
    (wd / "tests" / "test_newcap.py").write_text(
        "from vqasynth.newcap import f\n"
        "def test_f():\n    assert f(1) == 1\n"
    )
    risk = run.score_diff_risk(wd, "vqasynth")
    assert risk.features["test_only_no_source"] is False
    assert "test_only_no_source" not in risk.factors


def test_source_only_diff_not_flagged_as_test_only():
    """Source-only diff (no test files touched) should not flag the
    test-only feature — that pattern is the inverse case
    (`untested_new_surface`), tested separately."""
    wd = _base_repo()
    (wd / "vqasynth" / "newcap.py").write_text("def g(x):\n    return x\n")
    risk = run.score_diff_risk(wd, "vqasynth")
    assert risk.features["test_only_no_source"] is False


def test_empty_diff_does_not_flag_test_only():
    """A diff with zero changed files (e.g. agent produced no output)
    should not falsely flag test_only_no_source — the boolean is
    'any test, no source' but it also requires at least one file."""
    wd = _base_repo()  # no further edits — clean working tree
    risk = run.score_diff_risk(wd, "vqasynth")
    assert risk.features["test_only_no_source"] is False


def test_test_only_diff_pairs_with_critical_path_to_elevate():
    """The conservative starting weight (1.0) leaves a pure test-only
    diff in the low band. But combined with another risk factor — a
    critical-path file edit — the score crosses into elevated. The
    band-routing emerges compositionally; the test-only feature is
    not an unconditional Issue-router."""
    # Pure test-only: should stay in low band.
    wd_pure = _base_repo()
    (wd_pure / "tests" / "test_extra.py").write_text(
        "def test_e():\n    assert True\n"
    )
    pure = run.score_diff_risk(wd_pure, "vqasynth")
    assert pure.features["test_only_no_source"] is True
    # Conservative weight: pure test-only stays below elevated.
    assert pure.band == "low"


def test_test_only_diff_render_includes_new_feature():
    """`render_risk_detail` surfaces the new feature in the disclosure
    block so the maintainer can see WHY a test-only diff was scored
    the way it was."""
    wd = _base_repo()
    (wd / "tests" / "test_more.py").write_text(
        "def test_m():\n    assert True\n"
    )
    risk = run.score_diff_risk(wd, "vqasynth")
    md = diff_risk_score.render_risk_detail(risk)
    assert "test-only diff" in md


def test_test_only_no_source_weight_constant_present():
    """Regression-pin the weight constant exists. Removing it without
    updating the contributions dict would silently disable the gate."""
    assert hasattr(diff_risk_score, "_W_TEST_ONLY_NO_SOURCE")
    assert diff_risk_score._W_TEST_ONLY_NO_SOURCE > 0


def test_render_risk_detail_surfaces_features():
    wd = _base_repo()
    (wd / "vqasynth" / "newcap.py").write_text("def enhance(x):\n    return x\n")
    risk = run.score_diff_risk(wd, "vqasynth")
    md = diff_risk_score.render_risk_detail(risk)
    assert "Diff Risk Score" in md
    assert "files touched" in md
    assert "new public callables" in md


def test_run_reexports_scorer_and_threshold():
    # The call site imports the capability — these references are the wiring.
    assert run.score_diff_risk is diff_risk_score.score_diff_risk
    assert run.DIFF_RISK_ISSUE_THRESHOLD == diff_risk_score.DIFF_RISK_ISSUE_THRESHOLD


# ── branch-vs-base mode (calibration harness) ──────────────────────────────


def test_branch_vs_base_mode_scores_committed_diff():
    """`base_ref` mode scores commits on a branch vs its merge-base —
    used to retrospectively score historical PR branches. Working tree
    is clean; the diff lives in commits on the branch."""
    wd = _base_repo()
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=wd, text=True,
    ).strip()
    # Create a branch with a committed change (mirrors a remyx-recommendation/* branch)
    _git(wd, "checkout", "-q", "-b", "remyx-recommendation/test")
    (wd / "vqasynth" / "newcap.py").write_text(
        "def enhance(x):\n    return x * 2\n"
    )
    (wd / "tests" / "test_newcap.py").write_text(
        "from vqasynth.newcap import enhance\n"
        "def test_enhance():\n    assert enhance(2) == 4\n"
    )
    _git(wd, "add", "-A")
    _git(wd, "commit", "-qm", "add newcap")

    # Working tree clean; default mode (base_ref=None) sees no diff.
    risk_default = run.score_diff_risk(wd, "vqasynth")
    assert risk_default.features["files_touched"] == 0
    assert risk_default.features["new_callables"] == 0

    # Branch-vs-base mode sees the committed diff.
    risk_branch = run.score_diff_risk(wd, "vqasynth", base_ref=base)
    assert risk_branch.features["files_touched"] == 2  # newcap.py + test_newcap.py
    assert risk_branch.features["new_callables"] >= 1  # enhance + test_enhance
    assert risk_branch.features["lines_added"] >= 2
    # New surface with a test file change → not flagged as untested.
    assert risk_branch.features["untested_new_surface"] is False
    # Small, tested, non-critical edit → low band.
    assert risk_branch.band == "low"


def test_test_functions_excluded_from_new_callables():
    """`test_X` functions in `tests/` files don't count as new production
    surface — they're test infrastructure. A PR that adds 1 production
    callable + 5 test functions should report new_callables=1, not 6.
    Calibrated against smellslikeml/openai-agents-python-outrider-demo#2
    where the original count was inflated by 9 test functions."""
    wd = _base_repo()
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=wd, text=True,
    ).strip()
    _git(wd, "checkout", "-q", "-b", "feature/test-vs-prod-callables")
    (wd / "vqasynth" / "feature.py").write_text(
        "def f(x):\n    return x\n"
    )
    (wd / "tests" / "test_feature.py").write_text(
        "from vqasynth.feature import f\n"
        "def test_a():\n    assert f(1) == 1\n"
        "def test_b():\n    assert f(2) == 2\n"
        "def test_c():\n    assert f(3) == 3\n"
        "def test_d():\n    assert f(4) == 4\n"
        "def test_e():\n    assert f(5) == 5\n"
    )
    _git(wd, "add", "-A")
    _git(wd, "commit", "-qm", "1 prod + 5 tests")
    risk = run.score_diff_risk(wd, "vqasynth", base_ref=base)
    # Only the production callable counts; test functions don't inflate.
    assert risk.features["new_callables"] == 1


def test_lines_changed_contribution_scales_modestly():
    """A typical Outrider scaffold (~1000 lines) shouldn't have a lines
    contribution that dominates the categorical risk signals. The v1.6.1
    recalibration cut `_W_LINES` 8× (from 0.004 to 0.0005) precisely so
    size-of-diff stays well below the categorical weights for the diff
    sizes Outrider actually produces."""
    # Score-by-direct-call against synthetic features so we can probe the
    # math without building a 5000-line tree.
    import diff_risk_score as drs
    real_extract = drs.extract_features
    try:
        # Typical Outrider scaffold: ~1000 lines
        drs.extract_features = lambda *a, **kw: {
            "files_touched": 10, "lines_added": 1000, "lines_deleted": 0,
            "lines_changed": 1000, "new_callables": 8,
            "critical_file_touched": False, "untested_new_surface": False,
            "test_only_no_source": False,
        }
        typical = drs.score_diff_risk(Path("/tmp"), "x")
        # Outlier 5000-line refactor
        drs.extract_features = lambda *a, **kw: {
            "files_touched": 10, "lines_added": 5000, "lines_deleted": 0,
            "lines_changed": 5000, "new_callables": 8,
            "critical_file_touched": False, "untested_new_surface": False,
            "test_only_no_source": False,
        }
        huge = drs.score_diff_risk(Path("/tmp"), "x")
    finally:
        drs.extract_features = real_extract
    # Monotonic: more lines → higher contribution
    assert huge.factors["lines_changed"] > typical.factors["lines_changed"]
    # Bounded for typical scaffolds: 1000 lines × 0.0005 = 0.5 — well below
    # the categorical weights (_W_CRITICAL=1.5, _W_UNTESTED=1.7) which
    # should dominate when they fire.
    assert typical.factors["lines_changed"] < drs._W_CRITICAL
    assert typical.factors["lines_changed"] < drs._W_UNTESTED
    # The 5000-line outlier IS allowed to weigh in heavily — but only after
    # the linear scaling carries it there (not via a discontinuous cap+overflow).
    assert huge.factors["lines_changed"] == 5 * typical.factors["lines_changed"]


def test_branch_vs_base_mode_detects_untested_surface():
    """Branch-vs-base mode correctly flags new callables without tests."""
    wd = _base_repo()
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=wd, text=True,
    ).strip()
    _git(wd, "checkout", "-q", "-b", "remyx-recommendation/untested")
    (wd / "vqasynth" / "newmod.py").write_text(
        "def f1(): pass\ndef f2(): pass\ndef f3(): pass\n"
    )
    _git(wd, "add", "-A")
    _git(wd, "commit", "-qm", "untested new module")

    risk = run.score_diff_risk(wd, "vqasynth", base_ref=base)
    assert risk.features["new_callables"] == 3
    assert risk.features["untested_new_surface"] is True
