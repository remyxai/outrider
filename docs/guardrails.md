---
type: guardrails_spec
description: Path allowlist, block list, and edit-size caps the Outrider agent operates under.
tags: [outrider, guardrails, security]
---

# Guardrails

What Claude Code can and can't modify when Outrider drafts a PR.


## Allowed paths (defaults)

- `*.py` — any Python source, anywhere in the repo
- `.remyx-recommendation/**` — the spec bundle (scrubbed before commit)
- `**/*.md` — Markdown anywhere (README, CHANGELOG, docs/, ADR notes); the 50-line edit cap still applies to existing files

Extend the allowlist for your repo via the `guardrails-allowlist` input.


## Always blocked (by role, not directory)

- `.github/**` — CI / workflow config
- `*Dockerfile`, `*Dockerfile.*`, `*.dockerfile`, `*.sh` — container builds and shell scripts
- `*requirements*.txt`, `setup.py`, `setup.cfg`, `pyproject.toml`, `MANIFEST.in`, `*.lock` — dependency / build manifests

The block list takes precedence over the allowlist. Non-`.py` config not on the block list (e.g. `pipelines/*.yaml`) simply isn't allowed either.


## Edit-size caps (enforced after the Claude session)

- Each edit to a pre-existing file: ≤50 net lines (additions + deletions)
- At most 3 new `.py` files per run
- At least one newly-added function/method/class must be invoked from another changed file (an import alone doesn't count)


## What enforcement looks like

If Claude touches a path outside the allowlist or violates an edit cap, the run terminates with `rejected_path_violations` rather than opening a malformed PR. The validation runs after the Claude session and before any git push, so violations are caught locally — nothing reaches your repo.

The integration validator (one of the edit-size caps) is also what catches the "added code that nothing invokes" failure mode that downgrades to `issue_opened_no_integration`. That's the same gate as self-review's orphan check, but earlier in the pipeline.
