# REVIEW.md — Outrider

Review conventions for this repo. Loaded as first-class context by
Outrider on dispatch. Complements `CLAUDE.md` (coordination-before-coding)
and `CONTRIBUTING.md` (broad contribution guide).

## Verdicts

- **approve** — diff is scoped, tests updated, CI green, no unrelated
  changes, PR body names the paper / issue / decision it lands
- **request-changes** — new code paths without tests, cross-cutting
  refactor bundled with a feature landing, or an anti-pattern below
- **comment** — advisory: style, alt approaches, docs, follow-up ideas.
  Non-blocking; author decides whether to address inline or later

## Test bar

- Unit tests for new logic under `src/`
- CI green on the PR branch
- Dispatch-touching changes: verify the change on a real trigger to a
  fixture fork before merge — reviewer-judgment which surfaces qualify;
  unit tests alone don't catch dispatch-shape regressions

## Scope stance

- One paper's core mechanism per PR. Cluster context (why this matters,
  what it enables) lives in the PR narrative, not the diff
- Refactor and feature don't land in the same PR — split
- Formatting / style changes → separate PR

## Formatter / style

- `ruff` for lint + format; enforced at CI, not review

## Author-preference policy

- External contributors welcome; no author-preference gate on this repo
- **When Outrider dispatches against an external target repo whose
  maintainer prefers author-submitted PRs**, we defer or open an Issue —
  this is the peft#3450 lesson. Target-repo REVIEW.md governs there,
  not this one

## Response norms

- Reviews on Outrider-drafted PRs route via `@remyx-ai <instruction>`
  (planned). Until that lands, maintainer reads and responds directly
- On stalled PRs, comment before rebasing — surface first, then re-shape

## Benchmark expectations

Outrider has no PyTorch-benchmark-shaped eval harness. Equivalents:

- PRs affecting scoring / ranking: run against a representative paper
  set and report top-K delta vs. main
- PRs adding a provider / backend: at least one end-to-end dispatch
  demonstrating the new provider works
- PRs touching preflight: run the preflight decision matrix and note
  any changed verdicts

## Anti-patterns

- Draft PRs opened before scope agreement with the maintainer
- Brittle rule (regex, hardcoded prompt template, per-observation case)
  where the fix belongs upstream in relevance ranking or downstream in
  verification
- Baking a single observation into a prompt template as a generalized rule
- Soft-weighting a rule that produced false negatives — remove the rule
- Auto-artifact creation on every push (Issues, PRs, comments) as a
  coordination substrate — GitHub already has `gh issue create` /
  `gh pr create --draft` for that
- Silent truncation (top-N, sampling, no-retry) without a log line
  stating what was dropped

## Meta

- Maintainer-authored; Outrider reads, does not write
- To propose an update: PR against this file with rationale in the body
