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
