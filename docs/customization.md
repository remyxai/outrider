---
type: Customization Guide
title: Customization
description: How to tailor what Outrider does on your repo — the knobs you control and the signals it reads.
resource: https://github.com/remyxai/outrider/blob/main/docs/customization.md
tags: [outrider, customization, configuration]
timestamp: 2026-06-30T03:57:23Z
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

Optional override that routes the Claude Code subprocess at any Anthropic-Messages-compatible backend (z.ai's GLM Coding Plan, Moonshot's Kimi, AWS Bedrock, GCP Vertex, on-prem proxies). Empty (default) = Anthropic's API.

The full backend matrix — supported backends with their base URLs, secret names, default models, and recommended `claude-timeout` values; the auth-header gotcha that bites on non-Anthropic backends (the env vars `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are mutually exclusive); the per-dispatch backend-switching workflow template; cost-telemetry behavior per backend; and a 401-debug checklist — lives in [`backends.md`](backends.md).

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
- **`.github/ISSUE_TEMPLATE/`** — Phase B' (Issue-route convention pass) reads markdown frontmatter and Issue Forms (`.md` / `.yml` / `.yaml`), classifies templates by kind (bug / feature / new_model / question / other), and folds Outrider Issues into the best-fitting template. See [architecture.md](architecture.md#issue-route-convention-pass) for the picker logic.
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


## 5. Two-tier drafter / refiner setup (recommended default)

Outrider ships with two companion workflow templates that split exploration and commitment across two schedules and two model tiers. This is the recommended shape for any repo you want the pipeline to run continuously on.

### The two roles

| Role | Cadence | Model | Publish | Distinctive defaults | Cost/run |
|---|---|---|---|---|---|
| **Drafter** ([`outrider-daily.yml`](../.github/workflows/outrider-daily.yml)) | Daily (or `*/15` for high frequency) | Anthropic Claude Haiku 4.5 | `branch` — no PR/Issue | `maintain-state=true`, `staged-synthesis=false` | ~$0.20-0.40 |
| **Refiner** ([`outrider-weekly-refine.yml`](../.github/workflows/outrider-weekly-refine.yml)) | Weekly (Monday noon UTC) | Anthropic Claude Opus 4.8 | `pr` — opens draft PR + chain | `maintain-state=true`, `staged-synthesis=true` | ~$5-10 (incl. gap-gen + chain) |

The drafter accumulates a pool of branches through the arxiv frontier — every dispatch adds an `observed_landing_zone` or `rejected_shape` to `.remyx/repo_intel.yaml`. The refiner picks one candidate from that pool each week, generates a targeted gap analysis via Sonnet 4.6, and dispatches an Opus refinement with the gap analysis piped in as `lead-content`. The refinement's chain (fidelity → convention → test) runs inline; terminal artifact is a ready-for-review draft PR.

### Why the split

- **Higher commit-to-PR rate on borderline cases.** With a drafter branch already anchoring the extension point + test scaffolding + landing-zone shape, Opus's preflight has more grounding to commit to PR-shape rather than defer to Issue.
- **Cheap exploration of the arxiv frontier.** Daily drafter dispatches sample from an ever-growing candidate pool (arxiv adds ~200 ML papers/day); a single-shot refiner-only setup would need spec-generation to substitute for that breadth, and spec generators saturate on any given paper × repo pair while the arxiv corpus keeps growing.
- **Compounding intel accumulation.** Even failed drafter branches (category errors, no-code-link candidates) contribute negative-space signal to `repo_intel.yaml` — future preflight decisions benefit from the accumulated observed / rejected shape history.

### Onboarding

Requires one secret: `ANTHROPIC_API_KEY`. Optional: `LINEAR_API_KEY` for the durable-state pattern (refiner can file gap analyses as Linear issues and read them back via URL), but the default setup passes the gap analysis inline as raw markdown so no Linear provisioning is needed.

For customers with existing z.ai / Moonshot credits or Anthropic-outage hedging needs, either role can be routed at a different backend via `model-base-url` — see [`backends.md`](backends.md).

### Manual triggers for testing

Both workflows expose `workflow_dispatch` so you can run either role on demand:
- Drafter: any workflow dispatch produces one branch immediately (bypasses the cron)
- Refiner: `pick-override=<branch-name>` selects a specific branch, `pick-override-arxiv=<id>` provides the arxiv when it can't be resolved from `repo_intel.yaml` (useful before the drafter has landed a full week of dispatches)


## 5.1. Common customization recipes

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
