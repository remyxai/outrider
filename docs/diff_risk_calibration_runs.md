---
type: Calibration Log
title: Diff Risk Score calibration runs
description: Per-branch Diff Risk Score values across the portfolio with weight recalibration notes.
resource: https://github.com/remyxai/outrider/blob/main/docs/diff_risk_calibration_runs.md
tags: [outrider, diff-risk, calibration, internal-notes]
timestamp: 2026-06-30T03:57:23Z
---

# Diff Risk Score calibration runs

Each row is a `remyx-recommendation/*` branch scored against its merge-base with `origin/main`. Sorted by score descending so disputed-band candidates surface first.

## test_only_no_source feature (2026-06-17, +1.0 weight)

Added a symmetric counterpart to `untested_new_surface` for the AIDev rejection-pattern (arXiv:2606.13468) of agentic PRs that touch only test files with no corresponding source change. Conservative starting weight (`_W_TEST_ONLY_NO_SOURCE = 1.0`) leaves a pure test-only diff in the **low band** (logit −2.5 + 1.0 = −1.5 → score ≈ 0.18) — the flag is observability-and-routing-additive, not unconditional-Issue-router. Combined with another risk signal (critical-path edit, large lines) the feature contributes the nudge into elevated draft for human review.

Worth recalibrating after 4–6 weeks of operation: if test-only PRs continue to merge at similar rates as code-bearing PRs, the weight is over-conservative and can drop. If they reject at higher rates as the AIDev paper predicts, the weight is right or under-conservative and could rise.

## v1.6.1 weights (current)

Recalibrated 2026-06-16 after a cross-portfolio re-scoring exercise across ~20 fork targets showed v1.6.0 over-routing to the high band. The gate triages "draft PR for review vs RFC Issue for discussion" — not "is this diff risky" — so the bar for downgrade-to-Issue is now much higher. The size of an Outrider scaffold (9-12 files, 500-1000 lines, module + tests + wiring + docs) is the expected output shape, not a risk signal; categorical signals (`critical_file_touched`, `untested_new_surface`) carry the routing decisions.

| Branch | Date | Score | Band | Files | +Lines | -Lines | New cb | Crit | Untested | Top factor |
|---|---|---:|---|---:|---:|---:|---:|---|---|---|
| `pr-28` | 2026-06-15 | 0.53 | elevated | 6 | +927 | -0 | 11 | Y | N | `critical_file_touched` (+1.50) |
| `recuse-recommendation-A` † | 2026-06-15 | 0.18 | low | 9 | +1013 | -1 | 6 | N | N | `lines_changed` (+0.51) |
| `recuse-recommendation-B` † | 2026-06-11 | 0.12 | low | 3 | +360 | -1 | 6 | N | N | `new_callables` (+0.30) |
| `projectmem-recommendation` | 2026-06-12 | 0.11 | low | 4 | +367 | -1 | 2 | N | N | `lines_changed` (+0.18) |
| `prompt-injection-recommendation` | 2026-06-12 | 0.11 | low | 4 | +296 | -4 | 4 | N | N | `new_callables` (+0.20) |
| `graph-rec` | 2026-06-11 | 0.10 | low | 4 | +252 | -3 | 1 | N | N | `lines_changed` (+0.13) |

Distribution: **1 elevated / 5 low / 0 high**.

† Two Outrider runs of the **same** recommendation (arxiv 2606.06460v1, "Will the Agent Recuse Itself?") on the same target. Under v1.6.0 weights they routed to different bands (one high, one elevated) because the scaffolds differed in size (1013 LOC across 9 files vs 360 LOC across 3 files); under v1.6.1 they both land in `low`. The size variance no longer changes the routing — both are draft PRs for human review, which matches the gate's actual job. Run-to-run variance is the gate's blind spot today; tightening it is future work.

## v1.6.0 weights (historical — pre-recalibration)

The original v1.6.0 scores are preserved here for reference. The recalibration was driven by the cross-portfolio re-scoring exercise confirming the "over-routing to high" failure mode: of the runs that produced an artifact, 82% landed in high band, and zero produced a PR.

| Branch | v1.6.0 Score | v1.6.0 Band |
|---|---:|---|
| `pr-28` | 0.99 | high |
| `recuse-recommendation-A` | 0.94 | high |
| `recuse-recommendation-B` | 0.64 | elevated |
| `projectmem-recommendation` | 0.60 | elevated |
| `prompt-injection-recommendation` | 0.58 | elevated |
| `graph-rec` | 0.46 | low |

v1.6.0 distribution: **2 high / 3 elevated / 1 low**.

## Weight changes (v1.6.0 → v1.6.1)

| Weight | v1.6.0 | v1.6.1 | Change |
|---|---:|---:|---|
| `_W_INTERCEPT` | -2.0 | -2.5 | Lower baseline |
| `_W_FILES` | 0.18 | 0.02 | 9× less |
| `_W_LINES` | 0.004 (capped at 500 + 0.001/line overflow) | 0.0005 (linear, no cap) | 8× less, cap removed |
| `_W_NEW_CALLABLES` | 0.10 | 0.05 | Half |
| `_W_CRITICAL` | 1.6 | 1.5 | Slight reduction |
| `_W_UNTESTED` | 1.1 | 1.7 | Raised (Outrider almost always adds tests, so missing tests IS a real signal) |
| `DIFF_RISK_ELEVATED_THRESHOLD` | 0.50 | 0.50 | Hold |
| `DIFF_RISK_ISSUE_THRESHOLD` | 0.80 | 0.80 | Hold |
