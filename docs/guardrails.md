---
type: Guardrails Spec
title: Guardrails
description: Path allowlist, block list, and edit-size caps the Outrider agent operates under.
resource: https://github.com/remyxai/outrider/blob/main/docs/guardrails.md
tags: [outrider, guardrails, security]
timestamp: 2026-06-30T03:57:23Z
---

# Guardrails

What Claude Code can and can't modify when Outrider drafts a PR.


## Allowed paths (defaults)

- `*.py` — any Python source, anywhere in the repo
- `.remyx-recommendation/**` — the spec bundle (scrubbed before commit)
- `**/*.md` — Markdown anywhere (README, CHANGELOG, docs/, ADR notes)

Extend the allowlist for your repo via the `guardrails-allowlist` input.


## Always blocked

- `.github/workflows/**` — an agent editing its own workflow file is the one change that compounds silently across future runs (expanding `permissions:`, swapping the interest-id, adding an exfil step), so it is the single hard block.

That is the **entire** always-blocked list. Dockerfiles, `*.sh`, dependency/build manifests, and lockfiles are **not** blocked — they surface in the normal PR diff for human review. Per-fork blocking of these was found to add no safety over review while producing false-negatives on legitimate changes (task YAML, adapter JSON, `.gitignore`, `uv.lock` regeneration). Teams that want stricter blocking add globs via the `guardrails-blocklist` input; blocklist matches take precedence over the allowlist.

## Risky-surface signal (route-to-human, not block)

Indirect prompt injection (arXiv:2607.20759) most reliably abuses a small set of file roles — dependency manifests/installs, CI, git hooks, and container/shell scripts. A diff that touches any of those is **flagged**, not blocked: the PR is forced to draft, labelled `risky-surface`, and gets a "review before landing" section in its body listing the files. This directs a reviewer's attention without rejecting legitimate changes — a new method that genuinely needs a dependency still opens as a draft PR, just flagged. The matched roles are `RISKY_SURFACE_GLOBS` in `src/run.py`.


## Integration checks (enforced after the Claude session)

- At most 3 new `.py` files per run
- At least one newly-added function/method/class must be invoked from another changed file (an import alone doesn't count)

Historical note: a per-existing-file line-count cap was removed after observation that it produced false-negatives on legitimate paper implementations — large-but-focused trainer rewrites and big test additions matching a paper's property-test surface were flipping to Issue against the reviewer's actual preference. Scope discipline now lives downstream in the convention pass, which reads the target repo's own PR history rather than applying a hardcoded ceiling.


## What enforcement looks like

If Claude touches a path outside the allowlist, the run terminates with `rejected_path_violations` rather than opening a malformed PR. The validation runs after the Claude session and before any git push, so violations are caught locally — nothing reaches your repo.

The integration validator catches the "added code that nothing invokes" failure mode and downgrades to `issue_opened_no_integration`. That's the same signal as self-review's orphan check, but earlier in the pipeline.
