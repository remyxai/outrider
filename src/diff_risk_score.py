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
import subprocess
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
# Recalibrated 2026-06-16 from a cross-portfolio re-scoring exercise that
# showed the prior weights mechanically over-routed to high band: every
# typical Outrider scaffold (9-12 files, 500-1000 lines for module + tests
# + wiring + docs) crossed the high threshold by feature accumulation
# alone, before the categorical risk signals weighed in.
#
# The gate triages "draft PR for review vs RFC Issue for discussion",
# NOT "is this diff risky" — Outrider never auto-merges, so downgrade-
# to-Issue is reserved for cases the maintainer needs to discuss before
# reviewing a diff, not "the diff is big." The size of an Outrider
# scaffold IS its expected output shape, not a risk signal.
#
# Categorical signals (critical-path edit, untested new surface) remain
# the dominant risk drivers. The previous _LINES_CAP / _W_LINES_OVERFLOW
# pair is removed since the linear weight is now small enough not to
# need cap behaviour.
_W_INTERCEPT = -2.5
_W_FILES = 0.02          # per file touched
_W_LINES = 0.0005        # per added+deleted line (linear, no cap)
_W_NEW_CALLABLES = 0.05  # per newly-added public callable
_W_CRITICAL = 1.5        # any pre-existing critical-path file edited
_W_UNTESTED = 1.7        # new public surface added with no test-file change

# Symmetric to _W_UNTESTED, addressing a separate rejection pattern from
# the AIDev study (arXiv:2606.13468) of agentic PRs: a diff that touches
# ONLY test files with no corresponding source change. The pattern shows
# up as agents adding tests for code that doesn't exist yet, writing
# tests that exercise the wrong thing, or producing scaffolding without
# the implementation it claims to validate. Legitimate uses exist
# (regression tests for an upstream bugfix already in main, retroactive
# coverage of existing behavior), so the weight is conservative — at
# 1.0 a pure test-only diff scores well below the elevated threshold
# in isolation, but pairs with another risk factor (critical-path edit,
# large lines) to nudge into elevated draft. The band routes; the score
# itself doesn't block.
_W_TEST_ONLY_NO_SOURCE = 1.0


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


# ── Branch-vs-base helpers (testing mode) ──────────────────────────────────
#
# The default mode (base_ref=None) reads from the working tree vs HEAD —
# Outrider's runtime case where Claude Code's changes are uncommitted. For
# scoring historical PR branches (REMYX-107 calibration), we instead want
# the diff between the branch HEAD and its merge-base with main, so the
# helpers below switch to that comparison when a base_ref is supplied.

def _git(workdir: Path, *args: str) -> str:
    """Best-effort `git` invocation — returns stdout, swallows failures."""
    r = subprocess.run(
        ["git", *args], cwd=workdir, capture_output=True, text=True, check=False,
    )
    return r.stdout if r.returncode == 0 else ""


def _changed_files_branch_mode(workdir: Path, base_ref: str) -> list[str]:
    """Files changed by commits on this branch since `base_ref`."""
    import run  # lazy: shared build-artifact filter constants

    out = _git(workdir, "diff", "--name-only", "--diff-filter=ACMR", base_ref)
    paths = []
    for line in out.splitlines():
        p = line.strip()
        if not p:
            continue
        if any(sub in p for sub in run._BUILD_ARTIFACT_SUBSTRINGS):
            continue
        if any(p.endswith(suf) for suf in run._BUILD_ARTIFACT_SUFFIXES):
            continue
        paths.append(p)
    return paths


def _file_is_new_at(workdir: Path, path: str, base_ref: str) -> bool:
    """True if `path` did not exist at `base_ref`."""
    return not _git(workdir, "ls-tree", base_ref, "--", path).strip()


def _path_line_changes_at(
    workdir: Path, path: str, base_ref: str,
) -> tuple[int, int]:
    """Return (added, deleted) lines for `path` between `base_ref` and HEAD."""
    out = _git(workdir, "diff", "--numstat", base_ref, "--", path).strip()
    if not out:
        return 0, 0
    parts = out.split(None, 2)
    if len(parts) < 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def _added_callables_at(
    workdir: Path, path: str, base_ref: str,
) -> set:
    """Public callables added between `base_ref` and HEAD for `path`."""
    import run  # lazy

    if not path.endswith(".py"):
        return set()
    try:
        current = (workdir / path).read_text()
    except OSError:
        return set()
    now = run._public_callables(current)
    if _file_is_new_at(workdir, path, base_ref):
        return now
    base_source = _git(workdir, "show", f"{base_ref}:{path}")
    return now - run._public_callables(base_source)


# ── Mode-switching abstraction over the diff source ────────────────────────

def _changed_files(workdir: Path, base_ref: str | None) -> list[str]:
    import run  # lazy
    if base_ref is None:
        return run.changed_files(workdir)
    return _changed_files_branch_mode(workdir, base_ref)


def _file_is_new(workdir: Path, path: str, base_ref: str | None) -> bool:
    import run  # lazy
    if base_ref is None:
        return run._file_is_new(workdir, path)
    return _file_is_new_at(workdir, path, base_ref)


def _path_line_changes(
    workdir: Path, path: str, base_ref: str | None = None,
) -> tuple[int, int]:
    """Return (added, deleted) lines for `path`.

    Default mode (`base_ref=None`): working tree vs HEAD — Outrider runtime,
    where Claude's changes are uncommitted. `git diff HEAD` doesn't surface
    untracked new files, so we count a brand-new file's lines as additions.

    Branch-vs-base mode (`base_ref` supplied): HEAD vs `base_ref` — used to
    score historical PR branches against their merge-base.
    """
    import run  # lazy

    if base_ref is None:
        if run._file_is_new(workdir, path):
            try:
                return len((workdir / path).read_text().splitlines()), 0
            except OSError:
                return 0, 0
        return run._diff_line_changes(workdir, path)
    return _path_line_changes_at(workdir, path, base_ref)


def _added_callables(
    workdir: Path, path: str, base_ref: str | None,
) -> set:
    import run  # lazy
    if base_ref is None:
        return run._added_callables(workdir, path)
    return _added_callables_at(workdir, path, base_ref)


def extract_features(
    workdir: Path, package: str, base_ref: str | None = None,
) -> dict:
    """Static-diff features for the working tree vs HEAD (`base_ref=None`)
    or HEAD vs `base_ref` (branch-vs-base mode for historical-PR scoring).

    Reuses the same helpers the integration / stub-density gates run on, so
    the risk score is computed from identical inputs — no separate parse.
    """
    paths = _changed_files(workdir, base_ref)
    py_paths = [p for p in paths if p.endswith(".py")]

    lines_added = lines_deleted = 0
    for p in paths:
        a, d = _path_line_changes(workdir, p, base_ref)
        lines_added += a
        lines_deleted += d

    # Count only NEW PRODUCTION callables (test_X functions in tests/ are
    # test infrastructure, not new production surface — they shouldn't
    # inflate the score the way a new public API would).
    def _is_test_path(p: str) -> bool:
        return p.startswith("tests/") or Path(p).name.startswith("test_")

    new_callables = 0
    for p in py_paths:
        if _is_test_path(p):
            continue
        new_callables += len(_added_callables(workdir, p, base_ref))

    # Critical-path edits only count for files that already existed — a
    # brand-new __init__.py is package scaffolding, not a risky touch.
    critical = any(
        _is_critical(p) for p in paths if not _file_is_new(workdir, p, base_ref)
    )

    # Test-coverage impact: new public surface shipped without any change to
    # a test file is the classic under-reviewed pattern RADAR flags.
    test_changed = any(_is_test_path(p) for p in paths)
    untested = new_callables > 0 and not test_changed

    # Symmetric pattern from the AIDev rejection study: a diff that touches
    # ONLY test files with no corresponding source change. Agents adding
    # tests for code that doesn't exist, exercising the wrong thing, or
    # producing scaffolding without the implementation. The band-routing
    # logic nudges these to elevated draft when paired with another risk
    # factor; the score alone (at the conservative starting weight) leaves
    # legitimate cases (regression tests for upstream bugfixes already in
    # main, retroactive coverage of existing behavior) in the low band.
    non_test_changed = any(not _is_test_path(p) for p in paths)
    test_only_no_source = bool(paths) and test_changed and not non_test_changed

    return {
        "files_touched": len(paths),
        "lines_added": lines_added,
        "lines_deleted": lines_deleted,
        "lines_changed": lines_added + lines_deleted,
        "new_callables": new_callables,
        "critical_file_touched": critical,
        "untested_new_surface": untested,
        "test_only_no_source": test_only_no_source,
    }


def _band_for(score: float) -> str:
    if score >= DIFF_RISK_ISSUE_THRESHOLD:
        return "high"
    if score >= DIFF_RISK_ELEVATED_THRESHOLD:
        return "elevated"
    return "low"


def score_diff_risk(
    workdir: Path, package: str, base_ref: str | None = None,
) -> DiffRisk:
    """Calibrated Diff Risk Score for a static diff.

    Default mode (``base_ref=None``): scores the working-tree diff vs HEAD —
    Outrider's runtime case. Branch-vs-base mode (``base_ref`` is a SHA / ref
    name): scores HEAD vs ``base_ref`` — used to retrospectively score
    historical PR branches against their merge-base (REMYX-107 calibration).

    Returns a :class:`DiffRisk` whose ``band`` drives the orchestrator's
    risk-aware routing. Pure function of the static diff — no Claude call,
    no sampling, deterministic for a given tree.
    """
    f = extract_features(workdir, package, base_ref=base_ref)
    contributions = {
        "files_touched": _W_FILES * f["files_touched"],
        "lines_changed": _W_LINES * f["lines_changed"],
        "new_callables": _W_NEW_CALLABLES * f["new_callables"],
        "critical_file_touched": _W_CRITICAL if f["critical_file_touched"] else 0.0,
        "untested_new_surface": _W_UNTESTED if f["untested_new_surface"] else 0.0,
        "test_only_no_source": (
            _W_TEST_ONLY_NO_SOURCE if f["test_only_no_source"] else 0.0
        ),
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
    """Markdown breakdown of a risk score for a downgrade-Issue body.

    The headline (score + band + threshold) stays visible; the feature
    breakdown collapses into a <details> disclosure so the routing
    decision reads cleanly. Per-feature logit contributions are not
    surfaced — they're already in the RUN SUMMARY JSON
    (`diff_risk_factors`) that flows to Remyx telemetry, which is where
    weight calibration consumes them. Bare logit numbers without the
    weights are jargon for customer-facing copy.
    """
    f = risk.features
    return "\n".join([
        f"**Diff Risk Score**: {risk.score:.2f} / **{risk.band}** band "
        f"(auto-land threshold {DIFF_RISK_ISSUE_THRESHOLD:.2f})",
        "",
        "<details>",
        "<summary>Diff features scored</summary>",
        "",
        f"- files touched: {f['files_touched']}",
        f"- lines changed: +{f['lines_added']}/-{f['lines_deleted']}",
        f"- new public callables: {f['new_callables']}",
        f"- critical-path file edited: {f['critical_file_touched']}",
        f"- new surface without test change: {f['untested_new_surface']}",
        f"- test-only diff (no source change): {f['test_only_no_source']}",
        "</details>",
    ])
