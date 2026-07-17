# AGENTS.md — Outrider

Guidance for AI coding agents working on this repository. Read this before touching source.

## What this project is

Outrider is a **GitHub Action** that turns arXiv papers into draft PRs on target repos. It:

1. Queries the Remyx engine for a paper recommendation against a repo's `ResearchInterest`.
2. Clones the target repo, drafts an implementation via Claude Code, runs pytest + self-review.
3. Opens a draft PR (or Issue when preflight downgrades) attributed to `remyx-ai[bot]`.
4. Optionally runs an inline refinement chain: fidelity audit → convention pass → test gate.

The Action is **backend-agnostic** through Claude Code's Anthropic-Messages-compatible endpoints — Anthropic (default), z.ai/GLM, Moonshot/Kimi via the `provider` action input.

## Build & test

- **Full test suite:** `python3 -m pytest tests/ -q` (~15s, ~1050 tests + 1 skipped)
- **Targeted:** `python3 -m pytest tests/test_bot_token_default.py -x -q`
- **Validate `action.yml`:** `python3 -c "import yaml; yaml.safe_load(open('action.yml'))"`
- No `pip install -e .`; the action installs its own deps in `action.yml`'s composite steps.

Run tests before every commit. New behavior needs pinning tests before the PR opens.

## Architecture at a glance

- **`src/run.py`** — main logic (~16K LOC, one file, intentionally). Selection, preflight, coding invocation, chain phases, cost telemetry, bot-token minting — all in here. Search for `def ` before adding a function; the piece you want probably exists.
- **`action.yml`** — composite-action step definitions. Inputs threaded into `run.py` via `INPUT_*` env vars. Adding a new input means an `action.yml` entry + a reader in `run.py`.
- **`tests/`** — every phase has its own `test_*.py` file. Tests intercept subprocess calls, GitHub API calls, and Claude CLI calls with monkeypatch stubs.
- **`docs/`** — customer-facing docs. `backends.md` is the canonical vendor table. `customization.md` is the input-shape reference.

## Backend routing (provider input)

- `provider` accepts `anthropic` / `zai` / `moonshot` / `custom` / empty. Empty = passthrough (existing behavior byte-for-byte).
- Set-provider logic lives in `action.yml`'s Configure step, not in `run.py` — it writes `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL` to `$GITHUB_ENV` for the coding step.
- Per-model cost telemetry lives in `_BACKEND_RATES` (nested `{host: {model: rates}}`). To add a model, add a row there and a matching test in `test_model_base_url.py`.
- **Mutual exclusion:** Claude Code prefers `x-api-key` when both auth vars are set. The Configure step clears the non-selected auth var explicitly. Do not remove that clearing — non-Anthropic backends 401 without it.

## Chain phases (post-PR-open)

Sequence when `recommend` mode opens a PR: **fidelity audit → convention pass → test gate**. Sequence is enforced in `run_refinement_chain` (`src/run.py`, around L16410). Each phase inherits the run's `claude-timeout`.

- `fidelity_audited*` → continue to convention + test (existing).
- `fidelity_skipped_*` (except `no_pr`) → also continue (added in v1.7.33). Audit couldn't run but the PR is unchanged; downstream doesn't depend on fidelity signal.
- `fidelity_failed_*` → short-circuit conservatively (audit crashed; don't muck with the PR).
- `fidelity_skipped_no_pr` → short-circuit (nothing to work on).

## Conventions

- **Commit format:** conventional (`feat(scope): …`, `fix(scope): …`, `docs(scope): …`, `chore(scope): …`). Body describes the *why*, not the *what*.
- **No co-author line.** Repo convention omits `Co-Authored-By: Claude …`; the maintainers don't use it.
- **PR body:** terse on this public repo. No Linear IDs, no customer names, no dollar figures from internal trials. `test_*.py` names are fine.
- **Version discipline:** `@v1` is a moving tag; ship additive changes on `@v1` without a version bump. New input + new provider values + new rate table rows are all additive. **Breaking changes (rename, remove, semantic change) require reviewing every caller.**
- **Release convention:** every version bump = `git tag vX.Y.Z <sha>` + `git push origin vX.Y.Z` + `git tag -f v1 <sha>` + `git push -f v1` + `gh release create vX.Y.Z --notes ...`. The `gh release create` step is easy to forget; the git tag push alone does NOT surface in the GitHub UI.

## Guardrails — do not do these

- Don't change `_BACKEND_RATES` structure — additive rate-row updates only.
- Don't remove `provider` input's accepted values. Add new; never remove or rename.
- Don't add per-phase timeout inputs. `claude-timeout` is the single per-phase knob; this is a design decision, not an omission.
- Don't touch the `_recommit_via_api` bot-token flow without reading `_mint_bot_token`'s TTL handling first (installation tokens have a 60-min TTL; the re-mint path is deliberate).
- Don't `git push --no-verify` or `--force` on `main`.
- Don't skip tests or add `pytest.skip` without explicit justification.

## Working with slow backends (Kimi, GLM-5.2)

Kimi K3's thinking mode adds per-turn latency. Refinement runs on Kimi routinely take 20-40 min per phase. When editing timeout logic:

- `claude-timeout` default is 900s (adequate for Opus/Haiku).
- Recommended `3600s` for Kimi runs (Moonshot backend default in the CLI registry).
- The bot-token TTL is 60 min; re-mint fires at 55 min. Anything that skips `_github_token()`'s freshness check will 401 at push time.

## Where to look first

- **New failure in a chain phase** → `src/run.py`'s `run_refinement_chain` and the phase's own runner (`run_fidelity_audit`, `run_convention_pass`, `run_test_gate`).
- **Cost telemetry wrong** → `_record_claude_usage` and `_BACKEND_RATES` in `src/run.py`.
- **Auth 401 at push** → `_github_token` + `_mint_bot_token` + `commit_and_push`'s `git remote set-url` refresh.
- **Preflight routing surprise** → `preflight_route` in `src/run.py` and the Target-shape input flow from `action.yml`.
- **New input not being read** → check `action.yml`'s Recommend step env block; every input needs a matching `INPUT_*` line.

## What good work looks like here

- Tests before implementation — pin the behavior, then make the code match.
- Small commits with clear conventional prefixes.
- Public-repo PR bodies terse; detail belongs in the docs/, not the PR.
- Version + tag + `gh release create` for every ship (the release is the missing step half the time).
- End-to-end smoke on a real fork before force-pushing an upstream PR fix.
