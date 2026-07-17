---
name: outrider-experimentation
description: |
  Trigger, debug, and interpret Outrider Action runs on target repos as
  part of Claude-Code-driven paper-to-code work. Covers manual dispatch
  patterns, using remyxai-cli to scope paper search, fetching run logs
  and RUN SUMMARY JSON to interpret outcomes, checking GitHub coordination
  signals (existing PRs/Issues, upstream policy) before filing, and
  reading the fidelity audit's Coverage matrix to identify gaps before
  going upstream. Use when experimenting with Outrider on a target repo,
  debugging a failed or downgraded run, or preparing to shepherd a
  drafted PR upstream.
allowed-tools: Bash, Read
---

# Outrider experimentation — field guide

Ambient knowledge for using Outrider effectively on target repos: from dispatch to upstream. All commands here are ones I've actually used in practice; nothing speculative.

## When to use

- Setting up Outrider on a new target repo
- Dispatching a manual refinement / smoke run
- Diagnosing why a run downgraded to Issue or opened at a different outcome than expected
- Deciding whether an Outrider-drafted PR is ready to file upstream
- Cross-checking a paper against existing coordination signals on the target repo

## Set up Outrider on a target repo

Fastest path — CLI-generated workflow with the right backend selected:

```bash
remyxai outrider setup-local --repo owner/name --backend moonshot
```

Backends: `anthropic` (default, Opus), `zai` (GLM), `moonshot` (Kimi K3). Setup-local prompts only for the selected backend's secret; add others manually via `gh secret set ANTHROPIC_API_KEY --repo owner/name < token.txt` for per-dispatch switching.

Manual install (when the CLI shape doesn't quite fit): copy the workflow YAML from `remyxai/outrider`'s `.github/workflows/outrider.yml` and thread the `interest-id` you get from `remyxai interests get -f json`.

## Trigger manually

```bash
# Fresh recommend (let selection pass pick)
gh workflow run outrider.yml --repo owner/name --ref main

# Pin to a specific paper (skip selection)
gh workflow run outrider.yml --repo owner/name --ref main \
  -f pin-arxiv=2509.09675v1 \
  -f provider=moonshot \
  -f model=kimi-k3

# Refinement on an existing drafter branch
gh workflow run outrider.yml --repo owner/name --ref main \
  -f pin-arxiv=<id> \
  -f start-from-ref=<drafter-branch> \
  -f staged-synthesis=true \
  -f lead-content="REFINEMENT PASS on existing branch..."

# Convention pass on an already-open PR
gh workflow run outrider-convention.yml --repo owner/name --ref main -f pr-number=18
```

Key inputs to remember:
- `pin-arxiv`: exact `<id>[v<N>]` — bypasses selection, best for reproducible runs
- `pin-method`: free-text query for the selection pass to search (often prefer `search-method`; `pin-method` is a docs-only alias that GHA warns about)
- `start-from-ref`: refinement mode — pins to an existing branch
- `staged-synthesis=true`: enables research phase before coding (adds ~10 min for slow backends)
- `claude-timeout`: bump to `3600` for Kimi K3 or GLM-5.2 (thinking-mode adds per-turn latency)
- `rate-limit-days=0`: bypass the 7-day cadence guard for one-off smokes

## Use remyxai-cli to scope search

```bash
# What's in the current interest pool for a repo
remyxai interests get -i <name-or-uuid> -f json | jq '.recommendations[:5]'

# Search by method for a specific paper before triggering
remyxai search info --arxiv 2509.09675v1 -f json

# Search across the engine's corpus by free text
remyxai search query "curiosity driven exploration RLVR" -f json | jq '.results[:3]'
```

Use these before dispatching to avoid re-running the selection pass just to see if a paper is in-pool.

## Get context from an Action run

Runs' JSON RUN SUMMARY block is at the end of the log and is the authoritative source of truth:

```bash
# Fetch the per-job log (faster than per-run log; scoped to one job)
JOB_ID=$(gh api "repos/OWNER/REPO/actions/runs/$RUN_ID/jobs" --jq '.jobs[0].id')
gh api "repos/OWNER/REPO/actions/jobs/$JOB_ID/logs" 2>&1 | grep -A30 "RUN SUMMARY"
```

Fields worth grepping:

- `"model_backend"` — actual backend that served the run ("Anthropic" / "z.ai (GLM)" / "Moonshot (Kimi)"). Authoritative when the workflow name lies (e.g., "Outrider daily (branch-mode, drafter)" workflows on GLM-swapped forks say drafter but route to GLM).
- `"cost_usd"` — computed from `_BACKEND_RATES` when `cost_basis: "backend_rate_table"`; from Claude Code's envelope otherwise (Anthropic-rate estimate, approximate for GLM/Moonshot before the rate-table entry landed).
- `"status"` — `pr_opened_draft` / `issue_opened_preflight` / `issue_opened_no_integration` / `skipped_open_artifact` / `claude_failed` / `error`.
- `"chain"` — for recommend runs that chained into fidelity/convention/test. `fidelity_status: "fidelity_skipped_reference_mismatch"` means reference URL didn't self-identify with the arxiv — audit couldn't run, but PR is unchanged.

Common ones and what they mean:

| Status | What happened |
|---|---|
| `pr_opened_draft` | Full recommend produced a PR; likely worth reviewing |
| `issue_opened_preflight` | Preflight decided PR-shape was wrong before coding — usually correct (new-feature-needs-approval, no clean integration point) |
| `issue_opened_no_integration` | Coding ran, but preflight downgraded — sometimes correct, sometimes a false positive on a self-contained diff |
| `skipped_open_artifact` | Rate-limit-days guard fired (recent PR/Issue exists on target) |
| `skipped_low_confidence` | Selection couldn't find a moderate-tier candidate |
| `claude_failed` | Coding session errored — check log tail for the actual traceback |
| `error` | Something in run.py crashed before Claude Code was invoked — auth 401, git push, etc. |

## Check coordination signals before filing upstream

Before pushing an Outrider-drafted PR to the target upstream, check:

```bash
# Does the target repo have an AI-contribution policy?
gh api repos/UPSTREAM/REPO/contents/CLAUDE.md --jq '.content' | base64 -d 2>/dev/null | head -30
gh api repos/UPSTREAM/REPO/contents/AGENTS.md --jq '.content' | base64 -d 2>/dev/null | head -30
# Look for: "coordination issue required before PR", "AI-assistance disclosure required", etc.

# Are there existing PRs or Issues on this paper?
gh search prs --repo UPSTREAM/REPO --state all "$PAPER_TITLE" --json number,title,state,createdAt
gh search issues --repo UPSTREAM/REPO --state all "$PAPER_TITLE" --json number,title,state,createdAt

# Is there a competing implementation in a sibling fork?
gh search repos "$PAPER_TITLE" --limit 5

# Has anyone attempted coordination on the paper's own repo?
gh api repos/PAPER_REPO/issues --jq '.[] | select(.title | test("coord|port|implement"; "i"))'
```

Common gates that block going upstream:

- **Target repo requires coordination Issue.** Open one on the *target repo*, not the paper repo (this is a common misinterpretation). Reference `smellslikeml/peft#10` → `huggingface/peft#3450` as a working example.
- **AI-assistance disclosure required.** Every AI-assisted PR body must clearly state AI involvement + link the coordination Issue's approval comment.
- **Existing open PR on same paper.** Don't open a duplicate. Comment on the existing one instead.
- **Recent maintainer inactivity on that domain.** Bumping stale coordination Issues is fine; opening a PR when the last activity was months ago is riskier.

## Analyze the fidelity audit output

When a run's chain includes fidelity, the PR body gets a `## Coverage` matrix. Read it to see gaps before pushing upstream:

```bash
gh pr view $PR_NUMBER --repo OWNER/REPO --json body --jq '.body' | \
  awk '/## Coverage/,/^## /'
```

Fidelity statuses to look for in the RUN SUMMARY's `chain.fidelity_status`:

- `fidelity_audited` (or `_needs_judgment`, `_advisory`, `_paper_anchored`) — audit ran cleanly; Coverage matrix is populated with per-mechanism ✓/✗ marks
- `fidelity_skipped_reference_mismatch` — reference repo didn't back-reference the arxiv (as with `github.com/volcengine/verl` hosting many papers). Audit couldn't run against a specific reference; PR body has no Coverage matrix
- `fidelity_skipped_no_reference` — no reference impl exists at all; PR is paper-anchored only
- `fidelity_failed_*` — audit crashed; needs a re-run before trusting the PR

If Coverage matrix shows `✗` on a paper mechanism the maintainer would care about (e.g., "actor-wise perplexity bonus: not implemented" when the whole point is the actor bonus), that's a real gap. Options:

1. **Dispatch a refinement pass** with `lead-content` naming the specific gap
2. **Hand-fix in a follow-up commit** if the gap is small and mechanical
3. **Route to Issue instead** — Coverage below a threshold means the PR isn't ready; open an Issue with the gap analysis and defer

## Working with slow backends (Kimi K3, GLM-5.2)

Both have thinking-mode latency; runs are 20-40 min per phase, not 5-15 min.

- **claude-timeout**: `3600` (60 min) is baseline; threads through every phase.
- **cost proxy**: rate-table entries exist for Anthropic, z.ai's glm-4.6 / glm-5.2, and Moonshot's kimi-k3 / kimi-k2.7-code(-highspeed). Everything else falls back to Claude Code's envelope, which over-reports non-Anthropic cost by 3-10×.

## Active gotchas

- **`Kimi 429 engine overloaded`**: Moonshot server-side capacity. Wait 30 min and re-dispatch; transient.
- **Selection returns "no viable candidates"**: interest is stub-only. Verify with `remyxai interests get -i <name> -f json | jq '.context | length'` — expect > 500. If under, re-run `remyxai interests from-repo`.

## Related

- **CLI shape**: `.claude/skills/remyxai-cli.md` — the four command families (`interests`, `papers`, `outrider`, `search`) and common gotchas.
- **Backend routing**: `docs/backends.md` in this repo — the canonical per-vendor table.
- **Chain phase sequencing**: `AGENTS.md` `## Chain phases` section — which statuses continue vs short-circuit.
