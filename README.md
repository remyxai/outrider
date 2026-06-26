# Outrider — GitHub Action

A GitHub Action that picks the next arXiv paper most implementable in your codebase — opens a draft PR wiring it into an existing call site, or an Issue if a PR would be premature. Won't re-recommend a paper that's already in front of your team.

```yaml
- uses: remyxai/outrider@v1
  with:
    interest-id: ${{ vars.REMYX_INTEREST_ID }}
```


## What you get

- **Draft PRs** that wire a paper's contribution into an existing module, with a self-review section honestly noting what was implemented vs. left out
- **Issues** when a PR would be premature — pre-flight, validators, or self-review route the paper to discussion instead
- **No duplicate work** — the same paper isn't re-recommended once any Outrider or maintainer Issue references it; reopen to re-engage
- **A selection narrative** in the run's GitHub Actions step summary explaining why this paper (or why nothing actionable this run)


## Quickstart

```bash
pip install remyxai
remyxai outrider init --repo owner/name --auto-interest
```

The [`remyxai` CLI](https://github.com/remyxai/remyxai-cli) installs Outrider on a target repo via the Remyx GitHub App: writes the workflow, sets the repo secrets, and opens a bot-authored setup PR. Your local git isn't touched.

Requires `REMYXAI_API_KEY` (from [engine.remyx.ai](https://engine.remyx.ai) Settings) and an Anthropic key (`--anthropic-key` or `ANTHROPIC_API_KEY`).

<details>
<summary><b>Manual install (5 minutes)</b></summary>

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

6. **First run**: *Actions tab → Outrider → Run workflow*. Takes 4–6 minutes. A draft PR or Issue appears when complete.

</details>


## Costs

~$5–6 in Claude spend per full PR-route run (recommend + inline refinement chain); ~$1–2 for recommend-only with `chain: false`. With the default cadence guard, expect ~$2–4/mo at typical engagement patterns. You bring `ANTHROPIC_API_KEY`; Remyx API usage is covered by your engine.remyx.ai subscription.


## Examples

- **[smellslikeml/OpenRLHF PR #6](https://github.com/smellslikeml/OpenRLHF/pull/6)** — CFPO cross-modal grounding regularizer ([arXiv:2606.23206](https://arxiv.org/abs/2606.23206)). Full inline chain ran; canonical-first body shape; landed as ready-for-review.
- **[smellslikeml/NeMo-Curator Issue #5](https://github.com/smellslikeml/NeMo-Curator/issues/5)** — FinerWeb-10BT line-level filtering ([arXiv:2501.07314](https://arxiv.org/abs/2501.07314)). Paper-anchored fidelity audit (no public reference impl); self-review correctly routed to Issue.


## Documentation

- **[Configuration reference](docs/configuration.md)** — full inputs, outputs, status codes
- **[Customization](docs/customization.md)** — how to tailor Outrider to your repo + signals it reads
- **[Architecture](docs/architecture.md)** — selection taxonomy, pipeline, refinement chain
- **[Guardrails](docs/guardrails.md)** — what the agent can and can't modify
- **[Weekly summary mode](docs/weekly-summary.md)** — opt-in rolling digest comments


## License

Apache 2.0. See [LICENSE](./LICENSE).
