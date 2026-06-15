# Diff Risk Score calibration runs

Each row is a `remyx-recommendation/*` branch scored against its merge-base with `origin/main`. Sorted by score descending so disputed-band candidates surface first.

| Branch | Date | Score | Band | Files | +Lines | -Lines | New cb | Crit | Untested | Top factor |
|---|---|---:|---|---:|---:|---:|---:|---|---|---|
| `pr-37` | 2026-06-15 | 1.00 | high | 9 | +935 | -36 | 37 | Y | N | `lines_changed` (+3.88) |
| `pr-28` | 2026-06-15 | 0.95 | high | 4 | +408 | -0 | 9 | Y | N | `lines_changed` (+1.63) |
