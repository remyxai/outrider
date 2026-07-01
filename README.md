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
```

Install Outrider on a repo (engine-driven via the Remyx GitHub App — writes the workflow, sets the repo secrets, opens a bot-authored setup PR):

```bash
remyxai outrider init --repo owner/name --auto-interest
```

From then on, the scheduled cron handles it — a draft PR or Issue appears each cycle. Trigger an ad-hoc run on a specific paper without waiting:

```bash
remyxai outrider trigger --repo owner/name --pin-method 2410.20305v2
remyxai outrider trigger --repo owner/name --pin-method "knowledge distillation"
```

(Free-text method query or a literal arxiv id; pinning bypasses the candidate-selection pass.)

Requires `REMYXAI_API_KEY` (from [engine.remyx.ai](https://engine.remyx.ai) Settings) and an Anthropic key (`--anthropic-key` or `ANTHROPIC_API_KEY`). See [`remyxai-cli`](https://github.com/remyxai/remyxai-cli) for bulk-install, per-dispatch routing flags, and secret management.

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
       inputs:
         pin-method:
           description: 'Optional arxiv_id or method query to implement directly.'
           required: false
           default: ''
         claude-timeout:
           description: 'Wall-clock seconds for Claude Code (preflight + implementation).'
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
             pin-method: ${{ inputs.pin-method }}
             claude-timeout: ${{ inputs.claude-timeout }}
   ```

   For multi-provider routing (route this run at z.ai's GLM endpoint vs Anthropic per dispatch), see [`docs/backends.md`](docs/backends.md) — adds a `provider` workflow_dispatch input + a `Configure provider auth` step. `outrider setup-local` (v0.4.3+) generates that shape by default.

6. **First run**: *Actions tab → Outrider → Run workflow*. Takes 4–6 minutes. A draft PR or Issue appears when complete.

</details>


## Costs

~$2–3 per full PR-route run on Anthropic Opus (recommend + chain); ~$0.50–1 with `chain: false`. Cost varies by provider and model — see [`docs/backends.md`](docs/backends.md). With the default cadence guard, expect ~$1–2/mo at typical engagement. You bring `ANTHROPIC_API_KEY`; Remyx API usage is covered by your engine.remyx.ai subscription.


## Examples

- **[smellslikeml/OpenRLHF PR #6](https://github.com/smellslikeml/OpenRLHF/pull/6)** — CFPO cross-modal grounding regularizer ([arXiv:2606.23206](https://arxiv.org/abs/2606.23206)). Full inline chain ran; canonical-first body shape; landed as ready-for-review.
- **[smellslikeml/agents PR #8](https://github.com/smellslikeml/agents/pull/8)** — Entity-binding preconditions for tool calls ([arXiv:2606.30531v1](https://arxiv.org/abs/2606.30531v1)). +561 LOC / 7 files: new `entity_binding` module with `EntityBindingError` subclassing existing `ToolError`, so the ambiguity/clarification message is routed back to the LLM via the repo's existing self-correct channel. Opt-in via `@function_tool(entities=resolver)` — behavior-preserving for tools that don't declare a resolver. Runnable example + tests included.
- **[smellslikeml/peft PR #5](https://github.com/smellslikeml/peft/pull/5)** — Riemannian preconditioner for LoRA ([arXiv:2402.02347v3](https://arxiv.org/abs/2402.02347v3)). +284/-2 LOC across 3 files: new `preconditioned_optimizer.py` with `create_riemannian_optimizer` wired into the MetaMathQA benchmark's optimizer dispatch, `test_preconditioned_optimizer.py`, and a surgical +7/-2 wiring edit. Any config selecting `optimizer_type: "riemannian"` trains with the r×r preconditioner while non-LoRA params update unchanged. _Human shepherded this Outrider draft into an upstream contribution attempt at [huggingface/peft PR #3382](https://github.com/huggingface/peft/pull/3382) (Draft), coordinated via [issue #3380](https://github.com/huggingface/peft/issues/3380)._
- **[smellslikeml/opik PR #8](https://github.com/smellslikeml/opik/pull/8)** — SAFARI persistent short-term memory tool for the agentic trace judge ([arXiv:2606.24626v1](https://arxiv.org/abs/2606.24626v1)). +420/-3 LOC across 5 files: new `memory` `ToolExecutor` (record/recall/clear keyed notepad) in `default_tool_registry()`, wired into the existing tool-call loop at `judge.py:70`; state on the tool instance and registry rebuilt per `AgenticLLMJudge` so notes survive every round of one loop yet never leak across evaluations; 16 KB recall cap matching sibling drill-in tools. Extends the existing multi-tool investigation loop as a peer of read/scan/search rather than sitting beside it.
- **[Paired-run analysis](https://gist.github.com/smellslikeml/36bf4939d76f0f84d113e2ddde5e6d3c)** — controlled A/B across 10 forks running the same paper-implementation pipeline. Same paper, same chain, different models; surfaces how identical inputs can produce different artifact verdicts (PR vs Issue) depending on the agent's self-review strictness.


## Documentation

- **[Configuration reference](docs/configuration.md)** — full inputs, outputs, status codes
- **[Customization](docs/customization.md)** — how to tailor Outrider to your repo + signals it reads
- **[Architecture](docs/architecture.md)** — selection taxonomy, pipeline, refinement chain
- **[Guardrails](docs/guardrails.md)** — what the agent can and can't modify
- **[Model backends](docs/backends.md)** — route at any Anthropic-Messages-compatible backend (z.ai's GLM, Bedrock, Vertex, on-prem); auth-header matrix; per-dispatch backend-switching workflow template
- **[Environments](docs/environments.md)** — describe workflow-attached tooling (Claude Code skills, MCP servers, custom search) via `ENVIRONMENTS.md` so the agent knows to reach for it
- **[Weekly summary mode](docs/weekly-summary.md)** — opt-in rolling digest comments


## License

Apache 2.0. See [LICENSE](./LICENSE).
