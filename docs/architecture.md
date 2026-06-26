---
type: architecture_overview
description: Selection taxonomy, gating logic, and the full Outrider pipeline.
tags: [outrider, architecture, selection-pass, refinement-chain]
---

# Architecture

How Outrider decides what to recommend, what to scaffold, and how to refine.


## 1. Selection — four integration shapes

The selection pass classifies every candidate against your repo using a four-shape taxonomy. A candidate that doesn't fit one of these shapes is a structural mismatch and gets rejected.

| Shape | Definition | Verification gate |
|---|---|---|
| **addition** | Paper adds a new module wired into existing code. Most common. | Call site exists and the new module's I/O contract fits. |
| **replacement** | Strict drop-in for an existing component with the same I/O contract but better internals (smaller / faster / newer foundation). | I/O contracts are functionally equivalent, not just thematically related. |
| **simplification** | Merges two or more existing components into one with the same end-to-end contract. Pipeline collapses. | Merged contribution spans the existing boundary contract cleanly. |
| **extension** | Proposes a new capability your repo lacks AND that you've signaled openness to (README roadmap, an open `[RFC]` Issue, CONTEXT.md investment pattern). | Four gates: pipeline-compatible I/O, explicit team-direction signal, no existing implementation, tier=high + relevance ≥ 0.90. Without ≥1 direction signal, extension picks are RFC-fishing and get rejected. |

**Tie-break preference**: `simplification > replacement > addition > extension`. Extension is last-resort — picked only when the other three shapes fail AND the four gates pass.


## 2. Discharge model — avoiding duplicate work

The same paper isn't re-recommended once it's already in front of your team. The dedup gate counts:

- **Outrider-opened Issues** (any state, open OR closed) — closing an Issue means "the team has decided," still a discharge signal.
- **Maintainer-opened Issues** (RFCs, discussions) whose body links the paper's arxiv id — a stronger signal than Outrider's own, since you authored the thread.

**Re-engagement lever**: reopen the Issue to drop the paper from the discharge set so Outrider can re-recommend it.


## 3. Pipeline — full flow

```
GitHub cron fires the workflow
       ↓
Query engine.remyx.ai for the candidate pool + interest context
       ↓
Per-candidate viability gates:
  - confidence (tier above min-confidence)
  - PR exists for arxiv?
  - any prior Issue references arxiv? (Outrider OR maintainer; open OR closed)
  - cadence guard (rate-limit-days)
       ↓
Clone the target repo + detect package / default branch
       ↓
Selection pass (Claude agentic, ~5 min):
  Inputs:
    - candidate brief (with inline "✗ already filed" tags)
    - 4 integration shapes + tie-break ordering
    - verification tools: gh code-search, gh api, remyxai search query/info
  Outputs:
    - chosen_index + integration_shape + selection_reasoning
    - OR: broaden-search via /search/assets if no in-pool fit
    - OR: skipped_by_selection_verification if every candidate rejected
       ↓
External-pick dedup (if out-of-pool / extension candidate)
       ↓
Write the .remyx-recommendation/ spec bundle
       ↓
Pre-flight Claude pass: PR or Issue?
       ↓                              ↓
     ISSUE                            PR
       ↓                              ↓
   open Issue        Invoke Claude Code (implement integration)
                                      ↓
                     Path-allowlist + integration validator
                     (new module must be imported by a modified file)
                                      ↓
                     Stub-density + pytest + test-integration check
                                      ↓
                     Self-review pass (downgrade to Issue if orphan;
                     diff preserved in Issue body for manual review)
                                      ↓
                     Commit (bundle scrubbed) + push + open draft PR
                                      ↓
                     Inline refinement chain (chain: true, default):
                       Phase A — fidelity audit
                         · reference-anchored (clone + diff) OR
                         · paper-anchored (arxiv abstract; degraded mode)
                         → Coverage matrix in step summary
                       Phase B — convention pass
                         · extract upstream PR-body shape
                         · rewrite body (canonical-first folding)
                         · patch session (non-algorithmic alignment)
                       Phase C — test gate
                         · lint + targeted tests on touched files
                         · drop draft to ready-for-review on pass
       ↓
     Issue route also runs an inline pass (v1.6.17):
       Phase B' — issue convention pass
         · walk .github/ISSUE_TEMPLATE/*
         · classify template kinds (filter bug + question)
         · pick best-fitting template + rewrite Issue body
         · PATCH issue body
```

When `chain: true` (the default), the same run continues into the refinement chain on the artifact it just filed. Opt out with `chain: false`.

The Remyx engine (commit-history extraction, candidate pool, embedding pre-filter, ranking) runs server-side. This action is a pure consumer.


## 4. Fidelity audit anchors

Phase A audits the diff against a reference. The anchor depends on what's available:

| Mode | Anchor | When it fires |
|---|---|---|
| **Reference-anchored** (A1) | Cloned GitHub repo of the paper's reference impl | Reference URL extractable from PR body |
| **Paper-anchored** (A2, v1.6.16) | arXiv abstract page text | No reference URL, but arxiv id extractable. Lower precision — the audit reports its precision floor via the "Audit anchor" line in the Coverage matrix. |

A2 is the degraded mode. Before v1.6.16, papers without a public reference impl would cause Phase A to skip and cascade the skip to Phases B + C. A2 keeps the chain running.


## 5. Issue-route convention pass (v1.6.17)

When recommend mode routes to an Issue rather than a PR, the chain runs a single phase: the Issue-route convention pass.

| Step | Behavior |
|---|---|
| **1. Walk templates** | Read `.github/ISSUE_TEMPLATE/*.{md,yml,yaml}`; skip `config.yml`. Accept both legacy markdown frontmatter and modern Issue Forms. |
| **2. Classify kinds** | Heuristic: `bug` / `feature` / `new_model` / `question` / `other`. Drop bug + question kinds (paper-pitch Issues don't fit). |
| **3. Pick best-fitting template** | Claude one-shot. If no fit, fall through to scaffolding-collapse-only mode. |
| **4. Rewrite body** | Same one-shot emits the rewritten body in a delimited format (`===TEMPLATE_ID===` / `===RATIONALE===` / `===UPDATED_BODY===` / `===END===`) to avoid JSON-escape brittleness on long markdown. |
| **5. PATCH + label** | PATCH issue body via Issues API; apply `outrider:issue-convention-done`. |

Three terminal statuses:
- `issue_convention_aligned` — template picked + body folded
- `issue_convention_aligned_no_fitting_template` — templates exist but all bug/question; body still folded with scaffolding collapsed
- `issue_convention_skipped_no_templates` — no `.github/ISSUE_TEMPLATE/`; body still folded


## 6. What's NOT customizable via inputs

These design choices are baked into the action:

- The four integration shapes and their tie-break ordering — changing them changes Outrider's identity.
- Self-review heuristics (orphan detection, stub density threshold at 50%, integration validator).
- Phase A audit anchor selection (reference vs paper). Auto-decided based on paper provenance.
- Body-rewrite folding rules (canonical-first, Discovery context details block, one-line attribution). The prompts are fixed; the LLM's choices vary per repo.

File an Issue on `remyxai/outrider` if something here is blocking you.
