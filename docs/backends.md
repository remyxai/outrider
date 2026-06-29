---
type: customization_guide
description: How to route Outrider's Claude Code agent at any Anthropic-Messages-compatible model backend — z.ai's GLM Coding Plan, AWS Bedrock, GCP Vertex, on-prem proxies — including the auth-header gotchas, the workflow template, cost telemetry behavior, and a 401-debug checklist.
tags: [outrider, customization, model-backend, glm, bedrock, vertex, configuration]
---

# Model backends

Outrider's coding-agent step shells out to the `claude` CLI. Anything Claude Code can authenticate against, Outrider can route through.

By default Outrider talks to Anthropic's hosted API. To route at any other Anthropic-Messages-compatible backend — z.ai's GLM Coding Plan, AWS Bedrock with Claude, GCP Vertex with Claude, an on-prem proxy — set the `model-base-url` action input. The Outrider engine doesn't care which backend served the response; the spec bundle, validators, refinement chain, and selection pass are all backend-agnostic.

## Supported backends

| Backend | `model-base-url` value | Recommended secret name |
|---|---|---|
| Anthropic (default) | _(empty — uses `api.anthropic.com`)_ | `ANTHROPIC_API_KEY` |
| z.ai / GLM Coding Plan | `https://api.z.ai/api/anthropic` | `ZAI_API_KEY` |
| AWS Bedrock (Claude) | `https://bedrock-runtime.<region>.amazonaws.com` | (AWS SigV4 — uses the workflow's `aws-actions/configure-aws-credentials` chain) |
| GCP Vertex (Claude) | `https://<region>-aiplatform.googleapis.com/v1/projects/<proj>/...` | `GOOGLE_APPLICATION_CREDENTIALS` (OAuth via service-account JSON) |
| On-prem Anthropic-compat proxy | `https://<your-proxy>/v1` | (your convention) |

Naming convention: each provider's secret follows `<PROVIDER>_API_KEY` — matches the upstream conventions for `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc. Customers reading the workflow YAML can grep the secret name to figure out which provider is wired.


## Auth-header matrix (and why setting both env vars breaks)

Different backends expect different auth headers. Claude Code uses two distinct env vars depending on the auth path:

| Env var | Header Claude Code sends | Right for |
|---|---|---|
| `ANTHROPIC_API_KEY` | `x-api-key: <value>` | Default Anthropic |
| `ANTHROPIC_AUTH_TOKEN` | `Authorization: Bearer <value>` | z.ai's GLM (Bearer is what their gateway accepts; `x-api-key` returns HTTP 401) |

> **Mutual exclusion.** Setting **both** env vars in the runner environment makes Claude Code prefer `ANTHROPIC_API_KEY` (the `x-api-key` path) — which non-Anthropic backends like z.ai reject. The two env vars are not additive; they're mutually exclusive, and the workflow must choose one per dispatch.

The job-level conditional `${{ inputs.backend == 'glm' && '' || secrets.ANTHROPIC_API_KEY }}` does NOT evaluate to `''` when the condition is true — GitHub Actions's `&& ''` short-circuits as falsy and `||` falls through to the third operand. The reliable way to set "one or the other, never both" is a step that writes to `$GITHUB_ENV` (which DOES support empty values cleanly). See the template below.


## Workflow template — per-dispatch backend switching

The canonical pattern for A/B-comparing Anthropic vs a non-default backend on the same repo:

```yaml
on:
  workflow_dispatch:
    inputs:
      backend:
        description: 'Which model backend to route Claude Code at.'
        type: choice
        required: false
        default: 'anthropic'
        options:
          - anthropic
          - glm
      pin-method:
        description: 'Optional arxiv_id or method query.'
        required: false
        default: ''

jobs:
  recommend:
    runs-on: ubuntu-latest
    env:
      REMYX_API_KEY: ${{ secrets.REMYX_API_KEY }}
      # ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are set in the
      # 'Configure backend auth' step below (one or the other, never both).
    steps:
      - name: Configure backend auth
        shell: bash
        env:
          ANTHROPIC_API_KEY_SECRET: ${{ secrets.ANTHROPIC_API_KEY }}
          ZAI_API_KEY_SECRET: ${{ secrets.ZAI_API_KEY }}
        run: |
          if [ "${{ inputs.backend }}" = "glm" ]; then
            echo "ANTHROPIC_AUTH_TOKEN=$ZAI_API_KEY_SECRET" >> "$GITHUB_ENV"
          else
            echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY_SECRET" >> "$GITHUB_ENV"
          fi
      - uses: remyxai/outrider@v1
        with:
          interest-id: <uuid>
          pin-method: ${{ inputs.pin-method }}
          model-base-url: ${{ inputs.backend == 'glm' && 'https://api.z.ai/api/anthropic' || '' }}
```

Key properties:

- Default behavior unchanged: `backend=anthropic` (the default) sets `ANTHROPIC_API_KEY` and leaves `model-base-url` empty, so existing customers see no change
- Single source of auth truth: the `Configure backend auth` step writes one and only one env var; subsequent steps inherit it
- Per-dispatch switchable: dispatch with `backend=glm` to route through z.ai for that one run; default cron runs stay on Anthropic
- Adding a new backend later (Bedrock, Vertex) is a per-elif branch in the Configure step plus a `model-base-url` mapping


## Cost telemetry

Outrider tracks token counts straight from each Claude Code response envelope. Cost is computed from `tokens × rates`, with rate-table coverage per backend:

| `cost_basis` value | Meaning |
|---|---|
| `claude_code_envelope` | Default Anthropic path — the CLI's `total_cost_usd` field is authoritative because the CLI knows Anthropic's rates |
| `backend_rate_table` | Outrider has a per-million-token rate entry for the configured backend (currently: `api.z.ai`). Cost is computed from `tokens × table rates`, overriding the CLI's Anthropic-rate estimate |
| (none + step-summary warning) | Backend isn't in the rate table; falling back to the CLI's value with a "may be approximate" annotation. Token counts stay accurate; dollars are approximate by however much the backend's pricing differs from Anthropic's |

Customers routing at a backend Outrider doesn't yet recognize see accurate token counts and a step-summary annotation flagging the cost approximation. Customer-supplied per-backend rate inputs are on the roadmap.


The step summary shows the agent + backend pair on every run:

```
**Cost & tokens this run**
- Agent: Claude Code → z.ai (GLM)
- Cost: `$0.0258` (computed from z.ai (GLM) PAYG rates)
- Tokens: 18,200 in / 6,800 out
- Claude calls: 5
```


## Troubleshooting: HTTP 401 from a non-Anthropic backend

When a glm-routed (or other-backend-routed) run fails with `Failed to authenticate. API Error: 401 token expired or incorrect`, work down this checklist before anything else:

### 1. Which env var is set?

The workflow must set `ANTHROPIC_AUTH_TOKEN` (Bearer) for any non-default backend that uses Bearer auth, NOT `ANTHROPIC_API_KEY` (x-api-key). Inspect the workflow YAML — if you see both set unconditionally at the job level, that's the bug.

### 2. Is the secret value truncated?

If the env var name is right, the value might still be wrong. The most common cause is `gh secret set --body -` with disconnected stdin — `gh` interprets the literal `-` as the body value, leaving the secret as a single `-` character. The backend then receives `Authorization: Bearer -` and naturally rejects.

Add a temporary diagnostic step that logs the value's length + a short hash (NOT the value itself):

```yaml
- name: Diagnostic — ANTHROPIC_AUTH_TOKEN shape
  shell: bash
  run: |
    if [ -z "$ANTHROPIC_AUTH_TOKEN" ]; then
      echo "DIAG: ANTHROPIC_AUTH_TOKEN is EMPTY"
    else
      len=${#ANTHROPIC_AUTH_TOKEN}
      sha=$(echo -n "$ANTHROPIC_AUTH_TOKEN" | sha256sum | cut -c1-8)
      echo "DIAG: ANTHROPIC_AUTH_TOKEN length=$len  sha8=$sha"
    fi
```

If `length` is in single digits, the secret is truncated. Re-set via file input (avoids the `--body -` ambiguity):

```bash
printf '%s' "$YOUR_KEY" > /tmp/key
gh secret set ZAI_API_KEY --repo owner/name < /tmp/key
rm -f /tmp/key
```

### 3. Is the backend actually responsive?

Probe the backend directly with the same env var the action receives. If the curl succeeds with HTTP 200, the env propagation is fine and the issue is inside Claude Code; if curl also returns 401, the secret is wrong:

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

If the probe returns 200 but the action still gets 401, check whether Claude Code's bundled client has its own auth-config precedence (some versions may prefer cached OAuth credentials over env vars). A startup guard for this class of failure is on the roadmap.


## Related

- [`customization.md`](customization.md) — overview of every action input
- [`configuration.md`](configuration.md) — full reference table
