---
description: Rules for editing src/run.py, the monolithic main logic file.
applyTo: "src/run.py"
---

# src/run.py conventions

- **Single-file by design.** ~16K LOC. Do not split into modules — session-crossing state, single import point for the composite action.
- **Search before writing.** Grep for `def <name>` or the concept — the function you want probably exists (`_github_token`, `_record_claude_usage`, `run_fidelity_audit`, `_mint_bot_token`).
- **Test before commit.** Every new behavior needs a test in `tests/test_<area>.py`. Existing tests use monkeypatch to intercept subprocess, gh_api, and Claude CLI calls.
- **No new error handling for scenarios that can't happen.** Trust framework guarantees inside outrider. Only validate at external boundaries — GitHub API responses, action inputs, Claude subprocess output.
- **Don't touch `_BACKEND_RATES` structure.** Additive rate-row updates only. Adding a model = new row + a matching test in `test_model_base_url.py`.
- **Chain-phase sequencing lives in `run_refinement_chain`** (around L16410). fidelity → convention → test. `fidelity_skipped_*` continues to convention + test (except `no_pr`); `fidelity_failed_*` short-circuits.
