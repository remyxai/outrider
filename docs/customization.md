---
type: customization_guide
description: How to tailor what Outrider does on your repo — the knobs you control and the signals it reads.
tags: [outrider, customization, configuration]
---

# Customization

How to tailor what Outrider does on your repo — the knobs you control and the signals it reads.

> For the full reference table of every input and output, see [`configuration.md`](configuration.md). This document is the higher-level "what shapes Outrider's behavior" guide.


## 1. What you directly control

### Selection bar — `min-confidence`

Outrider tags each candidate paper with a confidence tier (`high` / `moderate` / `low` / `noise`) from its relevance ranking. `min-confidence: moderate` (default) lets moderate+ candidates through; raise to `high` for stricter signal, lower to `low` for higher-volume exploration on quiet repos.

### Pool size & freshness — `lookback`, `candidate-pool`

- `lookback: week` (default) pulls candidates from the past 7 days. Use `today` for daily fresh runs, `month` for slow-cadence repos.
- `candidate-pool: 25` is how many candidates the selection pass considers. A larger pool widens the window but doesn't change the relevance bar.

### Routing strictness — `test-integration-policy`

- `strict` (default): if new tests don't import a pre-existing module, the run downgrades to Issue. Right for application / pipeline repos.
- `soft`: open the PR anyway with a warning section. Right for layer / component repos (graph NN, kernels) where new standalone modules ARE the contribution.
- `off`: skip the gate entirely.

### Draft-state policy — `draft-mode`

- `always` (default): every PR opens as draft. The chain's test gate flips it to ready-for-review on a passing run.
- `on_test_failure`: tests pass → ready, tests fail → draft.
- `never`: tests pass → ready, tests fail → run is skipped entirely (no draft PR).

### Reproducibility — `pin-arxiv`

Set to a specific `arxiv_id` and Outrider skips the selection pass entirely, implementing that exact paper. Use for eval re-runs and demos.

### Method-targeted runs — `pin-method`

Set to a free-text method query (e.g. `"knowledge distillation"`) or a literal `arxiv_id`. Outrider resolves it to the top arxiv match and implements it directly — bypassing the candidate pool and selection pass. Strict superset of `pin-arxiv`: it also works on papers outside the interest's pool (via direct asset lookup). Mutually exclusive with `pin-arxiv`.

### Model backend — `model-base-url`

Optional override that points the Claude Code subprocess at any Anthropic-Messages-compatible backend. Empty (default) = `api.anthropic.com`. The `ANTHROPIC_API_KEY` repo secret holds the backend's key when this is set, not your Anthropic key.

Common backends:

| Backend | `model-base-url` | Recommended secret name |
|---|---|---|
| z.ai / GLM Coding Plan | `https://api.z.ai/anthropic` | `ZAI_API_KEY` |
| AWS Bedrock (Claude) | `https://bedrock-runtime.<region>.amazonaws.com` | `AWS_BEARER_TOKEN_BEDROCK` |
| GCP Vertex (Claude) | `https://<region>-aiplatform.googleapis.com/v1/projects/<proj>/...` | `GOOGLE_APPLICATION_CREDENTIALS` |
| On-prem Anthropic-compat proxy | `https://<your-proxy>/v1` | (your convention) |

**Important — auth env vars are mutually exclusive.** Different Anthropic-Messages-compatible backends use different auth headers:

| Backend | Auth header Claude Code sends | Env var the workflow sets | Must leave UNSET |
|---|---|---|---|
| Anthropic (default) | `x-api-key: <key>` | `ANTHROPIC_API_KEY` | `ANTHROPIC_AUTH_TOKEN` |
| z.ai / GLM Coding Plan | `Authorization: Bearer <token>` | `ANTHROPIC_AUTH_TOKEN` | `ANTHROPIC_API_KEY` |

When **both** env vars are set in the runner env, Claude Code uses `ANTHROPIC_API_KEY` (the x-api-key path) — which z.ai's gateway rejects with HTTP 401. The two env vars must be set **conditionally**, not both at once.

For per-dispatch backend switching, store each provider's key under its own canonical name (`ANTHROPIC_API_KEY` + `ZAI_API_KEY`) and let a `backend` `workflow_dispatch` input pick which env var to set at runtime:

```yaml
on:
  workflow_dispatch:
    inputs:
      backend:
        type: choice
        options: [anthropic, glm]
        default: anthropic
env:
  # Mutually exclusive: only one of these is non-empty per dispatch.
  ANTHROPIC_API_KEY: ${{ inputs.backend == 'glm' && '' || secrets.ANTHROPIC_API_KEY }}
  ANTHROPIC_AUTH_TOKEN: ${{ inputs.backend == 'glm' && secrets.ZAI_API_KEY || '' }}
steps:
  - uses: remyxai/outrider@v1
    with:
      interest-id: <uuid>
      model-base-url: ${{ inputs.backend == 'glm' && 'https://api.z.ai/api/anthropic' || '' }}
```

Naming convention: each provider's secret follows `<PROVIDER>_API_KEY` (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ZAI_API_KEY`) — matches upstream conventions and reads cleanly at the workflow-YAML level. The action passes both `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` env vars through to the Claude Code subprocess; the workflow YAML is what decides which one carries a value for any given dispatch.

Cost telemetry is backend-aware. When `model-base-url` matches a known backend in the action's rate table (currently `api.z.ai` for GLM Coding Plan PAYG), the action overrides Claude Code's Anthropic-rate `total_cost_usd` and computes cost from `tokens × backend rates` — the dollars in the step summary are authoritative for that rate sheet. For unknown backends the action falls back to the CLI's reported value and flags it as approximate in the step summary. Token counts are accurate for any backend that speaks the Anthropic Messages protocol. The step summary surfaces both the **agent** (Claude Code) and the **model backend** (Anthropic / z.ai (GLM) / your-host) so you can see which model server actually served the run.

### Filesystem reach — `guardrails-allowlist`

Extra path globs Claude Code may touch, **added on top of** the defaults (`*.py`, `.remyx-recommendation/**`, `**/*.md`). Most repos don't need this. See [`guardrails.md`](guardrails.md) for full details on what's allowed and what's always blocked.

### Cadence guard — `rate-limit-days`

Time-decayed throttle. Skip a run only if the most recently opened Remyx PR/Issue on the target is *younger* than `rate-limit-days`. The default `7` means "don't pile on within a week of the last artifact"; older open artifacts age out of the window and stop blocking — recognizing that maintainers often leave Issues open for weeks without active triage. Engagement (merge or close) still clears the gate immediately. Set `0` to disable the guard entirely (useful for batch trials); set a higher value (e.g. `30`) for a stricter, slower cadence on busy repos.


## 2. What Outrider reads from your repo

The selection pass is **agentic** — Claude has read-only tools and consults multiple sources before picking a candidate. The more signal your repo emits, the more confident (and faster) the selection.

### Codebase structure

- **Module surface** — Outrider clones your repo and walks `*.py` files to identify call sites. The Diff Risk Score gate also reads `git log` to detect critical-path files.
- **Package layout** — auto-detected from `setup.py` / `pyproject.toml` / top-level `__init__.py` placement.

### Convention signals

- **Recent merged PRs** — Phase B (convention pass) extracts your PR-body template and merge cadence from the last ~30 merged PRs. Re-uses your headings, table shapes, checklist patterns.
- **`.github/ISSUE_TEMPLATE/`** — Phase B' (Issue-route convention pass) reads markdown frontmatter and Issue Forms (`.md` / `.yml` / `.yaml`), classifies templates by kind (bug / feature / new_model / question / other), and folds Outrider Issues into the best-fitting template. See [REMYX-146 design](architecture.md#issue-route-convention-pass) for the picker logic.
- **`CONTRIBUTING.md` / `ORIENTATION.md`** — if present, the drafting prompt consults them for lint configs and house style. Defers to your repo's own config (ruff / pre-commit / black / flake8) rather than hardcoding rules.

### Direction signals (for **extension**-shape picks)

The selection pass's strictest shape — proposing a new capability your repo lacks — requires explicit team-direction signals before it'll route any candidate that way:

- **README roadmap section** — a "Coming soon" / "Roadmap" / "Planned" heading
- **CONTEXT.md investment pattern** — a recurring theme you've been shipping
- **Open `[RFC]` Issue** — a maintainer-authored thread inviting discussion of a capability

Without ≥1 of these, extension picks get rejected as "RFC-fishing." Add an RFC Issue or a roadmap line if you want Outrider to surface new-capability candidates.

### Discharge signals (to **not** re-recommend papers)

Outrider treats a paper as "discharged" — won't re-recommend it — when:

- **An Outrider-opened Issue exists** for that arxiv id (any state, open OR closed). Closing means "the team has decided."
- **A maintainer-opened Issue** (RFC, discussion) links the paper's arxiv id in its body — a stronger signal than Outrider's own.

Re-engagement lever: **reopen the Issue** to drop the paper from the discharge set so Outrider can re-recommend it.


## 3. What Outrider reads from your Research Interest

The `interest-id` input points at a `ResearchInterest` record in the Remyx engine — this is the primary surface for customizing what Outrider considers relevant.

- **Auto-interest context** — if you used `outrider init --auto-interest` (or `remyxai interests from-repo`), the engine extracts a structured `ExperimentHistory` from your commit log (3-pass extraction: themes, methods, infrastructure). This is the starting context.
- **Manual edits** — you can edit the interest's context body in the engine.remyx.ai UI to sharpen the framing. Concrete guidance ("we're focused on X but not Y") raises selection precision noticeably.
- **License preferences** — the interest carries license-class preferences. By default the action accepts `permissive` (Apache-2.0 / MIT / BSD); copyleft and NC licenses are gated.


## 4. Verification tools available to the selection pass

When the agent is choosing among candidates, it has these read-only tools at hand:

| Tool | Purpose |
|---|---|
| `gh code-search` | Verify a candidate's claimed call site exists in your repo |
| `gh api` | Read PRs, Issues, README, CONTRIBUTING.md, ISSUE_TEMPLATE directly |
| `gh-graph` | Walk a module's imports + reverse-imports (custom helper installed by the action) |
| `remyxai search query` | Broaden-search across the engine's full corpus when no in-pool candidate fits (the "deep research" refine path) |
| `remyxai search info` | Pull paper text + abstract by arxiv id for verification |

These tools are what make selection "structural fit" rather than keyword match.


## 5. Common customization recipes

| Goal | Set this |
|---|---|
| Higher-precision picks only | `min-confidence: high` |
| Allow proposing new capabilities your repo lacks | Add an RFC Issue or roadmap line; raise `min-confidence: high` (extension shape needs tier=high anyway) |
| Slow down cadence | Lengthen the cron schedule; default `rate-limit-days` already gates stacking |
| Batch trial mode (no cadence skipping) | `rate-limit-days: '0'` |
| Save cost on routine runs | `chain: false` (skip the refinement chain; ~$3-4 → ~$1-2 per run) |
| Eval re-run on a specific paper | `pin-arxiv: <arxiv_id>` |
| Layer/component repo (new modules ARE the contribution) | `test-integration-policy: soft` |
| Touch non-Python config (e.g. `pipelines/*.yaml`) | `guardrails-allowlist: 'pipelines/**/*.yaml'` |
| Quiet repo with little arxiv-relevant activity | `lookback: month`, `min-confidence: low` |


## 6. What you can't (currently) customize

These are baked into the action and not exposed as inputs — intentional design choices:

- **The four integration shapes** (addition / replacement / simplification / extension) and their tie-break ordering. Changing these would change Outrider's identity.
- **Self-review heuristics** (orphan detection, stub density threshold at 50%, integration validator). Tunable in source but not via inputs.
- **Phase A audit anchor selection** (reference-anchored vs paper-anchored). Auto-decided based on whether the paper has a reference repo URL.
- **Body-rewrite folding rules** (canonical-first, Discovery context details block, one-line attribution). The convention pass's prompt is fixed; the LLM's choices vary per repo.

If something in this list is blocking you, file an Issue on `remyxai/outrider` — we'll consider exposing it.
