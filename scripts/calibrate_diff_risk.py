#!/usr/bin/env python3
"""Calibration harness for the Diff Risk Score.

Scores every `remyx-recommendation/*` branch in the local clone against its
merge-base with `origin/main`, producing a Markdown table the maintainer can
qualitatively review (REMYX-107 step 2). No customer impact; runs entirely
against Outrider's own git history.

Usage (from the repo root):

    python3 scripts/calibrate_diff_risk.py                      # all branches
    python3 scripts/calibrate_diff_risk.py --limit 20           # most-recent N
    python3 scripts/calibrate_diff_risk.py --pattern 'pr-*'     # custom pattern
    python3 scripts/calibrate_diff_risk.py --output FILE.md     # write to file
    python3 scripts/calibrate_diff_risk.py --base origin/main   # custom base ref

Output rows: branch, score, band, files, lines (+/-), new_callables,
critical_file_touched, untested_new_surface, and the top logit contributor.
Sorted by score descending so disputed-band candidates surface first.

Operational note: the script does NOT switch your working tree between
branches — it reads each branch's tree via `git` plumbing commands against
the repo's `.git` directory. Safe to run with uncommitted changes in your
working tree (they're ignored).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

# Allow `import diff_risk_score` from the repo's src/.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


def _git(workdir: Path, *args: str, check: bool = False) -> str:
    """Return git stdout. ``check=True`` raises on failure."""
    r = subprocess.run(
        ["git", *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=check,
    )
    return r.stdout


def list_branches(workdir: Path, pattern: str) -> list[str]:
    """Local + remote refs matching ``pattern``, deduped by basename."""
    refs = _git(
        workdir,
        "for-each-ref",
        "--format=%(refname:short)",
        f"refs/heads/{pattern}",
        f"refs/remotes/origin/{pattern}",
    ).splitlines()
    seen = set()
    out = []
    for ref in refs:
        ref = ref.strip()
        if not ref:
            continue
        # Normalize "origin/<branch>" → "<branch>" for display, keep the
        # original ref for git operations.
        display = ref.split("/", 1)[1] if ref.startswith("origin/") else ref
        if display in seen:
            continue
        seen.add(display)
        out.append(ref)
    return out


def merge_base(workdir: Path, branch_ref: str, base: str) -> str | None:
    out = _git(workdir, "merge-base", branch_ref, base).strip()
    return out or None


def branch_commit_date(workdir: Path, branch_ref: str) -> str:
    """ISO date of the branch's HEAD commit, for ordering."""
    return _git(workdir, "log", "-1", "--format=%cI", branch_ref).strip()


def score_branch(
    workdir: Path,
    branch_ref: str,
    base_sha: str,
    package: str,
) -> dict | None:
    """Check out the branch into a temp worktree and score its diff vs base."""
    import diff_risk_score  # noqa: E402

    with tempfile.TemporaryDirectory(prefix="diffrisk-") as tmp:
        tmp_path = Path(tmp) / "tree"
        try:
            _git(
                workdir, "worktree", "add", "--quiet", "--detach",
                str(tmp_path), branch_ref,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            return {"_error": f"worktree add failed: {e.stderr or e}"}
        try:
            risk = diff_risk_score.score_diff_risk(
                tmp_path, package, base_ref=base_sha,
            )
            top = max(risk.factors.items(), key=lambda kv: kv[1]) if risk.factors else (None, 0)
            return {
                "score": risk.score,
                "band": risk.band,
                "features": risk.features,
                "top_factor": top[0],
                "top_factor_contribution": round(top[1], 2),
            }
        finally:
            _git(workdir, "worktree", "remove", "--force", str(tmp_path))


def render_table(rows: list[dict]) -> str:
    """Markdown table sorted by score descending."""
    rows_sorted = sorted(rows, key=lambda r: -(r.get("score") or 0))
    out = []
    out.append("# Diff Risk Score calibration runs")
    out.append("")
    out.append(
        "Each row is a `remyx-recommendation/*` branch scored against its "
        "merge-base with `origin/main`. Sorted by score descending so "
        "disputed-band candidates surface first."
    )
    out.append("")
    out.append(
        "| Branch | Date | Score | Band | Files | +Lines | -Lines | "
        "New cb | Crit | Untested | Top factor |"
    )
    out.append(
        "|---|---|---:|---|---:|---:|---:|---:|---|---|---|"
    )
    for r in rows_sorted:
        if "_error" in r:
            out.append(
                f"| `{r['branch']}` | {r['date'][:10]} | — | error | — | — | "
                f"— | — | — | — | {r['_error'][:60]} |"
            )
            continue
        f = r["features"]
        out.append(
            f"| `{r['branch']}` | {r['date'][:10]} | "
            f"{r['score']:.2f} | {r['band']} | "
            f"{f['files_touched']} | +{f['lines_added']} | "
            f"-{f['lines_deleted']} | {f['new_callables']} | "
            f"{'Y' if f['critical_file_touched'] else 'N'} | "
            f"{'Y' if f['untested_new_surface'] else 'N'} | "
            f"`{r['top_factor']}` (+{r['top_factor_contribution']}) |"
        )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--pattern", default="remyx-recommendation/*",
        help="Branch glob to score (default: %(default)s)",
    )
    ap.add_argument(
        "--base", default="origin/main",
        help="Base ref for merge-base computation (default: %(default)s)",
    )
    ap.add_argument(
        "--limit", type=int, default=None,
        help="Score only the N most-recent branches by commit date",
    )
    ap.add_argument(
        "--package", default="src",
        help="Package name passed to score_diff_risk (default: %(default)s)",
    )
    ap.add_argument(
        "--output", default="docs/diff_risk_calibration_runs.md",
        help="Where to write the calibration table (default: %(default)s; "
             "pass '-' for stdout)",
    )
    args = ap.parse_args()

    workdir = REPO_ROOT
    branches = list_branches(workdir, args.pattern)
    if not branches:
        print(f"no branches match {args.pattern!r}", file=sys.stderr)
        return 1

    # Annotate with commit date and sort newest-first; trim to --limit.
    dated = [(b, branch_commit_date(workdir, b)) for b in branches]
    dated.sort(key=lambda bd: bd[1], reverse=True)
    if args.limit:
        dated = dated[: args.limit]

    print(
        f"Scoring {len(dated)} branch(es) matching {args.pattern!r} "
        f"against {args.base}...",
        file=sys.stderr,
    )

    rows = []
    for ref, date in dated:
        display = ref.split("/", 1)[-1] if ref.startswith("origin/") else ref
        print(f"  → {display}", file=sys.stderr)
        base_sha = merge_base(workdir, ref, args.base)
        if not base_sha:
            rows.append({
                "branch": display, "date": date,
                "_error": f"no merge-base with {args.base}",
            })
            continue
        result = score_branch(workdir, ref, base_sha, args.package)
        if result is None or "_error" in (result or {}):
            rows.append({
                "branch": display, "date": date,
                "_error": (result or {}).get("_error", "score failed"),
            })
            continue
        rows.append({"branch": display, "date": date, **result})

    table = render_table(rows)
    if args.output == "-":
        print(table)
    else:
        out_path = REPO_ROOT / args.output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(table + "\n")
        print(f"wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
