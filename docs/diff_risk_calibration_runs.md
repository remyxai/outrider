# Diff Risk Score calibration runs

Each row is a `remyx-recommendation/*` branch scored against its merge-base with `origin/main`. Sorted by score descending so disputed-band candidates surface first.

| Branch | Date | Score | Band | Files | +Lines | -Lines | New cb | Crit | Untested | Top factor |
|---|---|---:|---|---:|---:|---:|---:|---|---|---|
| `pr-28` | 2026-06-15 | 0.99 | high | 6 | +927 | -0 | 11 | Y | N | `lines_changed` (+2.43) |
| `smellslikeml/openai-agents-python-outrider-demo#2` | 2026-06-11 | 0.64 | elevated | 3 | +360 | -1 | 6 | N | N | `lines_changed` (+1.44) |
| `smellslikeml/Arbor#2` | 2026-06-12 | 0.60 | elevated | 4 | +367 | -1 | 2 | N | N | `lines_changed` (+1.47) |
| `smellslikeml/openai-agents-python-outrider-demo#3` | 2026-06-12 | 0.58 | elevated | 4 | +296 | -4 | 4 | N | N | `lines_changed` (+1.2) |
| `smellslikeml/pytorch_geometric-outrider-demo#3` | 2026-06-11 | 0.46 | low | 4 | +252 | -3 | 1 | N | N | `lines_changed` (+1.02) |
