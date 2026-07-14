# Outrider — GitHub Action

Validating and comparing new methods against your own codebase is 10× the work of any single implementation. Schedule Outrider (or dispatch on demand) as a GitHub Action to wire arXiv methods (or your own design-doc leads) into real call sites, so your team can measure the change against the metrics you already track.

```yaml
- uses: remyxai/outrider@v1
  with:
    interest-id: ${{ vars.REMYX_INTEREST_ID }}
```

Each dispatch runs the coding agent in a fresh, ephemeral runner — candidates don't share state, testing variance stays low, and you can dispatch dozens per week without context pollution. Backends are pluggable: Anthropic Opus for the shipping commit, z.ai's GLM-5.2 at ~20× lower cost for scouting and branch-mode exploration.


## What you get

- **Draft PRs** wired to an existing call site, with a self-review noting what was implemented vs. left out
- **Issues** when preflight, validators, or self-review route the paper to discussion instead
- **Branch-only mode** (`publish: branch`) — pushes to the fork without opening a PR or Issue; explore N candidates before committing to any one
- **No duplicate work** — a paper isn't re-recommended once Outrider or a maintainer Issue references it
- **A selection narrative** in the step summary — why this paper, or why nothing this run


## Model backends

| Backend | Cost / full run | Best for |
|---|---|---|
| Anthropic Opus (default) | ~$2–3 | The commit — one candidate shipped as a draft PR |
| z.ai GLM-5.2 | ~$0.05–0.10 | Scouting, branch-mode exploration, batched candidate scans |

Route per-dispatch via a `provider` input — see [`docs/backends.md`](docs/backends.md) for the auth-header matrix and the switching workflow template. Rule of thumb: GLM for the exploration ladder, Opus for the candidate you commit to ship.


## Quickstart

```bash
pip install remyxai
remyxai outrider init --repo owner/name --auto-interest
```

Installs the action, writes the workflow, sets the secrets (`REMYX_API_KEY`, `ANTHROPIC_API_KEY`). Scheduled cron handles the weekly cadence from there.

Trigger an ad-hoc run:

```bash
remyxai outrider trigger --repo owner/name --pin-arxiv 2410.20305v2
remyxai outrider trigger --repo owner/name --pin-method "riemannian preconditioning LoRA optimizer"
```

`--pin-arxiv` implements the exact paper; `--pin-method` searches for the top hit. See [`remyxai-cli`](https://github.com/remyxai/remyxai-cli) for bulk-install and per-dispatch routing.

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

   For multi-provider routing (route this dispatch at z.ai's GLM endpoint vs Anthropic per run), see [`docs/backends.md`](docs/backends.md) — adds a `provider` input, a `ZAI_API_KEY` secret, and a `Configure provider auth` step. `outrider setup-local` (v0.4.3+) generates that shape by default; the workflow above is the Anthropic-only minimal path.

6. **First run**: *Actions tab → Outrider → Run workflow*. Takes 2–4 min on GLM, 4–6 min on Anthropic Opus. A draft PR, Issue, or branch appears when complete.

</details>


## Examples

Each PR below shows the **match** (what in the paper mapped to what in the repo) and the **shape** (how the wiring landed):

- **[peft #5](https://github.com/smellslikeml/peft/pull/5)** — Riemannian preconditioner for LoRA ([arXiv:2402.02347v3](https://arxiv.org/abs/2402.02347v3)). *Match:* the MetaMathQA benchmark already has a config-keyed optimizer dispatch. *Shape:* new `create_riemannian_optimizer`, +7/-2 wiring edit. _Human shepherded to [huggingface/peft #3382](https://github.com/huggingface/peft/pull/3382), coordinated via [issue #3380](https://github.com/huggingface/peft/issues/3380)._
- **[peft #8](https://github.com/smellslikeml/peft/pull/8)** — Scaling DoRA factored weight-norm ([arXiv:2603.22276v1](https://arxiv.org/abs/2603.22276v1)). *Match:* `DoraLinearLayer.forward` already exposes the `ENABLE_DORA_CACHING` opt-in-flag convention; a factored-norm path slots in behind a sibling `USE_FACTORED_DORA_NORM` flag, no dense `B @ A` product materialized. *Shape:* new `factored_weight_norm.py`, ~110 LOC diff, 5 tests parametrized over scaling values plus an end-to-end `LoraConfig(use_dora=True)` check; numerically equivalent to the dense path. Coordinated via [sockeye44/dorafactors #1](https://github.com/sockeye44/dorafactors/issues/1) where the PEFT maintainer explicitly invited the algorithmic path.
- **[OLMo-core #13](https://github.com/smellslikeml/OLMo-core/pull/13)** — Mechanism-driven preemptive instability monitor ([arXiv:2606.28116](https://arxiv.org/abs/2606.28116)). *Match:* `train/callbacks/` already has the reactive `StabilityMonitorCallback` shape; the preemptive variant registers alongside as a forward-hook + `record_metric` peer that fires thousands of steps before the loss diverges. *Shape:* `MechanismMonitorCallback` with QK spectral entropy + MoE routing entropy signals gated by a parameter-free rolling-window one-sided z-score detector; 12 tests covering GQA, layer/token sub-sampling, hook lifecycle, and state-dict window truncation. 
- **[OpenRLHF #14](https://github.com/smellslikeml/OpenRLHF/pull/14)** — MRPO step-level reward penalty ([arXiv:2606.31825v1](https://arxiv.org/abs/2606.31825v1)). *Match:* PPO advantages already carry per-step weighting; MRPO's decay factor slots in as a second multiplier. *Shape:* new hook wired into `RemoteExperienceMaker.compute_advantages_and_returns`, opt-in flag, default-off byte-identical. PR body names the sibling papers in the same PPO cluster as follow-ups.
- **[ag2 #9](https://github.com/smellslikeml/ag2/pull/9)** — Adaptive Context Elasticizer ([arXiv:2606.31564v1](https://arxiv.org/abs/2606.31564v1)). *Match:* `MiddlewareFactory` already extends the LLM-call pipeline; a new elastic middleware sits alongside `HistoryLimiter` / `TokenLimiter`. *Shape:* per-instance abstraction cache for reversibility, deterministic extractive digest keeps the middleware dependency-free.
- **[lerobot #9](https://github.com/smellslikeml/lerobot/pull/9)** — Dense Embodied Chain-of-Thought supervision ([arXiv:2606.30552v1](https://arxiv.org/abs/2606.30552v1)). *Match:* the annotator has staged language modules (plan / vqa); ECoT slots in as another stage with the same I/O contract. *Shape:* new `EcotReasoningModule`, wired into the executor as phase 4.5.
- **[atropos #16](https://github.com/smellslikeml/atropos/pull/16)** — Deterministic reward floor for reward-hacking mitigation ([arXiv:2606.27291v1](https://arxiv.org/abs/2606.27291v1)). *Match:* atropos exposes `RewardFunction` + `@registry.register` at `atroposlib/envs/reward_fns/`, the canonical extension point for reward-shape contributions in the RL environment framework. *Shape:* new `RewardFloor(RewardFunction)` with the paper's two rules (6-gram verbatim overlap + date-range lifted) — no invented detectors, uniform `-1.0` hard cap default, 28 tests covering rule triggers + registry integration + composition edge cases; docstring documents the grader-skip deviation (paper's other action doesn't map cleanly onto atropos's scalar reward contract).


## Documentation

- **[Configuration reference](docs/configuration.md)** — full inputs, outputs, status codes
- **[Customization](docs/customization.md)** — tailor Outrider to your repo + signals it reads
- **[Architecture](docs/architecture.md)** — selection taxonomy, pipeline, refinement chain
- **[Guardrails](docs/guardrails.md)** — what the agent can and can't modify
- **[Model backends](docs/backends.md)** — full backend/auth matrix + per-dispatch switching template
- **[Environments](docs/environments.md)** — describe workflow-attached tooling via `ENVIRONMENTS.md`
- **[Weekly summary mode](docs/weekly-summary.md)** — opt-in rolling digest comments


## License

Apache 2.0. See [LICENSE](./LICENSE).
