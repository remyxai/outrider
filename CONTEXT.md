---
type: project_context
description: Active investigation areas, stable architecture, and out-of-scope boundaries for the Outrider Action.
tags: [outrider, ai-coding-agent, github-action]
---

# CONTEXT.md

## What this file is

This file gives the Outrider Action — and any human or agent contributor
working on this repo — explicit signal about what the project's current
direction is, where extension-shaped contributions are welcome, and what
is intentionally out of scope.

When Outrider runs against this repo (self-dogfooding), the selection-pass
agent reads this file to evaluate candidate papers against the integration
archetype gate. Without it, the agent has no anchoring signal for the
`extension` archetype and correctly defaults to refusing speculative picks
— it can verify `addition`, `replacement`, and `simplification` against
existing call sites, but `extension` requires team direction that doesn't
exist in code alone.

With this file present, papers proposing methods that align with the
directions below are eligible for the `extension` acceptance path.

Updates to this file should be discussed via PR; the team is more interested
in this file being correct than in it being short.

## Stable architecture (what won't change)

The following choices are deliberate and won't be revisited without an RFC
discussion. Papers proposing to *replace* these are not extension-shaped and
will be skipped at verification:

* **Stateless per-run model.** Outrider runs as a GitHub Action. Cross-run
  state lives in GitHub itself — open Issues, merged PRs, the discharge set
  computed from prior artifacts. New persistent-memory subsystems
  (event-sourced logs, runtime memory stores, in-process caches that span
  runs) don't have a place here.
* **Hosted-model orchestration only.** Outrider calls Anthropic's hosted
  Claude via the Claude Code CLI. It does not train, fine-tune, or run model
  weights locally. LoRA training, hypernetwork adapters, model
  quantization/distillation, and similar training-side methods don't have
  call sites in this repo.
* **Deterministic exact-match dedup.** The `dedup_with_recent_*` paths use
  exact arxiv-id string matches against open/merged artifacts. This is
  intentional — network-free, unit-testable, and contract-stable. Swapping
  it for embedding similarity or fuzzy retrieval breaks a stability
  contract.
* **Per-paper, not per-method, recommendation unit.** One run produces at
  most one PR or Issue, on one paper. Multi-paper synthesis, cross-paper
  meta-analysis, and survey-style aggregations are out of scope for the
  per-run loop. The weekly-summary mode is the only aggregation surface, and
  it aggregates *runs*, not *papers*.

## Active investigation areas

The team is actively investigating the following directions. Papers that
propose methods clearly anchored to one of these — with a public-code
companion and a contract-equivalent call site — are eligible for
`extension`-archetype acceptance.

### 1. Instruction-file injection in implementation prompts

The implementation pass already injects the target repo's ExperimentHistory
into the Claude Code prompt. The next direction: detect and inject canonical
agent-instruction files when the target repo ships them — `AGENTS.md`,
`.cursorrules`, `.github/copilot-instructions.md`, `CLAUDE.md`. When present,
these should be added to the implementation prompt before the agent's first
turn.

Call site: implementation-prompt assembly in `src/run.py`.
Constraint: backwards-compatible (no change for repos without these files).
Open question: precedence order when multiple instruction files exist.

### 2. Selection-pass exploration-coverage refinement

`selection_coverage` currently captures `searches`, `file_reads`,
`visible_lines`, `search_to_read_ratio`. Methods that add structural-pattern
dimensions to this — quantifying whether the agent explored the target
repo's call graph linearly vs. with branching, or measuring how often the
agent revisited files — are extension-shaped.

Call site: `_selection_coverage_from_events` in `src/run.py`.
Constraint: must be computable from the existing event stream; no new
instrumentation hooks inside the agent's loop.

### 3. Multi-vendor coding-agent support

The current implementation pass invokes Claude Code. Methods that generalize
Outrider's prompt assembly, validator boundary, and failure-mode surfacing
across multiple coding-agent vendors (Aider, Goose, Copilot, Codex, others)
are extension-shaped. The boundary between vendor-specific and
vendor-agnostic is mostly drawn — see `_agent_failure_blocks` for the helper
boundary on failure-mode surfacing.

Call site: the `_invoke_claude_code` family in `src/run.py`.
Constraint: Claude Code's behavior stays identical; new vendor adapters land
alongside, not as replacements.

### 4. Maintainer-thread paper-title anchoring

The selection pass can resolve direct arxiv-id strings when a maintainer
thread (Issue, PR description) names one explicitly. Extension: when a
maintainer thread names a paper by *title* rather than ID, recognize and
resolve it. This addresses the common case where contributors say
"InstructSAM" or "RADAR" in conversation without remembering the arxiv ID.

Call site: maintainer-thread parsing in the selection-pass prompt + the
engine's search API.
Constraint: graceful fallback when the title is ambiguous or doesn't resolve
to a single paper.

### 5. PR-risk scoring evolution

The Diff Risk Score in `src/diff_risk_score.py` (adapted from
arXiv:2605.30208) is the current static-feature risk model, empirically
calibrated against a recalibration corpus. Extensions worth investigating:

* Semantically-aware features (e.g., test-presence-by-module rather than
  test-presence-by-diff).
* Multi-output variants that distinguish "drift risk" (test passes but
  introduces subtle issues) from "fail risk" (tests fail outright).
* Cheap predictor ensembles that combine the static-diff score with
  selection-coverage signal and historical merge-rate per target repo.

Call site: `src/diff_risk_score.py` + the `predict_pr_quality` family.
Constraint: the deterministic logistic stays as the baseline; new features
must be backwards-compatible.

### 6. Step-summary failure-mode surfacing

The step summary already surfaces selection-pass reasoning, diff-risk band,
and agent-failure-mode blocks. Extensions: additional structured fields that
downstream consumers (`gh run view` output, run-telemetry consumers, future
UI) can read without breaking changes.

Call site: `_write_step_summary` + the `_agent_failure_blocks` helper.
Constraint: GitHub-step-summary markdown only; no HTML side-channels.

## Open RFC Questions

The following are genuine open questions where a paper-shaped contribution
would be useful. If a candidate paper directly addresses one of these, it's
a strong `extension`-archetype match:

1. **How should the implementation pass decide between draft PR and Issue
   mode in borderline cases?** Current routing is rule-based (test-
   integration policy + diff-risk band). Is there a better signal?
2. **What's the right exploration budget for the selection-pass agent?**
   Current default is 25 turns / 360 seconds. Methods that adaptively
   adjust based on candidate-pool quality are interesting.
3. **How should weekly-summary mode surface trends across multiple runs?**
   Current implementation aggregates per-run summaries. More sophisticated
   trend-detection — what's the team converging on, what's getting deferred
   — would be more useful than the current digest format.

## Out of scope

The following are explicitly out of scope. Papers proposing these will be
skipped at verification regardless of relevance score:

* Replacing the Claude Code CLI with a fork or wrapper that intercepts model
  calls.
* Replacing Anthropic-hosted Claude with a self-hosted model.
* Replacing GitHub as the artifact-storage layer.
* Customer-side review-feedback capture loops that would require new
  long-running infrastructure (this is a long-term direction but not
  currently scoped; would require substantial new architecture, not a
  single-paper extension).

## How the Outrider agent should use this file

When evaluating candidates during selection-pass:

1. Read this file (if present) before scoring candidates.
2. Map each candidate's proposed method against the **Active investigation
   areas** and **Open RFC Questions** sections. A clean thematic match
   unlocks `extension` archetype eligibility.
3. Check **Out of scope** before considering any candidate that would touch
   architectural elements named in **Stable architecture**.
4. Even for `extension`-eligible candidates, still require a public-code
   companion. Direction signal alone doesn't substitute for a concrete
   reference implementation.

This file is itself eligible for paper-driven updates. A paper that proposes
a new active-investigation area for a maintenance system like Outrider can
motivate a PR that adds that area here.
