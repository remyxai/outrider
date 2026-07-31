# Manual install (5 minutes)

The [`remyxai` CLI](https://github.com/remyxai/remyxai-cli) sets Outrider up in one command (`remyxai outrider init`). Prefer that when you can. This page is the by-hand alternative when the CLI isn't an option — CI-restricted environments, org policies against installing helper tooling on developer machines, or just when you want to see what the CLI does before running it.

## Steps

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
       - cron: '0 14 * * 1'   # Mondays 14:00 UTC; pick any cadence
     workflow_dispatch:
       inputs:
         pin-arxiv:
           description: 'Optional arxiv_id to implement directly (bypasses selection).'
           required: false
           default: ''
         search-method:
           description: 'Optional method query — searches for the top-hit paper and implements it.'
           required: false
           default: ''
         publish:
           description: 'pr (default) or branch — branch mode pushes to the fork without opening PR/Issue.'
           required: false
           default: 'pr'
         claude-timeout:
           description: 'Wall-clock seconds for the Claude Code agent (preflight + implementation).'
           required: false
           default: '900'
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
             pin-arxiv: ${{ inputs.pin-arxiv }}
             search-method: ${{ inputs.search-method }}
             publish: ${{ inputs.publish }}
             claude-timeout: ${{ inputs.claude-timeout }}
   ```

   For multi-provider routing (route this dispatch at z.ai's GLM endpoint vs Anthropic per run), see [`backends.md`](backends.md) — adds a `provider` input, a `ZAI_API_KEY` secret, and a `Configure provider auth` step. `outrider setup-local` (v0.4.3+) generates that shape by default; the workflow above is the Anthropic-only minimal path.

6. **First run**: *Actions tab → Outrider → Run workflow*. Takes 2–4 min on GLM, 4–6 min on Anthropic Opus. A draft PR, Issue, or branch appears when complete.
