---
type: Spec Compilation Guide
title: Specification compilation (PaperCompiler)
description: How Outrider compiles paper-grounded constraints into the spec bundle — SPEC_COMPILATION.json plus a provenance-tagged section folded into SPEC.md.
resource: https://github.com/remyxai/outrider/blob/main/docs/spec-compilation.md
tags: [outrider, spec, papercompiler, provenance]
timestamp: 2026-09-14T00:00:00Z
---

# Specification compilation (PaperCompiler)

Adapts **PaperCompiler** ([arXiv:2609.02272v1](https://arxiv.org/abs/2609.02272v1))
to enrich Outrider's specification bundle with structured, provenance-tagged
constraints that steer the coding agent toward a higher-fidelity implementation.

PaperCompiler's insight is that a faithful paper-to-code implementation should
compile the paper's evidence into explicit, repository-level constraints —
while keeping honest about *where each constraint came from*: paper-supported,
inferred, externally delegated, or unresolved. Outrider applies that insight on
the **paper-anchored path** of `write_spec_bundle` (it is a no-op for brief-mode
and other non-paper runs, which carry no abstract to compile from).

## What the compilation produces

Two artifacts, both written only when a paper anchor is present:

### `SPEC_COMPILATION.json`

The machine-readable compilation. It carries:

- **`non_degradation_requirements`** — constraints the implementation must not
  degrade, each tagged with `evidence` (`paper-supported` / `inferred` /
  `externally_delegated`) and, for paper-supported ones, the grounding sentence
  lifted verbatim from the abstract as `source` provenance.
- **`cross_file_dependencies`** — dependency edges derived from the experiment
  scope (e.g. the orchestrator calling into compiled specification logic).
- **`file_constraints`** — file-level ownership and preservation constraints;
  concrete `src/*.py` paths named in the scope become ownership assignments.
- **`resoluteness`** — provenance accounting, including the honest `unresolved`
  bucket: constraint categories the abstract mentions nothing about are counted
  there rather than being silently invented.

### Provenance section folded into `SPEC.md`

The same compilation is rendered to markdown and appended to the `SPEC.md` the
coding agent actually reads — because PaperCompiler's central claim is that the
compiled spec must *constrain downstream generation*, not sit unread in a JSON
file consumed nowhere. The section lists the non-degradation requirements
(with their grounding snippets), cross-file dependencies, file-level
constraints, and the provenance accounting line.

## How grounding works

Rather than emitting a fixed constraint list, the compiler scans the paper's
own abstract for the categories a faithful spec is expected to cover — method
logic, evaluation protocol, cross-file consistency, evidence grounding — and:

- emits a `paper-supported` requirement for each category the abstract mentions,
  carrying the matching sentence as provenance;
- counts unmentioned categories toward `unresolved` instead of inventing them;
- marks benchmark/baseline concerns the abstract delegates elsewhere as
  `externally_delegated`;
- keeps target-native deductions (integration shape, test coverage) tagged
  `inferred`, so a reader can always tell paper-grounded requirements apart
  from Outrider's own conventions.

## Compatibility

The integration is addition-shaped: `SPEC_COMPILATION.json` and the appended
`SPEC.md` section appear **only** on the paper-anchored path. Runs without a
paper anchor (brief mode, etc.) are byte-for-byte unchanged.
