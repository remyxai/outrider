"""Calibrated static Diff Risk Score for Outrider's generated PRs.

Adapted from *Automating Low-Risk Code Review at Meta: RADAR, Risk
Calibration, and Review Efficiency* (arXiv:2605.30208). RADAR stratifies
every diff with a machine-learned **Diff Risk Score** computed over static
diff features (change size, files touched, surface added, critical-path
edits), then lets low-risk diffs auto-land while routing higher-risk diffs
to deeper review. A single tunable knob — the score percentile — trades
automation *yield* against *safety*; relaxing it from the 25th to the 50th
percentile raised RADAR's approve rate to ~60% while keeping the revert
rate at 1/3 and the production-incident rate at 1/50 of non-RADAR diffs.

This module ports the *result*, not the trained model: a single calibrated
risk number in [0, 1] plus a low / elevated / high band. The score is a
transparent logistic over exactly the static-diff features Outrider's
funnel already extracts for its other gates (lines changed, files touched,
new public callables, critical-file edits, test-coverage impact) — so it
drops in as one more deterministic gate without needing model internals,
multi-sampling, or any new telemetry infrastructure.

The band drives risk-aware routing at the `process_target` call site:

    score <  ELEVATED         → "low"      — flows straight through the funnel
    ELEVATED ≤ score < ISSUE  → "elevated" — still a PR, but forced to draft
                                             so a human reviews before it lands
    score ≥  ISSUE            → "high"     — routed to a human-review Issue/RFC
                                             instead of an auto-PR
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

# ── Calibrated thresholds on the 0–1 score ─────────────────────────────────
#
# RADAR exposes one tunable knob — the Diff Risk Score percentile — that
# trades automation yield for safety. These two cut points are that knob
# expressed as fixed score bands. Lowering ISSUE toward ELEVATED widens
# auto-PR yield (more diffs land without a human Issue) at the cost of
# safety; raising it is the conservative direction. Tuned so a typical
# small wiring PR (one new module + a sub-50-line edit + a test) sits well
# inside the low band.
DIFF_RISK_ELEVATED_THRESHOLD = 0.50
DIFF_RISK_ISSUE_THRESHOLD = 0.80

# Critical-path hints: edits to a pre-existing file whose path matches one
# of these carry production risk out of proportion to their size (process
# entry points, package surface, app/CLI/config wiring that lives in-tree).
# Matched on a simple substring basis — kept deliberately small.
CRITICAL_PATH_HINTS = (
    "__main__",
    "/run.py",
    "/cli.py",
    "/server.py",
    "/app.py",
    "/config.py",
    "/settings.py",
    "/__init__.py",
)

# ── Logistic feature weights ───────────────────────────────────────────────
#
# Signs and magnitudes are calibrated (not trained) so that a small,
# tested, non-critical wiring PR lands in "low", a moderate or untested
# change lands in "elevated", and a sprawling multi-file rewrite or an
# untested critical-path edit crosses into "high". The two categorical
# signals (critical-path edit, new surface shipped without any test change)
# are the dominant risk drivers, mirroring RADAR's finding that test
# coverage and blast radius matter more than raw line count.
_W_INTERCEPT = -2.0
_W_FILES = 0.18          # per file touched
_W_LINES = 0.004         # per added+deleted line
_W_NEW_CALLABLES = 0.10  # per newly-added public callable
_W_CRITICAL = 1.6        # any pre-existing critical-path file edited
_W_UNTESTED = 1.1        # new public surface added with no test-file change


@dataclass
class DiffRisk:
    """Result of scoring a working-tree diff against HEAD."""

    score: float                       # calibrated risk in [0, 1]
    band: str                          # "low" | "elevated" | "high"
    features: dict = field(default_factory=dict)   # raw static-diff features
    factors: dict = field(default_factory=dict)    # per-feature logit contribution


def _is_critical(path: str) -> bool:
    """True if `path` looks like a production-critical file."""
    p = "/" + path if not path.startswith("/") else path
    return any(hint in p for hint in CRITICAL_PATH_HINTS)


def _path_line_changes(workdir: Path, path: str) -> tuple[int, int]:
    """Return (added, deleted) lines for `path`.

    `git diff HEAD` does not surface still-untracked new files (the state
    Claude Code leaves the working tree in), so for a brand-new file we
    count its line count as additions; for a tracked file we defer to the
    funnel's existing numstat helper.
    """
    import run  # lazy: avoids a circular import at module load time

    if run._file_is_new(workdir, path):
        try:
            return len((workdir / path).read_text().splitlines()), 0
        except OSError:
            return 0, 0
    return run._diff_line_changes(workdir, path)


def extract_features(workdir: Path, package: str) -> dict:
    """Static-diff features for the working tree vs HEAD.

    Reuses the same helpers the integration / stub-density gates run on, so
    the risk score is computed from identical inputs — no separate parse.
    """
    import run  # lazy: avoids a circular import at module load time

    paths = run.changed_files(workdir)
    py_paths = [p for p in paths if p.endswith(".py")]

    lines_added = lines_deleted = 0
    for p in paths:
        a, d = _path_line_changes(workdir, p)
        lines_added += a
        lines_deleted += d

    new_callables = 0
    for p in py_paths:
        new_callables += len(run._added_callables(workdir, p))

    # Critical-path edits only count for files that already existed — a
    # brand-new __init__.py is package scaffolding, not a risky touch.
    critical = any(
        _is_critical(p) for p in paths if not run._file_is_new(workdir, p)
    )

    # Test-coverage impact: new public surface shipped without any change to
    # a test file is the classic under-reviewed pattern RADAR flags.
    test_changed = any(
        p.startswith("tests/") or Path(p).name.startswith("test_")
        for p in paths
    )
    untested = new_callables > 0 and not test_changed

    return {
        "files_touched": len(paths),
        "lines_added": lines_added,
        "lines_deleted": lines_deleted,
        "lines_changed": lines_added + lines_deleted,
        "new_callables": new_callables,
        "critical_file_touched": critical,
        "untested_new_surface": untested,
    }


def _band_for(score: float) -> str:
    if score >= DIFF_RISK_ISSUE_THRESHOLD:
        return "high"
    if score >= DIFF_RISK_ELEVATED_THRESHOLD:
        return "elevated"
    return "low"


def score_diff_risk(workdir: Path, package: str) -> DiffRisk:
    """Calibrated Diff Risk Score for the working-tree diff vs HEAD.

    Returns a :class:`DiffRisk` whose ``band`` drives the orchestrator's
    risk-aware routing. Pure function of the static diff — no Claude call,
    no sampling, deterministic for a given tree.
    """
    f = extract_features(workdir, package)
    contributions = {
        "files_touched": _W_FILES * f["files_touched"],
        "lines_changed": _W_LINES * f["lines_changed"],
        "new_callables": _W_NEW_CALLABLES * f["new_callables"],
        "critical_file_touched": _W_CRITICAL if f["critical_file_touched"] else 0.0,
        "untested_new_surface": _W_UNTESTED if f["untested_new_surface"] else 0.0,
    }
    z = _W_INTERCEPT + sum(contributions.values())
    score = 1.0 / (1.0 + math.exp(-z))
    factors = {k: round(v, 3) for k, v in contributions.items() if v}
    return DiffRisk(
        score=round(score, 4),
        band=_band_for(score),
        features=f,
        factors=factors,
    )


def render_risk_detail(risk: DiffRisk) -> str:
    """Markdown breakdown of a risk score for a downgrade-Issue body."""
    f = risk.features
    lines = [
        f"**Diff Risk Score**: {risk.score:.2f}  (band: **{risk.band}**, "
        f"auto-land threshold {DIFF_RISK_ISSUE_THRESHOLD:.2f})",
        "",
        "Static-diff features (RADAR-style):",
        "",
        f"- files touched: {f['files_touched']}",
        f"- lines changed: +{f['lines_added']}/-{f['lines_deleted']}",
        f"- new public callables: {f['new_callables']}",
        f"- critical-path file edited: {f['critical_file_touched']}",
        f"- new surface without test change: {f['untested_new_surface']}",
    ]
    if risk.factors:
        top = sorted(risk.factors.items(), key=lambda kv: kv[1], reverse=True)
        lines += ["", "Top risk contributors (logit):", ""]
        lines += [f"- `{k}` (+{v:.2f})" for k, v in top]
    return "\n".join(lines)
