# Remyx Recommendation — GitHub Action

A composite GitHub Action that, on a schedule you choose, picks the next arXiv paper for your team to integrate (via the Remyx engine API) and opens a draft pull request with a scaffolded implementation written by Claude Code.

```yaml
- uses: remyxai/remyx-recommendation-action@v1
  with:
    interest-id: ${{ vars.REMYX_INTEREST_ID }}
```

## What you get

Each scheduled run:

1. Queries `engine.remyx.ai` for the top-ranked paper against your team's configured `ResearchInterest` (Remyx already knows your repo's commit history and codifies what your team has been building).
2. Either:
   - Opens a **draft pull request** in your repo with a scaffolded integration module, matching tests, and a README append, OR
   - Opens an **Issue** with the paper's details and a discussion of what would block a clean integration — when Claude Code determines the paper can't be cleanly scaffolded against your existing code.
3. Reports outputs (`status`, `pr_url`, `issue_url`, `arxiv`, `tier`) you can chain into downstream steps.

## Setup (5 minutes)

### 1. Configure your interest at engine.remyx.ai

Sign up at [engine.remyx.ai](https://engine.remyx.ai). Connect your GitHub repo — Remyx ingests your commit history and creates a `ResearchInterest` that captures your team's research focus. Edit the interest's context body to sharpen the framing.

### 2. Get your `REMYX_API_KEY`

From the engine.remyx.ai Settings page, generate a long-lived API key for this repo's automation. Copy it.

### 3. Add secrets to your repo

In your repo's **Settings → Secrets and variables → Actions**, add:

| Secret | Source |
|---|---|
| `REMYX_API_KEY` | the key from step 2 |
| `ANTHROPIC_API_KEY` | your Anthropic key from [console.anthropic.com](https://console.anthropic.com) — used to invoke Claude Code |

### 4. Add the workflow

Create `.github/workflows/remyx-recommendation.yml`:

```yaml
name: Remyx Recommendation

on:
  schedule:
    - cron: '0 6 * * 1'        # Mondays at 06:00 UTC; pick whatever cadence suits you
  workflow_dispatch:            # also lets you trigger manually

jobs:
  recommend:
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
      issues: write
    env:
      # Inherited by the composite action's subprocesses.
      REMYX_API_KEY: ${{ secrets.REMYX_API_KEY }}
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    steps:
      - uses: remyxai/remyx-recommendation-action@v1
        with:
          interest-id: 'YOUR-INTEREST-UUID-HERE'
```

Replace `YOUR-INTEREST-UUID-HERE` with the UUID from engine.remyx.ai. (Tip: the engine UI offers a "copy workflow snippet" button that emits this YAML pre-filled.)

### 5. First run

Visit your repo's **Actions** tab → **Remyx Recommendation** → **Run workflow**. The first run takes 4-6 minutes; subsequent scheduled runs are identical. A draft PR appears in your **Pull Requests** tab when it completes.

## Inputs

| Input | Default | Description |
|---|---|---|
| `interest-id` | *(required)* | Remyx ResearchInterest UUID. |
| `github-token` | *(empty — falls back to `${{ github.token }}`)* | Token used for git push + PR/Issue creation. Leave unset for the standard same-repo install; the action will use the workflow's built-in `GITHUB_TOKEN`. Override with a PAT (`${{ secrets.MY_PAT }}`) only for cross-repo controller patterns. |
| `min-confidence` | `moderate` | Tier gate: `high` / `moderate` / `low`. Recommendations below this are skipped. |
| `draft-mode` | `always` | PR-draft policy: `always` (default), `on_test_failure`, or `never`. |
| `rate-limit-days` | `7` | Skip if a previous Remyx PR was opened within this window. Set `0` to rely only on per-paper dedup. |
| `guardrails-allowlist` | `''` | Comma-separated extra path globs Claude Code may modify (most repos don't need this). |

## Outputs

| Output | When set | Description |
|---|---|---|
| `status` | always | Run outcome (see status table below) |
| `pr_url` | when status starts with `pr_opened` | URL of the opened PR |
| `issue_url` | when `status == issue_opened` | URL of the opened Issue (paper couldn't be cleanly scaffolded) |
| `arxiv` | when a recommendation was fetched | arxiv_id of the picked paper |
| `tier` | when a recommendation was fetched | Confidence tier (`high` / `moderate` / `low` / `noise`) |

## Status codes

| Status | Meaning |
|---|---|
| `pr_opened` | PR opened ready-for-review (tests passed, `draft-mode != always`) |
| `pr_opened_draft` | PR opened as draft (the default under `draft-mode: always`, or when tests failed under `draft-mode: on_test_failure`) |
| `issue_opened` | Claude couldn't cleanly scaffold against your code; an Issue was opened with the paper + a discussion of what would block integration |
| `skipped_low_confidence` | Recommendation below `min-confidence` |
| `skipped_rate_limit` | A previous Remyx PR was opened within `rate-limit-days` |
| `skipped_pr_exists` | An open PR for this exact paper already exists |
| `skipped_test_failure` | Tests failed AND `draft-mode: never` |
| `claude_failed` | Claude CLI exited non-zero |
| `rejected_path_violations` | Claude touched files outside the guardrails allowlist; no PR opened |
| `error` | Unhandled exception (action step exits 1) |

## Guardrails — what Claude can and can't modify

Allowed paths (defaults):
- `<package>/*_integration.py` — new integration modules
- `tests/test_*.py` — new test files
- `.remyx-recommendation/**` — the spec bundle (scrubbed before commit, never lands in the PR)
- `README.md` — append-only attribution section

Always blocked:
- `.github/**`, `docker/**`, `pipelines/**`, `config/**`
- `requirements.txt`, `setup.py`, `pyproject.toml`, `MANIFEST.in`

If Claude touches anything outside the allowed set, the action rejects the run and does not open a PR. Use the `guardrails-allowlist` input to extend the allowed set for your repo.

## How it works

```
GitHub cron fires the workflow
       ↓
GET engine.remyx.ai/api/v1.0/papers/recommended?interest_id=<id>
GET engine.remyx.ai/api/v1.0/research-interests/<id>     ← team focus context
       ↓
Confidence + dedup gates
       ↓
Clone repo, branch from main
       ↓
Write .remyx-recommendation/ spec bundle (briefing for Claude)
       ↓
Invoke claude --dangerously-skip-permissions
       ↓
Either: open OPEN_AS_ISSUE.md (→ Issue mode) or write integration code
       ↓
Path-allowlist check + pytest
       ↓
Commit (with bundle scrubbed) + push + open draft PR
```

The recommendation engine (commit-history extraction, candidate pool, embedding pre-filter, Gemini ranking) lives server-side on engine.remyx.ai. This action is a pure consumer; the API call returns a fully-formed recommendation with reasoning + suggested experiment + interest context body.

## Costs

Per run:
- Remyx API: included in your engine.remyx.ai subscription
- Claude Code: ~$0.30-0.50 per draft-PR run (you bring your own `ANTHROPIC_API_KEY`)
- GitHub Actions minutes: ~5 minutes per run on `ubuntu-latest`

At weekly cadence (~4 runs/mo) with ~50% confidence-gate skip rate, expect ~$1-2/mo Claude + a handful of free-tier Actions minutes.

## License

Apache 2.0. See [LICENSE](./LICENSE).
