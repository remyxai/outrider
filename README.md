# Outrider — GitHub Action

Scouts the arXiv frontier for your repo. On a schedule you choose, Outrider picks the next paper most implementable against your codebase and either opens a draft PR wiring it into an existing call site, or starts a discussion Issue when a PR would be premature.

<p align="center">
  <img src="https://github.com/remyxai/outrider/releases/download/readme-assets/outrider-v1.gif" alt="Outrider demo" width="800">
</p>

```yaml
- uses: remyxai/outrider@v1
  with:
    interest-id: ${{ vars.REMYX_INTEREST_ID }}
```

## What you get

- **Draft PRs** that wire a paper's contribution into an existing module, with a self-review section in the body honestly noting what was implemented vs. left out
- **Issues** when a PR would be premature — pre-flight, validators, or self-review route the paper to discussion instead of scaffold-shaped PRs
- **One artifact per `rate-limit-days`** by default — no Issue spam

## Setup

Two install paths — pick whichever fits.

### One-command install (CLI)

The [`remyxai` CLI](https://github.com/remyxai/remyxai-cli) installs Outrider on a target repo via the Remyx GitHub App: writes the workflow, sets the repo secrets, and opens a bot-authored setup PR. Your local git isn't touched.

```bash
pip install remyxai
remyxai outrider init --repo owner/name --auto-interest
```

Requires `REMYXAI_API_KEY` (from [engine.remyx.ai](https://engine.remyx.ai) Settings) and an Anthropic key (`--anthropic-key` or `ANTHROPIC_API_KEY`). The `--auto-interest` flag auto-creates a `ResearchInterest` from the repo if one doesn't exist; drop it if you already have an interest UUID to wire in. If the Remyx GitHub App isn't installed on the target repo yet, the command surfaces the install link.

### Manual install (5 minutes)

1. **Sign up at [engine.remyx.ai](https://engine.remyx.ai)** and connect your repo. Remyx ingests your commit history and creates a `ResearchInterest`. Edit its context body to sharpen the framing.

2. **Generate a `REMYX_API_KEY`** from the engine.remyx.ai Settings page.

3. **Add two secrets** in your repo's *Settings → Secrets and variables → Actions*:
   - `REMYX_API_KEY` — from step 2
   - `ANTHROPIC_API_KEY` — your key from [console.anthropic.com](https://console.anthropic.com)

4. **Allow Actions to open PRs**: *Settings → Actions → General → Workflow permissions* → ☑ *Allow GitHub Actions to create and approve pull requests*. (Without this, the action returns `HTTP 403` at PR creation.)

5. **Add the workflow** at `.github/workflows/outrider.yml`:

   ```yaml
   name: Outrider
   on:
     schedule:
       - cron: '0 14 * * 1'  # Mondays 14:00 UTC; pick any cadence
     workflow_dispatch:
   jobs:
     recommend:
       runs-on: ubuntu-latest
       permissions:
         contents: write
         pull-requests: write
         issues: write
       env:
         REMYX_API_KEY: ${{ secrets.REMYX_API_KEY }}
         ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
       steps:
         - uses: remyxai/outrider@v1
           with:
             interest-id: 'YOUR-INTEREST-UUID-HERE'
   ```

   (Tip: the engine.remyx.ai UI has a "copy workflow snippet" button that emits this pre-filled.)

6. **First run**: *Actions tab → Outrider → Run workflow*. Takes 4–6 minutes. A draft PR or Issue appears when complete.

## Inputs

| Input | Default | Description |
|---|---|---|
| `interest-id` | *(required)* | Remyx ResearchInterest UUID |
| `github-token` | `${{ github.token }}` | Override only for cross-repo controller patterns |
| `min-confidence` | `moderate` | Tier gate: `high` / `moderate` / `low` |
| `draft-mode` | `always` | `always` / `on_test_failure` / `never` |
| `rate-limit-days` | `7` | Cadence guard. Skip the run if any Remyx artifact (PR **or** Issue) was opened within this window. Set `0` to disable. |
| `guardrails-allowlist` | `''` | Extra path globs Claude Code may modify, **added on top of** the defaults (`*.py`, `.remyx-recommendation/**`, `**/*.md`). Most repos won't need this. |
| `test-integration-policy` | `strict` | `strict` (demote to Issue if new tests don't import an existing module) / `soft` (open draft PR with warning) / `off` (skip the gate). Use `soft` for layer/component repos where standalone modules are the contribution. |
| `lookback` | `week` | Candidate pool window: `today` / `week` / `month` |
| `candidate-pool` | `25` | How many candidates the selection pass picks from |
| `claude-timeout` | `900` | Wall-clock seconds for the Claude Code implementation step. Bump for very large repos; lower to cap cost. |
| `pin-arxiv` | `''` | Optional `arxiv_id`. When set and present in the candidate pool, the action implements that exact paper and skips the selection pass — use it for reproducible eval re-runs. Empty = normal selection. |

## Outputs

| Output | When | Description |
|---|---|---|
| `status` | always | Run outcome — see status codes below |
| `pr_url` | `pr_opened*` | URL of the opened PR |
| `issue_url` | `issue_opened*` | URL of the opened Issue |
| `arxiv` | when a paper was picked | arxiv_id |
| `tier` | when a paper was picked | `high` / `moderate` / `low` / `noise` |
| `cost_usd` | always | Claude spend for this run |
| `input_tokens` / `output_tokens` | always | Token usage |

## Costs

- **Claude Code**: ~$2–3 per PR-track run (pre-flight + selection + implementation + self-review). Issue-track runs cost less since they skip the implementation pass. You bring `ANTHROPIC_API_KEY`.
- **Remyx API**: included in your engine.remyx.ai subscription.
- **GitHub Actions**: ~6–8 min on `ubuntu-latest` per run.

At weekly cadence (default `rate-limit-days: 7`), expect ~$2–4/mo Claude.

<details>
<summary><b>Status codes</b> (17 outcomes)</summary>

| Status | Meaning |
|---|---|
| `pr_opened` | PR opened ready-for-review (tests passed, `draft-mode != always`) |
| `pr_opened_draft` | PR opened as draft |
| `issue_opened_preflight` | Pre-flight Claude pass routed to Issue before implementation |
| `issue_opened` | Claude elected Issue-mode (wrote `OPEN_AS_ISSUE.md` instead of code) |
| `issue_opened_no_integration` | Diff adds code that nothing invokes |
| `issue_opened_stub_density` | New module is ≥50% stubs (`pass` / `NotImplementedError` / empty bodies) |
| `issue_opened_no_test_integration` | New tests don't import from any pre-existing module |
| `issue_opened_self_review` | Self-review judged the new code an orphan, unreachable from production |
| `skipped_low_confidence` | Recommendation below `min-confidence` |
| `skipped_rate_limit` | A Remyx PR or Issue was opened within `rate-limit-days` |
| `skipped_pr_exists` | Every candidate already has an open PR |
| `skipped_issue_exists` | Every candidate already has an open Remyx Issue — close one to retry that paper |
| `skipped_by_selection_verification` | Selection pass verified every candidate against the repo and rejected all (none structurally fit the existing modules) |
| `skipped_test_failure` | Tests failed AND `draft-mode: never` |
| `claude_failed` | Claude CLI exited non-zero |
| `rejected_path_violations` | Claude touched files outside the guardrails allowlist |
| `error` | Unhandled exception |

</details>

<details>
<summary><b>Guardrails</b> — what Claude can and can't modify</summary>

**Allowed paths** (defaults):
- `*.py` — any Python source, anywhere in the repo
- `.remyx-recommendation/**` — the spec bundle (scrubbed before commit)
- `**/*.md` — Markdown anywhere (README, CHANGELOG, docs/, ADR notes); the 50-line edit cap still applies to existing files

**Always blocked** by *role* (filename/type), not directory:
- `.github/**` — CI / workflow config
- `*Dockerfile`, `*Dockerfile.*`, `*.dockerfile`, `*.sh` — container builds and shell scripts
- `*requirements*.txt`, `setup.py`, `setup.cfg`, `pyproject.toml`, `MANIFEST.in`, `*.lock` — dependency / build manifests

The block list takes precedence over the allowlist. Non-`.py` config not on the block list (e.g. `pipelines/*.yaml`) simply isn't allowed either.

**Edit-size caps** (enforced after the Claude session):
- Each edit to a pre-existing file: ≤50 net lines (additions + deletions)
- At most 3 new `.py` files per run
- At least one newly-added function/method/class must be invoked from another changed file (an import alone doesn't count)

Extend the allowlist for your repo via the `guardrails-allowlist` input.

</details>

<details>
<summary><b>How it works</b></summary>

```
GitHub cron fires the workflow
       ↓
Query engine.remyx.ai for the candidate pool + interest context
       ↓
Rate-limit + per-paper dedup + confidence gates
       ↓
Clone the target repo + detect package / default branch
       ↓
Selection pass: verify each promising candidate against the cloned repo
(gh code-search + file reads). May reject all and exit early.
       ↓
Write the .remyx-recommendation/ spec bundle
       ↓
Pre-flight Claude pass: PR or Issue?
       ↓                              ↓
     ISSUE                            PR
       ↓                              ↓
   open Issue        Invoke Claude Code (implement integration)
                                      ↓
                     Path-allowlist + integration validator
                     (new module must be imported by a modified file)
                                      ↓
                     Stub-density + pytest + test-integration check
                                      ↓
                     Self-review pass (downgrade to Issue if orphan)
                                      ↓
                     Commit (bundle scrubbed) + push + open draft PR
```

The Remyx engine (commit-history extraction, candidate pool, embedding pre-filter, ranking) runs server-side. This action is a pure consumer.

</details>

## License

Apache 2.0. See [LICENSE](./LICENSE).
