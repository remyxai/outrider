# Diff Risk Score calibration runs

Each row is a `remyx-recommendation/*` branch scored against its merge-base with `origin/main`. Sorted by score descending so disputed-band candidates surface first.

| Branch | Date | Score | Band | Files | +Lines | -Lines | New cb | Crit | Untested | Top factor |
|---|---|---:|---|---:|---:|---:|---:|---|---|---|
| `pr-28` | 2026-06-15 | 1.00 | high | 6 | +823 | -0 | 17 | Y | N | `lines_changed` (+3.29) |
| `smellslikeml/openai-agents-python-outrider-demo#2` | 2026-06-11 | 0.82 | high | 3 | +360 | -1 | 15 | N | N | `new_callables` (+1.5) |
| `smellslikeml/openai-agents-python-outrider-demo#3` | 2026-06-12 | 0.71 | elevated | 4 | +296 | -4 | 10 | N | N | `lines_changed` (+1.2) |
| `smellslikeml/Arbor#2` | 2026-06-12 | 0.67 | elevated | 4 | +367 | -1 | 5 | N | N | `lines_changed` (+1.47) |
| `smellslikeml/pytorch_geometric-outrider-demo#3` | 2026-06-11 | 0.51 | elevated | 4 | +252 | -3 | 3 | N | N | `lines_changed` (+1.02) |
