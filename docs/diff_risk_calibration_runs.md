# Diff Risk Score calibration runs

Each row is a `remyx-recommendation/*` branch scored against its merge-base with `origin/main`. Sorted by score descending so disputed-band candidates surface first.

| Branch | Date | Score | Band | Files | +Lines | -Lines | New cb | Crit | Untested | Top factor |
|---|---|---:|---|---:|---:|---:|---:|---|---|---|
| `pr-28` | 2026-06-15 | 0.99 | high | 6 | +927 | -0 | 11 | Y | N | `lines_changed` (+2.43) |
| `smellslikeml/openai-agents-python-outrider-demo#4` † | 2026-06-15 | 0.94 | high | 9 | +1013 | -1 | 6 | N | N | `lines_changed` (+2.51) |
| `smellslikeml/openai-agents-python-outrider-demo#2` † | 2026-06-11 | 0.64 | elevated | 3 | +360 | -1 | 6 | N | N | `lines_changed` (+1.44) |
| `smellslikeml/Arbor#2` | 2026-06-12 | 0.60 | elevated | 4 | +367 | -1 | 2 | N | N | `lines_changed` (+1.47) |
| `smellslikeml/openai-agents-python-outrider-demo#3` | 2026-06-12 | 0.58 | elevated | 4 | +296 | -4 | 4 | N | N | `lines_changed` (+1.2) |
| `smellslikeml/pytorch_geometric-outrider-demo#3` | 2026-06-11 | 0.46 | low | 4 | +252 | -3 | 1 | N | N | `lines_changed` (+1.02) |

† `oai-agents-python-outrider-demo#2` and `#4` are two Outrider runs of the **same** recommendation (arxiv 2606.06460v1, "Will the Agent Recuse Itself?") on the same target. The scaffold in `#4` added 1013 LOC across 9 files vs. `#2`'s 360 LOC across 3 files — the gate routed `#4` to Issue and `#2` to PR, validating that the band routing tracks variance in scaffold blast-radius rather than just paper choice. Run-to-run variance is the gate's blind spot today; tightening it is REMYX-121's job.
