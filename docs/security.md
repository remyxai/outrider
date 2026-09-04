---
type: Security Model
title: Defense-in-depth
description: The layered controls that keep an autonomous coding agent, driven by untrusted GitHub issue/PR text, from exfiltrating secrets or landing unreviewed changes.
resource: https://github.com/remyxai/outrider/blob/main/docs/security.md
tags: [outrider, security, prompt-injection, defense-in-depth]
timestamp: 2026-08-13T00:00:00Z
---

# Defense-in-depth

Outrider drives an autonomous coding agent from **untrusted input** — the candidate
brief is assembled from arbitrary GitHub issue/PR text, which is the classic vector for
indirect prompt injection (arXiv:2607.20759). No single control is trusted to stop a
determined injection; instead six independent layers each fail safe, so an attack has to
defeat all of them to cause harm. The durable controls sit **outside** the model's
influence (deterministic code, stripped env, path enforcement); the model-facing layers
(input wrapping, canary) are treated as *signals*, never guarantees.

![Outrider agent-harness defense-in-depth](https://github.com/user-attachments/assets/180c6573-ad42-4502-b474-df79e7515034)

## The layers

**L1 — Trust boundary (ingestion).** *Secondary signal.* Every piece of untrusted text
is wrapped in `<untrusted_content source=…>` tags with a preamble instructing the agent
to treat it as data, not instructions (`_wrap_untrusted_content`, `src/run.py`). This
reduces naive injections; it is not relied on alone.

**L2 — Capability reduction (blast radius).** *Durable.* The spawned agent runs under a
PreToolUse Bash deny-gate wired via `claude --settings` (`src/agent_bash_gate.sh`),
fail-closed (exit 2), blocking package installs and any outbound network command. Its
subprocess environment is **whitelisted** at launch — `GITHUB_TOKEN`, `REMYX_API_KEY`
and `INPUT_*` are stripped, so `printenv`/`cat` can't surface them
(`tests/test_claude_subprocess_env.py`).

**L3 — Credential-leak prevention.** *Durable.* The clone's origin is rewritten
token-less so no credential persists in `.git/config`; the push re-authenticates
ephemerally. Every outbound payload (PR/Issue body, API calls) passes a **fail-closed**
scrubber (`_scrub_outbound_payload`): a credential match **aborts** the run
(`aborted_secret_in_payload`) rather than being silently redacted.

**L4 — Per-run canary.** *Detection signal.* The invocation template requires the agent
to emit a per-run canary trailer derived from `GITHUB_RUN_ID` + target repo. A missing or
wrong canary — the fingerprint of an injection that hijacked the agent's output —
downgrades routing from PR to Issue for a human to inspect.

**L5 — Deterministic post-agent verification.** *Durable.* After the session, before any
push: a path block-list (editing `.github/workflows/**` is the one hard block), a
**risky-surface** flag that routes dependency/CI/hook/container diffs to human review, an
integration gate (new code must be invoked by a changed file), and a stub-density gate.
See [Guardrails](guardrails.md) for the full path policy.

**L6 — Human-in-the-loop gate.** *External control.* Nothing auto-merges: output is
either a **draft PR** or a **human-review Issue**. The run also fails closed — a secret
match or a guard trip aborts rather than filing — and a maintainer can disable the
workflow outright.

## Where the model key lives

The provider key is a repo Actions secret, decrypted only inside the ephemeral
runner. Installing with `remyxai outrider init --github-secrets-only` seals it
against that repo's own Actions public key on your machine, so it reaches
GitHub as ciphertext and no copy is stored anywhere else. A manual install
(see [manual-install.md](manual-install.md)) has the same property — you set
the secret yourself.

## What we deliberately do not rely on

The two model-facing layers (L1 wrapping, L4 canary) are **signals**, not guarantees — a
sufficiently clever injection can evade either. The security argument rests on the durable
layers (L2, L3, L5, L6), which are deterministic code the model cannot reach. See
[Architecture](architecture.md) for where each control sits in the pipeline.
