---
name: remyxai
description: |
  Guidance for using `remyxai-cli` — managing research interests, querying paper
  recommendations, dispatching Outrider action runs, and running batched
  design-review cycles. Trigger when the user mentions Outrider, GitRank,
  paper recommendations, `remyxai` commands, or research-implementation
  workflows against a target repo.
allowed-tools: Bash, Read
---

# remyxai-cli guidance

Four command families: `interests`, `papers`, `outrider`, `search`. Full help via `--help` on any subcommand. Global option: `-f json` on most subcommands for machine-readable output.

## Setup for a new target repo

- **Own repo** (team maintains it): `remyxai outrider init --auto-interest`
- **External repo** (shepherding to it): `remyxai interests from-repo --url https://github.com/<owner>/<repo>`
- **Never** run `remyxai interests create --context <URL>` — produces stub-only contexts. Use `from-repo` or `--auto-interest`.

## Verify a new interest before relying on it

`--auto-interest` sometimes returns success without extracting ExperimentHistory. Always verify:

```bash
remyxai interests get -i <name-or-uuid> -f json | jq '.context | length'
# Expect > 500. Under that = stub, re-run `interests from-repo` explicitly.
```

Also note: `interests get` requires `-i <name-or-uuid>` — positional arguments do not work.

## Query recommendations

```bash
remyxai papers list -i <interest> -p week -n 30 --full
```

- `-p today|week|all` — period filter
- `--full` — full reasoning text (default truncates; usually you want `--full`)
- `-f json` — machine-readable
- `remyxai papers refresh` — trigger a fresh Gemini re-ranking run

## Dispatch Outrider — three trigger modes

```bash
# Default: ranker picks from the interest-scoped pool
remyxai outrider trigger --repo owner/name

# Search-driven: overrides pool with a query
remyxai outrider trigger --repo owner/name --search-method "<query>"

# Pin a specific paper (bypasses pool, most reproducible)
remyxai outrider trigger --repo owner/name --pin-arxiv <arxiv-id>
```

The target repo must already have Outrider installed (`remyxai outrider init` or `setup-local`). Authenticates via local `gh` CLI.

## Backend selection (cost matters)

- **Anthropic (default)**: premium reasoning, ~$3–5/dispatch. Best for shipping.
- **z.ai GLM-5.2** (`--provider zai --model glm-5.2`): ~10× cheaper. Best for exploration.

Rule of thumb: GLM for tier-1/2 exploration and batched trials; Anthropic for the one candidate you commit to shipping.

## Batched design-review cycles

`outrider explore` runs multiple cycles, deciding MERGE/ITERATE/LEAD/REJECT per candidate:

```bash
# Bounded run with GLM for cheap outer-loop reasoning
remyxai outrider explore --repo owner/name --cycles 5 --budget 50 \
   --provider zai --model glm-5.2

# Dry-run: hypothesis stage only, no dispatch (cheapest inspection)
remyxai outrider explore --repo owner/name --dry-run
```

Safety-by-design: never merges autonomously, respects preflight REJECTs, respects `--budget` (USD) and `--cycles` caps. Trace lands at `.remyx-autoresearch/trace.jsonl` in the current directory.

## Third-party shepherding discipline

Before dispatching against a repo you don't own, check for coordination signal:

1. Open feature-request issues on the target (search title + body for the mechanism)
2. Closed prior attempts on the same mechanism
3. In-flight PRs touching the same file/module

Coordinate — comment on the relevant issue or file a new one — **before** filing the PR. `huggingface/peft#3382` is the reference case: coordination issue #3380 + original author's endorsement preceded the PR and made review-engagement possible. Without that signal, even a technically-sound PR is friction.

## Common status checks

```bash
# Recent dispatches on a fork
gh run list --repo <owner>/<fork> --workflow outrider.yml -L 5

# Run summary + cost/token usage
gh run view <run-id> --repo <owner>/<fork>
```

## Interest ID drift across forks

The **source of truth** for which interest a fork is using is `<fork>/.github/workflows/outrider.yml` — not any local Python mapping or config. If you're auditing or updating, patch the workflow file directly (`gh api -X PUT /repos/<fork>/contents/.github/workflows/outrider.yml`), not a side file that may lag.
