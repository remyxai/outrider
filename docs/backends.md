---
type: Customization Guide
title: Model backends
description: Route Outrider's agent at non-default model backends (z.ai GLM, Moonshot Kimi, Bedrock, Vertex, on-prem) — auth, workflow template, cost telemetry, debug.
resource: https://github.com/remyxai/outrider/blob/main/docs/backends.md
tags: [outrider, customization, model-backends, glm, kimi, moonshot, bedrock, vertex]
timestamp: 2026-07-16T00:00:00Z
---

# Model backends

Outrider's coding-agent step shells out to the `claude` CLI. Anything Claude Code can authenticate against, Outrider can route through.

By default Outrider talks to Anthropic's hosted API. To route at any other Anthropic-Messages-compatible backend — z.ai's GLM Coding Plan, Moonshot's Kimi, AWS Bedrock with Claude, GCP Vertex with Claude, an on-prem proxy — set the `model-base-url` action input. The Outrider engine doesn't care which backend served the response; the spec bundle, validators, refinement chain, and selection pass are all backend-agnostic.

## Supported backends

| Backend | `model-base-url` value | Secret | Default model | Recommended `claude-timeout` |
|---|---|---|---|---|
| Anthropic (default) | _(empty — uses `api.anthropic.com`)_ | `ANTHROPIC_API_KEY` | `claude-opus-4-8` | `900` (default) |
| z.ai / GLM Coding Plan | `https://api.z.ai/api/anthropic` | `ZAI_API_KEY` | `glm-5.2` | `900` (default) |
| Moonshot / Kimi | `https://api.moonshot.ai/anthropic` | `MOONSHOT_API_KEY` | `kimi-k3` | `3600` (thinking-mode adds per-turn latency) |
| AWS Bedrock (Claude) | `https://bedrock-runtime.<region>.amazonaws.com` | (AWS SigV4 — uses the workflow's `aws-actions/configure-aws-credentials` chain) | (varies per Bedrock configuration) | `900` (default) |
| GCP Vertex (Claude) | `https://<region>-aiplatform.googleapis.com/v1/projects/<proj>/...` | `GOOGLE_APPLICATION_CREDENTIALS` (OAuth via service-account JSON) | (varies per Vertex configuration) | `900` (default) |
| On-prem Anthropic-compat proxy | `https://<your-proxy>/v1` | (your convention) | (varies) | `900` (default) |

Naming convention: each provider's secret follows `<PROVIDER>_API_KEY` — matches the upstream conventions for `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `MOONSHOT_API_KEY`, etc. Customers reading the workflow YAML can grep the secret name to figure out which provider is wired.

The `claude-timeout` input threads through every phase (selection, deep-search, preflight, audit, implementation, self-review). Bumping it for a slow backend lifts the ceiling on all phases uniformly — there's no per-phase timeout knob, and none is needed.


## Auth-header matrix (and why setting both env vars breaks)

Different backends expect different auth headers. Claude Code uses two distinct env vars depending on the auth path:

| Env var | Header Claude Code sends | Right for |
|---|---|---|
| `ANTHROPIC_API_KEY` | `x-api-key: <value>` | Default Anthropic |
| `ANTHROPIC_AUTH_TOKEN` | `Authorization: Bearer <value>` | Non-Anthropic backends that expect Bearer auth: z.ai's GLM, Moonshot's Kimi (both gateways return HTTP 401 to `x-api-key`) |

> **Mutual exclusion.** Setting **both** env vars in the runner environment makes Claude Code prefer `ANTHROPIC_API_KEY` (the `x-api-key` path) — which non-Anthropic backends like z.ai reject. The two env vars are not additive; they're mutually exclusive, and the workflow must choose one per dispatch.

The job-level conditional `${{ inputs.provider == 'zai' && '' || secrets.ANTHROPIC_API_KEY }}` does NOT evaluate to `''` when the condition is true — GitHub Actions's `&& ''` short-circuits as falsy and `||` falls through to the third operand. The reliable way to set "one or the other, never both" is a step that writes to `$GITHUB_ENV` (which DOES support empty values cleanly). See the template below.


## Workflow template — per-dispatch provider + model switching

The canonical pattern for A/B-comparing Anthropic vs a non-default backend on the same repo. `remyxai outrider setup-local` (CLI v0.4.3+) generates the two-provider variant of this (anthropic + zai); adding the `moonshot` `case` branch shown below is a one-line fork-side edit until CLI support for it lands:

```yaml
on:
  workflow_dispatch:
    inputs:
      provider:
        description: 'Which model provider to route Claude Code at.'
        type: choice
        required: false
        default: 'anthropic'
        options:
          - anthropic
          - zai
          - moonshot
      model:
        description: 'Specific model name (e.g. claude-opus-4-8, glm-5.2, kimi-k3). Empty = provider default.'
        required: false
        default: ''
      pin-method:
        description: 'Optional arxiv_id or method query.'
        required: false
        default: ''

jobs:
  recommend:
    runs-on: ubuntu-latest
    env:
      REMYX_API_KEY: ${{ secrets.REMYX_API_KEY }}
      # ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_MODEL
      # are set in the 'Configure provider auth' step below
      # (auth env vars are mutually exclusive; ANTHROPIC_MODEL is
      # optional and only set when the workflow_dispatch input is
      # non-empty).
    steps:
      - name: Configure provider auth
        id: prov
        shell: bash
        env:
          ANTHROPIC_API_KEY_SECRET: ${{ secrets.ANTHROPIC_API_KEY }}
          ZAI_API_KEY_SECRET: ${{ secrets.ZAI_API_KEY }}
          MOONSHOT_API_KEY_SECRET: ${{ secrets.MOONSHOT_API_KEY }}
          MODEL_INPUT: ${{ inputs.model }}
        run: |
          case "${{ inputs.provider }}" in
            zai)
              echo "ANTHROPIC_AUTH_TOKEN=$ZAI_API_KEY_SECRET" >> "$GITHUB_ENV"
              echo "base_url=https://api.z.ai/api/anthropic" >> "$GITHUB_OUTPUT"
              ;;
            moonshot)
              echo "ANTHROPIC_AUTH_TOKEN=$MOONSHOT_API_KEY_SECRET" >> "$GITHUB_ENV"
              echo "base_url=https://api.moonshot.ai/anthropic" >> "$GITHUB_OUTPUT"
              ;;
            *)
              echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY_SECRET" >> "$GITHUB_ENV"
              echo "base_url=" >> "$GITHUB_OUTPUT"
              ;;
          esac
          if [ -n "$MODEL_INPUT" ]; then
            echo "ANTHROPIC_MODEL=$MODEL_INPUT" >> "$GITHUB_ENV"
          fi
      - uses: remyxai/outrider@v1
        with:
          interest-id: <uuid>
          pin-method: ${{ inputs.pin-method }}
          model-base-url: ${{ steps.prov.outputs.base_url }}
```

Key properties:

- Default behavior unchanged: `provider=anthropic` (the default) sets `ANTHROPIC_API_KEY` and leaves `model-base-url` empty, so existing customers see no change
- Single source of auth truth: the `Configure provider auth` step writes one and only one auth env var; subsequent steps inherit it
- Per-dispatch switchable: dispatch with `provider=zai` or `provider=moonshot` to route through that vendor for one run; default cron runs stay on Anthropic
- Model selection independent of provider: `--model glm-5.2` and `--model glm-4.6` both work with `--provider zai`; `--model kimi-k3` and `--model kimi-k2.7-code` both work with `--provider moonshot`; `--model claude-opus-4-8` and `--model claude-sonnet-4-6` both work with `--provider anthropic`
- Adding a new backend later (Bedrock, Vertex) is a new `case` branch plus a base-URL mapping


## Cost telemetry

Outrider tracks token counts straight from each Claude Code response envelope. Cost is computed from `tokens × rates`, with rate-table coverage per backend:

| `cost_basis` value | Meaning |
|---|---|
| `claude_code_envelope` | Default Anthropic path — the CLI's `total_cost_usd` field is authoritative because the CLI knows Anthropic's rates |
| `backend_rate_table` | Outrider has per-model rate rows for the configured backend (currently: `api.z.ai` covers `glm-5.2`/`glm-4.6`; `api.moonshot.ai` covers `kimi-k3`/`kimi-k2.7-code`/`kimi-k2.7-code-highspeed`). Cost is computed from `tokens × per-model rates`, keyed by `ANTHROPIC_MODEL` (or the envelope's `model` field when present) and overriding the CLI's Anthropic-rate estimate |
| (none + step-summary warning) | Backend isn't in the rate table; falling back to the CLI's value with a "may be approximate" annotation. Token counts stay accurate; dollars are approximate by however much the backend's pricing differs from Anthropic's |

When `ANTHROPIC_MODEL` names a model not in the host's rate row (e.g. a newly-released tier we haven't added yet), cost is computed at the host's default-tier rates (glm-5.2 for z.ai; kimi-k3 for Moonshot) — closer than nothing, but off by the tier delta (3-4x on tier pairs). Customers routing at a backend Outrider doesn't yet recognize see accurate token counts and a step-summary annotation flagging the cost approximation.


The step summary shows the agent + backend pair on every run:

```
**Cost & tokens this run**
- Agent: Claude Code → z.ai (GLM)
- Cost: `$0.0258` (computed from z.ai (GLM) PAYG rates)
- Tokens: 18,200 in / 6,800 out
- Claude calls: 5
```


## Troubleshooting: HTTP 401 from a non-Anthropic backend

The action runs a startup auth-env validation before any agent call that catches the most common misconfigurations — missing var, the literal `-` value (from `gh secret set --body -` stdin-disconnect ambiguity), suspiciously short values, leading/trailing whitespace, and both `ANTHROPIC_API_KEY` + `ANTHROPIC_AUTH_TOKEN` set non-empty under a non-default backend. If the check fires it surfaces an ERROR with a short hash + length diagnostic (the value itself is never echoed into the log) and exits non-zero before wasting any clone or prompt-build work.

When the startup check passes but a run still fails with `Failed to authenticate. API Error: 401`, work down the remaining checklist:

### 1. Did the startup check log a warning?

The action emits a `⚠ auth check:` warning (non-fatal) on a few softer conditions — leading/trailing whitespace was stripped, or both env vars are set under a non-default backend. The warning text names the fix. If you see one, address it first.

### 2. Is the secret value actually correct?

Length and shape can be fine while the value itself is stale or wrong. Probe the backend directly with the same env var the action receives — if the curl succeeds with HTTP 200, the env propagation is fine and the issue is inside Claude Code; if the curl also returns 401, the secret is wrong:

```yaml
- name: Diagnostic — direct backend probe
  shell: bash
  run: |
    code=$(curl -sS -o /tmp/probe.json -w "%{http_code}" \
      -X POST "https://api.z.ai/api/anthropic/v1/messages" \
      -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" \
      -H "anthropic-version: 2023-06-01" \
      -H "content-type: application/json" \
      --data '{"model":"glm-4.6","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}')
    echo "DIAG: backend probe → HTTP $code"
    head -c 200 /tmp/probe.json
```

If you need to re-set a secret, prefer file input (avoids the `--body -` stdin ambiguity that the startup check now catches):

```bash
printf '%s' "$YOUR_KEY" > /tmp/key
gh secret set ZAI_API_KEY --repo owner/name < /tmp/key
rm -f /tmp/key
```

### 3. Claude Code's bundled client auth precedence

If the direct probe returns 200 but the action still gets 401, check whether Claude Code's bundled client has its own auth-config precedence (some versions may prefer cached OAuth credentials over env vars).


## Related

- [`customization.md`](customization.md) — overview of every action input
- [`configuration.md`](configuration.md) — full reference table
