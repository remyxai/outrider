"""
run.py — Entry point for the remyxai/outrider composite GitHub Action.

The action runs once per workflow invocation; it opens a draft PR (or
an Issue when the recommended paper can't be cleanly scaffolded)
against the repo the action runs in.

Flow:

  1. Recommendation: GET /api/v1.0/papers/recommended on engine.remyx.ai
     for the configured ResearchInterest. Remyx server-side handles
     commit-history extraction, candidate pool, embedding pre-filter,
     and LLM ranking — this action is a pure consumer.
  2. Confidence gate: skip Low / Noise tiers.
  3. Dedup: skip if an open PR already exists for this paper's arxiv_id
     (branch == `remyx-recommendation/{arxiv_id}`), or if any
     remyx-recommendation PR was opened within `rate-limit-days`.
  4. Clone the target repo (= GITHUB_REPOSITORY), branch from main.
  5. Write the spec bundle to `.remyx-recommendation/`:
       SPEC.md, PAPER.md, CONTEXT.md, GUARDRAILS.md, INVOCATION.md
  6. Invoke Claude Code (headless) with INVOCATION.md as the brief.
  7. Issue-fallback: if Claude wrote `.remyx-recommendation/OPEN_AS_ISSUE.md`
     (paper can't be scaffolded against this codebase) open an Issue
     with its reasoning and exit.
  8. Path-allowlist enforcement: reject if Claude touched files outside
     the allowed set.
  9. pytest in the workdir.
 10. Commit (with the bundle dir scrubbed), push, open the PR.

Inputs are read from env vars set by the action's `with:` block
(action.yml maps `inputs.X` → `INPUT_X`). Secrets and the workflow's
GITHUB_TOKEN are passed through unchanged.

  TARGET_REPO            — github.repository (the repo to operate on)
  INPUT_INTEREST_ID      — required, the Remyx ResearchInterest UUID
  INPUT_MIN_CONFIDENCE   — "high" | "moderate" | "low" (default: moderate)
  INPUT_DRAFT_MODE       — "always" | "on_test_failure" | "never" (default: always)
  INPUT_RATE_LIMIT_DAYS  — int, default 7
  REMYX_API_KEY          — engine.remyx.ai token (set as a workflow secret)
  ANTHROPIC_API_KEY      — Claude Code auth (set as a workflow secret)
  GITHUB_TOKEN           — workflow's built-in token, or a cross-repo PAT
"""
from __future__ import annotations

import ast
import base64
import datetime as dt
import io
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from diff_risk_score import (
    DIFF_RISK_ISSUE_THRESHOLD,
    render_risk_detail,
    score_diff_risk,
)
from exploration_structure import (
    exploration_structure_from_events,
    structure_enabled,
)
from instruction_files import render_instruction_files

# ─── Configuration ─────────────────────────────────────────────────────────

# Mirror REMYX_API_KEY → REMYXAI_API_KEY so the `remyxai` CLI authenticates
# in subprocesses spawned by the selection pass (Claude Code shell-out). The
# CLI reads REMYXAI_API_KEY; the action canonically uses REMYX_API_KEY.
if os.environ.get("REMYX_API_KEY") and not os.environ.get("REMYXAI_API_KEY"):
    os.environ["REMYXAI_API_KEY"] = os.environ["REMYX_API_KEY"]

REMYX_API_BASE = os.environ.get("REMYX_API_BASE", "https://engine.remyx.ai")
REMYX_RECOMMENDATION_PERIOD = os.environ.get("REMYX_RECOMMENDATION_PERIOD", "week")
REMYX_RECOMMENDATION_LIMIT = int(os.environ.get("REMYX_RECOMMENDATION_LIMIT", "25"))
# Max seconds to wait for recommendations to populate after triggering a
# refresh on an interest whose pool is empty (e.g. a brand-new interest
# whose daily ranking hasn't run yet). Polled, not a hard sleep.
REMYX_REFRESH_WAIT_S = int(os.environ.get("REMYX_REFRESH_WAIT_S", "150"))

# Map Remyx's 0.0-1.0 relevance_score onto confidence-gate tiers.
# Thresholds are intentionally generous on the high end since the action
# is one-shot per run, not a ranked list — we just need a "should we
# open a PR for this?" gate.
RELEVANCE_TIER_FLOOR = {
    "high":     float(os.environ.get("REMYX_TIER_HIGH_FLOOR",     "0.80")),
    "moderate": float(os.environ.get("REMYX_TIER_MODERATE_FLOOR", "0.60")),
    "low":      float(os.environ.get("REMYX_TIER_LOW_FLOOR",      "0.40")),
}

TIER_RANK = {"high": 3, "moderate": 2, "low": 1, "noise": 0, "near-random": 0}

# Paths Claude Code is allowed to create/modify. Customers can extend
# via the `guardrails-allowlist` input on the action (comma-separated).
#
# Permissive on the target package because the agent needs to add small
# wiring edits to existing files (e.g. a 3-line hook in evaluation.py) —
# the post-hoc check_integration() validator enforces that at least one
# newly-added callable is invoked from another changed file (rejecting
# scaffold-shaped runs where new code is defined but never called).
# Python source anywhere in the repo is editable: a wiring edit has to be
# able to reach the real call site, which often lives outside the target
# package (a pipeline/stage driver, an entrypoint module, etc.), and we
# don't want to hard-code any one repo's directory layout. Infra files that
# happen to sit alongside source — container builds, shell scripts,
# dependency/build manifests, CI config — are blocked by ROLE in
# ALWAYS_BLOCKED, which takes precedence.
DEFAULT_ALLOWLIST_GLOBS = [
    "*.py",
    ".remyx-recommendation/**",
    "**/*.md",               # Markdown anywhere (README, CHANGELOG, docs/,
                             # ADR notes). Diff is text-only and reviewable.
]

# Cap on number of newly-created .py files in the target package. A
# real integration adds one module, sometimes two; anything beyond
# that is scaffold-shaped.
MAX_NEW_PACKAGE_FILES = 3

# Stub density (fraction of function bodies that are pass / ellipsis /
# raise NotImplementedError / docstring-only) above which we route to
# Issue instead of opening a PR. At this density the paper's actual
# contribution isn't really present in the diff.
STUB_DENSITY_DOWNGRADE_THRESHOLD = 0.5

BUNDLE_DIR_NAME = ".remyx-recommendation"
BRANCH_PREFIX = "remyx-recommendation/"
PR_TITLE_PREFIX = "[Remyx Recommendation]"

# Vendor-console URLs surfaced in step_summary when the agent fails with a
# recognizable cause. Currently Anthropic-only; when alternative agent CLIs
# land, these become a per-agent lookup (`_AGENT_URLS = {"claude": ...,
# "aider": ...}`) keyed by the agent type recorded in the result dict.
_ANTHROPIC_BILLING_URL = "https://console.anthropic.com/settings/billing"
_ANTHROPIC_KEYS_URL = "https://console.anthropic.com/settings/keys"

# Files that are NEVER allowed to be touched. Blocked by ROLE (filename /
# type), not by directory, so the policy doesn't encode any one repo's
# layout: a Dockerfile is off-limits whether it sits at the root, under
# docker/, or anywhere else. `*` crosses `/` in path_matches_glob, so each
# pattern catches the file at the repo root and nested at any depth. This
# is checked before the allowlist and takes precedence, so even though
# `*.py` is allowlisted, build scripts and dependency manifests stay
# protected. (Replaces the old directory-based `docker/**` / `pipelines/**`
# / `config/**` blanket blocks, which were overfit to one repo's tree and
# locked out the stage drivers that are often the real call site.)
ALWAYS_BLOCKED = [
    ".github/**",            # CI / workflow config (GitHub-standard location)
    "*Dockerfile",           # container build recipes, anywhere
    "*Dockerfile.*",
    "*.dockerfile",
    "*.sh",                  # shell scripts (entrypoints, build hooks), anywhere
    "*requirements*.txt",    # pip dependency manifests, anywhere
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "MANIFEST.in",
    "*.lock",                # lockfiles (poetry.lock, uv.lock, …)
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("orchestrator")


# ─── Bundle + PR-body templates (module-level so they aren't indented by
# textwrap.dedent's "common leading whitespace" rule when interpolating
# multiline content like rec.spec_md or invocation prose). ────────────────

CANONICAL_ATTRIBUTION_URL = "https://engine.remyx.ai"
# Attribution links in PR bodies, Issues, and README appends point to
# the customer-facing product page on engine.remyx.ai, not to the
# orchestrator's private source repo (which would 404 for external
# readers).

# When Claude Code determines the paper can't be cleanly scaffolded (paper
# needs infra the repo lacks, integration point is too vague, datasets /
# checkpoints not available, etc.) it writes this file in the workdir
# instead of code. The orchestrator detects it and opens a discussion
# Issue rather than a PR — preserves the discovery surface without
# putting empty/throwaway scaffolding into a PR.
ISSUE_FALLBACK_FILENAME = f"{BUNDLE_DIR_NAME}/OPEN_AS_ISSUE.md"

_SPEC_MD_TEMPLATE = """\
---
type: implementation_spec
arxiv_id: {arxiv_id}
arxiv_url: https://arxiv.org/abs/{arxiv_id}
tier: {tier}
relevance_score: {relevance_score:.2f}
---

# Implementation spec — drafted by Remyx Recommendation

**Recommended paper**: [{paper_title}](https://arxiv.org/abs/{arxiv_id})
**Confidence**: {tier} (Remyx relevance {relevance_score:.2f})
**Research interest**: {interest_name}

---

## Team's research focus

{interest_context_block}

## Why this paper for this team

{reasoning}

## How this maps onto your repo (candidate selection)

{selection_block}

## Suggested experiment

{suggested_experiment}

## Paper abstract

{paper_abstract}
"""

_PAPER_MD_TEMPLATE = """\
---
type: paper
arxiv_id: {arxiv_id}
arxiv_url: https://arxiv.org/abs/{arxiv_id}
---

# {paper_title}

arxiv: https://arxiv.org/abs/{arxiv_id}

## Abstract

{paper_abstract}
"""

_ENVIRONMENT_MD_TEMPLATE = """\
---
type: workflow_environment
description: Workflow-provided tooling available in this environment.
---

# Tooling available in this environment

The following capabilities were attached to this run by the workflow
author (via an ENVIRONMENTS.md or ENVIRONMENT.md at the workflow
workspace or repo root). Prefer these over generic Read / Grep / Glob
when the described tool fits the task better.

{environment_body}
"""


_ENVIRONMENT_FILE_REF_TEMPLATE = """\
  6. .remyx-recommendation/ENVIRONMENT.md  — workflow-provided tooling
                                              hints (available tools +
                                              suggested usage patterns
                                              for this run's environment)
"""


_CONTEXT_MD_TEMPLATE = """\
---
type: team_history
description: Recent shipping history from the team's experiment trajectory.
---

# Team's recent shipping history

These are experiments the team has actually shipped — ground your
implementation in this trajectory. Don't propose ideas duplicating what
the team has already built; consider whether the new paper extends an
existing iteration_chain or starts a new one.

{experiment_history}
"""

_GUARDRAILS_MD_TEMPLATE = """\
---
type: path_guardrails
description: Path allowlist + blocked paths for the implementation pass.
---

# Path guardrails for this PR

You MAY create files matching:
```
{allowlist}
```

You MAY append-only modify:
```
README.md
```

You MUST NOT touch:
```
{blocked}
```

After the orchestrator validates your work, it checks the diff with
`git diff --name-only`. If any path you touched is outside the allowed
set, the PR is rejected and your work is not committed.
"""

_ORIENTATION_MD_TEMPLATE = """\
---
type: repo_orientation
description: Target-repo conventions — contributor guides, PR template, lint config, verification stack, nearby files.
---

# Repo orientation — conventions and patterns for this target repo

The orchestrator already read the target repo's convention-defining files
for you. Use the patterns below to shape your generated code, PR title,
PR body, and commit messages. Do NOT re-explore these files yourself
(that's redundant cost) — the relevant content is summarized here.

{contributor_guides_block}
{pr_template_block}
{recent_merged_prs_block}
{tooling_config_block}
{verification_stack_block}
{nearby_files_block}
{nearby_tests_block}

## How to use this orientation

- **PR title**: match the convention shown in the recent merged PRs above
  (the title pattern — e.g. `<scope>: <verb> <thing>` if that's what
  recent merges follow). Do not use Remyx-prefixed titles.
- **PR body**: if the PR template is shown above, conform to its section
  structure. Otherwise produce a clean summary + test plan.
- **Code style**: match what the existing nearby files do — import style
  (relative vs absolute), naming, formatting. The lint config (if shown)
  is the source of truth for what passes.
- **Type checking**: if the repo uses mypy or pyright, the orientation
  block lists the configured strictness. Match the patterns the existing
  tests use for any TypedDict / async / union narrowing.
- **Test design**: match the existing test patterns — go through public
  interfaces, not internal attributes; use the same fixtures and helpers
  the existing tests use.

If the orientation block is empty or missing a section, that signal is
informative: either the repo has no contributor guide / PR template /
lint config (treat as no strict convention to follow) or the orchestrator
couldn't read it (rare; surface in your summary if so).
"""

_RESEARCH_INVOCATION_MD_TEMPLATE = """\
---
type: research_invocation
description: Headless prompt for the research-phase Claude Code invocation.
---

You are the **research stage** of a two-stage Outrider dispatch. A downstream
coding session (a separate Claude Code invocation) will implement the paper
against the target repository; your job is to prepare its context by gathering
structured evidence and producing a single ``web_findings.json`` artifact.

# Your role

- **Investigate, do not implement.** You are explicitly forbidden from writing
  implementation code, editing production files, or opening PRs / commits.
  Your only write output is ``.remyx-recommendation/web_findings.json`` in the briefing bundle dir.
- **Decompose the paper into research subquestions** and dispatch parallel
  tool calls. Aim for at least 5 concurrent tool calls per turn unless you
  have already converged — the underlying tool-use loop resolves parallel
  calls in a single turn, so breadth-per-turn is essentially free latency.
- **Annotate every finding** with a one-line "why included" justification.
  Downstream stages use these annotations to spot rationalizations.
- **Bounded budget**: aim for ≤ 8 turns. If you cannot converge in that
  budget, halt and produce a partial ``web_findings.json`` with a
  ``truncation_reason`` field explaining what's missing.

# Research task

Paper: **{paper_title}** (arxiv:{arxiv_id})
Target repo: **{target_repo}**
Prior-attempt context: {prior_attempt_hint}

# Pre-fetched signals (deterministic; save your tool budget for judgment work)

{hf_linkage_block}

{sibling_impls_block}

# What the coding session needs

Write ``.remyx-recommendation/web_findings.json`` (the briefing bundle dir, NOT workspace root) with at least these fields:

```
{{
  "paper": {{
    "arxiv_id": str,
    "title": str,
    "core_mechanism": str,   // 2-3 sentences on the paper's central technical claim
    "call_site_candidates": [
      {{ "path": str, "why": str, "confidence": "high" | "medium" | "low" }}
    ]
  }},
  "target_repo": {{
    "conventions": [str],
    "prior_attempts_on_this_paper": [ {{ "branch": str, "sha": str, "outcome": str }} ],
    "sibling_implementations": [ {{ "url": str, "why_relevant": str }} ]
  }},
  "coordination_signals": [
    {{ "url": str, "kind": "issue" | "pr" | "discussion", "summary": str, "significance": str }}
  ],
  "scope_recommendations": {{
    "mode_hint": "Mode 1 (direct port)" | "Mode 2 (adapted port)" | "Mode 3 (inspired experiment)",
    "substitutions_to_expect": [str],
    "scope_to_avoid": [str]
  }},
  "provenance": [
    {{ "source_id": str, "tool": str, "url": str, "retrieved_at": str, "why": str }}
  ],
  "turn_count": int,
  "cost_usd": float | null,
  "truncation_reason": str | null
}}
```

# Available tools

Standard Claude Code toolkit: ``Read``, ``Bash``, ``WebFetch``, ``Grep``,
``Glob``. Use ``gh`` CLI via ``Bash`` for GitHub queries. When ``WebFetch``
returns binary content (typical for arxiv PDFs), pivot to the ar5iv HTML
mirror at ``ar5iv.labs.arxiv.org/html/<arxiv_id>``.

# Suggested decomposition (the first turn)

Dispatch these in parallel to seed the investigation:

- Fetch the paper's abstract page (arxiv or ar5iv HTML)
- ``gh api`` for the target-repo metadata + default branch + top-level files
- If prior-attempt context names a branch, ``gh api compare`` the baseline
  diff against ``main``
- ``gh issue list`` / ``gh api search issues`` for coordination signals on
  the target repo's upstream (open issues touching the paper's capability
  class — output-constrained, structured-output, whatever the paper is
  about)
- ``gh api`` for a recent-merged-PR sample on the target repo (~5 items)
  to establish convention patterns

Depth turns follow the fanout: verify the paper's methodology (Section 3),
read the baseline branch's core file, check upstream conventions
against the baseline's wiring, deepen high-signal coordination hits.

# Termination

Write your artifact to ``.remyx-recommendation/web_findings.json`` (the
briefing-bundle directory, NOT the workspace root — files at the root
would trip the path-allowlist gate on the downstream commit). Use the
``Write`` tool. Do NOT open any PRs / issues / commits. Do NOT call the
Task tool. Your final assistant message should be one paragraph
summarizing what the coding session should now do — no code, no diffs.
"""


_RESEARCH_FINDINGS_REF_TEMPLATE = """\
  6. ``.remyx-recommendation/web_findings.json`` — structured research context from the
                                              research phase: paper's core mechanism,
                                              call-site candidates with confidence,
                                              prior attempts, coordination signals,
                                              mode hint, and scope recommendations.
                                              Read this before opening files —
                                              call_site_candidates points you at
                                              the exact call sites the research
                                              phase already scoped.
"""


_REPO_INTEL_REF_TEMPLATE = """\
  7. ``.remyx-recommendation/REPO_INTEL.md`` — cross-run learning accumulated on
                                              this fork: confirmed landing zones
                                              (where prior branches landed cleanly),
                                              rejected mechanism shapes (with the
                                              reasons they didn't fit and caveats
                                              under which the rejection might not
                                              apply), coordination signals, and
                                              exploration budget. Consult before
                                              picking a call site — extending a
                                              confirmed landing zone is cheaper
                                              than discovering a new one, and
                                              proposing a rejected shape without
                                              matching a caveat is a known cost.
"""


_INVOCATION_MD_TEMPLATE = """\
---
type: agent_invocation
description: Headless prompt for the Claude Code CLI invocation.
---

You are a coding agent implementing a recommendation from the Remyx
Recommendation pipeline (attribution URL: {attribution_url}).

Read these files in order:
  1. .remyx-recommendation/SPEC.md         — the implementation spec (paper,
                                              why-this-paper, suggested
                                              experiment, team's research-
                                              focus body, abstract)
  2. .remyx-recommendation/PAPER.md        — paper title + abstract
  3. .remyx-recommendation/CONTEXT.md      — team context (recent merges,
                                              if Remyx returned any)
  4. .remyx-recommendation/GUARDRAILS.md   — what you may and may not modify
  5. .remyx-recommendation/ORIENTATION.md  — target repo's contributor guide,
                                              PR template, recent-merged-PR
                                              conventions, lint/type config,
                                              and a few sample existing files
                                              + tests near the planned call
                                              site. Use these patterns
                                              without re-exploring them.
{environment_file_ref}{research_findings_ref}{repo_intel_ref}

SPEC.md names a PROPOSED CALL SITE under "How this maps onto your repo"
(the file + function the selection pass judged most implementable). Start
there, and keep exploration minimal — broad repo-wandering is the main
cost to avoid:
  - Open ONLY that file plus the modules its target function directly
    imports or calls. Read narrow line ranges, not whole files.
  - Use grep / symbol search to confirm the call site and local
    conventions. Do NOT list or read the whole `{package}/` or `tests/`
    tree.
  - Skip generated, vendored, lockfile, data, and notebook files, and any
    file over ~1500 lines unless the call site is inside it.
  - Once you can name the exact function you will call, STOP exploring and
    implement. Confirming the call site should take only a few reads.
Depth at the chosen call site is fine; breadth across the repo is not.

# Step 1 — decide: PR or Issue

DEFAULT: open an Issue. PR is the exception, not the rule.

Open as PR only if BOTH of these hold:

  (a) You can identify a SPECIFIC existing module/function in `{package}/`
      where this paper's contribution slots in (the "call site").

  (b) You can deliver the paper's CORE INSIGHT or RESULT as a useful,
      scoped change at that call site. You do NOT need to reproduce the
      paper's full method, architecture, training procedure, or reported
      numbers. A small change that moves the repo in the paper's
      direction (a scorer, filter, metric, evaluation hook, or focused
      behavior change) is the INTENDED deliverable, not a fallback.

**Three implementation modes are legitimate.** Pick the one that best fits
this paper × this codebase, then implement it:

  - **Mode 1 (direct port).** Implement the paper's method as-described.
    Requires the repo to host the paper's full infrastructure. When it
    fits, this is the highest-fidelity outcome.

  - **Mode 2 (adapted port).** Implement the paper's CORE mechanism at
    full fidelity while substituting AUXILIARY components (learned
    estimators, bespoke optimizers, specialized datasets, benchmark
    suites) with target-native equivalents:
      • Replace a learned estimator with a parameter-free proxy
        approximating its signal (e.g. a learned MI estimator replaced
        by a vocab-overlap heuristic).
      • Replace a bespoke optimizer with the repo's existing optimizer path.
      • Cut the paper's separate benchmark / eval framework — evaluation
        belongs in a downstream PR.
    Example: RAPO's asymmetric per-token gradient weighting kept at full
    fidelity while its learned profile-token MI estimator was replaced
    with a vocab-overlap proxy, and its Psy-CoT template was cut.

  - **Mode 3 (inspired experiment).** Take the paper's core INSIGHT or
    FRAMING and implement a target-native experiment drawing on it. The
    PR does not reproduce the paper's method — it applies the paper's
    idea to the target's actual problem. Legitimate when the paper's
    method shape doesn't fit but its idea maps to a real target surface.
    Example: a paper proposing "trainable Bridge projector for
    cross-latent-space distillation" (requires a distillation trainer
    the repo lacks) could inspire "add latent-space consistency losses
    as an optional distillation objective in the existing
    consistency_distillation example" — same insight (cross-space
    supervision helps one-step students), target-native execution.

**Cite which mode you chose in the self-review** and, for Modes 2/3,
which specific components were substituted (Mode 2) or which insight
is being reframed (Mode 3). Un-cited substitutions or reframings read
as scope creep. The honesty discipline is STRICTER on adapted/inspired
outputs than on direct ports — the reader must be able to tell what's
from the paper and what's your target-native adaptation.

Open as Issue ONLY when all three modes fail:

  - Mode 1 fails: the paper's method needs infrastructure the repo lacks.
  - Mode 2 fails: substituting auxiliaries collapses the core to a naive
    baseline the paper explicitly improves upon.
  - Mode 3 fails: the paper's insight/framing doesn't map to any
    target-native experiment worth running (paper genuinely doesn't help
    this repo — pass with rationale).

If none of the three modes has a call-site anchor, that's the honest
"skip with rationale" outcome — a valid deliverable, not a failure.

If ANY of those hold, DO NOT WRITE CODE. Write a file at
`{issue_fallback_filename}` with this exact shape (Markdown):

```
# Title: short, action-oriented (becomes the Issue title)
Optional one-line subtitle.

## Why this paper is interesting for the team

(2-3 sentences from the spec + your own reading)

## What blocks a clean implementation

(Specifics: missing infra, no clear call site, required external
artifacts. Be concrete about what would need to exist for a real
integration to be drafted.)

## What we'd need to know / decide first

(1-3 questions or decisions the team should resolve before this becomes
implementable.)
```

The orchestrator detects this file and opens an Issue instead of a PR.
This is the HONEST outcome when a PR would be premature, not a failure.

# Step 2 — only if you're proceeding with PR: implement an INTEGRATION

The goal is the smallest change that calls into existing code and
delivers the paper's core insight as value to THIS repo. Implement the
RESULT, not the technique — do not port a trainer, model, or loss the repo
cannot host. NOT a scaffold. NOT a freestanding module.

Required outputs:

1. **At least one EDIT to an existing file** in `{package}/` that
   actually invokes your new code (the call site). A 3-line hook in
   `evaluation.py` that calls a new scorer is the model. Without this
   edit, the orchestrator will reject the run as scaffold-shaped.

   Keep each existing-file edit small — under ~50 lines net change.
   Larger edits get rejected.

2. **A capability-named module**, NOT `<paper-slug>_integration.py`.
   Pick a name that fits the repo's existing conventions and describes
   what the module DOES, not which paper it came from. Examples:
   `cot_grounding_check.py`, `pointcloud_quality.py`, `mask_refiner.py`.
   Paper attribution goes in the module docstring and README — never
   the filename. Keep the new file focused; if you need more than ~250
   lines, you're probably scaffolding.

3. **At least one new test that imports from a NON-NEW module** in
   `{package}/`. Pure self-tests of the new file don't prove
   integration. Example: a test that imports the existing call-site
   module, exercises the wiring edit you made, and asserts the
   integrated behavior.

4. **README documentation** (only if the repo's convention does this).
   ORIENTATION.md shows the repo's existing README style — if the
   convention is to mention new examples/modules in the README, add a
   short "(Capability) — adapted from (Paper Title)" section in the same
   shape as the existing entries. If the repo's README doesn't carry
   per-feature documentation, DON'T add a section. Do NOT add marketing
   attribution links to the codebase — attribution lives in the PR body
   footer (handled by the orchestrator), not in the maintainer's repo.

# Lint cleanly against the repo's own config

After your edits and before declaring done, run the repo's lint command
on the files you changed and fix anything it flags. The orchestrator
will run the same lint as a gate; failures block the PR from being
marked ready for review.

The repo's lint setup lives in ORIENTATION.md (`## Tooling config`) —
that's the canonical source. Typical shapes:

  - ``ruff check <changed_files>`` if ``[tool.ruff]`` is configured
  - ``flake8 <changed_files>`` if a ``.flake8`` or ``setup.cfg`` defines it
  - ``black --check <changed_files>`` for formatting
  - ``mypy <changed_files>`` only if the repo opts into it
  - ``make lint`` if a Makefile target exists

Use the repo's config, not your own preferences — strict ones lint
stylistic patterns (mutable default args, bare ``except``, unused imports,
double-unary minus, etc.) that more permissive configs allow. The
repo's rules are authoritative.

If the linter reports issues you cannot trivially fix without changing
behavior, prefer rewriting the surface expression to avoid the lint
warning rather than adding ``# noqa`` — explicit suppression should be
rare and signaled, not the cleanup path.

# Auto-format scope

When you invoke an auto-formatter (``ruff format``, ``black``, ``isort``,
``prettier``, etc.), it will reformat every line in scope — not just the
lines you semantically modified. The resulting diff carries cosmetic
whitespace / line-join / blank-line changes on lines you never touched,
which dilutes the semantic change and makes review harder.

Prefer **diff-scoped formatting**. For Python, that means ``darker``
(pipx-installable, wraps ``black``/``ruff format``/``isort`` and only
applies formatting to lines that differ from HEAD)::

    pipx install darker
    darker --revision HEAD <files-you-modified>

For other languages the tooling is uneven — ``prettier`` and ``rustfmt``
don't have native line-range flags. In those cases, prefer *no
auto-format* over full-file reformat: small formatting drift in a diff
is fine; format churn on unrelated lines is not.

If you can't scope the formatter to your changes, skip auto-format
entirely and hand-format only the lines you modified. A diff that shows
the semantic change without cosmetic noise is more takeover-friendly
than one that's uniformly formatted at the cost of a much larger review
surface.

# Honesty rules

- If the public surface of your new module is dominated by `TODO`,
  `pass`, or `raise NotImplementedError` (more than ~half the
  function bodies), you are scaffolding. STOP and write the Issue file
  instead — the orchestrator will reject the run anyway.
- If your new module would import cleanly but never be called by
  anything else in the repo, STOP and write the Issue file instead.
- The Issue-mode path is the correct route when a PR would be premature.
  It is NOT a failure mode.

Run pytest before declaring done. If tests fail, fix them or scope down
to a smaller integration; do not modify files outside the guardrails
allowlist.

# CRITICAL: do not run git commands

You MUST NOT run any `git` command during your session (`git init`,
`git checkout`, `git stash`, `git reset`, `git commit`, `git add`,
`git rm`, `git rebase`, etc. — none of them, including `git status`).
The orchestrator manages all version control. Past runs have hit
subtle issues where agents ran a `git checkout` to back out a
half-edit and left the working tree in an orphan state that broke
the PR.

If you need to back out an edit, use the file-edit tools to restore
the file's content. Look up the original content via standard read
tools — do not invoke git.

When complete, output a one-paragraph SUMMARY of what you built. Call out:
  - Which existing file you modified (the call site)
  - Which new module you created (the capability name)
  - The paper insight this delivers, and what you intentionally scoped
    out as unnecessary for that value — frame these as scoping decisions,
    not shortfalls. A focused slice that delivers the result is success.

Still distinguish "intentionally out of scope" (expected) from
"stubbed / incomplete" (TODO-dominated bodies) — the latter still routes
to an Issue per the honesty rules above.
"""

# Two helper Claude prompts: PR/Issue routing pre-flight (§6) and the
# post-implementation self-review (§4). Both are rendered with str.replace()
# rather than str.format() so the literal `{` / `}` in JSON examples don't
# need to be doubled.

_PREFLIGHT_PROMPT_TEMPLATE = """\
You are routing a paper recommendation for the Remyx Recommendation
orchestrator. Decide: should the implementation step run (PR), or
should we open an Issue for the team to discuss first?

Inputs follow at the end of this message:
  1. The paper spec (title, abstract, why-this-paper, suggested experiment)
  2. A candidate-selection rationale (in the spec, under "How this maps
     onto your repo") — when present, a prior pass already judged this
     paper implementable against THIS repo and named the call sites and
     the implementable SUBSET it targets.
  3. The target repo's module layout

Evaluate the SCOPED implementation the selection rationale describes — the
implementable subset wired into the named call sites — NOT the paper's
full or maximal contribution. A paper whose maximal form needs missing
infra (a trainer, a renderer, a synthesis engine) can still be a sound PR
if the selection rationale identifies a real, smaller slice that drops
into an existing call site (e.g. consuming a paper's released benchmark
through the existing eval path, rather than rebuilding its data-generation
engine). Don't route to ISSUE merely because the paper's headline method
is heavy — judge the scoped slice.

**Three implementation modes are legitimate, not just direct porting.** The
coding session picks which mode fits this paper × this codebase; preflight's
job is to check whether ANY of the three could produce a useful PR here:

  1. **Direct port** — implement the paper's method as-described. Requires
     the repo to host the paper's full infrastructure.
  2. **Adapted port** — implement the paper's CORE mechanism at full
     fidelity while substituting AUXILIARY components (learned estimators,
     bespoke optimizers, specialized datasets, benchmark suites) with
     target-native equivalents (parameter-free proxies, existing library
     functions, explicit scope cuts). Example: RAPO's asymmetric per-token
     gradient weighting kept at full fidelity while its learned MI estimator
     was replaced with a vocab-overlap proxy and its Psy-CoT template cut.
  3. **Inspired experiment** — take the paper's core INSIGHT or FRAMING and
     implement a target-native experiment drawing on it. The PR does not
     reproduce the paper's method — it applies the paper's *idea* to the
     target's actual problem. Example: a paper proposing "trainable Bridge
     projector for cross-latent-space distillation" could inspire a
     diffusers-native "add latent-space consistency losses to the existing
     consistency_distillation example" — same insight (cross-space
     supervision helps one-step students), target-native execution.

Route to PR if ANY of the three modes could produce a useful contribution.
Route to ISSUE only when all three fail:

  - Mode 1 fails: the paper's method needs infrastructure the repo lacks
  - Mode 2 fails: substituting auxiliaries collapses the core to a naive
    baseline the paper explicitly improves upon
  - Mode 3 fails: the paper's insight/framing doesn't map to any target-
    native experiment worth running (paper genuinely doesn't help this repo)

The coding session cites which mode was chosen and, for Modes 2/3, which
specific components were substituted or which insight was reframed. Modes
2/3 without a citation read as scope creep — the honesty discipline is
STRICTER on adapted/inspired outputs than on direct ports.

NEVER include token-shaped strings in any JSON field — the JSON you
write here flows verbatim into a public-repo PR or Issue body, and a
credential pattern in that body aborts the run via the outbound
scrubber. Specifically:

  - If a tool result contains an `Authorization:` header (e.g. from
    `curl -v`, `wget -d`, or any HTTP-debug output), do not quote the
    header verbatim. Describe what the call did instead.
  - If `git config --list` or a similar command exposes
    `http.https://github.com/.extraheader`, do not include the value
    in any field.
  - Do not run `env`, `printenv`, or `cat` against any `.env`,
    `credentials*`, or `*.key` file. If you happen to see such content
    in tool output, do not quote it.
  - If any string in any tool result looks like a credential (starts
    with `sk-ant-`, `ghp_`, `ghs_`, `gho_`, `gha_`, `github_pat_`, or
    `rmxu_`; or matches `Bearer` followed by 32+ random characters),
    replace it with `[REDACTED]` before including any surrounding
    context in your output.

Honest summarization of what tools did is fine and encouraged; only
verbatim quotes of headers / env values / tokens are forbidden.

Output a single JSON object. Start with `{` and end with `}`. No
Markdown fences, no prose before or after. Schema:

{
  "decision": "PR" | "ISSUE",
  "reasoning": "<2-3 sentences explaining the call>",
  "issue_title": "<if ISSUE: short, action-oriented title; else empty>",
  "tldr": "<if ISSUE: at-a-glance summary, max 240 chars. Cover what
            the paper actually offers, why a clean PR didn't fit, and
            what's worth deciding. The maintainer should be able to
            triage from this line alone; else empty>",
  "issue_body": "<if ISSUE: Markdown body with sections in this order
                  and with these EXACT headings:
                    '## Engineering analysis' (what the paper actually
                       contributes — NOT 'Why this paper is interesting',
                       which is rendered elsewhere by the orchestrator)
                    '## What blocks a clean implementation'
                    '## How to unblock this' (concrete questions and
                       decisions the maintainer can act on — NOT
                       'What we'd need to know')
                  else empty>",
  "replacement_experiment": "<if ISSUE: replacement for the paper's
                              suggested experiment when the original is
                              hollow or contradicts the routing decision.
                              Empty string keeps the paper's original
                              suggestion. Use this when you would
                              otherwise write 'the suggested experiment
                              is hollow' in your reasoning>"
}

--- Paper spec ---

__SPEC__

--- Repo layout (top-level modules in the target package + tests) ---

__LAYOUT__
"""

_SELECTION_PROMPT_TEMPLATE = """\
You are selecting which paper recommendation the Remyx Recommendation
orchestrator should implement as a draft PR against the target repo
(`__REPO_FULLNAME__`).

You are given a ranked candidate pool (top-N from the Remyx ranker)
and the target repo's module layout. Relevance rank is NOT
implementability: the top-ranked paper is frequently a model
architecture or training method with no call site in a data / inference
pipeline, while a lower-ranked candidate is a clean drop-in. Surface
overlap with a repo's keywords does NOT mean methodological fit — two
papers using "Stein's method" can belong to entirely different problem
classes (e.g. supervised encoding vs. posterior inference).

**Your job is to VERIFY before picking.** Use the tools below
iteratively. For your most promising candidate(s):
  - Find the call site you'd integrate into (`gh code-search` over the
    repo to locate the relevant module / function).
  - Read 1-2 lines of the actual code to confirm the integration shape
    is what the paper assumes (`gh api repos/<repo>/contents/<path>`
    or `curl -s https://raw.githubusercontent.com/<repo>/main/<path>`).
  - Check whether the team is actively working on a thread the paper
    extends (`gh issue list --repo <repo> --state open --search "..."`
    or `gh issue view <n> --repo <repo>` for specific Issues).

__ENVIRONMENT_HINT____REPO_INTEL__**Four legitimate integration shapes — classify each candidate you
consider.** A candidate that does NOT fit one of these four shapes is
a structural mismatch and should be rejected.

  - **addition** — paper adds a NEW module that is called from EXISTING
    code. The repo's current modules stay; new code is wired in. Most
    common shape. Verification: existing call site exists, the new
    module's I/O contract fits the forward path.

  - **replacement** — paper's contribution is a strict drop-in
    REPLACEMENT for an existing component with the same input/output
    contract but better internals (smaller / faster / simpler / newer
    foundation). The existing component is removed; the new one slots
    into its place. Verification: identify the existing component's
    I/O contract; confirm the paper's contract is functionally
    equivalent; estimate migration cost (which files change).

  - **simplification** — paper merges TWO OR MORE existing components
    into one with the same end-to-end contract. Pipeline collapses.
    Verification: identify the existing pipeline's boundary contract;
    confirm the merged contribution spans those boundaries cleanly;
    estimate migration cost.

  - **extension** — paper proposes a NEW capability the repo currently
    lacks but that fits as a natural extension of the existing pipeline
    shape AND the team has signaled openness to it. STRICTER bar than
    addition (no existing call site to anchor against). ALL FOUR gates
    must pass for a candidate to be classified as extension:
      1. **Pipeline-compatible I/O contract** — the new capability fits
         the repo's existing pipeline shape (e.g. for a data-pipeline
         repo, "dataset in, dataset out" is extension-compatible; a
         stage that requires a fundamentally new data shape is not).
      2. **Stated team-direction signal in the repo** — at least one
         explicit signal that the team is open to this capability:
         a README "future directions" / "roadmap" section naming the
         domain; an open Issue with title `[RFC]` / `[Proposal]` or
         labeled `rfc` / `discussion` whose body names this paper or
         a similar technique; a CONTEXT.md bullet showing recent
         investment in adjacent capabilities; the interest description
         itself naming the broader domain. Without ≥1 explicit signal,
         this is RFC-fishing, not extension — REJECT.
      3. **No existing implementation in the repo** — `gh code-search`
         confirms no existing module implements the candidate's
         contribution. If a partial implementation exists, this is
         addition or replacement, not extension.
      4. **Higher relevance + interest-alignment bar than addition** —
         tier MUST be `high` AND relevance MUST be ≥ 0.85 AND the
         `reasoning` field MUST verbalize the interest-alignment.
         Gates 1-3 carry the structural-fit load — gate 4 is a
         "ranker put this candidate in the top band" sanity check,
         not a second pass on relevance.
    Verification: cite the specific team-direction signal that satisfies
    gate 2 in the `team_direction_signal` schema field below. Cite the
    adjacent pipeline stage (upstream or downstream of the proposed new
    stage) in `proposed_call_site`.

Replacement and simplification need a STRICTER bar than addition: the
I/O contracts must align functionally, not just thematically. A paper
that "could replace" an existing component but whose actual inputs or
outputs differ from what downstream code expects is a structural
mismatch, not a replacement. Surface keyword overlap (same domain, same
technique name) does NOT make a replacement — only contract-equivalent
substitution does. Stein-Encoder is NOT a replacement for SteinVI even
though both invoke "Stein's method" — the I/O contracts are different
problem classes.

**Tie-break — when implementability is comparable, prefer
simplification > replacement > addition > extension.** A paper that
lets the maintainer simplify, accelerate, or replace an existing stage
tends to produce deeper engagement than a paper that adds a parallel
feature, all else equal. Extension is LAST-RESORT — picked only when
all three other shapes fail AND all four extension gates pass.
Reasons:

  - The repo's existing contracts are already in production. A
    proposal anchored on one of those contracts carries leverage that
    a net-new module doesn't — the maintainer doesn't have to decide
    "is this worth integrating at all" because the contract is
    already worth integrating.
  - Simplification proposals tend to ship as deliberation Issues
    (phased rollout, fallback paths, when-to-revisit thresholds)
    that preserve value even when not adopted as PRs.
  - Net-new add-alongside picks correlate with PRs that go stale or
    get rejected because the repo's actual call sites don't need them.
  - Extension picks have NO call site at all — they propose adding
    one. Without explicit team-direction signal, an extension pick is
    indistinguishable from RFC-fishing. The four gates exist to filter
    legitimate extensions (where the team has invited the capability)
    from speculation.

When two candidates score similarly on the verification bar, favor
the one that touches more existing call sites — even if its surface
relevance is lower. An add-alongside pick is still legitimate when
the broad pool genuinely lacks contract-anchored candidates; just
don't prefer it by default.

If after verification the pre-fetched candidates all turn out to be
poor structural fits, broaden the search:
  - `remyxai search info <arxiv_id>` — direct arxiv-id lookup; use this
    FIRST when a maintainer thread or repo context names a specific
    paper with an arxiv id (`arxiv NNNN.NNNNN`). The keyword search
    endpoint occasionally misses indexed assets whose names don't
    tokenize cleanly (CamelCase compound names, multi-word coinages),
    so direct lookup is the authoritative path when an id is known.
  - `remyxai search query "<technique_or_paper_name>"` — keyword search
    of the broader Remyx catalog. Use when no arxiv id is named and
    you're searching the technique space.
  - `remyxai papers list --interest <uuid> --limit 20 --format json`
    — pull a larger slice of the ranker's pool

When the broader catalog surfaces a candidate that satisfies one of the
three integration shapes — especially when a maintainer thread (an open
Issue, an active PR discussion) names a specific paper that the pool
doesn't contain AND the paper is not already in the "Already in the
team's attention" section above — you MAY return it as an
**out-of-pool pick** using the extended schema below (`chosen_index:
-2`). Papers in the discharge set have already been put in front of
the maintainer (either by Outrider or by a maintainer-opened RFC) and
must not be re-picked here, in-pool or out — selecting one wastes the
selection-pass budget and the dedup gate would skip it anyway.

The verification bar for out-of-pool picks is STRICTER than for
in-pool: the search result must explicitly match the contract the
maintainer thread (or the search query's intent) asks for — not merely
thematically related. If the search returns nothing that satisfies the
bar, fall back to `chosen_index: -1`.

Default to picking from the candidates below when one fits cleanly.
Returning `chosen_index: -1` is allowed when every in-pool candidate
fails verification AND no out-of-pool candidate cleanly satisfies the
verification bar — explain why in `reasoning`.

Tools available:
  - `gh code-search "<query>" --repo <repo>` — find call sites
  - `gh api repos/<repo>/contents/<path>` — read a file by path
  - `gh-graph <file_path>` — list a Python file's imports AND the files
    that import it (its call sites). After you locate a candidate's likely
    module, run `gh-graph` on it to walk *outward* to the call sites that
    depend on it, then read those to verify the I/O contract — this is the
    fastest way to ground "what plugs into what" rather than guessing.
  - `gh issue list/view` — see open maintainer concerns
  - `remyxai papers list/get` — inspect the ranker pool with reasoning
  - `remyxai interests get` — see the interest's project-summary context
  - `remyxai search info <arxiv_id>` — direct lookup of a known asset,
    bypasses keyword-search retrieval gaps
  - `remyxai search query` — keyword broaden beyond the pool if needed

Recovery strategies for missing/broken links (apply BEFORE concluding
"paper has no code" or "candidate is unreadable"):
  - **Arxiv URL variants.** If `arxiv.org/html/<id>` 404s or returns a
    near-empty page, try `arxiv.org/abs/<id>` first (most reliable),
    then `ar5iv.labs.arxiv.org/<id>` (HTML5 mirror — better for very
    recent papers), then `arxiv.org/pdf/<id>` as last resort.
  - **Dead live URLs (project pages, github repos).** Academic project
    pages routinely die within months. If `WebFetch <url>` 404s or
    times out, try `web.archive.org/web/<url>` for the latest archived
    snapshot. Especially relevant for `*.github.io/*` project pages
    and university-hosted demo sites.
  - **Engine reports `github_url: (none)` but code likely exists.**
    Don't take the engine's null at face value. In order:
      1. `gh search code "<distinctive method name from paper>"` — the
         method name (not the paper title) usually surfaces the official
         repo if one exists.
      2. WebFetch the arxiv abstract page and grep for `github.com/`
         links — abstracts often mention code that the engine's
         regex missed.
      3. If `huggingface_url` is populated, WebFetch the model card —
         it routinely cross-references the official codebase.
      4. If a project page is mentioned in the abstract, follow it
         one hop — code links cluster on project pages even when
         absent from the abstract.
    Treat "no code found" as a verdict you reach AFTER exhausting these,
    not a default. Many engine `github_url: (none)` cases have code
    reachable via one of these paths (ingest-side fixes may improve this
    over time; until then, agent-side recovery is the defense).
  - **Login-wall detection.** Pages from Colab, Drive, JSTOR, OpenReview
    can return HTTP 200 with sign-in content that LOOKS like real
    content. If a fetched page is < 500 chars of non-nav body AND
    contains "Sign in" / "Log in" / "Please log in", treat as
    unfetched (the content you got is not the content you wanted).
    Note in `reasoning` if a login-wall blocked verification.
  - **Failure budget.** Spend at most ~3 turns on recovery per
    candidate before moving on. If a candidate has multiple broken
    links and no reachable code/context, that itself is a signal —
    document the failed lookups in `reasoning` and reject the
    candidate rather than burning the turn budget guessing.

**Overriding the no-code penalty.** When a candidate has no code link
(license class `no-code-link`, compat 0.30) you MAY still pick it if
ALL THREE conditions hold:

  1. **Method is conceptually self-contained.** The abstract and any
     method-section text in the candidate brief contain enough detail
     that a competent implementer could reproduce the contribution
     without reference code. Empirical / analytical papers proposing a
     classification scheme, signal, or measurement typically qualify;
     complex algorithmic methods (novel attention variants, kernel
     tricks, training recipes with non-obvious hyperparameters)
     typically do not.

  2. **Verified existing call site with a clear contract match.** Same
     bar as a code-bearing addition — locate the existing function /
     module the new code would integrate into and confirm the I/O
     contract aligns.

  3. **Integration shape is `addition` or `simplification`, NOT
     `replacement` or `extension`.** Replacement touches code already
     in production, where the bar for borrowing from a no-code paper
     is much higher. Extension lacks a call site at all — combining
     "no code" + "no anchoring call site" is speculation, not a
     contract-anchored override.

When all three hold, set `chosen_index` to the candidate, classify the
shape, AND populate the new `code_override_justification` field with a
1-2 sentence audit trail explaining WHY this specific paper's method
is self-contained enough to implement from its description. The
override fires only when justification is explicit — lazy
justifications ("method is clear enough" without specifics about
which scheme/signal/measurement is being ported) are not durable and
will be reviewed downstream. When the no-code candidate does NOT meet
all three conditions, reject it in `rejected[]` with the standard
reason; the override is a narrow carve-out, not a general permission.

Stop iterating once you have enough evidence to pick (verified one
candidate fits one of the three shapes) OR to reject all (every
candidate has a structural mismatch). Don't burn turns on diminishing
returns.

Output a single JSON object. Start with `{` and end with `}`. No Markdown
fences, no prose before or after. Schema:

{
  "chosen_index": <integer index into the candidate list below,
                   -1 if every candidate failed verification,
                   -2 if you surfaced an out-of-pool candidate via
                       `remyxai search query` that cleanly fits>,
  "chosen_call_site": "<the specific path:function you verified the
                        paper plugs into for `addition`, or the existing
                        component(s) being replaced for `replacement` /
                        `simplification`; omit when chosen_index = -1
                        or when integration_shape = `extension` (use
                        `proposed_call_site` instead)>",
  "external_arxiv_id": "<arxiv_id of the out-of-pool paper; REQUIRED
                         when chosen_index = -2, omit otherwise>",
  "external_title": "<title of the out-of-pool paper from the search
                      result; REQUIRED when chosen_index = -2>",
  "external_query_used": "<the `remyxai search query` argument you
                           actually ran to surface it; REQUIRED when
                           chosen_index = -2>",
  "integration_shape": "addition" | "replacement" | "simplification"
                       | "extension"
                       (omit when chosen_index = -1),
  "team_direction_signal": "<REQUIRED when integration_shape =
                             `extension`, omit otherwise: the specific
                             repo signal that satisfies extension gate
                             2 — e.g. 'Issue #NN (open, labeled rfc)
                             names this paper directly' or
                             'README \"Future directions\" section names
                             this domain' or 'CONTEXT.md shipping
                             bullets show 4 recent commits in adjacent
                             stage'>",
  "proposed_call_site": "<REQUIRED when integration_shape = `extension`,
                          omit otherwise: the adjacent existing pipeline
                          stage (upstream or downstream of the proposed
                          new stage) — e.g. 'after pkg.module.stage_a,
                          before publish — same dataset I/O shape'>",
  "contract_match": "<one line — REQUIRED for replacement /
                      simplification AND for any chosen_index = -2:
                      how the existing component's I/O contract and the
                      paper's contract align (and where they don't).
                      Omit for in-pool addition.>",
  "migration_cost": "<one line — REQUIRED for replacement /
                      simplification AND for any chosen_index = -2:
                      list of files that would change in a real swap
                      (factory function, requirements, tests, docs).
                      Omit for in-pool addition.>",
  "verification_summary": "<one line: what you actually verified to
                            pick this — e.g. 'gh code-search confirmed
                            torchtune/training/quantization/_quantize.py
                            hosts the bit-allocation step the paper
                            extends'>",
  "code_override_justification": "<OPTIONAL: REQUIRED when the chosen
                                   candidate has license_class=`no-code-link`
                                   (compat 0.30) AND you are picking it as
                                   `addition` or `simplification` per the
                                   override carve-out documented above.
                                   1-2 sentences naming the specific
                                   self-contained signal/scheme/measurement
                                   being ported and why it does not need
                                   reference code to disambiguate. Omit
                                   when the chosen candidate has a code
                                   link, when chosen_index < 0, or when
                                   the integration_shape is `replacement`
                                   or `extension`.>",
  "reasoning": "<2-3 sentences: why this candidate's contribution maps
                 cleanly onto the verified call site (addition) or why
                 the contract match is clean (replacement /
                 simplification); cite an Issue number if alignment
                 surfaced one>",
  "rejected": [
    {"index": <int>, "why": "<one line: why this candidate fails
                              verification — e.g. 'paper assumes a
                              trainer the repo lacks', or 'shared
                              keyword but different problem class
                              (verified via <path>)', or 'proposed as
                              replacement but I/O contract differs:
                              X vs Y'>"}
  ],
  "is_re_pick": <boolean — set true ONLY when the chosen candidate has
                  been dispatched before on this fork (see "Already-
                  dispatched" section, when present, for the list).
                  Default false; omit when the section is absent.>,
  "re_pick_justification": "<REQUIRED when is_re_pick=true: name the
                             specific way this new dispatch materially
                             compounds on the prior landing. Valid
                             reasons: different mode citation (e.g.
                             prior was Mode 3, this is Mode 2);
                             different call-site scope (prior at
                             path/A, this at path/B); incorporates a
                             coordination signal absent from prior
                             (name it). Duplicate-work re-picks are
                             not allowed — set chosen_index to a
                             different candidate instead. Omit when
                             is_re_pick is false or omitted.>"
}

__DISCHARGED_PAPERS____ALREADY_DISPATCHED__--- Candidates (highest relevance first) ---

__CANDIDATES__

--- Repo layout (top-level modules in the target package + tests) ---

__LAYOUT__
"""

_SELF_REVIEW_PROMPT_TEMPLATE = """\
You are reviewing your own implementation of a paper recommendation
before the orchestrator opens a PR.

Inputs:
  1. The original implementation spec (read `.remyx-recommendation/SPEC.md`
     in the working directory)
  2. The full diff of your changes (provided at the end of this message)

Output a single JSON object. Start with `{` and end with `}`. No
Markdown fences, no prose before or after. Schema:

{
  "delivered":   [<bullets: the paper's insight/result this diff delivers
                   to the repo — the concrete value, at the call site>],
  "scoped_out":  [<bullets: parts of the paper intentionally NOT built
                   because they aren't needed for that value (note any
                   required infra in parentheses). These are scoping
                   decisions, not shortfalls — a focused slice that
                   delivers the result is the goal.>],
  "call_site":   "<which existing entry point the new code is invoked
                   from, or '(none)' if nothing in the product calls it>",
  "is_orphan":   <true if the new code is NOT reached from any pre-existing
                   execution path — no production / pipeline entry point
                   and no existing module invokes it (only the tests you
                   added, if any, call it). This is about REACHABILITY, not
                   quality: rich, correct code that the product never calls
                   is still an orphan. Do NOT use this field to judge
                   whether the code is "too simple" — triviality is scored
                   separately by stub density.>,
  "honest_summary": "<one short paragraph: the value this delivers in the
                     paper's direction, and what you intentionally scoped
                     out as unnecessary for it. Frame scoped-out parts as
                     deliberate boundaries, not as what you 'failed' to do.>",
  "mode_cited":  "<one of: 'Mode 1 (direct port)', 'Mode 2 (adapted port)',
                   'Mode 3 (inspired experiment)'. This tells the downstream
                   fidelity gate how to interpret the diff:
                     Mode 1 — expect the diff to match the paper's method;
                             fidelity gate does strict method-vs-diff.
                     Mode 2 — core mechanism at fidelity + substitutions
                             on auxiliaries; fidelity gate treats named
                             substitutions as defensible deviations.
                     Mode 3 — target-native experiment inspired by the
                             paper; fidelity gate skips method-vs-diff and
                             validates insight-preservation instead.>",
  "substitutions": [<Mode-2 ONLY: bullets naming each auxiliary component
                     you replaced with a target-native equivalent, in the
                     shape 'paper's <X> replaced by <Y>'. Empty list for
                     Mode 1 / Mode 3. Example: 'paper's learned MI
                     estimator replaced by prompt/response vocab-overlap
                     proxy'.>],
  "reframed_insight": "<Mode-3 ONLY: one sentence naming the paper's core
                       insight/framing that this diff draws on, and how
                       it maps to the target's actual problem. Empty
                       string for Mode 1 / Mode 2. Example: 'WorldSample's
                       insight that value overestimation signals unreliable
                       training transitions applies to SAC's critic ensemble
                       — the clipped-double-Q spread is a target-native
                       equivalent of the paper's world-model-augmented
                       overestimation signal.'>"
}

Be ruthless about reachability, but distinguish LIBRARY-shape API additions
from APPLICATION-shape orphan scaffolding. In a library — one that ships a
Python package for external users to import — a new public class/function
exported from a package `__init__.py` IS the API surface; external callers,
not internal library code, are the consumers. That is NOT orphan (set
is_orphan=false and note the library-API framing in honest_summary). In an
application repo (a CLI / service / pipeline), an addition that no pre-
existing entry point invokes is a real orphan (is_orphan=true).

Use the available tools to verify repo shape before deciding — signals of
library-shape include `pyproject.toml` with `[project]` + `packages`,
`setup.py` with `packages=`, or a `src/<pkg>/` layout; signals of application-
shape include a top-level `cli.py` / `main.py` / `server.py` without a
package declaration. When in doubt, quote the specific file evidence in
honest_summary so a reviewer can audit the reasoning.
Separately, list under scoped_out the parts of the paper you deliberately
left for later (e.g. a trainer/model the repo can't host) — you are not
required to reproduce the paper's full method, only to deliver its result.

NEVER include token-shaped strings in any JSON field — the JSON you
write here flows verbatim into a public-repo PR or Issue body, and a
credential pattern in that body aborts the run via the outbound
scrubber. Specifically:

  - If a tool result contains an `Authorization:` header (e.g. from
    `curl -v`, `wget -d`, or any HTTP-debug output), do not quote the
    header verbatim. Describe what the call did instead.
  - If `git config --list` or a similar command exposes
    `http.https://github.com/.extraheader`, do not include the value
    in any field.
  - Do not run `env`, `printenv`, or `cat` against any `.env`,
    `credentials*`, or `*.key` file. If you happen to see such content
    in tool output, do not quote it.
  - If any string in any tool result looks like a credential (starts
    with `sk-ant-`, `ghp_`, `ghs_`, `gho_`, `gha_`, `github_pat_`, or
    `rmxu_`; or matches `Bearer` followed by 32+ random characters),
    replace it with `[REDACTED]` before including any surrounding
    context in your output.

Honest summarization of what tools did is fine and encouraged; only
verbatim quotes of headers / env values / tokens are forbidden.

--- Diff ---

__DIFF__
"""

_PR_BODY_TEMPLATE = """\
> **Drafted by an autonomous discovery loop** — Remyx ranks recent arXiv papers against this team's research interest and shipping history; Claude Code selects the candidate most directly implementable against this repo from the lookback window and drafts it.
>
> **Recommended paper**: [{paper_title}](https://arxiv.org/abs/{arxiv_id})
> **Confidence**: {tier_emoji} {tier} (Remyx relevance {relevance_score:.2f})
> **Research interest**: {interest_name}
> **Implementation by**: Claude Code as autonomous agent

---

## Why this paper for this team

{reasoning}
{selection_section}{license_section}
## Suggested experiment

{suggested_experiment}

---

{test_section}

---

> **Want eval-on-every-PR?** Outrider Validate (coming soon, paid tier) runs your benchmark suite against this diff and posts the results as a PR comment. Design partner pilot is open — [join the waitlist](https://github.com/remyxai/outrider/discussions/19).

_Opened by the [Remyx Recommendation]({attribution_url}) orchestrator._
"""


# ─── Data classes ──────────────────────────────────────────────────────────


DRAFT_MODES = ("always", "on_test_failure", "never")

# Test-integration gate policy values. See Target.test_integration_policy.
TEST_INTEGRATION_POLICIES = ("strict", "soft", "off")

# Terminal statuses that should make the workflow step exit non-zero (red
# in CI). Everything else — Issues, skips, PRs — is a legitimate green
# outcome. `claude_failed` used to exit 0, so a run that produced no PR/Issue
# looked green; it now fails visibly. `weekly_summary_failed` is the
# weekly-summary mode's analog: a run that posted nothing should be red.
FAILURE_EXIT_STATUSES = {
    "error",
    "claude_failed",
    "weekly_summary_failed",
    "fidelity_failed_clone",
    "fidelity_failed_claude",
    "convention_failed_extraction",
    "convention_failed_misalignment",
    "convention_failed_patch",
    "convention_failed_push",
    "test_failed_setup",
    # Outbound credential-scrubber abort. Not a graceful skip — the
    # operator needs to investigate the body-assembly path (whatever
    # upstream content produced credential-shaped text). Surfaces red
    # in CI rather than silently green so the signal isn't lost.
    "aborted_secret_in_payload",
}


class LeadCapturedInBranchMode(RuntimeError):
    """Signals that publish=branch is in effect and an Issue would have
    been filed; the LEAD content is captured for the step summary instead
    of touching the target repo.

    Carries the intended title + fully-formatted body (footer + reengage
    note included) so process_target renders the captured content into the
    step summary — matching exactly what the maintainer would have seen if
    the Issue had been filed. Team reads the step summary and files the
    Issue manually via ``gh issue create`` when they decide to promote.
    Non-fatal graceful capture; run ends with ``lead_captured_no_issue``.
    """
    def __init__(self, title: str, body: str) -> None:
        super().__init__("Issue capture suppressed: publish=branch")
        self.title = title
        self.body = body


class BranchPushedFromDowngrade(RuntimeError):
    """Signals that publish=branch is in effect at a post-coding-agent
    downgrade point, and the branch has been pushed to the fork in lieu
    of opening a downgrade Issue.

    Under normal publish=pr mode, when a downstream check (integration
    validator, stub-density, diff-risk, self-review orphan, etc.) fires
    after the coding agent produced code, the flow calls open_downgrade
    which files an Issue with the diff attached. Under publish=branch,
    the whole point of the mode is *user reviews what the agent produced*
    — dropping the code because a rule fired defeats that purpose.

    This sentinel is raised after commit_and_push has pushed the branch,
    carrying the downgrade context (reason + detail) so process_target
    can surface why-would-have-been-blocked as informational content in
    the step summary. Team reviews the branch + reasoning, decides whether
    to promote to PR or discard.
    """
    def __init__(self, branch: str, branch_url: str, reason: str, detail: str) -> None:
        super().__init__(
            f"Downgrade suppressed under publish=branch: {reason}"
        )
        self.branch = branch
        self.branch_url = branch_url
        self.reason = reason
        self.detail = detail


class IssuesDisabledError(RuntimeError):
    """The target repo has its Issues tab disabled and the run's token
    can't re-enable it.

    Enabling Issues requires repo `administration` permission. Scoped
    GitHub-App installation tokens (how Remyx authenticates the bot) are
    deliberately *not* granted admin, so we can't flip `has_issues`. Treated
    as a graceful skip (`skipped_issues_disabled`), not a failure — the run
    stays green and the user can enable the Issues tab to receive Issue-mode
    recommendations.
    """


@dataclass
class Target:
    repo: str                         # "owner/name" — the target repo
                                      # (where PRs and Issues land).
                                      # The action either runs in this repo
                                      # (same-repo customer install) or
                                      # operates on it cross-repo from a
                                      # controller repo with a PAT in
                                      # FF_GITHUB_TOKEN. There is no
                                      # "fork mode" — PRs always go to
                                      # `repo` directly.
    interest_id: str = ""             # Remyx ResearchInterest UUID — pre-filled
                                      # from the engine.remyx.ai workflow snippet
    min_confidence: str = "moderate"
    rate_limit_days: int = 7
    # PR-draft policy:
    #   "always"          — every PR opens as draft (default; future
    #                       webhook + Modal eval flow will mark them
    #                       ready after the team's own evals pass)
    #   "on_test_failure" — tests pass: ready; tests fail: draft
    #   "never"           — tests pass: ready; tests fail: SKIP (don't open
    #                       PR at all). Equivalent to the old
    #                       draft_on_test_failure=False behavior.
    draft_mode: str = "always"
    guardrails_allowlist: list[str] = field(default_factory=list)
    # Test-integration gate policy:
    #   "strict" (default) — gate failure routes to Issue (current behavior)
    #   "soft"             — gate failure opens draft PR with warning section
    #   "off"              — skip the gate entirely
    # See `check_tests_touch_existing_modules`. Repos where standalone-module
    # contributions ARE the contribution shape (graph NN, kernels, layer
    # libraries) benefit from "soft"; application/pipeline repos should
    # keep "strict".
    test_integration_policy: str = "strict"
    # Per-run wall-clock budget for the Claude Code implementation step.
    # 600s was too tight on large repos; configurable via `claude-timeout`.
    claude_timeout_s: int = 900
    # Optional: force-select a specific candidate by arxiv_id (skips the
    # LLM selection pass) so eval re-runs are reproducible. Empty = normal
    # selection.
    pin_arxiv: str = ""
    # Optional: free-text method/technique query. When set, the recommend
    # phase resolves it to the top arxiv hit via /search/assets, builds a
    # single-item viable pool from that asset, and pins to it — bypassing
    # both the interest's candidate pool and the selection pass. Mutually
    # exclusive with pin_arxiv (validated in build_target_from_env).
    search_method: str = ""
    # Optional: route the Claude Code subprocess at a non-default base URL.
    # When set, becomes ``ANTHROPIC_BASE_URL`` for every Claude CLI call,
    # which is the Anthropic-Messages-compatible protocol any z.ai/GLM,
    # Bedrock, Vertex, or on-prem proxy backend speaks. The ANTHROPIC_API_KEY
    # secret should then be the backend's key, not the Anthropic key. Cost
    # telemetry (``total_cost_usd``) is the Claude CLI's estimate using its
    # built-in Anthropic-rate pricing — accurate for Anthropic, approximate
    # for any other backend. Token counts remain accurate regardless.
    model_base_url: str = ""
    # Inline refinement chain: when True (default), recommend mode continues
    # sequentially into fidelity audit → convention pass → test gate on the
    # just-opened PR, so the chain runs by default without the customer
    # deploying the standalone outrider-fidelity/convention/test workflows.
    # Set to False (action input `chain: false`) for cost-sensitive runs or
    # when using the separate-workflow chain pattern.
    chain_enabled: bool = True
    notes: str = ""


@dataclass
class Recommendation:
    paper_title: str
    arxiv_id: str
    tier: str                         # "high" / "moderate" / "low" / "noise"
    z_score: float                    # legacy; unused since the Remyx-API pivot
    spec_md: str                      # legacy; PR body now sources from
                                      # reasoning + suggested_experiment instead
    paper_abstract: str
    domain_summary: str
    raw_paper_md: str
    # New fields populated by query_remyx_recommendation() — match the Remyx
    # /papers/recommended response envelope so downstream renderers can pull
    # whichever fields they need.
    relevance_score: float = 0.0
    reasoning: str = ""
    suggested_experiment: str = ""
    recommendation_id: str = ""
    interest_name: str = ""
    interest_context: str = ""        # rich text body the customer wrote
                                      # on engine.remyx.ai (research focus,
                                      # current goals, what they care about)
    experiment_history: str = ""      # LLM-ready bullets from
                                      # ExperimentHistory format,
                                      # fetched from the research-interests
                                      # endpoint. Empty when the interest
                                      # has no linked history.
    # License + code-availability gate. Populated best-effort by
    # query_remyx_candidates after the Remyx fetch; missing data lands
    # as empty / "unknown" / 0.0 so downstream renderers can show the
    # red flag without blowing up the run. License compatibility is
    # scored against the target repo's own license (fetched once per
    # run).
    paper_github_url: str = ""        # canonical https://github.com/owner/repo
                                      # extracted from the Remyx resource
                                      # envelope, scraped from paper text,
                                      # or pulled from the arxiv abstract
                                      # page as a final fallback.
    paper_huggingface_url: str = ""   # canonical
                                      # https://huggingface.co/owner/model
                                      # extracted from the same sources.
                                      # When present, the HF Hub model-card
                                      # frontmatter is the authoritative
                                      # license source (preferred over the
                                      # GitHub LICENSE classifier output)
                                      # because it describes the *weights*
                                      # a customer would actually load.
    paper_license: str = ""           # SPDX-ish identifier as reported by
                                      # the most authoritative source
                                      # available (HF model card > GitHub
                                      # LICENSE). Examples: "Apache-2.0",
                                      # "GPL-3.0", "CC-BY-NC-SA-4.0",
                                      # "NOASSERTION".
    license_source: str = ""          # which signal produced ``paper_license``
                                      # — "huggingface" | "github" |
                                      # "github_content_sniff" | "" (none).
                                      # Used by the renderer + log for
                                      # provenance and by mismatch-detection
                                      # when both HF and GitHub disagree.
    license_class: str = "unknown"    # bucket — "permissive" | "copyleft" |
                                      # "nc" | "missing" | "no-code-link" |
                                      # "unknown". The "no-code-link" class
                                      # is distinct from "missing": the
                                      # former means we couldn't find any
                                      # code repo URL to inspect, the latter
                                      # means we *did* fetch and got nothing
                                      # parseable — different signal for
                                      # the maintainer.
    license_compat: float = 0.0       # ∈ [0, 1] vs the target repo's
                                      # license class; see
                                      # _license_compat_score for the rubric.
    family_summary: str = ""          # When candidates that share the same
                                      # code repo are coalesced (paper-version
                                      # families: one repo, multiple arxiv
                                      # releases), the representative carries
                                      # a human-readable summary of the
                                      # siblings. Empty for solo candidates.
    refine_query: str = ""            # Non-empty when this candidate reached
                                      # the pool via a deep-search refine
                                      # query (audit pass) rather than the
                                      # broad /papers/recommended ranking.
                                      # Carries the query text for provenance;
                                      # "" = broad pool. Drives the pool-
                                      # composition telemetry.


# ─── Helpers ───────────────────────────────────────────────────────────────


# Run-scoped cache for the self-minted remyx[bot] token — one mint attempt
# per run, success or failure. `permissions` carries the scopes the engine
# actually granted so capability-aware callers (the Discussion post) can
# branch instead of discovering a 403.
_BOT_TOKEN = {"attempted": False, "token": "", "permissions": {}}


def _mint_bot_token() -> str:
    """Self-mint a short-lived remyx[bot] installation token from the engine.

    The action already holds REMYX_API_KEY — exactly the credential the
    engine's ``/github/installation-token`` endpoint authenticates — so
    the bot identity must not depend on the customer's workflow YAML
    carrying a mint step. Called lazily by ``_github_token``; one attempt
    per run. Best-effort: any failure (engine unreachable, App not
    installed, no provisioned action for this repo) returns ``""`` and
    the caller falls back to GITHUB_TOKEN — the same graceful semantics
    as the YAML mint step's ``|| token=""``.
    """
    if _BOT_TOKEN["attempted"]:
        return _BOT_TOKEN["token"]
    _BOT_TOKEN["attempted"] = True
    api_key = (
        os.environ.get("REMYX_API_KEY") or os.environ.get("REMYXAI_API_KEY")
    )
    repo = (os.environ.get("TARGET_REPO") or "").strip()
    repo = repo.split("github.com/")[-1].strip("/")
    if not api_key or "/" not in repo:
        return ""
    req = urllib.request.Request(
        f"{REMYX_API_BASE}/api/v1.0/github/installation-token",
        data=json.dumps({"repo": repo}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read() or b"{}")
    except Exception as e:
        log.info(f"  bot-token self-mint unavailable ({e}); "
                 f"falling back to GITHUB_TOKEN")
        return ""
    _BOT_TOKEN["token"] = (data.get("token") or "").strip()
    _BOT_TOKEN["permissions"] = data.get("permissions") or {}
    if _BOT_TOKEN["token"]:
        log.info(f"  ✓ self-minted remyx[bot] token (scopes: "
                 f"{sorted(_BOT_TOKEN['permissions']) or '(unreported)'})")
    return _BOT_TOKEN["token"]


def _github_token() -> str:
    """Resolve the GitHub token to use for git push + API calls.

    Preference order:
      1. INPUT_GITHUB_TOKEN — explicit override: a cross-repo PAT, or a
         bot token the workflow's own mint step passed via the
         `github-token` input.
      2. Self-minted remyx[bot] installation token (engine-issued; see
         ``_mint_bot_token``). Makes the bot the DEFAULT author of every
         artifact — PRs, Issues, Discussion comments — even when the
         workflow YAML carries no mint step.
      3. GITHUB_TOKEN — the workflow's built-in token (artifacts author
         as github-actions[bot]).

    Two separate env vars rather than a single `${{ a || b }}` in
    action.yml because GitHub Actions' || operator on empty-string
    inputs returns '' instead of falling through (observed via v1.0.3
    git-push failure). Resolving in Python gives reliable semantics.
    """
    explicit = os.environ.get("INPUT_GITHUB_TOKEN", "").strip()
    if explicit:
        return explicit
    minted = _mint_bot_token()
    if minted:
        return minted
    return os.environ.get("GITHUB_TOKEN", "").strip()


# Defense-in-depth: refuse to send any GitHub API body whose string
# fields contain content matching known credential shapes (Anthropic
# API keys, GitHub PAT/App/OAuth tokens, Remyx API keys, JWTs, Bearer
# headers, env-var-style leaks). Outbound PR/Issue/Discussion bodies
# get assembled from many upstream paths — agent self-review, test
# stdout, pre-flight reasoning, file-fallback content — and a
# regression in any of them could otherwise leak a secret into a
# public repo via the GitHub API. Catching it at the API boundary is
# the one place that covers every upstream variant.
#
# Fails closed: raises ``OutboundSecretError`` rather than scrubbing
# in-place, because partial redaction risks letting variant token
# shapes through. The exception message reports the JSON path and a
# pattern identifier — never the matched secret itself.
_OUTBOUND_SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("github_token", re.compile(r"\bgh[psoaru]_[A-Za-z0-9]{20,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("remyx_api_key", re.compile(r"\brmxu_[A-Za-z0-9_-]{20,}\b")),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
        ),
    ),
    (
        "authorization_header",
        re.compile(r"(?i)Authorization\s*:\s*Bearer\s+[A-Za-z0-9_.-]{16,}"),
    ),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_.-]{32,}")),
    (
        "env_var_leak",
        re.compile(
            r"\b(?:ANTHROPIC_API_KEY|REMYX_API_KEY|GITHUB_TOKEN|"
            r"INPUT_GITHUB_TOKEN)\s*=\s*[A-Za-z0-9_.-]{16,}"
        ),
    ),
)


class OutboundSecretError(RuntimeError):
    """A GitHub API call was refused because the request payload
    matched credential patterns. Investigate the body-assembly path
    (PR template, Issue body, GraphQL variables, comment text) before
    retrying. The exception message includes only the JSON path and a
    pattern identifier — never the actual matched secret.

    Structured attrs (``path`` and ``patterns``) let ``process_target``
    route the abort to the dedicated ``aborted_secret_in_payload``
    status and surface the diagnostic in step-summary + run telemetry
    without parsing the message string."""

    def __init__(
        self,
        msg: str,
        *,
        path: str = "",
        patterns: list[str] | None = None,
    ) -> None:
        super().__init__(msg)
        self.path = path
        self.patterns = patterns or []


def _scan_for_secrets(text: str) -> list[str]:
    """Return pattern identifiers for any credential shapes found in
    ``text``. Empty when clean. The actual matched secret is never
    included in the return value, so a log message built from this
    can't propagate the leaked credential further."""
    if not text:
        return []
    hits: list[str] = []
    for name, pat in _OUTBOUND_SECRET_PATTERNS:
        if pat.search(text):
            hits.append(name)
    return hits


def _scrub_outbound_payload(payload: Any, _path: str = "") -> None:
    """Recursively scan ``payload`` for secret-shape strings and raise
    ``OutboundSecretError`` if any are found. No-op for ``None``.

    Used by ``gh_api`` and ``gh_graphql`` to refuse outbound requests
    whose bodies contain content matching credential patterns. Fails
    closed: the exception aborts the API call entirely rather than
    silently redacting, so the operator must investigate the upstream
    leak before the next request goes out."""
    if payload is None:
        return
    if isinstance(payload, str):
        hits = _scan_for_secrets(payload)
        if hits:
            # Diagnostic: log per-pattern match counts and lengths before
            # raising. Lengths discriminate real-token matches (typically
            # 40-100+ chars) from prose false positives (near the regex
            # minimum — e.g. 32-40 chars on the bearer pattern). The
            # matched content itself is never logged or included in the
            # exception message; only the lengths leave the runner.
            lengths_by_pattern: dict[str, list[int]] = {}
            for name, pat in _OUTBOUND_SECRET_PATTERNS:
                if name in hits:
                    lengths_by_pattern[name] = [
                        m.end() - m.start() for m in pat.finditer(payload)
                    ]
            detail = ", ".join(
                f"{n}(lens={lengths_by_pattern[n]})" for n in hits
            )
            log.error(
                f"outbound-payload scrubber matched at field {_path!r}: "
                f"{detail}; refusing to send"
            )
            raise OutboundSecretError(
                f"Outbound payload field {_path!r} matched credential "
                f"pattern(s) {hits}; refusing to send the API request. "
                f"Investigate the body-assembly path before retrying — "
                f"this is a leak-prevention abort, not a content issue. "
                f"See preceding log line for match lengths per pattern; "
                f"a match length near the regex minimum often indicates "
                f"a prose false positive vs. a real credential.",
                path=_path,
                patterns=hits,
            )
        return
    if isinstance(payload, dict):
        for k, v in payload.items():
            child = f"{_path}.{k}" if _path else str(k)
            _scrub_outbound_payload(v, child)
        return
    if isinstance(payload, list):
        for i, v in enumerate(payload):
            _scrub_outbound_payload(v, f"{_path}[{i}]")
        return
    # Numbers, bools, etc. pass through silently.


def gh_api(method: str, path: str, body: dict | None = None) -> Any:
    """Minimal GitHub API wrapper."""
    _scrub_outbound_payload(body)
    token = _github_token()
    if not token:
        raise RuntimeError(
            "Neither INPUT_GITHUB_TOKEN nor GITHUB_TOKEN is set. The "
            "action.yml should pass ${{ github.token }} as GITHUB_TOKEN "
            "by default; if you're invoking the script outside an Action, "
            "export GITHUB_TOKEN manually."
        )
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "feature-finder-orchestrator",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"GitHub {method} {path} → HTTP {e.code}: {body_text}") from e


def gh_graphql(
    query: str, variables: dict | None = None, token: str | None = None,
) -> dict:
    """Minimal GitHub GraphQL wrapper — sibling of ``gh_api``.

    The Discussions API is GraphQL-only (no REST endpoint exists for
    posting Discussion comments), so the weekly-summary mode needs this
    alongside the REST helper. Same token resolution and error shape as
    ``gh_api``: raises RuntimeError on transport errors AND on
    GraphQL-level errors (GraphQL returns HTTP 200 with an ``errors``
    array; surfacing those as exceptions keeps the two helpers
    behaviorally identical for callers). ``token`` overrides the resolved
    token — used by the Discussion-post permission fallback. Returns the
    ``data`` object.
    """
    _scrub_outbound_payload(variables)
    token = token or _github_token()
    if not token:
        raise RuntimeError(
            "Neither INPUT_GITHUB_TOKEN nor GITHUB_TOKEN is set. The "
            "action.yml should pass ${{ github.token }} as GITHUB_TOKEN "
            "by default; if you're invoking the script outside an Action, "
            "export GITHUB_TOKEN manually."
        )
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql", data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "feature-finder-orchestrator",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            resp = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"GitHub GraphQL → HTTP {e.code}: {body_text}") from e
    if resp.get("errors"):
        raise RuntimeError(
            f"GitHub GraphQL errors: {json.dumps(resp['errors'])[:500]}"
        )
    return resp.get("data") or {}


def slugify(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s.lower()).strip("-")
    return s[:max_len]


def format_pr_title(rec: "Recommendation") -> str:
    """Return a clean PR title for the recommendation, no Outrider prefix.

    Drops the historical ``[Remyx Recommendation]`` prefix so the title
    matches how a human contributor would title the PR. Outrider
    attribution is preserved in the PR body footer; dedup falls back to
    body-marker recognition (``"Remyx Recommendation" in body``) for
    PRs created without the legacy title prefix.
    """
    return rec.paper_title


def format_branch_name(rec: "Recommendation") -> str:
    """Return a clean branch name for the recommendation, no Outrider prefix.

    Drops the historical ``remyx-recommendation/`` prefix. Uses a
    slugified paper title (more human-readable than the bare arxiv id)
    with the arxiv id as a fallback identifier when the title is empty.
    Dedup paths that previously matched against ``BRANCH_PREFIX`` now
    fall back to identifying our PRs via the body marker.

    When INPUT_START_FROM_REF is set, this is a refinement run
    on a prior draft — append ``-refined`` so the push doesn't collide
    with (or force-push over) the original artifact when the same paper
    drives both runs.
    """
    if rec.paper_title:
        base = slugify(rec.paper_title)
    else:
        base = rec.arxiv_id or "paper-recommendation"
    if (os.environ.get("INPUT_START_FROM_REF") or "").strip():
        return f"{base}-refined"
    return base


def _remote_branch_exists(target: "Target", branch: str) -> bool:
    """Whether ``branch`` already exists on the fork's remote via
    ``git ls-remote``-shaped GitHub API call.

    Returns False on any network error — the collision-suffix logic
    treats "unknown" as "no collision" so it doesn't over-suffix on
    transient failures. The subsequent push either succeeds (no
    collision existed) or fails cleanly (collision surfaces as a
    non-fast-forward, which is the caller's problem to handle).
    """
    if not branch:
        return False
    token = _github_token()
    if not token:
        return False
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{target.repo}/git/ref/heads/{branch}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "outrider-collision-check",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        return False
    except (urllib.error.URLError, OSError):
        return False


def _apply_branch_collision_suffix(
    target: "Target", branch: str, max_bumps: int = 20,
) -> str:
    """When ``branch`` already exists on the fork's remote, append
    ``-v2``, ``-v3``, ... until an unused name is found. Preserves the
    ``-refined`` convention: refinement runs (with the suffix already
    baked in by ``format_branch_name``) still get ``-v2``, ``-v3`` on
    top when repeated refinements land on the same base.

    Falls back to a ``-YYYYMMDD`` timestamp suffix if ``-v20`` is still
    colliding (astronomically unlikely; hedges against runaway).

    Returns the collision-free branch name; equals ``branch`` unchanged
    when no collision was detected.
    """
    if not _remote_branch_exists(target, branch):
        return branch

    for bump in range(2, max_bumps + 1):
        candidate = f"{branch}-v{bump}"
        if not _remote_branch_exists(target, candidate):
            log.info(
                "  → branch collision: '%s' exists on %s; using '%s'",
                branch, target.repo, candidate,
            )
            return candidate

    # Extreme fallback: date-stamped variant. Never expected to fire.
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    fallback = f"{branch}-{stamp}"
    log.warning(
        "  ⚠ branch collision: exhausted -v2..-v%d suffixes for '%s'; "
        "using date-stamped fallback '%s'",
        max_bumps, branch, fallback,
    )
    return fallback


def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


# ─── Remyx API recommendation ──────────────────────────────────────────────


def _remyx_get(path: str, *, params: dict | None = None) -> dict:
    """GET against the Remyx engine API with the configured API key.
    Raises RuntimeError on non-2xx response."""
    api_key = os.environ.get("REMYX_API_KEY") or os.environ.get("REMYXAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "REMYX_API_KEY (or REMYXAI_API_KEY) is required. Generate one "
            "from your engine.remyx.ai settings and add it as a workflow "
            "secret."
        )
    url = REMYX_API_BASE.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "feature-finder-orchestrator",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(
            f"Remyx API GET {path} → HTTP {e.code}: {body}"
        ) from e


def _remyx_post(path: str, body: dict) -> dict:
    """POST against the Remyx engine API with the configured API key.
    Raises RuntimeError on non-2xx response. Mirrors ``_remyx_get``."""
    api_key = os.environ.get("REMYX_API_KEY") or os.environ.get("REMYXAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "REMYX_API_KEY (or REMYXAI_API_KEY) is required. Generate one "
            "from your engine.remyx.ai settings and add it as a workflow "
            "secret."
        )
    url = REMYX_API_BASE.rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "feature-finder-orchestrator",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(
            f"Remyx API POST {path} → HTTP {e.code}: {body_text}"
        ) from e


def _refresh_and_poll_recommendations(target: Target, fetch_fn) -> list:
    """Trigger a recommendation refresh for the interest, then poll until
    picks appear or ``REMYX_REFRESH_WAIT_S`` elapses.

    A brand-new interest (or one whose daily ranking hasn't run since the
    last cron) returns an empty pool; the engine ranks asynchronously after
    a POST to /papers/recommended/refresh. Returns the populated list, or
    [] if nothing landed within the budget.
    """
    log.info("  → empty recommendation pool; triggering "
             "/papers/recommended/refresh and polling")
    try:
        _remyx_post(
            "/api/v1.0/papers/recommended/refresh",
            {"interest_id": target.interest_id},
        )
    except Exception as e:
        log.warning(f"    (refresh trigger failed: {e})")
    deadline = time.monotonic() + REMYX_REFRESH_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(10)
        try:
            papers = fetch_fn()
        except Exception as e:
            log.warning(f"    (poll failed: {e}; retrying)")
            continue
        if papers:
            log.info(f"    ✓ recommendations populated ({len(papers)})")
            return papers
    return []


def _relevance_to_tier(score: float) -> str:
    if score >= RELEVANCE_TIER_FLOOR["high"]:
        return "high"
    if score >= RELEVANCE_TIER_FLOOR["moderate"]:
        return "moderate"
    if score >= RELEVANCE_TIER_FLOOR["low"]:
        return "low"
    return "noise"


# ─── License + code-availability gate ─────────────────────────────────────
#
# Adoption-blockers we've hit in practice: papers with no LICENSE file at
# all, and papers with CC-BY-NC* licenses that block commercial use.
# Both cost the maintainer real investigation time before the constraint
# becomes visible. The gate's job is to surface that signal at
# recommendation time — soft-scored, not hard-filtered, so a research
# repo can still see NC papers if it wants to.

_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)"
)
_HUGGINGFACE_URL_RE = re.compile(
    r"https?://huggingface\.co/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)"
)

# Top-level GitHub paths that look like owner names in the URL but aren't
# repos — skip them when scraping paper text for code links.
_GITHUB_NON_REPO_OWNERS = frozenset({
    "orgs", "topics", "marketplace", "settings", "notifications",
    "issues", "pulls", "explore", "trending", "features", "about",
    "search", "login", "signup", "new", "codespaces", "sponsors",
})

# HuggingFace top-level paths that aren't model owners — same idea, the
# regex catches any /<word>/<word> shape, so we filter the platform
# pages out before treating the URL as an owner/model slug.
_HUGGINGFACE_NON_MODEL_OWNERS = frozenset({
    "spaces", "datasets", "docs", "blog", "join", "login", "settings",
    "pricing", "tasks", "papers", "models", "search", "new",
    "api", "chat", "huggingchat",
})

# SPDX bucket classification. The lists are intentionally short — they
# cover what we actually see on arxiv-linked repos. Anything else falls
# through to "unknown" (visible in the report, not blocking).
_PERMISSIVE_SPDX = frozenset({
    "apache-2.0", "mit", "bsd-2-clause", "bsd-3-clause", "isc",
    "0bsd", "unlicense", "wtfpl", "cc0-1.0", "cc-by-4.0", "cc-by-3.0",
    "zlib", "boost-1.0", "bsl-1.0", "postgresql",
})
_COPYLEFT_SPDX = frozenset({
    "gpl-2.0", "gpl-3.0", "agpl-3.0", "lgpl-2.1", "lgpl-3.0",
    "mpl-2.0", "epl-2.0", "cc-by-sa-4.0", "cc-by-sa-3.0",
})
# CC-BY-NC and CC-BY-ND variants are the NC bucket — adoption-blocking
# for code/model use in commercial or relicensed downstream work.
_NC_SPDX_PREFIXES = ("cc-by-nc-", "cc-by-nd-")
_NC_SPDX_EXACT = frozenset({"cc-by-nc-4.0", "cc-by-nd-4.0"})


def _extract_github_urls(*texts: str) -> list[str]:
    """Return de-duped ``owner/repo`` slugs scraped from any input text.

    Looks for ``github.com/<owner>/<repo>`` substrings. Strips a trailing
    ``.git`` and any trailing punctuation/path. Filters known-non-repo
    owner paths (``github.com/orgs``, ``github.com/topics``, etc.).
    Order-preserving so the first-mentioned repo (typically the paper's
    canonical implementation) wins downstream.
    """
    seen: list[str] = []
    for text in texts:
        if not text:
            continue
        for owner, name in _GITHUB_URL_RE.findall(text):
            if owner.lower() in _GITHUB_NON_REPO_OWNERS:
                continue
            name = re.sub(r"\.git$", "", name)
            # Strip a trailing path/fragment/query if one snuck in.
            name = re.sub(r"[^A-Za-z0-9._-].*$", "", name)
            if not name:
                continue
            slug = f"{owner}/{name}"
            if slug not in seen:
                seen.append(slug)
    return seen


def _extract_huggingface_urls(*texts: str) -> list[str]:
    """Return de-duped ``owner/model`` slugs from any input text.

    Parallel to ``_extract_github_urls`` but for HuggingFace Hub model
    URLs. Filters platform-page paths (``huggingface.co/spaces``,
    ``huggingface.co/datasets``, etc.) that share the ``<word>/<word>``
    shape but aren't model identifiers.

    Note: HF Hub also hosts datasets and Spaces; this function targets
    *models* (the most common adoption surface for a paper's code).
    A future extension could add a separate dataset extractor when the
    license gate grows to cover dataset licensing too.
    """
    seen: list[str] = []
    for text in texts:
        if not text:
            continue
        for owner, name in _HUGGINGFACE_URL_RE.findall(text):
            if owner.lower() in _HUGGINGFACE_NON_MODEL_OWNERS:
                continue
            name = re.sub(r"[^A-Za-z0-9._-].*$", "", name)
            # Trailing sentence punctuation can land inside the regex
            # match (the dot/underscore/hyphen char class is permissive)
            # — strip it so a URL that ends a sentence still resolves.
            name = name.rstrip(".,;:!?-_")
            if not name:
                continue
            slug = f"{owner}/{name}"
            if slug not in seen:
                seen.append(slug)
    return seen


# Per-process cache for arxiv abstract-page scrapes. Arxiv abstract
# pages are essentially static within a run, so one fetch per id is
# enough. Key: arxiv_id (with or without version suffix); value: a tuple
# ``(github_slugs, hf_slugs)`` extracted from the page HTML.
_ARXIV_PAGE_CACHE: dict[str, tuple[list[str], list[str]]] = {}


def _fetch_arxiv_abstract_page_urls(arxiv_id: str) -> tuple[list[str], list[str]]:
    """Best-effort fallback for candidates where the Remyx envelope and
    paper-text scrape didn't surface code/model URLs.

    Most arxiv papers list the canonical implementation URL on the
    abstract page — either in the author-supplied abstract (a "Code:"
    line), in the "Other formats" / "Code, Data, Media" sidebar, or via
    paperswithcode integration. We pull the page HTML and run the same
    GitHub + HF extractors over it.

    Returns ``(github_slugs, hf_slugs)``; either or both may be empty.
    Never raises — license enrichment must stay best-effort.
    """
    arxiv_id = (arxiv_id or "").strip()
    if not arxiv_id:
        return [], []
    if arxiv_id in _ARXIV_PAGE_CACHE:
        return _ARXIV_PAGE_CACHE[arxiv_id]
    url = f"https://arxiv.org/abs/{arxiv_id}"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "feature-finder-orchestrator",
                "Accept": "text/html",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"  arxiv page fetch for {arxiv_id} failed: {e}")
        _ARXIV_PAGE_CACHE[arxiv_id] = ([], [])
        return [], []
    gh = _extract_github_urls(html)
    hf = _extract_huggingface_urls(html)
    _ARXIV_PAGE_CACHE[arxiv_id] = (gh, hf)
    return gh, hf


# arxiv.org/html/<id> is a newer rendered-HTML surface for papers that
# includes URLs mentioned in the paper body — which the abstract page
# almost never does. Every rendered page also carries footer refs to
# `github.com/arXiv/html_feedback` + `github.com/brucemiller/LaTeXML`;
# these are filtered out as boilerplate.
_ARXIV_HTML_CACHE: dict[str, list[str]] = {}
_ARXIV_HTML_NOISE = ("arxiv/html_feedback", "brucemiller/latexml")


def _fetch_arxiv_html_urls(arxiv_id: str) -> list[str]:
    """Fetch ``https://arxiv.org/html/<arxiv_id>`` and return github slugs
    from the paper body, filtering the standard arxiv footer refs.

    Used as a fallback source for license detection when the primary
    path (Remyx envelope + abstract-page scrape + GitHub `/license`)
    doesn't surface a usable SPDX. Never raises.
    """
    arxiv_id = (arxiv_id or "").strip()
    if not arxiv_id:
        return []
    if arxiv_id in _ARXIV_HTML_CACHE:
        return _ARXIV_HTML_CACHE[arxiv_id]
    try:
        req = urllib.request.Request(
            f"https://arxiv.org/html/{arxiv_id}",
            headers={
                "User-Agent": "feature-finder-orchestrator",
                "Accept": "text/html",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"  arxiv HTML fetch for {arxiv_id} failed: {e}")
        _ARXIV_HTML_CACHE[arxiv_id] = []
        return []
    slugs = [
        s for s in _extract_github_urls(html)
        if s.lower() not in _ARXIV_HTML_NOISE
    ]
    _ARXIV_HTML_CACHE[arxiv_id] = slugs
    return slugs


_ARXIV_PDF_CACHE: dict[str, list[str]] = {}


def _fetch_arxiv_pdf_text(arxiv_id: str, timeout_s: int = 20) -> str:
    """Fetch ``https://arxiv.org/pdf/<arxiv_id>`` and extract body text
    via ``pdftotext``. Returns ``""`` on any failure — network, missing
    binary, decode error, oversized PDF. Never raises.

    Papers that arxiv hasn't rendered to HTML (endpoint returns 404) are
    still available as PDF. The PDF body reliably contains github URLs
    the authors advertise, so this is the last-resort discovery source
    when HTML + project-page scraping both come up empty.
    """
    arxiv_id = (arxiv_id or "").strip()
    if not arxiv_id:
        return ""
    try:
        req = urllib.request.Request(
            f"https://arxiv.org/pdf/{arxiv_id}",
            headers={
                "User-Agent": "feature-finder-orchestrator",
                "Accept": "application/pdf",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            pdf_bytes = resp.read(20 * 1024 * 1024 + 1)
        if len(pdf_bytes) > 20 * 1024 * 1024:
            log.debug(f"  arxiv PDF for {arxiv_id} exceeds 20 MB cap; skipping")
            return ""
    except Exception as e:
        log.debug(f"  arxiv PDF fetch for {arxiv_id} failed: {e}")
        return ""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-", "-"],
            input=pdf_bytes,
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError:
        log.debug("  pdftotext binary unavailable; skipping PDF-text discovery")
        return ""
    except Exception as e:
        log.debug(f"  pdftotext for {arxiv_id} failed: {e}")
        return ""
    if result.returncode != 0:
        log.debug(f"  pdftotext for {arxiv_id} exit={result.returncode}")
        return ""
    return result.stdout.decode("utf-8", errors="replace")


def _fetch_arxiv_pdf_github_slugs(arxiv_id: str) -> list[str]:
    """Extract github slugs from arxiv PDF body text. Cached, filtered
    against the standard arxiv noise list. Never raises.
    """
    arxiv_id = (arxiv_id or "").strip()
    if not arxiv_id:
        return []
    if arxiv_id in _ARXIV_PDF_CACHE:
        return _ARXIV_PDF_CACHE[arxiv_id]
    text = _fetch_arxiv_pdf_text(arxiv_id)
    slugs = [
        s for s in _extract_github_urls(text)
        if s.lower() not in _ARXIV_HTML_NOISE
    ]
    _ARXIV_PDF_CACHE[arxiv_id] = slugs
    return slugs


def _title_words(title: str) -> list[str]:
    """Words >= 4 chars from the paper title, lowercased. Used as the
    substring corpus for repo-name overlap scoring."""
    return [w.lower() for w in re.findall(r"[A-Za-z]{4,}", title)]


def _score_slug_title_overlap(slug: str, title: str) -> int:
    """Count how many title words appear as substrings in the repo name
    (the last path segment of ``slug``, lowercased). Zero means no
    lexical evidence the repo is the paper's own — a signal to refuse
    attribution rather than pick a citation URL by default.
    """
    name = slug.split("/")[-1].lower()
    return sum(1 for w in _title_words(title) if w in name)


def _rank_slugs_by_title_overlap(slugs: list[str], title: str) -> list[str]:
    """Rank github slugs so the one whose repo name shares the most
    substrings with the paper title comes first. Handles compound repo
    names like ``EntityBindingFailures`` where token-splitting fails but
    substring matching against title words succeeds.
    """
    return sorted(slugs, key=lambda s: -_score_slug_title_overlap(s, title))


# Extract every ``http(s)://...`` URL from HTML (not just github). Used
# for the project-page one-hop step in retry: an arxiv HTML page might
# not cite the paper's github repo directly but might link to a project
# page (``moonmath.ai/hyperquant/``) whose HTML in turn cites the repo.
_ANY_URL_RE = re.compile(r'https?://[A-Za-z0-9./_\-~%?=&:#]+')


def _fetch_url_html(url: str, timeout_s: int = 10) -> str:
    """Best-effort HTTP GET returning HTML body or ``""``. Never raises."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "feature-finder-orchestrator",
                "Accept": "text/html",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"  URL fetch for {url!r} failed: {e}")
        return ""


def _extract_project_page_urls(html: str, title: str) -> list[str]:
    """Extract URLs from ``html`` that look like paper-project pages —
    URL path contains a title-word substring. Filters out github (already
    handled by the direct-arxiv-HTML path) and boilerplate hosts.
    """
    if not html:
        return []
    title_words = _title_words(title)
    if not title_words:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _ANY_URL_RE.finditer(html):
        url = match.group(0)
        # Strip trailing punctuation the regex might have grabbed
        url = url.rstrip(".,;:!)\"'")
        lower = url.lower()
        if any(host in lower for host in (
            "github.com/", "arxiv.org/", "huggingface.co/",
            "doi.org/", "openreview.net/",
        )):
            continue
        # Path (after the host) contains at least one title-word substring?
        try:
            path = url.split("//", 1)[1].split("/", 1)[1].lower()
        except IndexError:
            continue
        if not any(w in path for w in title_words):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= 3:  # cap the one-hop fanout
            break
    return out


_README_CACHE: dict[str, str] = {}


def _fetch_repo_readme(owner_repo: str) -> str:
    """Fetch the repo's README raw content via GitHub API.

    Uses ``GET /repos/{owner_repo}/readme`` which returns the primary
    README (README.md / README.rst / README anywhere in root). Returns
    the decoded content or ``""`` on any failure. Cached per-process.
    """
    if not owner_repo:
        return ""
    if owner_repo in _README_CACHE:
        return _README_CACHE[owner_repo]
    try:
        resp = gh_api("GET", f"/repos/{owner_repo}/readme")
        b64 = resp.get("content") or ""
        content = base64.b64decode(b64).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"  README fetch for {owner_repo!r} failed: {e}")
        content = ""
    _README_CACHE[owner_repo] = content
    return content


def _verify_repo_matches_paper(
    slug: str, paper_title: str, arxiv_id: str,
) -> bool:
    """Positive verification that a candidate github repo is the paper's
    own repo: fetch its README and check whether the paper's title (or a
    distinctive title word) or the arxiv id appears in it.

    Complements ``_score_slug_title_overlap``: repo-name matching is fast
    but shallow (misses cases where the paper's short-name doesn't appear
    in the repo name). README matching is fast enough to run per-candidate
    and catches "the repo name doesn't obviously match but the README
    clearly describes this paper" cases.

    Returns ``True`` when either the arxiv id appears in the README, or
    at least 2 distinct >=4-char title words appear. Never raises.
    """
    readme = _fetch_repo_readme(slug)
    if not readme:
        return False
    readme_l = readme.lower()
    if arxiv_id and arxiv_id.lower() in readme_l:
        return True
    title_words = _title_words(paper_title)
    if not title_words:
        return False
    hits = sum(1 for w in title_words if w in readme_l)
    return hits >= 2


_ARCHITECTURE_ADD_PATTERNS = (
    r"\bnew\s+\w*\s*(?:transformer|model|backbone|architecture)\b",
    r"\b(?:attention|linear-attention|gla)\s+processor\b",
    r"\bnew\s+\w*\s*(?:tuner|adapter)\b",
    r"\bmodel\s+class\s+with\s+no\s+weights\b",
    r"\bmodel/pipeline/scheduler\s+description\b",
    r"\barchitectural\s+sibling\b",
    r"\bregister\w*\s+.*(?:model|processor)\b",
)


def _is_architecture_add_shape(body: str) -> bool:
    """Return True when the Issue body reads as an architecture-add artifact.

    Architecture-add shape means the paper proposes a new model class,
    backbone, attention processor, or tuner that would require multi-file
    registration and — if intended as a runnable capability — a pretrained
    checkpoint. These are the Issues where "does a checkpoint exist?" is a
    load-bearing question the reader wants answered.
    """
    if not body:
        return False
    for pat in _ARCHITECTURE_ADD_PATTERNS:
        if re.search(pat, body, re.IGNORECASE):
            return True
    return False


_HF_PAPER_CACHE: dict[str, dict] = {}


def _fetch_hf_paper_linkage(arxiv_id: str, timeout_s: int = 5) -> dict | None:
    """Fetch a paper's canonical HF Hub linkage envelope, or ``None``.

    Uses ``GET /api/papers/{arxiv_id}`` — HF Hub's paper index endpoint,
    which returns ``linkedModels`` / ``linkedDatasets`` / ``linkedSpaces``
    populated by HF's own crawler + model-card frontmatter (`arxiv:` tag)
    + manual paper-page curation. This is the authoritative "does this
    paper have a public checkpoint" answer — no author heuristics, no
    substring matching.

    Returns:
    - ``{"title": str, "linked_models": [...], "linked_datasets": [...],
       "linked_spaces": [...]}`` when the paper is indexed on HF
      (regardless of whether it has any linked artifacts).
    - ``None`` when the paper is not indexed on HF (title comes back
      null) or on any network failure. Caller should treat ``None`` as
      "no signal" — don't confuse it with "definitely no checkpoint".

    Never raises. Cached per arxiv_id within a run.
    """
    if not arxiv_id:
        return None
    key = arxiv_id.strip().lower().replace("v", " ").split()[0]
    if key in _HF_PAPER_CACHE:
        return _HF_PAPER_CACHE[key]

    url = f"https://huggingface.co/api/papers/{key}"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "outrider-hf-paper-linkage",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.debug(f"  HF paper linkage for {key!r} failed: {e}")
        _HF_PAPER_CACHE[key] = None
        return None

    if not isinstance(data, dict) or not data.get("title"):
        # HF returned an envelope but this arxiv id isn't indexed on
        # huggingface.co/papers — no signal, not "no checkpoint".
        _HF_PAPER_CACHE[key] = None
        return None

    parsed = {
        "title": data.get("title", ""),
        "linked_models": data.get("linkedModels") or [],
        "linked_datasets": data.get("linkedDatasets") or [],
        "linked_spaces": data.get("linkedSpaces") or [],
    }
    _HF_PAPER_CACHE[key] = parsed
    return parsed


# Well-known ML-library orgs whose repos are strong sibling-implementation signals.
# Curated conservatively — presence in the top-10 search hit is a stronger "this
# paper is worth engaging with" signal when the repo owner is one of these.
_SIBLING_LIB_ORGS = frozenset({
    "huggingface", "EleutherAI", "allenai", "microsoft", "google-research",
    "google", "openai", "meta-research", "facebookresearch", "deepmind",
    "pytorch", "tensorflow", "nvidia", "pytorch-labs", "ray-project",
    "unslothai", "axolotl-ai-cloud", "outlines-dev", "guidance-ai",
    "lightning-ai", "vllm-project", "sglang-project", "linkedin",
    "salesforce", "IBM", "MIT-IBM-Watson-AI-Lab", "cornell-tech",
})

_SIBLING_IMPL_CACHE: dict[str, list[dict] | None] = {}


def _fetch_sibling_implementations(
    arxiv_id: str, paper_title: str, target_repo: str = "",
    timeout_s: int = 5,
) -> list[dict]:
    """Search GitHub for adjacent-library implementations of this paper.

    Uses GitHub's repository search API with the paper title + arxiv id;
    filters to top hits whose owner is a well-known ML-library org
    (:data:`_SIBLING_LIB_ORGS`). Excludes the target repo itself so a
    matching PR body on the target doesn't self-reference.

    Returns a list of ``{"full_name": str, "stars": int, "description":
    str, "why_relevant": str}`` dicts, top ~5 sorted by stars. Empty list
    when no signal or on any network failure — callers should treat
    empty as "no adjacent implementations found," not as an error.

    Cached per (arxiv_id, target_repo) within a run.
    """
    if not arxiv_id or not paper_title:
        return []
    cache_key = f"{arxiv_id}|{target_repo}"
    if cache_key in _SIBLING_IMPL_CACHE:
        return _SIBLING_IMPL_CACHE[cache_key] or []

    # Strip version suffix from arxiv id — canonical index is versionless
    arxiv_bare = arxiv_id.strip().lower().replace("v", " ").split()[0]
    # Search on the paper title + arxiv id — either match is a signal.
    query = f'{paper_title} arxiv:{arxiv_bare}'
    url = (
        "https://api.github.com/search/repositories"
        f"?q={urllib.parse.quote(query)}&sort=stars&order=desc&per_page=20"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "outrider-sibling-impl-search",
    }
    token = os.environ.get("INPUT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.debug(f"  sibling-impl search for {arxiv_bare!r} failed: {e}")
        _SIBLING_IMPL_CACHE[cache_key] = None
        return []

    items = data.get("items", []) if isinstance(data, dict) else []
    hits: list[dict] = []
    for item in items:
        full_name = item.get("full_name", "")
        if full_name == target_repo:
            continue  # exclude the target itself
        owner = full_name.split("/", 1)[0] if "/" in full_name else ""
        if owner not in _SIBLING_LIB_ORGS:
            continue
        stars = item.get("stargazers_count", 0)
        desc = (item.get("description") or "").strip()[:200]
        hits.append({
            "full_name": full_name,
            "stars": stars,
            "description": desc,
            "why_relevant": f"{owner} is a well-known ML-library org; "
                            f"repo matched paper title + arxiv id search",
        })
        if len(hits) >= 5:
            break

    _SIBLING_IMPL_CACHE[cache_key] = hits
    return hits


_SIBLING_CLAIM_RE = re.compile(
    r"(?:next\s+to|alongside|sibling(?:s)?\s+(?:of|to)|mirror(?:s|ing)?)\s+"
    r"`([A-Za-z_][A-Za-z0-9_.]*)`",
    re.IGNORECASE,
)


def _extract_sibling_claims(body: str) -> list[str]:
    """Extract identifiers cited as siblings in an Issue/PR body.

    Matches phrases like ``sits next to `X```, ``alongside `X```,
    ``sibling of `X```, ``mirrors `X```. Returns the unique list of
    identifiers preserving first-appearance order. Empty body → [].
    """
    if not body:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _SIBLING_CLAIM_RE.finditer(body):
        ident = m.group(1)
        if ident not in seen:
            seen.add(ident)
            out.append(ident)
    return out


def _query_ccc_class_defs(workdir: "Path", timeout_s: int = 20) -> list[dict]:
    """Return all Python class definitions in ``workdir`` via ``ccc grep``.

    Uses cocoindex-code's structural grep (``ccc grep 'class \\NAME:'``) —
    AST-based, not text-regex. Requires ``ccc`` on ``PATH`` (installed by
    the ENVIRONMENTS.md workflow step) but not a built index or running
    daemon, so it works reliably even on the first run.

    Returns ``[]`` when cocoindex isn't attached, when the workdir is
    invalid, or when the subprocess fails for any reason. Never raises.
    """
    if not workdir or not workdir.exists():
        return []
    if not shutil.which("ccc"):
        return []
    try:
        result = subprocess.run(
            ["ccc", "grep", r"class \NAME:", ".",
             "--lang", "python", "--no-color"],
            cwd=str(workdir), capture_output=True, text=True, timeout=timeout_s,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log.debug(f"  ccc grep failed: {e}")
        return []
    return _parse_ccc_grep_output(result.stdout)


def _parse_ccc_grep_output(output: str) -> list[dict]:
    """Parse ``ccc grep --no-color`` output into ``{kind, name, file, line}``.

    Format from ``cocoindex_code.grep.render_file``:
    - file path on its own line (no gutter)
    - match lines: ``  <n>| <source line>`` (line-number gutter)
    - ``---`` separator between multi-match files (skipped)
    """
    lines = output.split("\n")
    results: list[dict] = []
    current_file: str | None = None
    gutter_line = re.compile(r"^\s*(\d+)\|\s*(.*)$")
    def_shape = re.compile(r"^\s*(class|def)\s+(\w+)")
    for raw in lines:
        if not raw.strip() or raw.strip() == "---":
            continue
        gm = gutter_line.match(raw)
        if gm and current_file:
            body = gm.group(2)
            dm = def_shape.match(body)
            if dm:
                results.append({
                    "kind": dm.group(1),
                    "name": dm.group(2),
                    "file": current_file,
                    "line": int(gm.group(1)),
                })
            continue
        current_file = raw.strip()
    return results


def _rank_by_name_similarity(
    identifier: str, candidates: list[dict],
    min_ratio: float = 0.5, top_k: int = 3,
) -> list[dict]:
    """Rank candidate class definitions by name similarity to ``identifier``.

    Uses ``difflib.SequenceMatcher.ratio`` — stdlib, deterministic, no
    embedding-model dep. Drops self-matches and anything below
    ``min_ratio``. Returns at most ``top_k``, each annotated with the
    computed ``similarity``.
    """
    from difflib import SequenceMatcher
    scored: list[tuple[float, dict]] = []
    for c in candidates:
        if c.get("name") == identifier:
            continue
        ratio = SequenceMatcher(None, c["name"], identifier).ratio()
        if ratio >= min_ratio:
            scored.append((ratio, c))
    scored.sort(key=lambda p: -p[0])
    return [dict(c, similarity=round(r, 3)) for r, c in scored[:top_k]]


def _enumerate_definitions(
    identifier: str, workdir: "Path", max_results: int = 3,
) -> list[dict]:
    """Return the top-``max_results`` class definitions in ``workdir``
    structurally similar to ``identifier``.

    Silent no-op when cocoindex-code isn't attached (``ccc`` not on PATH),
    or when the workdir has no class definitions to enumerate. Kept as
    a single-argument entry point so callers don't need to know about
    the class-def enumeration step.
    """
    if not identifier or not workdir or not workdir.exists():
        return []
    candidates = _query_ccc_class_defs(workdir)
    if not candidates:
        return []
    return _rank_by_name_similarity(identifier, candidates, top_k=max_results)


def _format_convention_precedents_section(
    claims_to_precedents: dict[str, list[dict]],
) -> str:
    """Render the convention-precedents section for an Issue body.

    Only emits a section when at least one claim has non-empty
    precedents — silence beats a spurious "no matches found" block when
    cocoindex wasn't attached or the workdir had no class definitions.
    """
    if not any(claims_to_precedents.values()):
        return ""
    lines = [
        "",
        "**Convention precedents (via cocoindex AST search)**",
        "",
    ]
    for claim, precedents in claims_to_precedents.items():
        if not precedents:
            continue
        lines.append(
            f"_Structurally similar to `{claim}` — top {len(precedents)}:_"
        )
        for p in precedents:
            sim = p.get("similarity")
            sim_str = f" (similarity: {sim:.2f})" if sim is not None else ""
            lines.append(
                f"- `{p['name']}` ({p['kind']}){sim_str} — "
                f"`{p['file']}:{p['line']}`"
            )
        lines.append("")
    return "\n".join(lines)


def _enrich_body_with_convention_precedents(
    body: str, workdir: "Path",
) -> str:
    """Append a convention-precedents block to ``body`` when any sibling
    claims resolve via cocoindex AST enumeration.

    Converts agent-inferred sibling claims ("sits alongside
    `X`") into reader-verifiable, similarity-ranked precedents backed by
    ``ccc grep 'class \\NAME:'``. Silent no-op when cocoindex isn't
    attached, no sibling claims are detected, or no precedents pass the
    similarity threshold.
    """
    if not body:
        return body
    claims = _extract_sibling_claims(body)
    if not claims:
        return body
    resolutions: dict[str, list[dict]] = {}
    for claim in claims:
        resolutions[claim] = _enumerate_definitions(claim, workdir)
    section = _format_convention_precedents_section(resolutions)
    if not section:
        return body
    return body + "\n" + section


def _format_hf_checkpoint_section(linkage: dict | None) -> str:
    """Render an Issue-body block reporting HF Hub checkpoint availability.

    Called only for architecture-add-shaped Issues. Consumes the output
    of ``_fetch_hf_paper_linkage``:

    - ``None`` → paper is not indexed on huggingface.co/papers, so we
      have no authoritative signal. Return an empty string (no section).
      Better to omit than to hallucinate a false "not found" claim.
    - ``linkage`` with populated ``linked_models`` → list them (green).
    - ``linkage`` with empty ``linked_models`` → paper IS indexed and
      genuinely has no linked checkpoint (yellow).
    """
    if linkage is None:
        return ""
    models = linkage.get("linked_models", [])
    if models:
        items = "\n".join(
            f"- [`{m.get('id', '')}`](https://huggingface.co/{m.get('id', '')})"
            for m in models if isinstance(m, dict) and m.get("id")
        )
        return (
            "\n**Hub checkpoint availability** 🟢 "
            f"{len(models)} public checkpoint(s) linked to this paper on "
            "Hugging Face Hub (checked via `/api/papers/{arxiv_id}` at run "
            f"time):\n\n{items}\n"
        )
    return (
        "\n**Hub checkpoint availability** 🟡 "
        "This paper is indexed on huggingface.co/papers but has no linked "
        "checkpoint. If a checkpoint is later published and linked to the "
        "paper page, reopen this Issue to have Outrider revisit.\n"
    )


def _gather_arxiv_html_candidate_slugs(arxiv_id: str, paper_title: str) -> list[str]:
    """Collect github repo slugs from arxiv HTML + one-hop project pages
    + PDF-text fallback.

    Papers sometimes cite prior work / dependencies as github URLs in
    their arxiv HTML but *not* their own repo (which is instead linked
    via a project-page URL). And some papers never get rendered to HTML
    at all — the ``arxiv.org/html/<id>`` endpoint 404s — but their PDF
    still names the code repo. This gathers candidates from three
    sources:

      1. github URLs directly in the arxiv HTML (same as v1.7.1)
      2. project-page URLs from the arxiv HTML — a URL whose path
         contains title-word substrings, e.g. ``moonmath.ai/hyperquant/``
         for a paper titled "HyperQuant..." — fetched one hop, then
         github URLs extracted from each
      3. github URLs extracted from the PDF body via ``pdftotext`` —
         only invoked when 1 and 2 both came up empty (the arxiv-HTML-
         404 case), so the common path never spends the PDF fetch.

    Returns a deduplicated list; downstream ranks and verifies each.
    Never raises.
    """
    arxiv_id = (arxiv_id or "").strip()
    if not arxiv_id:
        return []
    # 1. Direct github URLs from arxiv HTML (existing behavior)
    direct = _fetch_arxiv_html_urls(arxiv_id)
    # 2. One-hop project pages — fetch the arxiv HTML raw so we can
    # scan for project-page URLs (footer + boilerplate already stripped
    # from `direct`'s output, so we re-fetch the HTML to get the full
    # URL space).
    arxiv_html = _fetch_url_html(f"https://arxiv.org/html/{arxiv_id}")
    from_project_pages: list[str] = []
    for project_url in _extract_project_page_urls(arxiv_html, paper_title or ""):
        page_html = _fetch_url_html(project_url)
        if not page_html:
            continue
        gh_from_page = [
            s for s in _extract_github_urls(page_html)
            if s.lower() not in _ARXIV_HTML_NOISE
        ]
        from_project_pages.extend(gh_from_page)
    # 3. PDF-text fallback — only spent when the HTML surface produced
    # nothing at all. Handles papers arxiv never rendered to HTML.
    from_pdf: list[str] = []
    if not direct and not from_project_pages:
        from_pdf = _fetch_arxiv_pdf_github_slugs(arxiv_id)
    # Dedupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for s in direct + from_project_pages + from_pdf:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _retry_license_via_arxiv_html(candidate: "Recommendation", target_class: str) -> bool:
    """When the primary license path left ``license_class`` unfavorable
    (``no-code-link`` / ``unknown`` / ``missing``), try discovering fresh
    github URLs from arxiv's HTML surface + linked project pages, and
    re-classify against those. Returns ``True`` when the retry produced
    a bucketable result and updated the candidate in place, ``False``
    otherwise. Never raises.

    Attribution gates, applied in order to avoid mislabeling a cited-
    prior-work repo as the paper's own:
      - README verification (primary): fetch each candidate's README and
        require that the arxiv id OR at least 2 distinct title words
        appear before accepting. Dispositive evidence — a repo whose
        README references the paper is the paper's own project.
      - Title-overlap threshold (tie-breaker): among README-verified
        candidates, prefer those whose repo name shares a >=4-char
        substring with the paper title. Only used to order multiple
        surviving candidates, never as a filter.

    Ordering rationale: README-verify runs first because it's stronger
    evidence than a name-overlap heuristic. The prior ordering
    (title-overlap → README-verify) filtered out acronym-named repos
    whose acronym isn't literally in the paper title — e.g. an arxiv
    paper "Visually Grounded Self-Reflection ..." with a "VRRL"
    repo would drop at zero title-overlap even though its README
    explicitly cites the arxiv id.
    """
    unfavorable = ("no-code-link", "unknown", "missing")
    if candidate.license_class not in unfavorable:
        return False
    paper_title = candidate.paper_title or ""
    slugs = _gather_arxiv_html_candidate_slugs(candidate.arxiv_id, paper_title)
    if not slugs:
        log.info(
            "  ↻ license retry on %s… (%s): no github slugs from arxiv "
            "HTML / project-pages / PDF fallback",
            paper_title[:50], candidate.arxiv_id,
        )
        return False
    log.info(
        "  ↻ license retry on %s… (%s): %d candidate slug(s) → %s",
        paper_title[:50], candidate.arxiv_id, len(slugs), slugs[:5],
    )
    # README verification first (dispositive evidence): a repo whose
    # README references the arxiv id or ≥2 title words is the paper's
    # own project, regardless of whether the repo name looks acronym-y.
    verified = [
        s for s in slugs
        if _verify_repo_matches_paper(s, paper_title, candidate.arxiv_id)
    ]
    if not verified:
        log.info(
            "  ↻ license retry on %s…: 0 of %d slug(s) README-verified",
            paper_title[:50], len(slugs),
        )
        return False
    # Title-overlap becomes a tie-breaker across README-verified survivors:
    # prefer the one whose repo name shares more substring with the title.
    verified.sort(key=lambda s: -_score_slug_title_overlap(s, paper_title))
    # Pass 1: prefer a verified repo with a real (bucketable) license.
    for slug in verified:
        fresh_spdx = _fetch_repo_license(slug)
        if not fresh_spdx or fresh_spdx.upper() == "NOASSERTION":
            continue
        fresh_class = _classify_license(fresh_spdx)
        if fresh_class in ("unknown", "missing"):
            continue
        candidate.paper_license = fresh_spdx
        if not candidate.paper_github_url:
            candidate.paper_github_url = f"https://github.com/{slug}"
        candidate.license_source = "arxiv_html_retry"
        candidate.license_class = fresh_class
        candidate.license_compat = _license_compat_score(fresh_class, target_class)
        log.info(
            "  ↻ license retry via arxiv HTML on %s…: %s (%s) via %s",
            paper_title[:50], fresh_spdx, fresh_class, slug,
        )
        return True
    # Pass 2: no bucketable-license repo across verified candidates — but
    # we DID find the paper's code repo (README-verified). Record the
    # discovery so downstream (LEAD content, coordination checks, preflight
    # reasoning, ranker feedback) knows the code exists, even when the
    # license gate then blocks the PR route. Classifying as ``missing``
    # rather than ``no-code-link`` correctly signals "code found, unlicensed"
    # to the compat scorer (0.0 for any target policy), which is a stricter
    # + more accurate state than ``no-code-link`` (0.3) — unlicensed code
    # is real legal-status information, not just an absence of evidence.
    top_slug = verified[0]
    if not candidate.paper_github_url:
        candidate.paper_github_url = f"https://github.com/{top_slug}"
    candidate.license_source = "arxiv_html_retry"
    candidate.license_class = "missing"
    candidate.license_compat = _license_compat_score("missing", target_class)
    candidate.paper_license = ""
    log.info(
        "  ↻ license retry via arxiv HTML on %s…: code repo found "
        "but no bucketable license via %s",
        paper_title[:50], top_slug,
    )
    return True


# Cached extracted text (title + abstract) per arxiv id. Populated by
# ``_fetch_arxiv_abstract_text`` for paper-anchored fidelity audits (the
# Phase A degraded mode used when a paper has no public reference impl).
_ARXIV_ABSTRACT_TEXT_CACHE: dict[str, str] = {}

# Strip HTML tags + collapse whitespace; the arxiv abstract block has
# light markup (italics, math, line breaks) but no nested structure that
# we'd lose to a flat strip.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_WHITESPACE_RE = re.compile(r"\s+")

# Match the arxiv abstract page's title + abstract blocks. Both are
# stable surfaces — arxiv has rendered titles as ``<h1 class="title
# mathjax">`` and abstracts as ``<blockquote class="abstract mathjax">``
# for ~15 years. Greedy-stop on the closing tag.
_ARXIV_TITLE_RE = re.compile(
    r'<h1[^>]*class="[^"]*\btitle\b[^"]*"[^>]*>(.*?)</h1>',
    re.IGNORECASE | re.DOTALL,
)
_ARXIV_ABSTRACT_BLOCK_RE = re.compile(
    r'<blockquote[^>]*class="[^"]*\babstract\b[^"]*"[^>]*>(.*?)</blockquote>',
    re.IGNORECASE | re.DOTALL,
)


def _fetch_arxiv_abstract_text(arxiv_id: str) -> str:
    """Return the paper's title + abstract as plaintext.

    Used by the paper-anchored Phase A audit (Phase A's degraded mode
    when a paper has no public reference impl). The abstract is the
    densest method-summary surface we can fetch deterministically
    without parsing the PDF; for the audit prompt's purposes, "the paper
    claims X — does the diff implement X" can be answered from the
    abstract for most papers, and the audit honestly reports its
    precision floor via the ``Audit anchor`` line in the Coverage
    matrix.

    Returns ``""`` for any failure (fetch error, no abstract block
    found, empty arxiv id) — callers treat that as "no paper anchor
    available" and fall back to the skip path. Never raises.
    """
    arxiv_id = (arxiv_id or "").strip()
    if not arxiv_id:
        return ""
    if arxiv_id in _ARXIV_ABSTRACT_TEXT_CACHE:
        return _ARXIV_ABSTRACT_TEXT_CACHE[arxiv_id]
    url = f"https://arxiv.org/abs/{arxiv_id}"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "feature-finder-orchestrator",
                "Accept": "text/html",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"  arxiv abstract-text fetch for {arxiv_id} failed: {e}")
        _ARXIV_ABSTRACT_TEXT_CACHE[arxiv_id] = ""
        return ""

    parts: list[str] = []
    m_title = _ARXIV_TITLE_RE.search(html)
    if m_title:
        title = _HTML_WHITESPACE_RE.sub(
            " ", _HTML_TAG_RE.sub("", m_title.group(1))
        ).strip()
        if title.lower().startswith("title:"):
            title = title.split(":", 1)[1].strip()
        if title:
            parts.append(f"Title: {title}")
    m_abs = _ARXIV_ABSTRACT_BLOCK_RE.search(html)
    if m_abs:
        abstract = _HTML_WHITESPACE_RE.sub(
            " ", _HTML_TAG_RE.sub("", m_abs.group(1))
        ).strip()
        if abstract.lower().startswith("abstract:"):
            abstract = abstract.split(":", 1)[1].strip()
        if abstract:
            parts.append(f"Abstract: {abstract}")
    text = "\n\n".join(parts)
    _ARXIV_ABSTRACT_TEXT_CACHE[arxiv_id] = text
    return text


def _classify_license(spdx: str) -> str:
    """Map an SPDX-ish license id onto an adoption bucket.

    Returns one of ``"permissive"``, ``"copyleft"``, ``"nc"``,
    ``"missing"`` (no LICENSE found / empty string), or ``"unknown"``
    (we got *something* but couldn't bucket it — e.g. an unfamiliar SPDX
    or a custom license name). ``"missing"`` is louder than ``"unknown"``
    because no LICENSE means no legal permission to redistribute or
    modify at all — that's the loudest red flag we can surface.
    """
    lo = (spdx or "").lower().strip()
    if not lo:
        return "missing"
    if lo in _PERMISSIVE_SPDX:
        return "permissive"
    if lo in _COPYLEFT_SPDX:
        return "copyleft"
    if lo in _NC_SPDX_EXACT or any(lo.startswith(p) for p in _NC_SPDX_PREFIXES):
        return "nc"
    return "unknown"


def _license_compat_score(paper_class: str, target_class: str) -> float:
    """Soft compatibility score for the paper-vs-target license pairing.

    Returns a float in ``[0, 1]`` suitable for multiplicative ranking
    (``1.0`` = adopt freely, ``0.0`` = effectively blocked). The rubric
    is intentionally conservative against the target repo: permissive
    targets (the common case for production code) absorb permissive
    freely and get a yellow flag on copyleft / a red flag on NC. A
    copyleft target absorbs both permissive and copyleft freely. Per-
    repo overrides for the weighting are a future extension.
    """
    if paper_class == "permissive":
        return 1.0
    if paper_class == "missing":
        return 0.0
    if paper_class == "nc":
        return 0.1
    if paper_class == "copyleft":
        # Copyleft into copyleft is fine; copyleft into a permissive
        # target forces a re-license discussion the maintainer should
        # see up front.
        return 0.7 if target_class == "copyleft" else 0.5
    if paper_class == "no-code-link":
        # We couldn't find a code URL to inspect. That's a yellow flag,
        # not a red one — distinct from "missing" (which means we *did*
        # fetch a LICENSE endpoint and got nothing parseable). Score
        # below "unknown" since the maintainer has less information,
        # but above "missing" since there's no positive assertion of
        # "no permission granted."
        return 0.3
    return 0.5  # "unknown" — visible in the report, not silently filtered


# Substring fingerprints for content-sniffing LICENSE files when GitHub's
# classifier punts to NOASSERTION. Ordered by specificity (longer / more
# specific keys come first so e.g. NC-SA isn't shadowed by plain NC). The
# CC variants are the headline case: GitHub's classifier returns
# NOASSERTION for every Creative Commons license that isn't an exact
# match against its pattern set, which means CC-BY-NC / CC-BY-NC-SA /
# CC-BY-ND repos silently get classified as "missing" — the inverse of
# what the gate is meant to surface.
_LICENSE_CONTENT_FINGERPRINTS: tuple[tuple[str, str], ...] = (
    # CC-BY-NC-SA variants (NC + ShareAlike). Check before plain NC.
    ("Attribution-NonCommercial-ShareAlike 4.0", "CC-BY-NC-SA-4.0"),
    ("Attribution-NonCommercial-ShareAlike 3.0", "CC-BY-NC-SA-3.0"),
    # CC-BY-NC-ND (NC + NoDerivatives). Check before plain NC and ND.
    ("Attribution-NonCommercial-NoDerivatives 4.0", "CC-BY-NC-ND-4.0"),
    # CC-BY-NC alone.
    ("Attribution-NonCommercial 4.0", "CC-BY-NC-4.0"),
    ("Attribution-NonCommercial 3.0", "CC-BY-NC-3.0"),
    # CC-BY-ND alone.
    ("Attribution-NoDerivatives 4.0", "CC-BY-ND-4.0"),
    # CC-BY-SA (copyleft).
    ("Attribution-ShareAlike 4.0", "CC-BY-SA-4.0"),
    ("Attribution-ShareAlike 3.0", "CC-BY-SA-3.0"),
    # CC-BY alone (permissive).
    ("Attribution 4.0 International", "CC-BY-4.0"),
    # Standard FOSS licenses GitHub usually catches, but listed here for
    # the edge cases (custom header, multi-license LICENSE files where
    # GitHub punts but the body still includes the canonical text).
    ("Apache License", "Apache-2.0"),
    ("GNU AFFERO GENERAL PUBLIC LICENSE", "AGPL-3.0"),
    ("GNU LESSER GENERAL PUBLIC LICENSE", "LGPL-3.0"),
    ("GNU GENERAL PUBLIC LICENSE", "GPL-3.0"),
    ("Mozilla Public License", "MPL-2.0"),
    ("Permission is hereby granted, free of charge", "MIT"),
    ("Redistribution and use in source and binary forms", "BSD-3-Clause"),
)


def _sniff_license_from_content(b64_content: str) -> str:
    """Best-effort SPDX classification from raw LICENSE file content.

    Decodes ``b64_content`` (GitHub's `/license` endpoint returns the
    LICENSE file as base64) and looks for distinctive substrings within
    the first 2KB. Returns the matched SPDX id, or ``""`` if nothing
    matched. The 2KB window covers every license preamble we care about
    and bounds CPU on multi-MB LICENSE files (yes, those exist).

    Order matters: NC-SA / NC-ND / NC are checked before SA / ND so
    Creative Commons composites aren't mis-classified as their less-
    restrictive cousins.
    """
    if not b64_content:
        return ""
    try:
        decoded = base64.b64decode(b64_content).decode(
            "utf-8", errors="replace"
        )
    except Exception:
        return ""
    head = decoded[:2048]
    for needle, spdx in _LICENSE_CONTENT_FINGERPRINTS:
        if needle in head:
            return spdx
    return ""


# Per-run cache: avoid re-hitting GitHub for the same repo across a
# refresh + re-poll cycle. Keys are ``"owner/repo"``.
_LICENSE_CACHE: dict[str, str] = {}

# Per-run cache for HF model-card license lookups. Keys are
# ``"owner/model"``; values are SPDX-ish strings (or "" on miss).
_HF_LICENSE_CACHE: dict[str, str] = {}


def _fetch_hf_license(owner_model: str) -> str:
    """Return the SPDX-ish license id for an HF Hub model, or ``""``.

    Calls ``GET https://huggingface.co/api/models/{owner}/{model}`` —
    the model-card metadata endpoint. HF returns a JSON envelope whose
    ``cardData.license`` field carries the license declared in the
    model card's YAML frontmatter. This is the **authoritative** source
    for *weight* licensing: it describes what a customer actually loads
    with ``AutoModel.from_pretrained(...)``, which is what the gate
    cares about.

    The HF Hub API is unauthenticated for public models — no token
    required. Returns ``""`` on any failure (404, network flake, missing
    license field) so the caller can degrade silently to the GitHub
    license result. Never raises.

    SPDX value normalization: HF allows free-text license strings as
    well as SPDX ids. We surface the raw value here and let
    ``_classify_license`` bucket it; the existing CC-prefix matchers
    cover the common free-text variants (``cc-by-nc-4.0`` and friends).
    """
    if not owner_model:
        return ""
    if owner_model in _HF_LICENSE_CACHE:
        return _HF_LICENSE_CACHE[owner_model]
    url = f"https://huggingface.co/api/models/{owner_model}"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "feature-finder-orchestrator",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.debug(f"  HF license fetch for {owner_model!r} failed: {e}")
        _HF_LICENSE_CACHE[owner_model] = ""
        return ""
    card = data.get("cardData") or {}
    raw = card.get("license")
    # HF allows the license field to be either a string or a list
    # (multi-license declarations). Normalize to a single SPDX string.
    if isinstance(raw, list):
        spdx = (str(raw[0]).strip() if raw else "")
    elif isinstance(raw, str):
        spdx = raw.strip()
    else:
        spdx = ""
    _HF_LICENSE_CACHE[owner_model] = spdx
    return spdx


def _fetch_repo_license(owner_repo: str) -> str:
    """Return the SPDX-ish license id for ``owner_repo``, or ``""``.

    Calls ``GET /repos/{owner}/{repo}/license``. When GitHub finds a
    LICENSE file but its classifier returns ``NOASSERTION`` — which
    happens for every Creative Commons license (CC-BY-NC*, CC-BY-ND*,
    CC-BY-SA, CC-BY) and a long tail of custom academic / research
    licenses — fall back to content-sniffing the file body for a
    distinctive substring before giving up. If neither GitHub's
    classifier nor the sniffer matches, return ``"NOASSERTION"`` so the
    upstream classifier buckets the result as ``"unknown"`` (yellow
    flag) rather than ``"missing"`` (red flag, reserved for "no LICENSE
    file at all").

    Returns ``""`` only on real fetch failure (404, auth error, rate
    limit, network flake). Never raises — license lookup must not block
    the pipeline.
    """
    if not owner_repo:
        return ""
    if owner_repo in _LICENSE_CACHE:
        return _LICENSE_CACHE[owner_repo]
    try:
        resp = gh_api("GET", f"/repos/{owner_repo}/license")
        spdx = ((resp.get("license") or {}).get("spdx_id") or "").strip()
        if spdx.lower() == "noassertion":
            sniffed = _sniff_license_from_content(resp.get("content") or "")
            spdx = sniffed if sniffed else "NOASSERTION"
    except Exception as e:
        log.debug(f"  license fetch for {owner_repo!r} failed: {e}")
        spdx = ""
    _LICENSE_CACHE[owner_repo] = spdx
    return spdx


def _fetch_interest_context(interest_id: str) -> tuple[str, str, str]:
    """Fetch the interest's name + rich-text focus body + experiment-history
    bullets once per run.

    Returns (interest_name, interest_context, experiment_history). The
    context body is the rich text the customer wrote on engine.remyx.ai
    about their research focus / goals. The experiment_history is the
    LLM-ready bullet summary of the team's shipping trajectory — empty
    string when the interest has no linked
    ExperimentHistory or when the engine hasn't deployed the field yet.

    Best-effort: on any failure we return empty strings and fall back to
    the reasoning-only brief.
    """
    try:
        interest = _remyx_get(f"/api/v1.0/research-interests/{interest_id}")
        return (
            (interest.get("name") or ""),
            (interest.get("context") or "").strip(),
            (interest.get("experiment_history") or "").strip(),
        )
    except Exception as e:
        log.warning(f"    (interest context fetch failed: {e}; "
                    f"continuing with reasoning-only brief)")
        return "", "", ""


def _paper_to_recommendation(
    paper: dict, fallback_interest_name: str, interest_context: str,
    experiment_history: str,
) -> Recommendation:
    """Map one /papers/recommended envelope entry to a Recommendation."""
    relevance = float(paper.get("relevance_score") or 0.0)
    resource = paper.get("resource") or {}
    arxiv_id = paper.get("resource_id") or resource.get("arxiv_id") or ""
    abstract = (resource.get("abstract") or resource.get("summary") or "").strip()
    reasoning = (paper.get("reasoning") or "").strip()
    suggested = (paper.get("suggested_experiment") or "").strip()
    # Best-effort code + model URL extraction. Check known resource
    # keys first (cheapest — structured data when present), then fall
    # back to scraping the paper text. First hit wins for each kind.
    paper_github_url = ""
    for key in ("github_url", "code_url", "repo_url", "code",
                "paperswithcode_url"):
        v = (resource.get(key) or "").strip()
        if v and "github.com/" in v:
            paper_github_url = v
            break
    if not paper_github_url:
        slugs = _extract_github_urls(abstract, reasoning, suggested)
        if slugs:
            paper_github_url = f"https://github.com/{slugs[0]}"
    paper_huggingface_url = ""
    for key in ("hf_url", "huggingface_url", "model_card_url",
                "huggingface_model_url"):
        v = (resource.get(key) or "").strip()
        if v and "huggingface.co/" in v:
            paper_huggingface_url = v
            break
    if not paper_huggingface_url:
        hf_slugs = _extract_huggingface_urls(abstract, reasoning, suggested)
        if hf_slugs:
            paper_huggingface_url = f"https://huggingface.co/{hf_slugs[0]}"
    return Recommendation(
        paper_title=paper.get("title") or "(untitled)",
        arxiv_id=arxiv_id,
        tier=_relevance_to_tier(relevance),
        z_score=0.0,                       # legacy field, unused
        spec_md="",                        # legacy; rendered from fields below
        paper_abstract=abstract,
        domain_summary="",
        raw_paper_md="",
        relevance_score=relevance,
        reasoning=reasoning,
        suggested_experiment=suggested,
        recommendation_id=paper.get("recommendation_id") or "",
        interest_name=paper.get("interest_name") or fallback_interest_name,
        interest_context=interest_context,
        experiment_history=experiment_history,
        paper_github_url=paper_github_url,
        paper_huggingface_url=paper_huggingface_url,
    )


# ─── Deep-search retrieval loop ────────────────────────────────────────────
#
# The broad pass (/papers/recommended) only surfaces candidates whose
# embedding profile matches what the engine has already indexed for the
# interest — which means themes adjacent to but outside the repo's
# import graph (substitutes for an imported model, training-recipe
# upgrades, alternative implementations of a stage) never reach the
# candidate pool. The audit pass clusters the broad pool, compares
# against the repo's recent Issue history + README scope to spot
# under-represented themes, drafts 1-3 refine queries, and pulls extra
# candidates from /search/assets to merge into the final pool.


def _remyx_search_assets(
    query: str, max_results: int = 5, use_llm: bool = True,
) -> list[dict]:
    """POST ``/api/v1.0/search/assets`` and return the raw asset list.

    Mirrors the remyxai-cli `search_assets` helper (same auth, same
    endpoint). Returns the asset dicts as-is so the caller can map them
    into ``Recommendation`` objects with provenance metadata attached.
    Never raises — a flaky refine fetch shouldn't break the broad pool.
    """
    if not query or not query.strip():
        return []
    body = {
        "query": query.strip(),
        "max_results": min(max(1, int(max_results)), 50),
        "use_llm": use_llm,
    }
    try:
        resp = _remyx_post("/api/v1.0/search/assets", body)
    except Exception as e:
        log.warning(f"    refine query {query!r} failed: {e}")
        return []
    return resp.get("assets") or []


def _remyx_get_asset(arxiv_id: str) -> dict | None:
    """GET ``/api/v1.0/search/assets/{arxiv_id}`` and return the asset dict.

    The authoritative path when an arxiv id is known — bypasses keyword-
    search retrieval gaps. The keyword `_remyx_search_assets` endpoint
    occasionally misses indexed assets whose names don't tokenize
    cleanly (CamelCase compound names, multi-word coinages);
    direct arxiv-id lookup retrieves the asset regardless of
    search-side retrieval quality.

    Returns the asset dict on success (same envelope shape as the
    entries `_remyx_search_assets` returns, so downstream consumers
    don't need to switch on the source). Returns ``None`` on 404 /
    network failure / missing asset. Never raises — selection-pass
    broadening must not block the run.
    """
    arxiv_id = (arxiv_id or "").strip()
    if not arxiv_id:
        return None
    try:
        resp = _remyx_get(f"/api/v1.0/search/assets/{arxiv_id}")
    except Exception as e:
        log.debug(f"    asset lookup for {arxiv_id!r} failed: {e}")
        return None
    # The CLI's `search info` endpoint returns the asset directly at the
    # top level (not nested under an "assets" key, unlike the keyword
    # search which returns a list envelope). Return as-is when the
    # response shape looks like a single asset; tolerate the alternative
    # shape if the endpoint ever changes.
    if isinstance(resp, dict) and (resp.get("arxiv_id") or resp.get("title")):
        return resp
    return None


_PIN_METHOD_ARXIV_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")


def _resolve_pin_method(query: str) -> dict | None:
    """Resolve a pin-method query to a single /search/assets envelope dict.

    Detects whether the input is a literal arxiv_id (post-2007 form
    ``NNNN.NNNN[N][vN]``) and short-circuits to direct asset lookup via
    ``_remyx_get_asset`` — both faster and immune to keyword-search
    retrieval gaps. Otherwise treats it as a free-text method query and
    returns the top hit from ``_remyx_search_assets``. Returns ``None``
    when nothing matches.
    """
    q = (query or "").strip()
    if not q:
        return None
    if _PIN_METHOD_ARXIV_RE.match(q):
        return _remyx_get_asset(q)
    assets = _remyx_search_assets(q, max_results=1)
    return assets[0] if assets else None


def _asset_to_recommendation(
    asset: dict, refine_query: str,
    fallback_interest_name: str, interest_context: str,
    experiment_history: str,
) -> Recommendation:
    """Map one /search/assets envelope entry to a Recommendation.

    The asset envelope differs from /papers/recommended: it carries
    `github_url`, `categories`, `abstract` at the top level, no
    `reasoning`, and no `relevance_score`. The refine query is
    threaded into the synthetic reasoning so downstream renderers
    (the candidate brief, selection prompt) can see how this candidate
    reached the pool.
    """
    arxiv_id = (asset.get("arxiv_id") or "").strip()
    title = (asset.get("title") or "(untitled)").strip()
    abstract = (asset.get("abstract") or "").strip()
    paper_github_url = (asset.get("github_url") or "").strip()
    paper_huggingface_url = (
        asset.get("hf_url") or asset.get("huggingface_url")
        or asset.get("model_card_url") or ""
    ).strip()
    if not paper_huggingface_url:
        # Scrape the abstract as a fallback — the asset envelope's
        # huggingface_url field isn't always populated.
        hf_slugs = _extract_huggingface_urls(abstract)
        if hf_slugs:
            paper_huggingface_url = f"https://huggingface.co/{hf_slugs[0]}"
    # Search results carry no engine-ranked relevance — they're keyword/
    # LLM-matched against a free-text query, not the interest's profile.
    # Synthesize a score that lands in the "moderate" tier (≥ 0.60 by
    # default) so refine candidates survive the default min_confidence
    # filter; the selection pass still chooses among the pool on its
    # own merits. Sits below the broad pool's typical relevance so a
    # tied selection prefers a ranked candidate.
    synthetic_relevance = 0.65
    reasoning = (
        f"Surfaced by Outrider deep-search refine query "
        f"`{refine_query}` against /search/assets. The engine's "
        f"normal ranking did not place this paper in the interest's "
        f"broad pool — it's here because the audit pass identified an "
        f"under-represented theme this paper covers."
    )
    return Recommendation(
        paper_title=title,
        arxiv_id=arxiv_id,
        tier=_relevance_to_tier(synthetic_relevance),
        z_score=0.0,
        spec_md="",
        paper_abstract=abstract,
        domain_summary="",
        raw_paper_md="",
        relevance_score=synthetic_relevance,
        reasoning=reasoning,
        suggested_experiment="",
        recommendation_id="",
        interest_name=fallback_interest_name,
        interest_context=interest_context,
        experiment_history=experiment_history,
        paper_github_url=paper_github_url,
        paper_huggingface_url=paper_huggingface_url,
        refine_query=refine_query,
    )


def _fetch_repo_readme(repo: str, max_chars: int = 2000) -> str:
    """Return the target repo's README (truncated), or ``""``.

    Used as a scope hint for the audit pass — anchors what themes the
    maintainer says the repo is *about*, which can diverge from what
    the import graph alone would suggest. Best-effort, never raises.
    """
    try:
        resp = gh_api("GET", f"/repos/{repo}/readme")
    except Exception as e:
        log.debug(f"  README fetch for {repo} failed: {e}")
        return ""
    content = resp.get("content") or ""
    encoding = resp.get("encoding") or ""
    if encoding == "base64":
        try:
            decoded = base64.b64decode(content).decode(
                "utf-8", errors="replace"
            )
        except Exception:
            return ""
    else:
        decoded = content
    decoded = decoded.strip()
    if len(decoded) > max_chars:
        decoded = decoded[:max_chars].rstrip() + "\n…[truncated]"
    return decoded


def _recent_outrider_issue_titles(
    target: Target, n: int = 8,
) -> list[str]:
    """Return the last ``n`` Outrider-opened Issue titles on the target.

    Includes closed Issues — they still represent territory Outrider has
    already covered, so they count toward "themes the audit pass should
    look beyond." Title-only (we don't need the bodies for theme
    audit). Best-effort, returns ``[]`` on fetch failure.
    """
    try:
        raw = gh_api(
            "GET",
            f"/repos/{target.repo}/issues"
            f"?state=all&sort=created&direction=desc&per_page=30",
        ) or []
    except Exception as e:
        log.debug(f"  recent-issues fetch for {target.repo} failed: {e}")
        return []
    titles: list[str] = []
    for it in raw:
        if it.get("pull_request"):
            continue
        title = (it.get("title") or "").strip()
        body = it.get("body") or ""
        if not (title.startswith(PR_TITLE_PREFIX)
                or "Remyx Recommendation" in body):
            continue
        titles.append(title)
        if len(titles) >= n:
            break
    return titles


_AUDIT_PROMPT_TEMPLATE = """\
You are auditing a candidate pool of arXiv papers for relevance gaps
against a target code repository, and proposing 1-3 *refine queries*
that would surface adjacent-but-missing themes from the Remyx search
backend.

The goal: catch high-value papers that the engine's normal ranking
misses because they fall outside the repo's import graph. Typical gaps:
the broad pool over-represents one or two stages of the pipeline while
adjacent themes the maintainer cares about (substitutes for an imported
model, training-recipe upgrades, alternative implementations of a
stage) are absent despite being core to the repo's thesis.

Target repo
-----------
__REPO_FULLNAME__

Research interest the team registered
-------------------------------------
__INTEREST_NAME__

__INTEREST_CONTEXT__

Repo README excerpt (top __README_CHARS__ chars)
------------------------------------------------
__README__

Themes Outrider has already surfaced recently (last __RECENT_N__ Issues)
------------------------------------------------------------------------
__RECENT_ISSUES__
__REPO_INTEL__
Broad candidate pool currently being considered (__BROAD_N__ papers)
--------------------------------------------------------------------
__BROAD_BRIEF__

Your task
---------
1. Cluster the broad pool by theme. Identify themes that are
   *over-represented* (3+ papers covering the same angle) OR that match
   the repo's recent-Issues history (already-covered territory).
2. Identify themes the repo's README + interest context implies the
   maintainer cares about, but that are absent or under-represented in
   the broad pool.
3. **When the fork has accumulated cross-run learning** (a "Cross-run
   learning" section above), use it to steer refine queries: bias
   toward extending confirmed landing zones (queries that surface
   mechanisms that fit those paths' shapes); avoid searching for
   rejected shapes unless a caveat clearly applies; and reserve some
   query budget for the exploration bucket so novel-shape candidates
   still enter the pool.
4. **Bias your refine queries toward SIMPLIFY / REPLACE / ACCELERATE
   angles over ADD-ALONGSIDE angles.** Look specifically for:
     - Two-model or multi-step pipeline stages that could become one
     - Imported foundation models that have published successors
     - Multi-step processes the repo runs that could become single-pass
     - Libraries the repo depends on that could be retired
     - Stages where the imported model's claim (speedup, accuracy)
       could be empirically validated against the repo's typical scale
   Ask "what could SIMPLIFY or REPLACE stage X in this repo?" before
   asking "what's ADJACENT to X?". An add-alongside query is acceptable
   only when the README or interest context explicitly names a missing
   capability — otherwise the repo's existing contracts already
   represent the highest-leverage surfaces to improve.
5. For each under-represented theme that's a genuine fit, draft a single
   keyword-style search query — 4-8 terms, no quotes, no boolean
   operators. The Remyx /search/assets backend is keyword-matched, so
   the strongest signal words should appear first.
6. Output 1-3 queries (no more than 3). Quality beats quantity — if the
   broad pool already covers everything the maintainer would care about,
   return zero queries with a one-line reasoning. If you propose a
   query, the reasoning must explain *what theme* it targets, *why* the
   broad pool missed it, and which existing repo contract it anchors on
   (or call out explicitly that it's an add-alongside justified by an
   explicit README/interest signal).

Output strictly this JSON object (no prose wrapper):
{
  "refine_queries": ["query 1", "query 2", ...],
  "reasoning": "one paragraph explaining the audit and the queries"
}
"""


def _render_broad_brief(candidates: list[Recommendation]) -> str:
    """Compact one-line-per-candidate brief for the audit prompt.

    Lighter than `_render_candidate_brief` — the audit pass works on
    theme distribution, not per-paper verification, so we drop the long
    reasoning bodies and keep title + arxiv + categories-or-tier.
    """
    lines = []
    for i, c in enumerate(candidates):
        abstract = " ".join((c.paper_abstract or "").split())
        lines.append(
            f"[{i}] {c.paper_title}  (arxiv {c.arxiv_id or 'n/a'}, "
            f"tier {c.tier})\n"
            f"     {abstract[:200]}"
        )
    return "\n".join(lines)


def audit_and_refine_pool(
    target: Target, broad_candidates: list[Recommendation],
    interest_name: str, interest_context: str, experiment_history: str,
    max_queries: int = 3,
) -> list[Recommendation]:
    """Run the audit pass and merge refine-query results into the pool.

    Returns the deduped *combined* list (broad ∪ refine). Refine
    candidates are appended after the broad pool — order matters for the
    selection-pass index, and broad-ranked picks should keep their slot.
    Dedup is on arxiv_id with version-stripped fallback to catch
    cross-version duplicates (matches the Issue-dedup logic).

    Best-effort across the board: audit failure, parse failure, refine
    fetch failure all degrade gracefully to "just return the broad
    pool." Never raises.
    """
    if len(broad_candidates) == 0:
        return broad_candidates
    recent_issues = _recent_outrider_issue_titles(target, n=8)
    readme = _fetch_repo_readme(target.repo, max_chars=2000)
    readme_block = readme or "(README unavailable)"
    recent_block = (
        "\n".join(f"- {t}" for t in recent_issues)
        if recent_issues else "(no prior Outrider Issues on this repo)"
    )
    interest_block = (
        interest_context.strip() or "(no interest context recorded)"
    )

    # Cross-run learning: fetch the fork's .remyx/repo_intel.yaml via
    # the GitHub Contents API (no workdir yet — this runs upstream of
    # prepare_workdir). Opt-in via INPUT_MAINTAIN_STATE. When present,
    # inject a "Cross-run learning" section into the audit prompt so
    # refine queries steer toward extending confirmed zones + avoiding
    # rejected shapes + preserving the exploration budget.
    intel_block = ""
    maintain_state = (
        (os.environ.get("INPUT_MAINTAIN_STATE") or "").strip().lower()
        in ("true", "1", "yes")
    )
    if maintain_state:
        intel = _load_fork_repo_intel_remote(target)
        if intel is not None:
            rendered = _render_repo_intel_for_selection(intel)
            if rendered:
                intel_block = (
                    "\nCross-run learning accumulated on this fork\n"
                    "-------------------------------------------\n"
                    f"{rendered}"
                )
                log.info(
                    "  → audit: threading repo_intel priors "
                    "(%d zones, %d rejected shapes)",
                    len(intel.get("observed_landing_zones") or []),
                    len(intel.get("rejected_shapes") or []),
                )

    prompt = (
        _AUDIT_PROMPT_TEMPLATE
        .replace("__REPO_FULLNAME__", target.repo)
        .replace("__INTEREST_NAME__", interest_name or "(unnamed)")
        .replace("__INTEREST_CONTEXT__", interest_block)
        .replace("__README_CHARS__", "2000")
        .replace("__README__", readme_block)
        .replace("__RECENT_N__", str(len(recent_issues)))
        .replace("__RECENT_ISSUES__", recent_block)
        .replace("__REPO_INTEL__", intel_block)
        .replace("__BROAD_N__", str(len(broad_candidates)))
        .replace("__BROAD_BRIEF__", _render_broad_brief(broad_candidates))
    )
    # Audit timeout inherits the run's claude-timeout budget by default so
    # a customer who bumped `claude-timeout` for a slower backend (e.g.
    # GLM on a large monorepo) gets the same headroom on the audit
    # pass — same pattern as the preflight call. REMYX_AUDIT_TIMEOUT_S
    # is kept as an env-var escape hatch for cases that need a tighter
    # ceiling specifically on the audit (e.g. CI budget bounds).
    timeout_s = int(
        os.environ.get("REMYX_AUDIT_TIMEOUT_S", "")
        or target.claude_timeout_s
    )
    audit_max_turns = int(os.environ.get("REMYX_AUDIT_MAX_TURNS", "5"))
    log.info(
        f"  → audit pass over {len(broad_candidates)} broad candidates "
        f"(timeout={timeout_s}s, max-turns={audit_max_turns}, "
        f"recent_issues={len(recent_issues)}, "
        f"readme={'yes' if readme else 'no'})"
    )
    # The audit pass is pure reasoning over inlined context — no repo
    # navigation needed. Hand Claude an empty tempdir so the agentic
    # loop has nothing to wander into.
    with tempfile.TemporaryDirectory(prefix="outrider-audit-") as tmp:
        ok, output = _run_claude_oneshot(
            Path(tmp), prompt, timeout_s, max_turns=audit_max_turns,
        )
    if not ok:
        log.warning(f"  audit call failed: {output[:200]}; "
                    f"skipping refine pass")
        return broad_candidates
    data = _extract_json_object(output)
    if data is None:
        log.warning(f"  audit: couldn't parse JSON; raw: {output[:300]!r}")
        return broad_candidates
    queries = data.get("refine_queries") or []
    if not isinstance(queries, list):
        log.warning(f"  audit: refine_queries not a list "
                    f"({type(queries).__name__}); skipping refine")
        return broad_candidates
    # Bound spend regardless of what the audit returns.
    queries = [str(q).strip() for q in queries if str(q).strip()][:max_queries]
    log.info(f"  audit: {len(queries)} refine quer"
             f"{'y' if len(queries) == 1 else 'ies'}: "
             f"{(data.get('reasoning') or '')[:160]}")
    if not queries:
        return broad_candidates
    _RUN_REFINE_QUERIES.extend(queries)
    # Pre-collect existing arxiv ids (with version-stripped variants) so
    # refine results that duplicate the broad pool are skipped silently.
    seen: set[str] = set()
    for c in broad_candidates:
        if c.arxiv_id:
            seen.add(c.arxiv_id)
            seen.add(_arxiv_versionless(c.arxiv_id))
    refine_recs: list[Recommendation] = []
    per_query = int(os.environ.get("REMYX_REFINE_PER_QUERY", "5"))
    for q in queries:
        log.info(f"    → refine /search/assets {q!r} (max_results={per_query})")
        assets = _remyx_search_assets(q, max_results=per_query)
        for a in assets:
            arxiv_id = (a.get("arxiv_id") or "").strip()
            if not arxiv_id:
                continue
            if arxiv_id in seen or _arxiv_versionless(arxiv_id) in seen:
                continue
            seen.add(arxiv_id)
            seen.add(_arxiv_versionless(arxiv_id))
            refine_recs.append(_asset_to_recommendation(
                a, refine_query=q,
                fallback_interest_name=interest_name,
                interest_context=interest_context,
                experiment_history=experiment_history,
            ))
    log.info(f"  audit: {len(refine_recs)} new refine candidates "
             f"after dedup (broad pool was {len(broad_candidates)})")
    return broad_candidates + refine_recs


def query_remyx_candidates(target: Target) -> list[Recommendation]:
    """Pull the top-N recommendations for ``target.interest_id`` over the
    configured lookback window and return them as a relevance-ranked list.

    The window is ``REMYX_RECOMMENDATION_PERIOD`` (default ``"week"`` — the
    past 7 days) and the pool size is ``REMYX_RECOMMENDATION_LIMIT``
    (default 25), both surfaced as the ``lookback`` / ``candidate-pool``
    action inputs. Remyx owns commit-history extraction, candidate pool,
    embedding pre-filter, LLM ranking, and reasoning generation; the
    action is a pure consumer.

    The earlier behaviour took only ``papers[0]``, which wasted the
    lookback: the top-ranked paper is often a model-architecture or
    training-method paper with no call site in a data-pipeline repo, while
    a lower-ranked candidate is a clean drop-in. Returning the full pool
    lets ``select_recommendation`` pick the most implementable candidate.

    Backed by the Remyx engine's ``GET /api/v1.0/papers/recommended``
    endpoint.
    """
    if not target.interest_id:
        raise RuntimeError(
            f"target {target.repo!r} has no interest_id configured. "
            f"Get the interest_id from engine.remyx.ai (Settings → "
            f"Workflow snippet) and pass it via the action's "
            f"`with: interest-id: ...` input."
        )

    log.info(f"  → querying Remyx /papers/recommended "
             f"(interest={target.interest_id[:8]}…, "
             f"period={REMYX_RECOMMENDATION_PERIOD}, "
             f"limit={REMYX_RECOMMENDATION_LIMIT})")
    def _fetch_papers() -> list:
        resp = _remyx_get(
            "/api/v1.0/papers/recommended",
            params={
                "interest_id": target.interest_id,
                "period":      REMYX_RECOMMENDATION_PERIOD,
                "limit":       REMYX_RECOMMENDATION_LIMIT,
            },
        )
        return resp.get("papers") or []

    papers = _fetch_papers()
    if not papers:
        # A brand-new interest (or one whose daily refresh hasn't run since
        # the last cron) has no ranked picks yet. Trigger a refresh and poll
        # rather than failing the run outright.
        papers = _refresh_and_poll_recommendations(target, _fetch_papers)
    if not papers:
        raise RuntimeError(
            f"Remyx returned no recommendations for interest "
            f"{target.interest_id} in period={REMYX_RECOMMENDATION_PERIOD} "
            f"even after triggering /papers/recommended/refresh and waiting "
            f"{REMYX_REFRESH_WAIT_S}s. The interest may have no fresh picks "
            f"in this window."
        )

    interest_name, interest_context, experiment_history = (
        _fetch_interest_context(target.interest_id)
    )
    candidates = [
        _paper_to_recommendation(
            p, interest_name, interest_context, experiment_history,
        )
        for p in papers
    ]
    # Deep-search refine — on by default. Costs one extra Claude call
    # (~$0.5–1.0 per run) plus a few GitHub API calls + N /search/assets
    # calls; in return, the audit pass catches papers the broad ranking
    # misses because they fall outside the repo's import graph. Opt out
    # with REMYX_DEEP_SEARCH=0 if the cost isn't worth it for a given
    # target.
    if os.environ.get("REMYX_DEEP_SEARCH", "1") != "0":
        candidates = audit_and_refine_pool(
            target, candidates,
            interest_name=interest_name,
            interest_context=interest_context,
            experiment_history=experiment_history,
        )
    # License + code-availability enrichment. Runs AFTER deep search so
    # refine-pass candidates get the same license signals as broad-pass
    # ones. Best-effort — any GitHub flake leaves the fields at their
    # dataclass defaults. Opt-out for offline/unit tests via
    # REMYX_LICENSE_GATE=0.
    if os.environ.get("REMYX_LICENSE_GATE", "1") != "0":
        _enrich_candidate_licenses(candidates, target)
    # Identity-tuple dedup. Paper-version siblings (one code repo,
    # multiple arxiv releases over time) inflate the candidate pool
    # with what is really one engineering target. Collapse them so
    # the selection pass doesn't waste reasoning on "which arxiv id" when
    # the real choice is "which weights from one repo." Runs after
    # license enrichment so the arxiv-page fallback has had its chance
    # to populate URLs for both siblings (otherwise dedup misses when
    # one sibling has a URL and the other doesn't).
    candidates = _coalesce_candidate_families(candidates)
    for i, c in enumerate(candidates):
        # Surface the identity-tuple inputs in the log so we can see at
        # a glance which provenance won (GitHub vs HF vs none).
        url_hint = ""
        if c.paper_huggingface_url:
            hf_slug = c.paper_huggingface_url.split("huggingface.co/")[-1]
            url_hint = f" hf={hf_slug[:40]}"
        elif c.paper_github_url:
            gh_slug = c.paper_github_url.split("github.com/")[-1]
            url_hint = f" gh={gh_slug[:40]}"
        source_hint = (
            f" [{c.license_source}]" if c.license_source else ""
        )
        log.info(f"    [{i}] {c.paper_title[:55]}…  "
                 f"relevance={c.relevance_score:.2f}  tier={c.tier}  "
                 f"license={c.paper_license or '(none)'} "
                 f"({c.license_class}, compat={c.license_compat:.2f})"
                 f"{source_hint}{url_hint}")
    return candidates


# Canonical rendering order for license-class distributions. Any class
# outside this list (future additions) is appended after, so the line
# never silently drops a bucket.
_LICENSE_CLASS_ORDER = (
    "permissive", "copyleft", "nc", "no-code-link", "unknown", "missing",
)


def _pool_composition(candidates: list[Recommendation]) -> tuple[int, int]:
    """(broad, refine) candidate counts, post family-dedup.

    Counted from the per-candidate ``refine_query`` provenance marker so
    the numbers reflect the pool the selection pass actually saw —
    ``_coalesce_candidate_families`` may have collapsed siblings from
    either source.
    """
    refine = sum(1 for c in candidates if c.refine_query)
    return len(candidates) - refine, refine


def _license_class_counts(candidates: list[Recommendation]) -> dict[str, int]:
    """Per-class license distribution across the candidate pool."""
    counts: dict[str, int] = {}
    for c in candidates:
        cls = c.license_class or "unknown"
        counts[cls] = counts.get(cls, 0) + 1
    return counts


def _format_license_class_counts(counts: dict) -> str:
    """Single-line distribution: ``permissive: 4 · nc: 1 · missing: 30``.

    Canonical class order first, unexpected classes appended;
    zero-count classes omitted (the dict only carries observed ones).
    """
    parts = [f"{k}: {counts[k]}" for k in _LICENSE_CLASS_ORDER if counts.get(k)]
    parts += [
        f"{k}: {v}" for k, v in counts.items()
        if k not in _LICENSE_CLASS_ORDER and v
    ]
    return " · ".join(parts) if parts else "(no candidates)"


def _coalesce_candidate_families(
    candidates: list[Recommendation],
) -> list[Recommendation]:
    """Collapse paper-version siblings that share a code repo.

    The unit of engineering choice is the repo + model weights, not the
    arxiv id. Papers that share a ``github.com/<owner>/<repo>`` slug
    represent one family of work with multiple paper releases over
    time. Treating them as distinct candidates forces the selection
    pass to reason about "which paper" when the real choice is "which
    weights from one repo."

    The dedup key is the GitHub slug only. HF-org-level dedup is
    skipped — unrelated models from the same author/org would
    false-positive (two different research lines under
    ``huggingface.co/microsoft/*`` are not one family).

    The highest-relevance candidate in each family becomes the
    representative; its ``family_summary`` field gains a one-line
    description of the siblings so downstream renderers can surface
    the merged version history at a glance. Solo candidates (no shared
    repo with any sibling) pass through unchanged.

    Order-preserving for unchanged candidates so the broad-pool
    ranking that downstream consumers depend on is not perturbed
    except where families collapse.
    """
    if len(candidates) <= 1:
        return candidates
    # Build a mapping: github_slug → list of indices into ``candidates``
    # that share it. Candidates with no GitHub URL skip grouping —
    # they're never merged with anyone. The dedup key is lowercased
    # because GitHub URLs are case-insensitive for owner/repo
    # (``github.com/Owner/Repo`` and ``github.com/owner/repo`` resolve
    # to the same project) but different upstream envelopes occasionally
    # supply the URL in different cases.
    families: dict[str, list[int]] = {}
    for i, c in enumerate(candidates):
        if not c.paper_github_url:
            continue
        slug = _extract_github_urls(c.paper_github_url)
        if not slug:
            continue
        families.setdefault(slug[0].lower(), []).append(i)
    # Indices to drop (siblings being collapsed into their representative).
    drop: set[int] = set()
    for slug, idxs in families.items():
        if len(idxs) < 2:
            continue
        # Representative = highest-relevance candidate in the family.
        idxs.sort(key=lambda i: candidates[i].relevance_score, reverse=True)
        rep_idx = idxs[0]
        sibling_descriptors = []
        for j in idxs[1:]:
            sibling = candidates[j]
            sibling_descriptors.append(
                f"{sibling.paper_title} (arxiv {sibling.arxiv_id or 'n/a'})"
            )
            drop.add(j)
        rep = candidates[rep_idx]
        rep.family_summary = (
            f"Coalesced from {len(idxs)} paper-version siblings under "
            f"`github.com/{slug}` (representative: highest relevance). "
            f"Siblings: " + "; ".join(sibling_descriptors)
        )
        log.info(
            f"  family-coalesce: {slug} merges {len(idxs)} candidates → "
            f"keeping {rep.paper_title[:40]}… (relevance {rep.relevance_score:.2f})"
        )
    return [c for i, c in enumerate(candidates) if i not in drop]


def _enrich_candidate_licenses(
    candidates: list[Recommendation], target: Target,
) -> None:
    """Populate license + compat fields on each Recommendation in place.

    Resolution order per candidate:

    1. If neither ``paper_github_url`` nor ``paper_huggingface_url`` is
       set, scrape the arxiv abstract page once as a fallback (covers
       the ~70% case where the engine envelope omits both URLs).
    2. If a HuggingFace model URL is available, fetch the model-card
       frontmatter license — this is the authoritative source for
       *weight* licensing (what a customer actually loads).
    3. Fall back to the GitHub LICENSE classifier (with the v1.3.9
       NOASSERTION content-sniffer).
    4. Cross-validate when both sources are present and disagree.
    5. If no URL surfaces from any source, classify as ``"no-code-link"``
       — distinct from ``"missing"``, which is reserved for "we *did*
       call the LICENSE endpoint and got nothing parseable."

    Best-effort throughout — any fetch failure leaves the dataclass
    defaults intact (or the partial result it got so far). The gate is
    advisory; it must never block the pipeline.
    """
    target_spdx = _fetch_repo_license(target.repo)
    target_class = _classify_license(target_spdx)
    log.info(f"  → license gate: target {target.repo!r} = "
             f"{target_spdx or '(none)'} ({target_class})")
    for c in candidates:
        # Step 1: arxiv-page fallback when nothing has surfaced yet.
        if not c.paper_github_url and not c.paper_huggingface_url:
            gh_slugs, hf_slugs = _fetch_arxiv_abstract_page_urls(c.arxiv_id)
            if gh_slugs:
                c.paper_github_url = f"https://github.com/{gh_slugs[0]}"
            if hf_slugs:
                c.paper_huggingface_url = (
                    f"https://huggingface.co/{hf_slugs[0]}"
                )
        # Step 2: HF model card (authoritative for weight licensing).
        hf_spdx = ""
        if c.paper_huggingface_url:
            hf_slug = _extract_huggingface_urls(c.paper_huggingface_url)
            if hf_slug:
                hf_spdx = _fetch_hf_license(hf_slug[0])
        # Step 3: GitHub LICENSE (with v1.3.9 NOASSERTION content sniff).
        gh_spdx = ""
        if c.paper_github_url:
            gh_slug = _extract_github_urls(c.paper_github_url)
            if gh_slug:
                gh_spdx = _fetch_repo_license(gh_slug[0])
        # Step 4: pick the most authoritative result + log mismatches.
        if hf_spdx:
            c.paper_license = hf_spdx
            c.license_source = "huggingface"
            if gh_spdx and _classify_license(gh_spdx) != _classify_license(hf_spdx):
                log.warning(
                    f"  license mismatch on {c.paper_title[:50]}…: "
                    f"HF says {hf_spdx} ({_classify_license(hf_spdx)}), "
                    f"GitHub says {gh_spdx} ({_classify_license(gh_spdx)}); "
                    f"preferring HF (weights are the adoption target)"
                )
        elif gh_spdx:
            c.paper_license = gh_spdx
            c.license_source = (
                "github_content_sniff" if gh_spdx not in ("NOASSERTION",)
                and gh_spdx.lower() not in _PERMISSIVE_SPDX
                and gh_spdx.lower() not in _COPYLEFT_SPDX
                else "github"
            )
        # Step 5: bucket. "no-code-link" when we never had any URL to
        # try; the regular classifier covers the SPDX-present cases.
        if not c.paper_github_url and not c.paper_huggingface_url:
            c.license_class = "no-code-link"
        else:
            c.license_class = _classify_license(c.paper_license)
        c.license_compat = _license_compat_score(c.license_class, target_class)
        # Step 6: retry via arxiv HTML when the primary path landed on an
        # unfavorable bucket. Fires only when the primary path already
        # ran and gave up — never overrides a successful classification.
        _retry_license_via_arxiv_html(c, target_class)


def query_remyx_recommendation(target: Target) -> Recommendation:
    """Back-compat shim: the single highest-ranked recommendation.

    Retained for callers / tests that only want the top pick. The
    orchestrator now calls ``query_remyx_candidates`` and runs a
    selection pass over the full pool instead.
    """
    return query_remyx_candidates(target)[0]



# ─── Dedup ─────────────────────────────────────────────────────────────────


def existing_pr_for(target: Target, branch: str) -> dict | None:
    """Return the PR dict if an open PR exists on the target repo for `branch`."""
    head_owner = target.repo.split("/")[0]
    head = f"{head_owner}:{branch}"
    prs = gh_api("GET", f"/repos/{target.repo}/pulls?state=open&head={head}")
    return prs[0] if prs else None


def open_remyx_issues(target: Target) -> list[dict]:
    """Open Remyx Recommendation Issues on the target repo.

    Back-compat shim. New callers should prefer ``_remyx_issues(target,
    state="all")`` so dedup respects closed Issues too (the symmetric
    discharge invariant — a paper has been addressed by Outrider once
    any Outrider Issue exists for it, open or closed).
    """
    return _remyx_issues(target, state="open")


def _remyx_issues(target: Target, state: str = "open") -> list[dict]:
    """Outrider-opened Issues on the target repo, filtered to ours.

    ``state`` mirrors GitHub's ``/issues?state=`` param: ``"open"``,
    ``"closed"``, or ``"all"``. Bounded to the first 100 issues per
    state (200 total for ``state="all"`` — pragmatic cap on retrieval).

    GitHub's /issues endpoint also returns PRs (they carry a
    'pull_request' key) — those are filtered out; PRs are deduped
    separately by ``existing_pr_for``. We keep only items that look
    like one of ours: the title carries the PR_TITLE_PREFIX or the
    body has the orchestrator's attribution footer.

    Use ``state="all"`` for dedup gates so a closed Outrider Issue
    suppresses re-recommendation of the same paper — reopen-the-Issue
    is the maintainer's re-engagement lever.
    """
    try:
        issues = gh_api(
            "GET",
            f"/repos/{target.repo}/issues?state={state}&per_page=100",
        ) or []
    except Exception as e:
        log.debug(f"  fetch issues (state={state}) for {target.repo} failed: {e}")
        return []
    ours = []
    for it in issues:
        if it.get("pull_request"):
            continue
        title = it.get("title") or ""
        body = it.get("body") or ""
        if title.startswith(PR_TITLE_PREFIX) or "Remyx Recommendation" in body:
            ours.append(it)
    return ours


def _all_remyx_issues(target: Target) -> list[dict]:
    """Convenience wrapper: every Outrider Issue (open + closed)."""
    return _remyx_issues(target, state="all")


def _remyx_open_prs(target: Target) -> list[dict]:
    """Open Outrider-opened PRs on the target repo.

    The weekly digest's review checklist covers both artifact routes —
    an idle draft PR is exactly as actionable as an open Issue. Ours =
    head branch carries the recommendation prefix, or the title carries
    the PR prefix. Best-effort, returns ``[]`` on fetch failure.
    """
    try:
        prs = gh_api(
            "GET", f"/repos/{target.repo}/pulls?state=open&per_page=100",
        ) or []
    except Exception as e:
        log.debug(f"  fetch open PRs for {target.repo} failed: {e}")
        return []
    ours = []
    for pr in prs:
        title = pr.get("title") or ""
        head_ref = ((pr.get("head") or {}).get("ref")) or ""
        if (title.startswith(PR_TITLE_PREFIX)
                or head_ref.startswith(BRANCH_PREFIX)):
            ours.append(pr)
    return ours


def _arxiv_linked_issues(target: Target, state: str = "all") -> list[dict]:
    """All Issues on the target repo whose body links an arxiv paper,
    regardless of who opened them.

    Maintainer-opened RFCs, community-opened Issues, and Outrider Issues
    all qualify — the arxiv-in-body match is the discharge signal. A
    maintainer who opens an RFC linking arxiv 2605.26004 has signaled
    exactly as strongly as Outrider would have by opening its own
    Issue: the paper is already in the team's attention.

    Returns Issues sorted as GitHub returned them (most-recently-updated
    first by default). PRs are excluded. The "Outrider-prefixed"
    filter from ``_remyx_issues`` does NOT apply here — that's the
    whole point.
    """
    try:
        issues = gh_api(
            "GET",
            f"/repos/{target.repo}/issues?state={state}&per_page=100",
        ) or []
    except Exception as e:
        log.debug(
            f"  fetch arxiv-linked issues (state={state}) for "
            f"{target.repo} failed: {e}"
        )
        return []
    out = []
    for it in issues:
        if it.get("pull_request"):
            continue
        body = it.get("body") or ""
        if _arxiv_id_from_issue_body(body):
            out.append(it)
    return out


def _all_discharge_issues(target: Target) -> list[dict]:
    """Merged discharge set: Outrider Issues + maintainer arxiv-linked
    Issues. The dedup gate's input.

    De-duplicated by Issue number so an Outrider Issue that happens to
    also link arxiv (which it always does) isn't double-counted. Order
    preserved as GitHub returned: most-recently-updated first.

    Each entry is annotated in-place with a ``_remyx_source`` key set
    to either ``"outrider"`` (matches the Outrider-prefix filter) or
    ``"maintainer"`` (passed only the arxiv-link filter). Downstream
    rendering uses this for the ``[Outrider]`` / ``[Maintainer]`` tag
    in the selection prompt's discharge section.
    """
    outrider_issues = _all_remyx_issues(target)
    outrider_numbers = {it.get("number") for it in outrider_issues if it.get("number") is not None}
    for it in outrider_issues:
        it["_remyx_source"] = "outrider"
    arxiv_issues = _arxiv_linked_issues(target)
    merged = list(outrider_issues)
    for it in arxiv_issues:
        num = it.get("number")
        if num is None or num in outrider_numbers:
            continue
        it["_remyx_source"] = "maintainer"
        merged.append(it)
    return merged


def _arxiv_versionless(s: str) -> str:
    """Drop a trailing ``v<digits>`` from an arxiv id.

    The engine pool and the broadening-search path don't agree on whether
    to include the version suffix — engine candidates carry ``2605.26102v1``
    while a `remyxai search query` result for the same paper comes back as
    ``2605.26102``. issue_for_paper does a substring match on the issue
    body, and substring matching is directional: ``2605.26102v1`` is NOT
    a substring of ``2605.26102``, so a versioned candidate misses an open
    Issue that was filed from the versionless side."""
    return re.sub(r"v\d+$", "", s or "")


def issue_for_paper(open_issues: list[dict], rec: Recommendation) -> dict | None:
    """Return an already-open Remyx Issue for this paper, if any.

    Match order (returns the first hit):
      1. Arxiv id (versioned and versionless variants) appearing as
         ``arxiv.org/abs/<id>`` in the Issue body — primary key for
         engine-pool candidates.
      2. Sibling-paper identity: when the candidate has a code URL or
         HF model URL, an existing Issue that references the same
         ``github.com/<owner>/<repo>`` or ``huggingface.co/<owner>/<model>``
         counts as "already open for this family." Catches paper-
         version duplicates (one repo, multiple arxiv releases) where
         each release has its own arxiv id but the engineering target
         is one repo.
      3. Exact title match (only used when the recommendation has no
         arxiv id — covers the OPEN_AS_ISSUE path where the title is
         Claude-authored).

    Pure (no network) so the matching is unit-testable; the fetch lives
    in open_remyx_issues.
    """
    arxiv_needles: list[str] = []
    if rec.arxiv_id:
        arxiv_needles.append(f"arxiv.org/abs/{rec.arxiv_id}")
        stripped = _arxiv_versionless(rec.arxiv_id)
        if stripped and stripped != rec.arxiv_id:
            arxiv_needles.append(f"arxiv.org/abs/{stripped}")
    family_needles: list[str] = []
    if rec.paper_github_url:
        # Normalize to the bare owner/repo slug so trailing paths / .git
        # don't shadow the match.
        gh_slug = _extract_github_urls(rec.paper_github_url)
        if gh_slug:
            family_needles.append(f"github.com/{gh_slug[0]}")
    if rec.paper_huggingface_url:
        hf_slug = _extract_huggingface_urls(rec.paper_huggingface_url)
        if hf_slug:
            family_needles.append(f"huggingface.co/{hf_slug[0]}")
    title_match = f"{PR_TITLE_PREFIX} {rec.paper_title}"
    for it in open_issues:
        body = it.get("body") or ""
        if any(n in body for n in arxiv_needles):
            return it
        if any(n in body for n in family_needles):
            return it
        if not rec.arxiv_id and (it.get("title") or "") == title_match:
            return it
    return None


def _most_recent_open_artifact_age_days(target: Target) -> int | None:
    """Return the age in days of the most recently opened Remyx PR/Issue,
    or ``None`` if no open Remyx artifact exists on the target.

    Used by the cadence guard to time-decay throttling: only artifacts
    opened *within* ``rate_limit_days`` block subsequent runs; older open
    artifacts have aged out of the throttle window and don't fire the
    gate. The caller does the threshold comparison so it can also surface
    the age in run telemetry.

    Age is days since ``created_at``, floored. When multiple open
    artifacts exist, returns the *smallest* age (the most recently
    opened) — that's the one the throttle cares about ("was something
    just opened?"). Older co-existing artifacts don't matter for cadence;
    the per-paper discharge filter handles same-paper retries
    independently.
    """
    now = dt.datetime.now(dt.timezone.utc)

    def _age_days(created_iso: str) -> int | None:
        try:
            created = dt.datetime.fromisoformat(
                (created_iso or "").replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            return None
        return max(0, int((now - created).total_seconds() // 86400))

    ages: list[int] = []

    # PRs — identified by branch prefix. Only open PRs are returned.
    prs = gh_api(
        "GET", f"/repos/{target.repo}/pulls?state=open&per_page=50"
    ) or []
    for pr in prs:
        ref = pr.get("head", {}).get("ref", "")
        if not ref.startswith(BRANCH_PREFIX):
            continue
        age = _age_days(pr.get("created_at", ""))
        if age is None:
            continue
        log.info(f"  open Remyx PR exists ({age}d old): {pr['html_url']}")
        ages.append(age)

    # Issues — identified by title prefix or body attribution marker.
    # GitHub's /issues endpoint also returns PRs (they carry a
    # 'pull_request' key); filter those out so we don't double-count.
    issues = gh_api(
        "GET", f"/repos/{target.repo}/issues?state=open&per_page=50"
    ) or []
    for it in issues:
        if it.get("pull_request"):
            continue
        title = it.get("title") or ""
        body = it.get("body") or ""
        if not (title.startswith(PR_TITLE_PREFIX) or "Remyx Recommendation" in body):
            continue
        age = _age_days(it.get("created_at", ""))
        if age is None:
            continue
        log.info(f"  open Remyx Issue exists ({age}d old): {it['html_url']}")
        ages.append(age)

    return min(ages) if ages else None


def open_remyx_artifact_exists(target: Target) -> bool:
    """Cadence guard — return True iff a *recent* open Remyx artifact
    blocks the run.

    Time-decayed: an open Remyx PR/Issue blocks new runs only while it's
    younger than ``rate_limit_days``. Older open artifacts have aged out
    of the throttle window and no longer fire the gate — recognizing that
    real maintainers often leave Issues open for weeks without active
    triage, and the action should resume cadence rather than mute the
    repo indefinitely. Engagement (merge or close) clears the gate
    immediately. Per-paper dedup (a separate gate) handles same-paper
    retries; this guard is purely about not stacking *recent* unresolved
    work.

    ``rate_limit_days = 0`` disables the guard entirely.
    """
    if target.rate_limit_days <= 0:
        return False
    age = _most_recent_open_artifact_age_days(target)
    return age is not None and age < target.rate_limit_days


# ─── Workdir + spec bundle ─────────────────────────────────────────────────


def prepare_workdir(target: Target) -> Path:
    """Clone the target repo, return the workdir.

    The action operates on `target.repo` directly — branches are pushed
    to it, PRs open against its main. Authentication is via
    GITHUB_TOKEN (either the workflow's built-in token when the action
    runs in the target repo, or a cross-repo PAT like FF_GITHUB_TOKEN
    when the action lives in a separate controller repo).
    """
    workdir = Path(tempfile.mkdtemp(prefix=f"rr-{slugify(target.repo)}-"))
    token = _github_token()
    if not token:
        raise RuntimeError(
            "No GitHub token available for clone+push. Either pass "
            "`with: github-token: ${{ secrets.MY_PAT }}` or rely on the "
            "default ${{ github.token }} the action.yml threads through."
        )
    # Use the modern github.com auth convention: token as the `x-access-token`
    # user. This is more portable across the workflow GITHUB_TOKEN (which
    # works fine with the bare-token-as-username form too) and PATs (which
    # work either way), avoiding any ambiguity that left the clone URL
    # credential-less on the v1.0.3 push failure.
    repo_url = f"https://x-access-token:{token}@github.com/{target.repo}.git"

    log.info(f"  → cloning {target.repo} to {workdir}")
    # Skip Git-LFS smudge: the orchestrator only reads code structure and
    # makes small edits — it never needs the LFS blobs (model weights,
    # datasets). Fetching them is slow, and a repo whose LFS bandwidth
    # budget is exhausted fails the clone outright ("exceeded its LFS
    # budget") even though every file we touch is plain text. Pointer files
    # are checked out instead.
    clone_env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
    subprocess.run(
        ["git", "clone", "--depth", "20", repo_url, str(workdir)],
        check=True, env=clone_env,
    )
    # Refinement mode: when INPUT_START_FROM_REF names a branch /
    # tag / SHA on the fork, check that ref out on top of the default-branch
    # clone. Downstream: the sanity check in commit_and_push validates
    # against origin/<start_from_ref> instead of origin/<default>, and the
    # derived branch name gets a "-refined" suffix so the refinement push
    # doesn't collide with the original artifact when the same paper feeds
    # both runs.
    start_from_ref = (os.environ.get("INPUT_START_FROM_REF") or "").strip()
    if start_from_ref:
        log.info(f"  → fetching + checking out start-from-ref '{start_from_ref}'")
        # Explicit refspec so ``origin/<ref>`` remote-tracking exists too —
        # commit_and_push's sanity check resolves ``origin/<start-from-ref>``,
        # and a plain ``git fetch origin <ref>`` only updates FETCH_HEAD.
        subprocess.run(
            ["git", "fetch", "--depth", "20", "origin",
             f"{start_from_ref}:refs/remotes/origin/{start_from_ref}"],
            cwd=workdir, check=True, env=clone_env,
        )
        subprocess.run(
            ["git", "checkout", "-B", start_from_ref,
             f"origin/{start_from_ref}"],
            cwd=workdir, check=True,
        )
    # The branch head is normally re-authored via the git data API
    # (see commit_and_push → _recommit_via_api), which stamps the bot
    # identity itself. This local identity is the fallback: it's what the
    # already-pushed commit carries if that API path can't run. Use the
    # bot's canonical GitHub no-reply identity so even the fallback links
    # to remyx-ai[bot] (id 289541483) — GitHub attributes commits by
    # matching the author email to an account.
    subprocess.run(
        ["git", "config", "user.email",
         "289541483+remyx-ai[bot]@users.noreply.github.com"],
        cwd=workdir, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "remyx-ai[bot]"],
        cwd=workdir, check=True,
    )
    return workdir


def detect_package_name(workdir: Path) -> str:
    """Best-effort guess at the importable package name in workdir."""
    for cand in workdir.iterdir():
        if cand.is_dir() and (cand / "__init__.py").exists() and not cand.name.startswith((".", "test")):
            return cand.name
    return "src"


def _orient_contributor_guides(workdir: Path, cap: int = 3000) -> str:
    """Read the canonical agent-instruction files; concatenate, truncate to ``cap``.

    Covers the full canonical set documented by the Instructions-as-Code
    study and the major coding-agent vendors — per-agent files
    (``CLAUDE.md``, ``AGENTS.md``, ``.cursorrules``,
    ``.github/copilot-instructions.md``), then human contributor docs
    (``CONTRIBUTING.md``), then team-direction context (``CONTEXT.md``,
    which carries active investigation areas, stable architecture, and
    out-of-scope boundaries). Order is precedence-from-most-specific so the
    agent's context-window position for each guide is stable across runs.
    Each chunk is annotated with a structural-signal line (length + section
    count), the dimension the study found predictive of merge-rate gains.

    Delegates to :mod:`instruction_files`; repos without these files are
    unaffected (empty string out).
    """
    return render_instruction_files(workdir, cap=cap)


def _orient_pr_template(workdir: Path, cap: int = 2000) -> str:
    """Read PR templates from .github/PULL_REQUEST_TEMPLATE/ or root."""
    candidates: list[Path] = []
    tmpl_dir = workdir / ".github" / "PULL_REQUEST_TEMPLATE"
    if tmpl_dir.is_dir():
        candidates.extend(sorted(tmpl_dir.glob("*.md")))
    root_tmpl = workdir / ".github" / "pull_request_template.md"
    if root_tmpl.is_file():
        candidates.append(root_tmpl)
    chunks: list[str] = []
    for path in candidates[:3]:  # at most 3 templates
        try:
            body = path.read_text(errors="replace").strip()
        except OSError:
            continue
        if not body:
            continue
        rel = path.relative_to(workdir).as_posix()
        snippet = body[:cap] + ("\n…[truncated]" if len(body) > cap else "")
        chunks.append(f"### `{rel}`\n\n```markdown\n{snippet}\n```")
    return "\n\n".join(chunks)


def _orient_recent_merged_prs(repo: str, limit: int = 10) -> str:
    """Pull recent merged PRs via gh_api for title + body convention extraction."""
    if not repo:
        return ""
    try:
        params = f"state=closed&sort=updated&direction=desc&per_page={limit * 2}"
        prs = gh_api("GET", f"repos/{repo}/pulls?{params}")
    except Exception:
        return ""
    if not isinstance(prs, list):
        return ""
    merged = [p for p in prs if p.get("merged_at")][:limit]
    if not merged:
        return ""
    lines = [f"Last {len(merged)} merged PRs on `{repo}` (most recent first):\n"]
    for pr in merged:
        num = pr.get("number")
        title = (pr.get("title") or "").strip()
        author = (pr.get("user") or {}).get("login", "?")
        labels = [
            (lab.get("name") or "").strip()
            for lab in (pr.get("labels") or [])
            if lab.get("name")
        ]
        label_str = f"  labels: [{', '.join(labels)}]" if labels else ""
        lines.append(f"- #{num} (by @{author}): {title}{label_str}")
    # Include the body of the 3 most-recent merges so the agent can see
    # the section pattern (Summary / Test plan / etc).
    lines.append("\nBody samples (3 most recent, truncated):")
    for pr in merged[:3]:
        num = pr.get("number")
        body = (pr.get("body") or "").strip()
        if not body:
            continue
        snippet = body[:800] + ("\n…[truncated]" if len(body) > 800 else "")
        lines.append(f"\n#### PR #{num} body\n```markdown\n{snippet}\n```")
    return "\n".join(lines)


def _orient_tooling_config(workdir: Path) -> str:
    """Extract lint/type/test config from common config files."""
    chunks: list[str] = []
    # pyproject.toml — extract [tool.X] sections only (keep budget tight)
    pyproject = workdir / "pyproject.toml"
    if pyproject.is_file():
        try:
            body = pyproject.read_text(errors="replace")
        except OSError:
            body = ""
        if body:
            tool_sections: list[str] = []
            current_section: list[str] = []
            in_tool_block = False
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("["):
                    if in_tool_block and current_section:
                        tool_sections.append("\n".join(current_section))
                    current_section = []
                    in_tool_block = stripped.startswith("[tool.") or stripped.startswith(
                        "[project.optional-dependencies"
                    )
                if in_tool_block:
                    current_section.append(line)
            if in_tool_block and current_section:
                tool_sections.append("\n".join(current_section))
            if tool_sections:
                joined = "\n\n".join(tool_sections)
                snippet = joined[:2500] + (
                    "\n…[truncated]" if len(joined) > 2500 else ""
                )
                chunks.append(f"### `pyproject.toml` (tool sections)\n\n```toml\n{snippet}\n```")

    # Standalone tool configs (just list presence + first 60 lines each)
    for name in (".ruff.toml", "ruff.toml", "mypy.ini", "pyrightconfig.json", "tox.ini"):
        path = workdir / name
        if not path.is_file():
            continue
        try:
            body = path.read_text(errors="replace")
        except OSError:
            continue
        snippet = "\n".join(body.splitlines()[:60])
        chunks.append(f"### `{name}`\n\n```\n{snippet}\n```")

    # Makefile — list verification-flavored targets if any
    mk = workdir / "Makefile"
    if mk.is_file():
        try:
            body = mk.read_text(errors="replace")
        except OSError:
            body = ""
        if body:
            target_lines = [
                line for line in body.splitlines()
                if line and not line.startswith((" ", "\t", "#"))
                and ":" in line
            ]
            verify_targets = [
                line for line in target_lines
                if any(kw in line.split(":")[0].lower() for kw in
                       ("format", "lint", "typecheck", "type-check", "mypy",
                        "pyright", "test", "check", "sync"))
            ]
            if verify_targets:
                chunks.append(
                    "### `Makefile` (verification-relevant targets)\n\n```make\n"
                    + "\n".join(verify_targets) + "\n```"
                )
    return "\n\n".join(chunks)


def _detect_verification_stack(workdir: Path) -> tuple[str, list[str]]:
    """Detect package manager + verification commands from repo signals.

    Returns ``(package_manager, commands)``. Commands are listed in the
    order they should run. Empty list if no verification stack detected.
    """
    pkg_mgr = "pip"
    if (workdir / "uv.lock").is_file():
        pkg_mgr = "uv"
    elif (workdir / "poetry.lock").is_file():
        pkg_mgr = "poetry"
    elif (workdir / "Pipfile.lock").is_file():
        pkg_mgr = "pipenv"
    elif (workdir / "pyproject.toml").is_file():
        pkg_mgr = "pip+pyproject"

    commands: list[str] = []

    # 1. Makefile targets — most explicit signal
    mk = workdir / "Makefile"
    if mk.is_file():
        try:
            body = mk.read_text(errors="replace")
        except OSError:
            body = ""
        targets_present = {
            line.split(":")[0].strip()
            for line in body.splitlines()
            if line and not line.startswith((" ", "\t", "#")) and ":" in line
        }
        for target in ("format", "lint", "typecheck", "type-check", "tests", "test"):
            if target in targets_present:
                commands.append(f"make {target}")

    # 2. tox / nox orchestration
    if not commands:
        if (workdir / "tox.ini").is_file():
            commands.append("tox")
        elif (workdir / "noxfile.py").is_file():
            commands.append("nox")

    # 3. Direct invocation from pyproject.toml signals
    if not commands and (workdir / "pyproject.toml").is_file():
        try:
            body = (workdir / "pyproject.toml").read_text(errors="replace")
        except OSError:
            body = ""
        if "[tool.ruff" in body:
            commands.append("ruff format --check .")
            commands.append("ruff check .")
        elif "[tool.black" in body:
            commands.append("black --check .")
        if "[tool.mypy" in body:
            commands.append("mypy .")
        if (workdir / "pyrightconfig.json").is_file():
            commands.append("pyright")
        if "[tool.pytest" in body or "pytest" in body:
            commands.append("pytest")

    return pkg_mgr, commands


def _orient_verification_stack(workdir: Path) -> str:
    """Format detected verification stack as a markdown section.

    Returns "" when no commands AND no specific package-manager signal
    were detected (i.e. nothing useful to report). When commands are
    detected, format as a markdown list. When only the package manager
    is detected (no commands), report the package manager so the agent
    knows the dependency-install path.
    """
    pkg_mgr, commands = _detect_verification_stack(workdir)
    if not commands and pkg_mgr == "pip":
        # Default fallback with no commands — no useful signal to report.
        return ""
    lines = [f"Package manager: `{pkg_mgr}`"]
    if commands:
        lines.extend(["", "Detected verification commands (run in order):"])
        for cmd in commands:
            lines.append(f"  - `{cmd}`")
    return "\n".join(lines)


def _orient_nearby_files(workdir: Path, package: str, cap_files: int = 5) -> str:
    """List up to ``cap_files`` existing modules in the package root with first docstring line."""
    pkg_dir = workdir / package
    if not pkg_dir.is_dir():
        return ""
    py_files = sorted(pkg_dir.glob("*.py"))[:cap_files]
    if not py_files:
        return ""
    lines: list[str] = []
    for path in py_files:
        rel = path.relative_to(workdir).as_posix()
        first_lines = ""
        try:
            text = path.read_text(errors="replace")
            doc = ast.get_docstring(ast.parse(text)) or ""
            first_lines = doc.splitlines()[0] if doc else ""
        except (SyntaxError, OSError):
            pass
        if first_lines:
            lines.append(f"- `{rel}` — {first_lines[:90]}")
        else:
            lines.append(f"- `{rel}`")
    return "\n".join(lines)


def _orient_nearby_tests(workdir: Path, cap_files: int = 5) -> str:
    """List up to ``cap_files`` test files; include the first ~30 lines of one as a pattern sample."""
    tests_dir = workdir / "tests"
    if not tests_dir.is_dir():
        return ""
    test_files = sorted(tests_dir.rglob("test_*.py"))[:cap_files]
    if not test_files:
        return ""
    lines: list[str] = []
    lines.append(f"{len(test_files)} test file(s) listed:")
    for path in test_files:
        rel = path.relative_to(workdir).as_posix()
        lines.append(f"- `{rel}`")
    # Include a sample of the first test file's imports and one test fn
    sample = test_files[0]
    try:
        text = sample.read_text(errors="replace")
    except OSError:
        text = ""
    if text:
        sample_lines = text.splitlines()[:40]
        snippet = "\n".join(sample_lines)
        lines.append(
            f"\nSample pattern from `{sample.relative_to(workdir).as_posix()}`:\n```python\n{snippet}\n```"
        )
    return "\n".join(lines)


def _collect_repo_orientation(workdir: Path, target: Target, package: str) -> str:
    """Assemble the repo orientation content for ORIENTATION.md.

    Returns the formatted markdown body. Returns "" if no orientation
    content could be gathered (e.g. fresh repo with no conventions).
    """
    def _section(title: str, body: str) -> str:
        if not body.strip():
            return ""
        return f"## {title}\n\n{body}"

    blocks = {
        "contributor_guides_block": _section(
            "Contributor guides", _orient_contributor_guides(workdir)
        ),
        "pr_template_block": _section("PR template(s)", _orient_pr_template(workdir)),
        "recent_merged_prs_block": _section(
            "Recent merged PRs (title + body convention)",
            _orient_recent_merged_prs(target.repo) if target.repo else "",
        ),
        "tooling_config_block": _section(
            "Tooling and lint/type config", _orient_tooling_config(workdir)
        ),
        "verification_stack_block": _section(
            "Detected verification stack", _orient_verification_stack(workdir)
        ),
        "nearby_files_block": _section(
            f"Existing modules in `{package}/`", _orient_nearby_files(workdir, package)
        ),
        "nearby_tests_block": _section(
            "Existing tests (pattern corpus)", _orient_nearby_tests(workdir)
        ),
    }
    # If every section came up empty, return "" so the caller can skip
    # writing the file.
    if not any(v.strip() for v in blocks.values()):
        return ""
    return _ORIENTATION_MD_TEMPLATE.format(**blocks)


def _load_environments_md(workdir: Path, max_bytes: int = 4096) -> str:
    """Load a workflow-authored ENVIRONMENTS.md (or ENVIRONMENT.md), strip
    OKF/YAML frontmatter, cap size. Returns the markdown body or "" if no
    such file is present.

    Search order (first hit wins):
      1. $GITHUB_WORKSPACE/ENVIRONMENTS.md
      2. $GITHUB_WORKSPACE/ENVIRONMENT.md
      3. <workdir>/ENVIRONMENTS.md
      4. <workdir>/ENVIRONMENT.md

    The file is a workflow-authored surface: the workflow author writes it
    to describe what tooling they attached to this run (skills, MCP
    servers, custom search, private lint plugins, etc.). Outrider strips
    the YAML frontmatter (the agent only needs the body) and caps size
    to avoid a runaway prompt injection.
    """
    import re
    candidates: list[Path] = []
    ws = os.environ.get("GITHUB_WORKSPACE", "")
    if ws:
        candidates.append(Path(ws) / "ENVIRONMENTS.md")
        candidates.append(Path(ws) / "ENVIRONMENT.md")
    candidates.append(workdir / "ENVIRONMENTS.md")
    candidates.append(workdir / "ENVIRONMENT.md")

    for p in candidates:
        try:
            if not p.is_file():
                continue
            raw = p.read_text(errors="replace")
        except OSError:
            continue
        m = re.match(r"^---\r?\n.*?\r?\n---\r?\n(.*)$", raw, flags=re.DOTALL)
        body = (m.group(1) if m else raw).strip()
        if not body:
            continue
        encoded = body.encode("utf-8")
        truncated = False
        if len(encoded) > max_bytes:
            body = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()
            body += "\n\n... (truncated at {} bytes)".format(max_bytes)
            truncated = True
        log.info(
            "  ✓ workflow environment loaded: %s (%d bytes%s)",
            p, len(body.encode("utf-8")), " truncated" if truncated else "",
        )
        return body
    return ""


# ─── repo-intel: per-fork cross-run learning ───────────────────────────────
#
# `.remyx/repo_intel.yaml` on the fork's main branch carries cross-run
# learning accumulated from prior Outrider dispatches: confirmed landing
# zones (with the arxiv + mode that proved them), rejected mechanism
# shapes (with reason codes + caveats under which the rejection might
# not apply), coordination signals, and exploration budget. Threaded
# into the coding session via REPO_INTEL.md so the agent can extend
# confirmed landings instead of re-discovering them, and avoid known-
# rejected shapes unless a caveat applies.
#
# Opt-in via INPUT_MAINTAIN_STATE — never fails a run when the file is
# missing / malformed / unfetchable.

_REPO_INTEL_SCHEMA_VERSION = 1


def _load_fork_repo_intel_remote(target: "Target") -> dict | None:
    """Fetch ``.remyx/repo_intel.yaml`` from the fork's main branch via the
    GitHub Contents API — used by callers that don't have a cloned workdir
    yet (e.g. the audit/refine pass, which runs before ``prepare_workdir``).

    Same validation semantics as ``_load_fork_repo_intel``: returns a dict
    with ``schema_version == 1`` or None on any failure (missing file,
    malformed YAML, network error, PyYAML unavailable). Never raises.
    """
    token = _github_token()
    if not token:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{target.repo}/contents/.remyx/repo_intel.yaml",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.raw",
                "User-Agent": "outrider-repo-intel",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return None
    if not content.strip():
        return None
    try:
        import yaml
    except ImportError:
        log.warning("  ⚠ repo_intel remote load: PyYAML unavailable; skipping")
        return None
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("schema_version") != _REPO_INTEL_SCHEMA_VERSION:
        return None
    return parsed


def _load_fork_repo_intel(workdir: Path) -> dict | None:
    """Load ``.remyx/repo_intel.yaml`` from the fork.

    Tries a direct filesystem read of ``<workdir>/.remyx/repo_intel.yaml``
    first — under ``actions/checkout@v4`` the workdir is checked out at
    main's HEAD, so the file is present in the tree if the fork owner
    committed it. Falls back to ``git show origin/main:.remyx/repo_intel.yaml``
    for edge cases (workdir on a non-main branch, or a bare-clone setup).

    Returns a validated dict (schema_version == 1) or None when the file
    is absent, malformed, or unreadable — never raises.
    """
    if not workdir or not workdir.exists():
        return None

    content: str = ""
    intel_path = workdir / ".remyx" / "repo_intel.yaml"
    if intel_path.is_file():
        try:
            content = intel_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            content = ""

    if not content.strip():
        # Fallback: try git show origin/main (bare-clone or non-main-HEAD workdirs)
        try:
            result = subprocess.run(
                ["git", "show", "origin/main:.remyx/repo_intel.yaml"],
                cwd=workdir, capture_output=True, text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        content = result.stdout

    try:
        import yaml
    except ImportError:
        # PyYAML must be installed in the action's Python env
        # (see action.yml's pip install step). Bail loudly rather than
        # silently masking as a parse error.
        log.warning(
            "  ⚠ .remyx/repo_intel.yaml present but PyYAML not installed; "
            "action.yml must include `pip install pyyaml`; skipping"
        )
        return None
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as e:
        log.warning("  ⚠ .remyx/repo_intel.yaml YAML parse failed: %s; skipping", e)
        return None
    if not isinstance(parsed, dict):
        return None
    version = parsed.get("schema_version")
    if version != _REPO_INTEL_SCHEMA_VERSION:
        log.warning(
            "  ⚠ .remyx/repo_intel.yaml has schema_version=%r; expected %d; skipping",
            version, _REPO_INTEL_SCHEMA_VERSION,
        )
        return None
    return parsed


def _repo_intel_iso_now() -> str:
    """Timezone-aware UTC ISO-8601 timestamp for repo_intel entries.

    Kept as a helper so tests can monkey-patch it deterministically.
    """
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dominant_directory(paths: list[str]) -> str:
    """Common directory prefix across a list of changed paths.

    Filters out ``test/`` and ``tests/`` prefixes before scanning (the test
    file is a byproduct, not the landing zone). Returns the DIR most
    frequently touched, with a trailing slash. Empty string when no
    non-test paths carry a directory.
    """
    if not paths:
        return ""
    src_paths = [
        p for p in paths
        if not p.startswith(("test/", "tests/"))
    ]
    scan = src_paths if src_paths else paths
    dirs: list[str] = []
    for p in scan:
        if "/" not in p:
            continue
        # Trim to directory (strip filename)
        d = p.rsplit("/", 1)[0] + "/"
        dirs.append(d)
    if not dirs:
        return ""
    from collections import Counter
    return Counter(dirs).most_common(1)[0][0]


_LANDING_SHAPE_PATTERNS: list[tuple[str, str]] = [
    # Matching cue → shape_tag; scanned against a lowercased composite of
    # honest_summary + call_site + reframed_insight
    ("library-shape", "library-shape-public-api"),
    ("public api", "library-shape-public-api"),
    ("__all__", "library-shape-public-api"),
    ("decorator", "decorator-hook"),
    ("@tool", "decorator-hook"),
    ("middleware", "middleware"),
    ("callback", "callback"),
    ("register_", "registry-add"),
    ("registry", "registry-add"),
]


def _extract_shape_tags_from_review(self_review: dict) -> list[str]:
    """Heuristic shape-tag inference from self_review content.

    Scans honest_summary + call_site + reframed_insight for known cues
    and returns matching shape tags. Empty list falls back to
    ``["unclassified"]`` so the entry is still merge-able but reviewers
    know to categorize by hand.
    """
    if not self_review:
        return ["unclassified"]
    corpus = " ".join([
        (self_review.get("honest_summary") or ""),
        (self_review.get("call_site") or ""),
        (self_review.get("reframed_insight") or ""),
    ]).lower()
    tags: list[str] = []
    for cue, tag in _LANDING_SHAPE_PATTERNS:
        if cue in corpus and tag not in tags:
            tags.append(tag)
    return tags or ["unclassified"]


_REJECTION_SHAPE_PATTERNS: list[tuple[str, str, str]] = [
    # (cue_substring, shape_tag, reason_code) — scanned against lowercased
    # preflight reasoning. First match wins; falls back to (None, None) so
    # the observation lands untagged for human categorization.
    ("survey", "survey-or-analysis-paper", "not_a_method_paper"),
    ("classification framework", "survey-or-analysis-paper", "not_a_method_paper"),
    ("not a method paper", "survey-or-analysis-paper", "not_a_method_paper"),
    ("http-serving", "http-serving-framework", "domain_mismatch"),
    ("http serving", "http-serving-framework", "domain_mismatch"),
    ("reranking", "reranker-decision-layer", "no_public_middleware_surface"),
    ("reranker", "reranker-decision-layer", "no_public_middleware_surface"),
    ("benchmark harness", "benchmark-paper", "benchmark_shape"),
    ("training infrastructure", "training-infra", "not_runtime_shape"),
    ("distributed training", "training-infra", "not_runtime_shape"),
]


def _classify_rejection_shape(preflight_rationale: str) -> tuple[str | None, str | None]:
    """Heuristic-classify a preflight rejection rationale into a
    ``(shape_tag, reason_code)`` pair. Returns ``(None, None)`` when no
    pattern matches — the observation still gets recorded, just untagged
    for human review.
    """
    text = (preflight_rationale or "").lower()
    for cue, tag, code in _REJECTION_SHAPE_PATTERNS:
        if cue in text:
            return tag, code
    return None, None


def _merge_landing_zone(intel: dict, path: str, shape_tags: list[str], entry: dict) -> bool:
    """Idempotent merge of a confirmed landing-zone entry.

    - If a zone with the same ``path`` exists: merge shape_tags (union),
      update-or-append ``confirmed_by`` by arxiv, bump ``last_observed``.
    - Otherwise: append a new zone.

    Returns True if the intel dict was mutated.
    """
    if not path:
        return False
    zones = intel.setdefault("observed_landing_zones", [])
    now = _repo_intel_iso_now()
    entry = dict(entry)  # copy so caller's dict isn't mutated
    entry.setdefault("timestamp", now)
    arxiv = entry.get("arxiv")

    for z in zones:
        if z.get("path") == path:
            existing_tags = set(z.get("shape_tags") or [])
            merged = existing_tags | set(shape_tags)
            merged.discard("unclassified") if len(merged) > 1 else None
            z["shape_tags"] = sorted(merged)
            confirmed = z.setdefault("confirmed_by", [])
            for cb in confirmed:
                if cb.get("arxiv") == arxiv:
                    cb.update(entry)
                    z["last_observed"] = now
                    return True
            confirmed.append(entry)
            z["last_observed"] = now
            return True

    zones.append({
        "path": path,
        "shape_tags": sorted(set(shape_tags)),
        "confirmed_by": [entry],
        "last_observed": now,
    })
    return True


def _merge_rejected_shape(
    intel: dict,
    shape_tag: str | None,
    reason_code: str | None,
    reason_summary: str,
    arxiv: str,
) -> bool:
    """Idempotent merge of a rejected-shape observation.

    Matches by ``shape_tag`` when present; otherwise groups by the first
    80 chars of ``reason_summary`` (rough clustering for untagged
    observations). Same-arxiv duplicates update the timestamp only.

    Returns True if the intel dict was mutated.
    """
    if not arxiv:
        return False
    rejected = intel.setdefault("rejected_shapes", [])
    now = _repo_intel_iso_now()
    match_key = shape_tag or f"untagged:{(reason_summary or '')[:80].strip().lower()}"

    for r in rejected:
        r_key = r.get("shape_tag") or (
            f"untagged:{(r.get('reason_summary') or '')[:80].strip().lower()}"
        )
        if r_key == match_key:
            observed = r.setdefault("observed", [])
            for o in observed:
                if o.get("arxiv") == arxiv:
                    o["timestamp"] = now
                    return True
            observed.append({"arxiv": arxiv, "timestamp": now})
            return True

    rejected.append({
        "shape_tag": shape_tag,
        "reason_code": reason_code,
        "reason_summary": (reason_summary or "")[:400],
        "observed": [{"arxiv": arxiv, "timestamp": now}],
    })
    return True


def _fetch_branch_files_changed(target: "Target", branch: str) -> list[str]:
    """List filenames touched by the branch tip's most-recent commit.

    Uses the fork's HEAD commit metadata via the GitHub API — this is
    the branch that Outrider just pushed, so the tip is our own bot
    commit. Returns [] on any failure (never blocks the write path).
    """
    if not branch:
        return []
    token = _github_token()
    if not token:
        return []
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{target.repo}/commits/{branch}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "outrider-repo-intel",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return []
    files = data.get("files") or []
    return [f.get("filename", "") for f in files if f.get("filename")]


def _put_fork_repo_intel(target: "Target", intel: dict) -> bool:
    """Write the updated intel dict to ``.remyx/repo_intel.yaml`` on the
    fork's main branch via the GitHub Contents API.

    Retries once on 409 (concurrent modification) by re-fetching the
    current SHA + re-attempting. Never raises; returns False on any
    failure. The next dispatch's write catches the missed observation
    (idempotent merge).
    """
    try:
        import yaml
    except ImportError:
        log.warning("  ⚠ repo_intel write skipped: PyYAML unavailable")
        return False

    token = _github_token()
    if not token:
        log.warning("  ⚠ repo_intel write skipped: no GitHub token")
        return False

    body_yaml = yaml.safe_dump(intel, sort_keys=False, default_flow_style=False)
    content_b64 = base64.b64encode(body_yaml.encode("utf-8")).decode("ascii")
    url = f"https://api.github.com/repos/{target.repo}/contents/.remyx/repo_intel.yaml"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "outrider-repo-intel",
    }

    for attempt in range(3):
        # Fetch current SHA (or accept "file does not exist" 404 to seed fresh)
        sha: str | None = None
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                current = json.loads(resp.read())
                sha = current.get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log.warning(f"  ⚠ repo_intel write: GET failed {e.code}; skipping")
                return False
        except (urllib.error.URLError, OSError):
            log.warning("  ⚠ repo_intel write: GET network error; skipping")
            return False

        payload = {
            "message": "chore(repo-intel): update from Outrider dispatch",
            "content": content_b64,
            "branch": "main",
        }
        if sha:
            payload["sha"] = sha

        put_req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={**headers, "Content-Type": "application/json"},
            method="PUT",
        )
        try:
            urllib.request.urlopen(put_req, timeout=20)
            log.info(
                "  ✓ repo_intel: wrote %d bytes to %s main",
                len(body_yaml), target.repo,
            )
            return True
        except urllib.error.HTTPError as e:
            if e.code == 409 and attempt < 2:
                log.info(
                    "  → repo_intel write conflict on attempt %d; refetching + retrying",
                    attempt + 1,
                )
                continue
            log.warning(f"  ⚠ repo_intel write failed with {e.code}")
            return False
        except (urllib.error.URLError, OSError):
            log.warning("  ⚠ repo_intel write network error")
            return False
    return False


def _update_fork_repo_intel(
    target: "Target", result: dict, workdir: Path,
) -> None:
    """Post-terminal-state hook: extract observations from ``result``,
    merge into repo_intel, PUT back to the fork's main branch.

    Fires only when ``INPUT_MAINTAIN_STATE`` is truthy. Silent no-op
    otherwise. Never fails a run — any error is logged and swallowed
    so the terminal state that already succeeded isn't disturbed.

    Observations extracted:

    - ``branch_pushed_no_pr`` / ``pr_opened_draft`` / ``pr_opened`` →
      confirmed landing zone (path from files-changed; shape_tags from
      self_review heuristics; confirmed_by entry with arxiv, mode,
      branch/pr, call_site_specifics).
    - ``lead_captured_no_issue`` / ``issue_opened_preflight`` →
      rejected shape (shape_tag + reason_code from preflight rationale
      pattern-match, else untagged; reason_summary carries the raw
      rationale for human categorization).
    """
    if not (os.environ.get("INPUT_MAINTAIN_STATE") or "").strip().lower() in (
        "true", "1", "yes",
    ):
        return

    arxiv = result.get("arxiv") or ""
    status = result.get("status") or ""
    if not arxiv or not status:
        return

    intel = _load_fork_repo_intel(workdir) or {
        "schema_version": 1,
        "fork": target.repo,
        "observed_landing_zones": [],
        "rejected_shapes": [],
        "coordination_signals": [],
    }
    mutated = False

    if status in ("branch_pushed_no_pr", "pr_opened_draft", "pr_opened"):
        self_review = result.get("self_review") or {}
        mode = self_review.get("mode_cited", "")
        branch = result.get("branch") or ""
        pr_number = result.get("pr_number")
        call_site_specifics = (self_review.get("call_site") or "")[:200]

        files = _fetch_branch_files_changed(target, branch) if branch else []
        path = _dominant_directory(files)
        if not path:
            # Fallback: parse a slash-form path out of call_site text
            m = re.search(r"([\w./-]+/)[\w.-]+\.py", call_site_specifics)
            if m:
                path = m.group(1)

        if path:
            entry: dict = {"arxiv": arxiv, "mode": mode,
                           "call_site_specifics": call_site_specifics}
            if branch:
                entry["branch"] = branch
            if pr_number:
                entry["pr"] = pr_number
            shape_tags = _extract_shape_tags_from_review(self_review)
            if _merge_landing_zone(intel, path, shape_tags, entry):
                mutated = True
                log.info(
                    "  → repo_intel: landing zone recorded (path=%s, tags=%s, arxiv=%s)",
                    path, shape_tags, arxiv,
                )

    elif status in ("lead_captured_no_issue", "issue_opened_preflight"):
        # Preflight rationale often lives under different keys depending
        # on which path routed here — check the common ones.
        rationale = (
            result.get("preflight_reasoning")
            or result.get("preflight_rationale")
            or (result.get("preflight") or {}).get("reasoning")
            or (result.get("preflight") or {}).get("rationale")
            or ""
        )
        shape_tag, reason_code = _classify_rejection_shape(rationale)
        if _merge_rejected_shape(intel, shape_tag, reason_code, rationale, arxiv):
            mutated = True
            log.info(
                "  → repo_intel: rejection recorded (shape=%s, arxiv=%s)",
                shape_tag or "(untagged)", arxiv,
            )

    if not mutated:
        return

    intel["last_updated"] = _repo_intel_iso_now()
    _put_fork_repo_intel(target, intel)


def _extract_dispatched_arxivs(intel: dict | None) -> list[dict]:
    """Flatten repo_intel's ``observed_landing_zones.confirmed_by`` into a
    de-duplicated list of dispatched arxivs with their prior landing
    metadata. Returns [] when intel is None or has no landings.

    Each entry: ``{arxiv, path, mode, branch|pr, timestamp}``. Multiple
    landings for the same arxiv (e.g. Mode 3 first, then Mode 2 refinement)
    produce multiple entries so the selection Claude can see the full
    prior scope for a re-pick justification.
    """
    if not intel:
        return []
    zones = intel.get("observed_landing_zones") or []
    dispatched: list[dict] = []
    for z in zones:
        path = z.get("path", "?")
        for cb in z.get("confirmed_by") or []:
            entry = {
                "arxiv": cb.get("arxiv", "?"),
                "path": path,
                "mode": cb.get("mode", "(unknown mode)"),
                "timestamp": cb.get("timestamp", ""),
            }
            if cb.get("pr"):
                entry["anchor"] = f"PR #{cb['pr']}"
            elif cb.get("branch"):
                entry["anchor"] = f"branch:{cb['branch']}"
            else:
                entry["anchor"] = ""
            dispatched.append(entry)
    return dispatched


def _render_already_dispatched_for_selection(intel: dict | None) -> str:
    """Render the "already-dispatched on this fork" prompt section for
    the selection pass. Empty string when intel is None or has no
    landings (the selection prompt then behaves as before).
    """
    dispatched = _extract_dispatched_arxivs(intel)
    if not dispatched:
        return ""
    lines = [
        "",
        "**Already-dispatched arxivs on this fork** — a prior Outrider run",
        "has already produced a branch or PR for each of the following. Do",
        "NOT pick one of these unless the new dispatch would MATERIALLY",
        "compound on the prior landing. Valid reasons to re-pick:",
        "",
        "  - Different mode citation (prior was Mode 3, new run targets Mode 2)",
        "  - Different call-site scope (prior landed at path/A, new run at path/B)",
        "  - Incorporates a coordination signal absent from the prior landing",
        "",
        "Set `is_re_pick: true` AND provide `re_pick_justification` when you",
        "pick a prior-dispatched arxiv. Otherwise, prefer a novel candidate —",
        "duplicate work wastes budget.",
        "",
    ]
    for d in dispatched:
        anchor = f" · {d['anchor']}" if d.get("anchor") else ""
        lines.append(
            f"  - arxiv:{d['arxiv']} — path `{d['path']}` · {d['mode']}{anchor}"
        )
    lines.append("")
    return "\n".join(lines)


def _render_repo_intel_for_selection(intel: dict | None) -> str:
    """Compact prompt-inline rendering of repo_intel for the selection pass.

    Terser than ``_render_repo_intel_md`` — the selection prompt is long
    already; this block adds just enough structure for Claude to reason
    about landing-zone priors + rejected-shape caveats + exploration
    budget without ballooning the token budget.

    Returns "" when intel is None or empty; the selection prompt uses the
    empty-string form as a no-op — the ``__REPO_INTEL__`` placeholder is
    always present in the template, but resolves to nothing when the
    fork has no cross-run learning yet.
    """
    if not intel:
        return ""

    lines: list[str] = [
        "",
        "**This fork has accumulated cross-run learning from prior Outrider",
        "dispatches.** Use it as PRIORS (not filters) when picking a candidate —",
        "exploration budget is respected.",
        "",
    ]

    zones = intel.get("observed_landing_zones") or []
    if zones:
        lines.append("**Confirmed landing zones** — extending these = lower risk:")
        for z in zones:
            path = z.get("path", "?")
            tags = ", ".join(z.get("shape_tags") or []) or "(unspecified)"
            recent = z.get("confirmed_by") or []
            recent_str = "; ".join(
                f"arxiv:{cb.get('arxiv', '?')} ({cb.get('mode', '?')})"
                for cb in recent[:3]
            )
            lines.append(f"- `{path}` — shape: {tags}"
                         + (f"; recent: {recent_str}" if recent_str else ""))
        lines.append("")

    rejected = intel.get("rejected_shapes") or []
    if rejected:
        lines.append("**Rejected shapes** — avoid unless a caveat applies:")
        for r in rejected:
            tag = r.get("shape_tag", "?")
            summary = (r.get("reason_summary") or r.get("reason_code") or "").strip()
            lines.append(f"- `{tag}` — {summary}")
            caveats = r.get("when_this_penalty_should_NOT_apply") or []
            for c in caveats[:3]:
                lines.append(f"  - Caveat: {c}")
        lines.append("")

    exp = intel.get("exploration_budget") or {}
    frac = exp.get("novel_shape_fraction")
    if frac:
        lines.append(
            f"**Exploration budget**: ~{float(frac) * 100:.0f}% of dispatches "
            "allocated to novel shapes. Bias toward confirmed zones for the "
            "remainder but don't hard-filter novel candidates — noting "
            "novel picks in your reasoning is fine."
        )
        lines.append("")

    lines.append(
        "When picking, note in your `reasoning` field whether the pick "
        "aligns with a confirmed zone, is a novel-shape exploration, or "
        "exercises a rejected-shape caveat."
    )
    lines.append("")
    return "\n".join(lines)


def _render_repo_intel_md(intel: dict) -> str:
    """Render a validated repo_intel dict as ``REPO_INTEL.md`` markdown."""
    lines = [
        "---",
        "type: repo_intel",
        f"schema_version: {intel.get('schema_version', 1)}",
        f"fork: {intel.get('fork', '')}",
        f"last_updated: {intel.get('last_updated', '')}",
        "---",
        "",
        "# Cross-run learning for this fork",
        "",
        "This file records what has landed on this fork across prior Outrider",
        "dispatches, what shapes have been rejected, and coordination signals",
        "accumulated across runs. Consult it before picking a call site —",
        "extending a confirmed landing zone is cheaper and less risky than",
        "discovering a new one, and proposing a rejected shape without",
        "matching a caveat is a known cost.",
        "",
    ]

    zones = intel.get("observed_landing_zones") or []
    if zones:
        lines.append("## Confirmed landing zones — extend these where the mechanism fits")
        lines.append("")
        for z in zones:
            path = z.get("path", "?")
            tags = ", ".join(z.get("shape_tags") or []) or "(unspecified)"
            lines.append(f"- **`{path}`** — shape: `{tags}`")
            for cb in z.get("confirmed_by") or []:
                arxiv = cb.get("arxiv", "?")
                mode = cb.get("mode", "?")
                anchor = cb.get("branch") or (f"PR #{cb['pr']}" if "pr" in cb else "?")
                lines.append(f"  - `arxiv:{arxiv}` · {mode} · {anchor}")
                specifics = cb.get("call_site_specifics")
                if specifics:
                    lines.append(f"    — {specifics}")
        lines.append("")

    rejected = intel.get("rejected_shapes") or []
    if rejected:
        lines.append("## Rejected shapes — avoid unless a caveat applies")
        lines.append("")
        for r in rejected:
            tag = r.get("shape_tag", "?")
            summary = r.get("reason_summary") or r.get("reason_code") or ""
            lines.append(f"- **`{tag}`** — {summary}")
            caveats = r.get("when_this_penalty_should_NOT_apply") or []
            if caveats:
                lines.append("  - This rejection does NOT apply if:")
                for c in caveats:
                    lines.append(f"    - {c}")
            observed = r.get("observed") or []
            if observed:
                obs_str = ", ".join(
                    f"`arxiv:{o.get('arxiv', '?')}`" for o in observed[:5]
                )
                lines.append(f"  - Observed on: {obs_str}")
        lines.append("")

    signals = intel.get("coordination_signals") or []
    if signals:
        lines.append("## Coordination signals — external context useful across dispatches")
        lines.append("")
        for s in signals:
            src = s.get("source", "?")
            topics = ", ".join(s.get("topic_tags") or [])
            suffix = f" ({topics})" if topics else ""
            lines.append(f"- {src}{suffix}")
        lines.append("")

    exp = intel.get("exploration_budget") or {}
    frac = exp.get("novel_shape_fraction")
    if frac:
        lines.append(
            f"## Exploration budget: ~{float(frac) * 100:.0f}% of dispatches "
            "are allocated to novel shapes not yet in the observed map."
        )
        lines.append("")

    modes = intel.get("mode_history") or {}
    if modes:
        totals = ", ".join(
            f"{k.replace('_count', '').replace('mode_', 'Mode ')}={v}"
            for k, v in modes.items() if k.startswith("mode_") and k.endswith("_count")
        )
        if totals:
            lines.append(f"## Mode history: {totals}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ─── selection reasoning path verification ──────────────────────────────────
#
# The selection agent sometimes cites paths in its reasoning that don't
# exist in the target repo — e.g. grepping `unsloth/` on a fork that only
# has `studio/`, then concluding "no such code exists." That's a
# confidently-wrong reasoning failure mode that's hard to catch after the
# fact from log inspection. Extracting cited paths from the reasoning and
# checking each against the workdir gives an operator a concrete "0/2
# cited paths verified" signal in the step summary before trusting the
# verdict.

_PATH_TOKEN_RE = re.compile(r"[a-zA-Z_][\w./\-]*[\w/]")

_PATH_TOKEN_URL_SUBSTRINGS = (
    "http://", "https://", "github.com/", "arxiv.org/", "huggingface.co/",
)


def _extract_referenced_paths(text: str) -> list[str]:
    """Extract candidate filesystem paths mentioned in reasoning prose.

    Matches tokens containing at least one slash and starting with a
    letter/underscore — the shape of a Python source file (``foo/bar.py``)
    or a directory reference (``foo/bar/``). Filters out:
      - single-slash tokens without a `.py` suffix or trailing slash
        (github-repo shapes like ``org/repo``)
      - URLs and paper-index references (``github.com/...``,
        ``arxiv.org/...``, ``huggingface.co/...``)

    Best-effort: may miss references embedded in prose without slashes,
    may include false positives that the workdir verification then flags
    as ``not_found``. That's fine — this is a diagnostic signal, not a
    gate.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _PATH_TOKEN_RE.finditer(text):
        path = match.group(0)
        if "/" not in path:
            continue
        lower = path.lower()
        if any(host in lower for host in _PATH_TOKEN_URL_SUBSTRINGS):
            continue
        # Skip github-repo shape: exactly one slash, no `.py` suffix, no
        # trailing slash — that's `org/repo`, not a repo-internal path.
        if path.count("/") == 1 and not path.endswith(".py") and not path.endswith("/"):
            continue
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def _verify_paths_in_workdir(workdir: Path, paths: list[str]) -> dict:
    """Split cited paths into ``verified`` (exist in workdir) and
    ``not_found``. Returns a dict with the two lists and the raw ``cited``
    input for provenance.
    """
    verified: list[str] = []
    not_found: list[str] = []
    for path in paths:
        candidate = workdir / path.rstrip("/")
        if candidate.exists():
            verified.append(path)
        else:
            not_found.append(path)
    return {"cited": list(paths), "verified": verified, "not_found": not_found}


def _check_selection_paths(workdir: Path, reasoning: str) -> dict:
    """Extract path claims from a selection-reasoning string and verify
    each against the workdir. Convenience wrapper around
    ``_extract_referenced_paths`` + ``_verify_paths_in_workdir``.
    """
    return _verify_paths_in_workdir(
        workdir, _extract_referenced_paths(reasoning)
    )


def write_spec_bundle(
    workdir: Path, target: Target, rec: Recommendation, package: str,
    selection_note: str = "",
    env_body: str | None = None,
) -> None:
    """Write the .remyx-recommendation/ bundle that Claude Code reads as its brief.

    ``selection_note`` is the candidate-selection rationale: why this
    paper was picked from the pool as the most implementable against THIS
    repo, including the call sites it targets. It's written into the spec
    so BOTH the pre-flight routing pass and the implementer evaluate the
    same scoped framing the selection pass reasoned about — without it,
    pre-flight re-derives PR-vs-Issue from the abstract alone and can
    contradict the selection (e.g. judging a benchmark paper's maximal
    form needs infra the repo lacks, while the selection identified an
    implementable subset).
    """
    bundle = workdir / BUNDLE_DIR_NAME
    bundle.mkdir(exist_ok=True)

    interest_block = (
        rec.interest_context
        if rec.interest_context
        else "(no research-focus body configured for this interest on engine.remyx.ai)"
    )
    note = (selection_note or "").strip()
    selection_block = (
        note
        if note and not note.startswith("(")
        else "(no separate selection rationale — this was the top-ranked candidate)"
    )
    # LEAD-to-PR override: when INPUT_LEAD_CONTENT is set, replace the paper's
    # suggested_experiment with the LEAD's verbatim scope. This turns a
    # dispatch into a scoped-experiment implementation rather than a
    # full-paper-integration attempt — the preflight + implementer both
    # reason against the LEAD's scoped framing, which is exactly what the
    # human who chose to convert this LEAD to a PR wants shipped.
    #
    # URLs matching a tool-plane connector's owned domain get pre-resolved
    # before substitution. Linear URLs fetch their issue body via the Linear
    # connector using LINEAR_API_KEY; unknown URLs / raw text fall through
    # unchanged (backward compat with the classic WebFetch flow).
    from tool_plane.lead_content_routing import resolve_lead_content

    lead_content_raw = (os.environ.get("INPUT_LEAD_CONTENT") or "").strip()
    lead_content_override, lead_tool_response = resolve_lead_content(lead_content_raw)
    if lead_tool_response is not None:
        log.info(
            f"  → lead-content routed via {lead_tool_response.connector} "
            f"connector: status={lead_tool_response.status} "
            f"latency={lead_tool_response.latency_ms:.0f}ms"
        )
    effective_experiment = lead_content_override or (rec.suggested_experiment or "(none)")
    (bundle / "SPEC.md").write_text(_SPEC_MD_TEMPLATE.format(
        paper_title=rec.paper_title,
        arxiv_id=rec.arxiv_id,
        tier=rec.tier,
        relevance_score=rec.relevance_score,
        interest_name=rec.interest_name or "(unnamed interest)",
        interest_context_block=interest_block,
        reasoning=rec.reasoning or "(no reasoning provided)",
        selection_block=selection_block,
        suggested_experiment=effective_experiment,
        paper_abstract=rec.paper_abstract or "(abstract unavailable)",
    ))

    (bundle / "PAPER.md").write_text(_PAPER_MD_TEMPLATE.format(
        paper_title=rec.paper_title,
        arxiv_id=rec.arxiv_id,
        paper_abstract=rec.paper_abstract,
    ))

    # CONTEXT.md — team's shipping history bullets,
    # fetched from the research-interests endpoint. Skipped entirely
    # when no history is linked, so INVOCATION.md's "if Remyx returned
    # any" caveat continues to hold.
    if rec.experiment_history:
        (bundle / "CONTEXT.md").write_text(_CONTEXT_MD_TEMPLATE.format(
            experiment_history=rec.experiment_history,
        ))

    allowlist = effective_allowlist(target, package)
    (bundle / "GUARDRAILS.md").write_text(_GUARDRAILS_MD_TEMPLATE.format(
        allowlist="\n".join(allowlist),
        blocked="\n".join(ALWAYS_BLOCKED),
    ))

    # ENVIRONMENT.md — workflow-authored tooling hints picked up from
    # $GITHUB_WORKSPACE (or workdir) if the workflow author left an
    # ENVIRONMENTS.md / ENVIRONMENT.md file describing skills, MCP
    # servers, or other agent-usable tooling attached to this run.
    # Only written when non-empty; the invocation's file list refs it
    # only when it's present.
    # Accept a pre-loaded env_body when the caller has already loaded it —
    # the selection pass injects the same body upstream, so avoid a second
    # load. None means "not pre-loaded, load now."
    environment_body = env_body if env_body is not None else _load_environments_md(workdir)
    environment_file_ref = ""
    if environment_body:
        (bundle / "ENVIRONMENT.md").write_text(_ENVIRONMENT_MD_TEMPLATE.format(
            environment_body=environment_body,
        ))
        environment_file_ref = _ENVIRONMENT_FILE_REF_TEMPLATE

    # Research-findings ref: filled when the staged-synthesis research phase
    # ran and produced web_findings.json in the briefing bundle dir. Coding session
    # reads it as another bundle-adjacent context file.
    research_findings_ref = (
        _RESEARCH_FINDINGS_REF_TEMPLATE
        if (workdir / BUNDLE_DIR_NAME / "web_findings.json").exists()
        else ""
    )

    # Repo-intel: per-fork cross-run learning. Opt-in via INPUT_MAINTAIN_STATE
    # — loads .remyx/repo_intel.yaml from the fork's main branch and renders
    # it as REPO_INTEL.md for the coding session. Never fails the run when the
    # file is missing / malformed / unreadable.
    repo_intel_ref = ""
    maintain_state = (
        (os.environ.get("INPUT_MAINTAIN_STATE") or "").strip().lower()
        in ("true", "1", "yes")
    )
    if maintain_state:
        intel = _load_fork_repo_intel(workdir)
        if intel is not None:
            (bundle / "REPO_INTEL.md").write_text(_render_repo_intel_md(intel))
            repo_intel_ref = _REPO_INTEL_REF_TEMPLATE
            log.info(
                "  ✓ repo_intel loaded: %d observed_landing_zones, "
                "%d rejected_shapes, %d coordination_signals",
                len(intel.get("observed_landing_zones") or []),
                len(intel.get("rejected_shapes") or []),
                len(intel.get("coordination_signals") or []),
            )
        else:
            log.info("  → maintain-state=true but no .remyx/repo_intel.yaml on origin/main; continuing without")

    (bundle / "INVOCATION.md").write_text(_INVOCATION_MD_TEMPLATE.format(
        package=package,
        attribution_url=CANONICAL_ATTRIBUTION_URL,
        issue_fallback_filename=ISSUE_FALLBACK_FILENAME,
        environment_file_ref=environment_file_ref,
        research_findings_ref=research_findings_ref,
        repo_intel_ref=repo_intel_ref,
    ))

    # ORIENTATION.md — target repo's contributor guides, PR template, recent
    # merged-PR conventions, lint/type config, detected verification stack,
    # and a few sample nearby files/tests. Pre-read so the agent doesn't
    # broad-explore the repo to rediscover conventions. Skipped entirely
    # when no orientation content can be gathered.
    orientation_body = _collect_repo_orientation(workdir, target, package)
    if orientation_body:
        (bundle / "ORIENTATION.md").write_text(orientation_body)


# ─── Claude Code invocation ────────────────────────────────────────────────


# Per-run token/cost totals, accumulated across every `claude` call in a
# run (pre-flight, selection, implementation, self-review) and surfaced in
# the RUN SUMMARY + $GITHUB_OUTPUT.
_RUN_COST = {
    "cost_usd": 0.0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_input_tokens": 0,
    "num_turns": 0,
    "claude_calls": 0,
    # Set by _record_claude_usage on each call so the result dict can
    # surface them. "Anthropic" by default; "z.ai (GLM)" / "AWS Bedrock"
    # / etc. when ANTHROPIC_BASE_URL routes elsewhere. cost_basis is
    # "backend_rate_table" when we computed cost from a known per-backend
    # rate card, or "claude_code_envelope" when we trusted the CLI's
    # total_cost_usd field (correct for Anthropic, approximate for
    # unknown backends).
    "model_backend": "Anthropic",
    "cost_basis": "claude_code_envelope",
    # Number of `--output-format json` envelopes that parsed cleanly,
    # were not error envelopes, but carried no input/output token counts.
    # Observed against some non-Anthropic backends where the CLI's
    # terminal envelope occasionally drops the `usage` block on
    # otherwise-successful responses. When > 0 the run's token totals
    # are an under-count.
    "envelopes_without_usage": 0,
}

# Refine queries the audit pass actually executed this run, including ones
# that returned zero new candidates (those are signal too — "explored, no
# hits"). Run-scoped like _RUN_COST; surfaced on the result dict so the
# weekly summary can aggregate themes across runs.
_RUN_REFINE_QUERIES: list[str] = []


def _reset_run_cost() -> None:
    _RUN_COST.update(
        cost_usd=0.0, input_tokens=0, output_tokens=0,
        cache_read_input_tokens=0, num_turns=0, claude_calls=0,
        model_backend="Anthropic", cost_basis="claude_code_envelope",
        envelopes_without_usage=0,
    )
    _RUN_REFINE_QUERIES.clear()
    _BOT_TOKEN.update(attempted=False, token="", permissions={})


# ── Per-backend pricing for cost telemetry ─────────────────────────────────
#
# When ``ANTHROPIC_BASE_URL`` routes Claude Code at a non-Anthropic backend
# (z.ai / GLM, Bedrock, on-prem proxies), the CLI's ``total_cost_usd`` field
# uses its built-in Anthropic-rate table — wrong by a constant factor for
# any other backend. This table lets us override with the backend's actual
# rate card when the URL matches a known host substring.
#
# Rates are USD per million tokens: ``(input, output, cache_read)``. Update
# as providers publish new rates. Backends not in the table fall back to
# the CLI's ``total_cost_usd`` (with a "may be approximate" note in the
# step summary so the operator knows the figure isn't authoritative for
# their backend).
_BACKEND_RATES: dict[str, tuple[float, float, float]] = {
    # z.ai GLM (Anthropic-Messages-compat endpoint) — PAYG rates for the
    # default GLM-4.6 routing. Users on the GLM Coding Plan subscription
    # don't pay per-token, but the per-token estimate stays useful as a
    # "what would this cost outside the subscription" indicator.
    "api.z.ai": (0.60, 2.20, 0.06),
}


def _validate_claude_auth_env() -> tuple[bool, list[str]]:
    """Pre-flight check on the Claude Code subprocess auth env.

    Catches the common misconfigurations that otherwise surface as
    opaque HTTP 401s from the agent CLI, costing the operator a full
    run's worth of debugging:

    - Missing auth env var for the configured backend.
    - Truncated / placeholder secret values: literal ``"-"`` (the
      ``gh secret set --body -`` stdin-disconnect ambiguity), empty,
      length below 8 chars (any plausible API key is longer).
    - Leading / trailing whitespace from copy-paste — warns and uses
      the stripped value rather than failing.
    - Both ``ANTHROPIC_API_KEY`` and ``ANTHROPIC_AUTH_TOKEN`` set
      non-empty when a non-default backend is configured. Claude Code
      prefers the ``x-api-key`` (API_KEY) path; non-Anthropic backends
      usually reject it. Warns rather than fails — the configured
      AUTH_TOKEN may still be the intended one, but the customer
      almost always has a workflow bug if both are set.

    Returns ``(ok, warnings)`` — ``ok=False`` is a hard fail and the
    caller should exit non-zero; warnings are logged but non-fatal.
    Values are never echoed in any returned message or log line; the
    diagnostic carries length + an 8-char SHA-256 prefix only.
    """
    import hashlib

    def _shape(name: str, val: str) -> str:
        if not val:
            return f"{name}=(empty)"
        sha8 = hashlib.sha256(val.encode()).hexdigest()[:8]
        return f"{name} length={len(val)} sha8={sha8}"

    warnings: list[str] = []
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")

    non_default = bool(base_url) and "api.anthropic.com" not in base_url
    if non_default:
        primary_name = "ANTHROPIC_AUTH_TOKEN"
        primary_val = auth_token
        if api_key and auth_token:
            warnings.append(
                "Both ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN are set "
                "while a non-default backend is configured. Claude Code "
                "will prefer ANTHROPIC_API_KEY (x-api-key), which "
                "non-Anthropic backends typically reject with HTTP 401. "
                "Set only ANTHROPIC_AUTH_TOKEN for non-default backends."
            )
    else:
        primary_name = "ANTHROPIC_API_KEY"
        primary_val = api_key

    if not primary_val:
        log.error(
            "  ✗ auth check: %s is not set — agent calls will fail with "
            "HTTP 401. Set the secret on your repo and dispatch again.",
            primary_name,
        )
        return False, warnings

    if primary_val == "-":
        log.error(
            "  ✗ auth check: %s value is the literal '-'. Usually means "
            "`gh secret set --body -` was called with disconnected stdin. "
            "Re-set via file input: "
            "`printf '%%s' \"$KEY\" > /tmp/k && gh secret set %s "
            "--repo owner/name < /tmp/k`.",
            primary_name, primary_name,
        )
        return False, warnings

    if len(primary_val) < 8:
        log.error(
            "  ✗ auth check: %s is suspiciously short — likely truncated. %s",
            primary_name, _shape(primary_name, primary_val),
        )
        return False, warnings

    stripped = primary_val.strip()
    if stripped != primary_val:
        warnings.append(
            f"{primary_name} has leading/trailing whitespace; using "
            f"stripped value. Original {_shape(primary_name, primary_val)}; "
            f"stripped length {len(stripped)}."
        )
        os.environ[primary_name] = stripped

    log.debug(
        "  auth check: %s OK (%s)",
        primary_name, _shape(primary_name, os.environ.get(primary_name, "")),
    )
    return True, warnings


def _detect_backend(base_url: str) -> tuple[str, tuple[float, float, float] | None]:
    """Identify the Anthropic-Messages-compat backend behind ANTHROPIC_BASE_URL.

    Returns ``(display_name, rates_or_None)``. When the host isn't in the
    rate table, returns the raw host as the display name and ``None`` for
    rates — the caller falls back to the CLI's reported ``total_cost_usd``
    (which is Anthropic-rate; only correct for default Anthropic or
    Bedrock-Claude, miscalibrated for any other backend).
    """
    if not base_url:
        return ("Anthropic", None)  # default — CLI's envelope cost is correct
    host = base_url.split("://", 1)[-1].split("/", 1)[0]
    display_overrides = {
        "api.z.ai": "z.ai (GLM)",
    }
    for key, rates in _BACKEND_RATES.items():
        if key in host:
            return (display_overrides.get(key, host), rates)
    return (host, None)


def _record_claude_usage(env: dict) -> None:
    """Accumulate one `claude --output-format json` envelope's usage.

    Tokens come straight from the envelope (accurate for any backend that
    speaks the Anthropic Messages protocol). Cost is either:
    - computed from tokens × per-backend rates when ``ANTHROPIC_BASE_URL``
      matches a known non-Anthropic backend (``_BACKEND_RATES``), or
    - taken from the CLI's ``total_cost_usd`` field otherwise (default
      Anthropic — correct; Bedrock-Claude — correct; unknown backend —
      approximate, flagged via ``cost_basis``).
    """
    _RUN_COST["claude_calls"] += 1
    _RUN_COST["num_turns"] += int(env.get("num_turns") or 0)
    u = env.get("usage") or {}
    in_tok = int(u.get("input_tokens") or 0)
    out_tok = int(u.get("output_tokens") or 0)
    cache_in = int(u.get("cache_read_input_tokens") or 0)
    _RUN_COST["input_tokens"] += in_tok
    _RUN_COST["output_tokens"] += out_tok
    _RUN_COST["cache_read_input_tokens"] += cache_in

    # Successful envelope but no usage payload — accumulate a counter
    # so the rate of these is observable in telemetry, without
    # surfacing in customer-visible logs or the step summary. Error
    # envelopes legitimately carry no usage; don't count those.
    if in_tok == 0 and out_tok == 0 and not bool(env.get("is_error")):
        _RUN_COST["envelopes_without_usage"] = (
            _RUN_COST.get("envelopes_without_usage", 0) + 1
        )

    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    backend_name, rates = _detect_backend(base_url)
    if rates is not None and "api.anthropic.com" not in base_url:
        # Compute from tokens × backend rates (USD per million).
        rate_in, rate_out, rate_cache = rates
        cost = (in_tok * rate_in + out_tok * rate_out + cache_in * rate_cache) / 1_000_000
        _RUN_COST["cost_usd"] += cost
        _RUN_COST["cost_basis"] = "backend_rate_table"
    else:
        _RUN_COST["cost_usd"] += float(env.get("total_cost_usd") or 0.0)
        _RUN_COST["cost_basis"] = "claude_code_envelope"
    _RUN_COST["model_backend"] = backend_name


# Subprocess env whitelist for Claude Code invocations. The Claude CLI
# inherits whatever env we pass; if we passed `os.environ` verbatim, the
# agent's tool calls (Bash, `printenv`, `git config --list`, `curl -v`)
# could echo secrets the parent runner holds — REMYX_API_KEY,
# INPUT_GITHUB_TOKEN (the bot's installation token), INPUT_* action
# inputs, GITHUB_ACTOR, etc. Stripping at the launch boundary stops
# those from entering the agent's context in the first place; pairs
# with v1.6.4's outbound-body scrubber (catches secrets at egress),
# v1.6.8's per-pattern diagnostic logging, and v1.6.10's narrow
# prompt-level redaction rules.
#
# Whitelist contains only what the CLI legitimately needs:
#   - Auth: ANTHROPIC_API_KEY (required), plus optional ANTHROPIC_BASE_URL
#     / ANTHROPIC_MODEL for routing / model overrides
#   - System: PATH / HOME / USER / LOGNAME / TERM
#   - Locale: LANG / LC_*
#   - Temp dirs: TMPDIR / TMP / TEMP
#   - XDG paths for the CLI's per-user state
#   - CI sentinels (CI, GITHUB_ACTIONS) — informational, carry no secrets
#   - GitHub auth for the agent's `gh` CLI verification tools — see
#     the note below
#
# GITHUB_TOKEN (the workflow's built-in runner token, NOT the bot's
# installation token) is included so the selection-pass agent's `gh`
# CLI invocations (gh code-search, gh issue list, gh api repos/.../
# contents/..., gh issue view) can authenticate. Without it, the agent
# falls back to unauthenticated GitHub API at 60 req/hr per shared
# runner IP and can't view private-repo content at all — observed
# verification-quality degradation in the 2026-06-17 v1.6.10 dispatch
# whose selection_reasoning explicitly noted "Search and issue tools
# are unauthenticated here."
#
# Trade-off: the workflow GITHUB_TOKEN is repo-scoped (the agent
# can't reach other repos with it) but its scope is set by the
# workflow's `permissions:` block, which currently includes
# `pull-requests: write` / `issues: write`. That's broader than
# read-only verification needs, and the leak vector is real if the
# agent echoes the value verbatim. We accept the trade-off because
# the egress defenses landed in v1.6.4 / v1.6.8 / v1.6.10 catch the
# echo at multiple layers, and the bot's installation token (via
# INPUT_GITHUB_TOKEN) stays stripped — the agent never sees the
# higher-privilege cross-repo token the orchestrator uses for PR /
# Issue creation.
#
# An engine-side scoped read-only token mint is the principled
# long-term fix; this whitelist entry is the fast unblock pending
# that work.
#
# If a Claude CLI feature legitimately requires a new env var, add it
# explicitly with a comment naming the case. Don't broaden to `ANTHROPIC_*`
# wildcards — future Anthropic env vars may carry telemetry tokens the
# agent shouldn't see verbatim.
_CLAUDE_ENV_WHITELIST: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    # ANTHROPIC_AUTH_TOKEN — used by Claude Code as a Bearer credential
    # for non-default backends (z.ai's GLM Coding Plan requires this:
    # https://docs.z.ai/devpack/tool/claude). When set, Claude Code sends
    # "Authorization: Bearer <token>" instead of "x-api-key: <key>". z.ai's
    # gateway rejects x-api-key with HTTP 401, so without this whitelist
    # entry, any glm-routed run fails at auth.
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TERM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TMPDIR",
    "TMP",
    "TEMP",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "CI",
    "GITHUB_ACTIONS",
    # Workflow built-in token — repo-scoped — for the agent's `gh`
    # verification tooling. NOT the bot's installation token (that
    # arrives via INPUT_GITHUB_TOKEN, which stays stripped). See the
    # comment block above for the trade-off rationale.
    "GITHUB_TOKEN",
)


def _claude_subprocess_env() -> dict[str, str]:
    """Build the env dict for Claude CLI subprocess invocations.

    Returns a minimal whitelist of the parent env, stripping every var
    not on ``_CLAUDE_ENV_WHITELIST``. Defense in depth at the launch
    boundary — the v1.6.4 outbound-body scrubber catches secrets at
    egress; this stops them from entering the agent's context at all.
    """
    env: dict[str, str] = {}
    for name in _CLAUDE_ENV_WHITELIST:
        v = os.environ.get(name)
        if v is not None:
            env[name] = v
    return env


def _format_agent_cli_failure(
    tool: str, returncode: int, stdout: str, stderr: str
) -> str:
    """Build an agent-CLI failure diagnostic that puts the real cause where it
    survives truncation.

    Provider-agnostic: ``tool`` is the CLI's program name (``claude`` today;
    Aider / Goose / Codex / Copilot as they land), used only for labeling. On
    a hard reject (usage limit, credit balance, auth) the agent CLI exits fast
    with the cause on **stderr** and either nothing or a partial, unparseable
    JSON fragment on stdout. Callers tail-slice this string
    (``invoke_claude_code`` keeps the last 4KB, the orchestrator stores the
    last 1KB into ``claude_log_tail``), so the stderr must land at the *end*
    to survive truncation — otherwise bulky stdout crowds it out and the log
    tail shows noise instead of the error.
    """
    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    parts = [f"[{tool} exited {returncode}, no JSON envelope parsed]"]
    if stdout:
        # stdout is usually noise / a partial fragment here; cap its head so
        # it can't push the stderr out of the caller's tail-slice window.
        head = stdout[:500]
        if len(stdout) > 500:
            head += " …(truncated)"
        parts.append(f"--- STDOUT (head) ---\n{head}")
    parts.append("--- STDERR ---\n" + (stderr or "(empty)"))
    return "\n".join(parts)


def _run_claude_json(
    cmd_prefix: list[str], prompt: str, cwd: Path, timeout_s: int
) -> tuple[bool, str]:
    """Run `claude … --output-format json -p <prompt>`, accumulate token/cost
    usage into _RUN_COST, and return (ok, model_text).

    With --output-format json the CLI prints a single envelope object
    ({result, total_cost_usd, usage, num_turns, is_error, …}); the model's
    actual answer is in `result`, so callers that parse a JSON decision out
    of the answer get the inner text, not the envelope. Falls back to raw
    stdout (no usage recorded) if the envelope doesn't parse.
    """
    cmd = [*cmd_prefix, "--output-format", "json", "-p", prompt]
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, env=_claude_subprocess_env(),
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, f"claude CLI timed out after {timeout_s}s"
    except FileNotFoundError:
        return False, ("claude CLI not found on PATH "
                       "(install: npm install -g @anthropic-ai/claude-code)")
    raw = (proc.stdout or "").strip()
    try:
        env = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        env = None
    if isinstance(env, dict):
        _record_claude_usage(env)
        text = env.get("result") or ""
        is_error = bool(env.get("is_error")) or proc.returncode != 0
        # On error, always append the CLI stderr — the envelope's `result`
        # often omits the operational cause (e.g. usage limit) that stderr
        # carries. Skip if stderr is already echoed inside `result`.
        if is_error and proc.stderr and proc.stderr.strip() not in text:
            text = (text + "\n--- STDERR ---\n" + proc.stderr.strip()).strip()
        return (not is_error), text
    # Envelope didn't parse — surface the CLI's exit code and stderr so the
    # real failure cause reaches `claude_log_tail`. No usage recorded (no
    # envelope to account).
    return proc.returncode == 0, _format_agent_cli_failure(
        cmd_prefix[0], proc.returncode, proc.stdout, proc.stderr
    )


def _run_claude_stream(
    cmd_prefix: list[str], prompt: str, cwd: Path, timeout_s: int
) -> tuple[bool, str, list[dict]]:
    """Like ``_run_claude_json`` but with the full tool transcript.

    Runs ``claude … --output-format stream-json --verbose -p <prompt>`` and
    parses the JSONL event stream. Returns ``(ok, text, events)`` where
    ``text`` is the final result event's answer string (same string the json
    envelope's ``result`` field carries, so verdict parsing is unchanged) and
    ``events`` is every parsed stream event — the selection coverage parser
    walks the ``tool_use`` / ``tool_result`` blocks in it.

    Token/cost usage is recorded exactly once, off the terminal
    ``{"type": "result", …}`` event (same shape as the json envelope), so
    accounting matches ``_run_claude_json``. ``--verbose`` is required by the
    CLI when ``stream-json`` is paired with ``-p``.
    """
    cmd = [*cmd_prefix, "--output-format", "stream-json", "--verbose",
           "-p", prompt]
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, env=_claude_subprocess_env(),
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, f"claude CLI timed out after {timeout_s}s", []
    except FileNotFoundError:
        return False, ("claude CLI not found on PATH "
                       "(install: npm install -g @anthropic-ai/claude-code)"), []
    events: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(ev, dict):
            events.append(ev)
    final = next(
        (e for e in reversed(events) if e.get("type") == "result"), None
    )
    if final is not None:
        _record_claude_usage(final)
        text = final.get("result") or ""
        is_error = bool(final.get("is_error")) or proc.returncode != 0
        # On error, always append the CLI stderr — the result event's text
        # often omits the operational cause (e.g. usage limit) that stderr
        # carries. Skip if stderr is already echoed inside the result text.
        if is_error and proc.stderr and proc.stderr.strip() not in text:
            text = (text + "\n--- STDERR ---\n" + proc.stderr.strip()).strip()
        return (not is_error), text, events
    # No terminal result event — surface exit code + stderr so the real
    # failure cause reaches `claude_log_tail`.
    return proc.returncode == 0, _format_agent_cli_failure(
        cmd_prefix[0], proc.returncode, proc.stdout, proc.stderr
    ), events


def _strip_leading_frontmatter(text: str) -> str:
    """Strip a leading YAML frontmatter block (``---`` … ``---``) from ``text``.

    INVOCATION.md carries OKF-conformant YAML frontmatter — metadata for the
    file, not instructions for the agent. The file is passed verbatim as the
    Claude CLI's ``-p`` value, and the CLI's option parser reads a leading
    ``---`` as an unknown flag (``error: unknown option '---'``), hard-failing
    the call in ~0.2s before any work runs. Send only the instruction body so
    the prompt never opens with a token the parser mistakes for an option.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            return "".join(lines[i + 1:]).lstrip("\n")
    # No closing fence — not a frontmatter block; leave untouched.
    return text


def write_research_invocation(
    workdir: Path, rec: "Recommendation", target: "Target",
) -> None:
    """Write RESEARCH_INVOCATION.md into the bundle for the staged-synthesis pass.

    Called before the research phase runs when INPUT_STAGED_SYNTHESIS is set.
    Fills the paper title / arxiv ID / target repo / prior-attempt hint into
    the research template so the invocation has a concrete task to work on.
    """
    bundle = workdir / BUNDLE_DIR_NAME
    bundle.mkdir(exist_ok=True)

    # Prior-attempt hint: if start-from-ref names a preserved branch on the
    # target fork, surface it explicitly so the research phase probes it.
    prior_ref = (os.environ.get("INPUT_START_FROM_REF") or "").strip()
    if prior_ref:
        prior_attempt_hint = (
            f"an earlier dispatch produced the branch ``{prior_ref}`` on the "
            f"fork; the research phase should probe it as a baseline candidate."
        )
    else:
        prior_attempt_hint = "no prior-attempt branch is pinned; probe git history for any earlier attempts."

    # Pre-fetched signals interpolated into the research prompt:
    #  1. HF Hub paper index — canonical "does this paper have public
    #     checkpoints / datasets / spaces" data via `_fetch_hf_paper_linkage`
    #     (same source the Issue-downgrade path already uses).
    #  2. Sibling implementations — GitHub repo search filtered to well-known
    #     ML-library orgs. Presence of an adjacent-library implementation is
    #     the strongest "this paper is worth engaging with" signal available
    #     to the research phase.
    #
    # Both are pre-fetched deterministically in Python — Claude sees them as
    # static context, doesn't spend a tool call to look them up. Empty blocks
    # (paper not indexed / no sibling impls found) render as "(none)" rather
    # than being omitted, so downstream reviewers of the prompt can tell the
    # difference between "not looked up" and "looked up, no signal."
    hf_linkage = _fetch_hf_paper_linkage(rec.arxiv_id)
    sibling_hits = _fetch_sibling_implementations(
        rec.arxiv_id, rec.paper_title, target.repo,
    )
    hf_signal = (
        f"{len(hf_linkage.get('linked_models', []))} models, "
        f"{len(hf_linkage.get('linked_datasets', []))} datasets"
        if hf_linkage else "not indexed"
    )
    log.info(
        f"  → research-phase enrichment pre-fetched: "
        f"HF Hub linkage ({hf_signal}); "
        f"sibling implementations (hits={len(sibling_hits)})"
    )
    hf_linkage_block = _render_hf_linkage_block(rec.arxiv_id)  # cached, reuses fetch
    sibling_impls_block = _render_sibling_impls_block(
        rec.arxiv_id, rec.paper_title, target.repo,
    )

    (bundle / "RESEARCH_INVOCATION.md").write_text(
        _RESEARCH_INVOCATION_MD_TEMPLATE.format(
            paper_title=rec.paper_title or "(unknown title)",
            arxiv_id=rec.arxiv_id or "(unknown)",
            target_repo=target.repo,
            prior_attempt_hint=prior_attempt_hint,
            hf_linkage_block=hf_linkage_block,
            sibling_impls_block=sibling_impls_block,
        )
    )


def _render_hf_linkage_block(arxiv_id: str) -> str:
    """Render the HF-Hub-linkage context block for the research prompt.

    Uses the existing ``_fetch_hf_paper_linkage`` helper. Returns a
    single markdown section — always emits, even when no signal, so the
    prompt structure is stable.
    """
    linkage = _fetch_hf_paper_linkage(arxiv_id)
    if linkage is None:
        return (
            "**HF Hub linkage** — paper not indexed on huggingface.co/papers "
            "(no signal about public checkpoints via this channel; if a "
            "reference implementation exists, it must be reached via arxiv "
            "citation or GitHub search)."
        )
    lines = [
        "**HF Hub linkage** — paper indexed on "
        f"huggingface.co/papers/{arxiv_id.strip().lower().replace('v', ' ').split()[0]}:",
    ]
    if linkage["linked_models"]:
        top = linkage["linked_models"][:3]
        lines.append(f"- **Linked models** ({len(linkage['linked_models'])} total):")
        for m in top:
            mid = m.get("id") or m.get("modelId") or "(unknown)"
            lines.append(f"  - `{mid}`")
    else:
        lines.append("- Linked models: (none)")
    if linkage["linked_datasets"]:
        top = linkage["linked_datasets"][:2]
        lines.append(f"- **Linked datasets** ({len(linkage['linked_datasets'])} total):")
        for d in top:
            did = d.get("id") or "(unknown)"
            lines.append(f"  - `{did}`")
    if linkage["linked_spaces"]:
        lines.append(f"- Linked spaces: {len(linkage['linked_spaces'])} (see huggingface.co/papers page)")
    lines.append(
        "  \nIf a linked model exists, its model card + config typically "
        "carry the canonical call-site pattern — cheaper to read than the "
        "paper's PDF for implementation-mappable detail."
    )
    return "\n".join(lines)


def _render_sibling_impls_block(
    arxiv_id: str, paper_title: str, target_repo: str,
) -> str:
    """Render the sibling-implementation context block for the research prompt.

    Uses ``_fetch_sibling_implementations``. Returns a single markdown
    section; always emits, empty-signal renders as "(none found)".
    """
    hits = _fetch_sibling_implementations(arxiv_id, paper_title, target_repo)
    if not hits:
        return (
            "**Sibling implementations in adjacent ML-library orgs** — "
            "none found via GitHub repo search filtered to well-known ML-library "
            "owners (huggingface, EleutherAI, allenai, microsoft, etc.). "
            "Either this paper hasn't been adopted cross-library yet, or the "
            "search terms didn't surface the implementation — consider a "
            "search-method-shaped probe from the research phase if the paper "
            "is well-known."
        )
    lines = [
        "**Sibling implementations in adjacent ML-library orgs**:",
    ]
    for h in hits:
        lines.append(
            f"- **{h['full_name']}** ({h['stars']}★) — {h['description'] or '(no description)'}"
        )
    lines.append(
        "  \nThese are strong coordination signals — if the same paper is "
        "already implemented in an adjacent library, the target repo's PR "
        "should acknowledge that (either build on the pattern or explicitly "
        "cite the difference). Check the linked repos' README / relevant "
        "modules before scoping."
    )
    return "\n".join(lines)


def invoke_research_phase(workdir: Path, timeout_s: int = 600) -> tuple[bool, str]:
    """Invoke the research-phase Claude Code CLI pass.

    Reads ``.remyx-recommendation/RESEARCH_INVOCATION.md`` as the prompt,
    runs the CLI, and expects ``web_findings.json`` to appear in the
    briefing bundle directory when the pass completes successfully. Returns
    ``(success, stdout/stderr tail)``.

    Failure modes handled gracefully upstream: caller should treat a
    False return as "no research context available for coding session"
    and continue with the classic single-invocation flow rather than
    fail the whole dispatch.

    Uses a tighter default timeout (600s / 10 min) than the coding
    invocation — the research phase should converge in ≤ 8 turns per the
    prompt's own budget; a longer wall clock usually signals a hung
    tool call rather than legitimate work.
    """
    invocation = _strip_leading_frontmatter(
        (workdir / BUNDLE_DIR_NAME / "RESEARCH_INVOCATION.md").read_text()
    )
    log.info(f"  → invoking research phase (timeout={timeout_s}s) in {workdir}")
    cmd = ["claude", "--dangerously-skip-permissions"]
    # Cap turns via the same knob the coding invocation honors, but with a
    # tighter default for the research phase (8 turns per the prompt's
    # bounded-budget instruction).
    max_turns = os.environ.get("REMYX_RESEARCH_MAX_TURNS", "8").strip()
    if max_turns:
        cmd += ["--max-turns", max_turns]
    ok, text = _run_claude_json(cmd, invocation, workdir, timeout_s)
    findings_path = workdir / BUNDLE_DIR_NAME / "web_findings.json"
    if ok and not findings_path.exists():
        # Session succeeded but the agent didn't write the artifact — surface
        # this as a soft failure so the caller falls back to non-staged flow.
        log.warning(
            "  ✗ research phase completed but web_findings.json is missing; "
            "coding session will run without research context."
        )
        return False, text[-4000:]
    if ok:
        log.info(f"  ✓ research phase produced web_findings.json ({findings_path.stat().st_size} bytes)")
    else:
        log.warning(f"  ✗ research phase failed; coding session will run without research context.")
    return ok, text[-4000:]


def invoke_claude_code(workdir: Path, timeout_s: int = 900) -> tuple[bool, str]:
    """Invoke the Claude Code CLI in headless mode with the workdir as context.

    Returns (success, stdout/stderr). Success means CLI exit 0 — caller still
    validates the produced changes with the path-allowlist check + tests.

    ``REMYX_CLAUDE_MAX_TURNS`` (optional) caps the agent's tool-use turns to
    bound cost; unset means no cap (avoids truncating legitimate work).
    """
    invocation = _strip_leading_frontmatter(
        (workdir / BUNDLE_DIR_NAME / "INVOCATION.md").read_text()
    )
    log.info(f"  → invoking Claude Code (timeout={timeout_s}s) in {workdir}")
    cmd = ["claude", "--dangerously-skip-permissions"]
    max_turns = os.environ.get("REMYX_CLAUDE_MAX_TURNS", "").strip()
    if max_turns:
        cmd += ["--max-turns", max_turns]
    ok, text = _run_claude_json(cmd, invocation, workdir, timeout_s)
    if not ok:
        # The returned `text` is tail-truncated downstream (telemetry keeps
        # only the last ~1KB), which can clip the CLI's real failure cause.
        # Emit the full output to the action log here — CI captures stdout
        # untruncated — so the complete error (e.g. usage limit / credit
        # balance) is always recoverable from the run logs.
        log.error(
            "Claude Code implementation call failed — full output:\n%s", text
        )
    return ok, text[-4000:]   # last 4KB retained for the telemetry log tail


# ─── Pre-flight routing + self-review (§4, §6) ─────────────────────────────


def _run_claude_oneshot(
    workdir: Path, prompt: str, timeout_s: int, max_turns: int | None = None
) -> tuple[bool, str]:
    """Run the Claude CLI headless with `prompt` and return (ok, stdout).

    Used for the pre-flight routing and the self-review passes — both
    expect a JSON object back, not a full code-generation session.
    Failures here are non-fatal: the orchestrator falls through to the
    normal implementation flow.

    `max_turns` caps tool-use rounds for agentic flows (selection now uses
    this to bound spend). None = no cap (matches prior behavior).
    """
    cmd = ["claude", "--dangerously-skip-permissions"]
    if max_turns is not None:
        cmd += ["--max-turns", str(max_turns)]
    return _run_claude_json(cmd, prompt, workdir, timeout_s)


def _run_claude_oneshot_streaming(
    workdir: Path, prompt: str, timeout_s: int, max_turns: int | None = None
) -> tuple[bool, str, list[dict]]:
    """Streaming variant of ``_run_claude_oneshot`` used by the selection pass.

    Returns ``(ok, text, events)`` — same contract as ``_run_claude_oneshot``
    plus the parsed tool transcript, so the selection pass can compute
    exploration-coverage telemetry from the agent's actual actions. Only the
    selection pass uses this; the other one-shot callers (pre-flight,
    self-review, audit) stay on the cheaper single-envelope runner.
    """
    cmd = ["claude", "--dangerously-skip-permissions"]
    if max_turns is not None:
        cmd += ["--max-turns", str(max_turns)]
    return _run_claude_stream(cmd, prompt, workdir, timeout_s)


def _extract_json_object(s: str) -> dict | None:
    """Pull the first JSON object out of `s`. Tolerant of prose wrappers."""
    if not s:
        return None
    try:
        start = s.index("{")
        end = s.rindex("}")
    except ValueError:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None


def _repo_layout_manifest(workdir: Path, package: str, max_lines: int = 60) -> str:
    """Short module-by-module manifest of the target repo for pre-flight.

    Lists the .py files under `{package}/` with the first line of their
    module docstring (where present) and the names of the test files
    under `tests/`. Capped to `max_lines` to keep the prompt cheap.
    """
    lines: list[str] = []
    pkg_dir = workdir / package
    if pkg_dir.is_dir():
        py_files = sorted(pkg_dir.rglob("*.py"))
        lines.append(f"# {package}/ ({len(py_files)} modules)")
        for p in py_files:
            rel = p.relative_to(workdir).as_posix()
            doc_first = ""
            try:
                doc = ast.get_docstring(ast.parse(p.read_text())) or ""
                doc_first = doc.splitlines()[0] if doc else ""
            except (SyntaxError, OSError):
                pass
            if doc_first:
                lines.append(f"  {rel}  — {doc_first[:80]}")
            else:
                lines.append(f"  {rel}")
    tests_dir = workdir / "tests"
    if tests_dir.is_dir():
        test_files = sorted(tests_dir.rglob("test_*.py"))[:20]
        if test_files:
            lines.append(f"\n# tests/ ({len(test_files)} files shown)")
            for p in test_files:
                lines.append(f"  {p.relative_to(workdir).as_posix()}")
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"  ... ({len(lines) - max_lines} more)"]
    return "\n".join(lines) or "(empty)"


def preflight_routing(
    workdir: Path, package: str, timeout_s: int = 180
) -> dict | None:
    """Cheap Claude pass that decides PR vs Issue BEFORE implementation.

    Returns the parsed JSON ({decision, reasoning, issue_title, issue_body})
    or None on any failure (parse error, timeout, missing CLI). On None
    the orchestrator falls through to the regular implementation flow,
    so a failed pre-flight never blocks a PR — it just doesn't save the
    Claude budget.

    ``timeout_s`` defaults to 180 for direct callers (kept for backwards
    compatibility with tests and ad-hoc invocations); the production call
    site in ``process_target`` passes ``target.claude_timeout_s`` so a
    customer who bumped ``claude-timeout`` (large monorepo / slower
    backend) gets the same headroom on preflight as on implementation.
    """
    spec_path = workdir / BUNDLE_DIR_NAME / "SPEC.md"
    if not spec_path.exists():
        return None
    spec_md = spec_path.read_text()
    layout = _repo_layout_manifest(workdir, package)
    prompt = (
        _PREFLIGHT_PROMPT_TEMPLATE
        .replace("__SPEC__", spec_md)
        .replace("__LAYOUT__", layout)
    )
    log.info("  → pre-flight routing pass (PR vs Issue)")
    ok, output = _run_claude_oneshot(workdir, prompt, timeout_s)
    if not ok:
        log.warning(f"  pre-flight call failed: {output[:200]}; "
                    f"falling through to implementation")
        return None
    data = _extract_json_object(output)
    if data is None:
        log.warning(f"  pre-flight: couldn't parse JSON; raw: {output[:300]!r}")
        return None
    decision = str(data.get("decision") or "").upper()
    if decision not in ("PR", "ISSUE"):
        log.warning(f"  pre-flight: invalid decision {decision!r}; "
                    f"falling through to implementation")
        return None
    data["decision"] = decision
    log.info(f"  pre-flight decision: {decision} — "
             f"{(data.get('reasoning') or '')[:120]}")
    return data


_ARXIV_ABS_RE = re.compile(
    r"arxiv\.org/abs/(\d{4}\.\d{4,6})(v\d+)?", re.IGNORECASE,
)


def _arxiv_id_from_issue_body(body: str) -> str | None:
    """Pull the first ``arxiv.org/abs/<id>`` reference from an Issue body
    and return the versionless id. Returns None when no arxiv reference
    is present (e.g. OPEN_AS_ISSUE downgrades whose title is Claude-
    authored).
    """
    if not body:
        return None
    m = _ARXIV_ABS_RE.search(body)
    if not m:
        return None
    return m.group(1)


def _discharged_index(issues: list[dict]) -> dict[str, dict]:
    """Build an arxiv-id -> {number, state, title, source} index from the
    all-state discharge set. Used by both the discharge-set prompt
    section and the in-pool candidate annotation. Keyed on the
    versionless arxiv id so a candidate at 2605.26102v3 matches an Issue
    that linked 2605.26102v1.

    ``source`` is ``"outrider"`` or ``"maintainer"``, taken from the
    ``_remyx_source`` annotation set by ``_all_discharge_issues``. Falls
    back to ``"outrider"`` when unset (the v1.4.7/v1.4.8 path didn't
    carry source info, so callers that pass in only Outrider Issues get
    sensible defaults).
    """
    out: dict[str, dict] = {}
    for it in issues:
        body = it.get("body") or ""
        arxiv = _arxiv_id_from_issue_body(body)
        if not arxiv:
            continue
        # First write wins — issues are ordered most-recent-first by
        # GitHub default, so the freshest reference for a paper takes
        # precedence when duplicates exist.
        if arxiv in out:
            continue
        out[arxiv] = {
            "number": it.get("number"),
            "state": it.get("state") or "open",
            "title": it.get("title") or "",
            "source": it.get("_remyx_source") or "outrider",
        }
    return out


def _render_discharged_papers(issues: list[dict], cap: int = 50) -> str:
    """Render the "Already filed by Outrider" section for the selection
    prompt. Returns ``""`` when no Outrider Issues exist for the target
    so the template stays byte-stable for new installs and customers with
    no prior recommendations.

    The cap bounds prompt size for long-tail customers — we keep the
    most-recent N entries. Issues arrive most-recent-first from the
    GitHub /issues endpoint, so a simple slice preserves recency.
    """
    if not issues:
        return ""
    capped = issues[:cap]
    bullets: list[str] = []
    truncated_arxiv_ids: set[str] = set()
    for it in capped:
        body = it.get("body") or ""
        arxiv = _arxiv_id_from_issue_body(body)
        if not arxiv:
            continue
        if arxiv in truncated_arxiv_ids:
            continue
        truncated_arxiv_ids.add(arxiv)
        number = it.get("number") or "?"
        state = it.get("state") or "open"
        title = (it.get("title") or "").strip()
        # Strip the standard "[Remyx Recommendation] " prefix for
        # readability — the section header already carries that context.
        # Doesn't apply to maintainer-opened Issues (different titles),
        # but the strip is no-op on those.
        if title.startswith(PR_TITLE_PREFIX + " "):
            title = title[len(PR_TITLE_PREFIX) + 1:]
        if len(title) > 80:
            title = title[:77] + "…"
        source = it.get("_remyx_source") or "outrider"
        source_tag = "[Outrider]" if source == "outrider" else "[Maintainer]"
        bullets.append(
            f"- arxiv {arxiv} — \"{title}\" — Issue #{number} ({state}) "
            f"{source_tag}"
        )
    if not bullets:
        return ""
    skipped = max(0, len(issues) - len(capped))
    footer = (
        f"\n…and {skipped} older Issue(s) omitted from this list."
        if skipped else ""
    )
    return (
        "--- Already in the team's attention (do NOT re-pick) ---\n"
        "\n"
        "These papers have an existing Issue referencing the arxiv id on\n"
        "this repository. Outrider-opened Issues are marked [Outrider];\n"
        "maintainer-opened Issues (RFCs, discussions) are marked\n"
        "[Maintainer]. Either way the paper is already in front of the\n"
        "team — selecting one (in-pool or out-of-pool) would just\n"
        "re-confirm what's already on record. Skip them.\n"
        "\n"
        "A [Maintainer]-tagged paper is a STRONGER stay-away signal than\n"
        "[Outrider]: the maintainer themselves filed the discussion. If\n"
        "you believe one should be revisited, the lever is for the\n"
        "maintainer to reopen the Issue — not for selection to re-pick.\n"
        "\n"
        + "\n".join(bullets)
        + footer
        + "\n\n"
    )


def _render_candidate_brief(
    candidates: list[Recommendation],
    discharged: dict[str, dict] | None = None,
) -> str:
    """Numbered, relevance-ranked brief of the candidate pool for the
    selection pass. Index matches list position so the model's
    ``chosen_index`` maps straight back.

    When ``discharged`` is provided (arxiv-id -> {number, state, title}
    from the prior-Outrider-Issues set), candidates whose arxiv id
    matches an entry carry an inline ``✗ already filed: #NN (state)``
    annotation so the dedup state is visible inside the candidate brief
    itself, not just the standalone discharge section.
    """
    discharged = discharged or {}
    blocks: list[str] = []
    for i, c in enumerate(candidates):
        abstract = " ".join((c.paper_abstract or "").split())
        # License gate line — surfaced to the selection pass so it can
        # weigh adoption-blocking license/code-availability signals
        # into its choice. Omitted when no enrichment ran. Includes
        # both GitHub and HuggingFace URLs when present so the selection
        # pass sees the same provenance the gate evaluated.
        license_line = ""
        if (c.paper_github_url or c.paper_huggingface_url
                or c.paper_license or c.license_class != "unknown"):
            url_segs = []
            if c.paper_github_url:
                url_segs.append(f"gh={c.paper_github_url}")
            if c.paper_huggingface_url:
                url_segs.append(f"hf={c.paper_huggingface_url}")
            urls = "  ".join(url_segs) if url_segs else "(no code/model link)"
            source_seg = (
                f"  source={c.license_source}" if c.license_source else ""
            )
            license_line = (
                f"\n    code/license: {urls}  "
                f"license={c.paper_license or '(none)'} "
                f"({c.license_class}, compat={c.license_compat:.2f})"
                f"{source_seg}"
            )
        family_line = (
            f"\n    family: {c.family_summary}" if c.family_summary else ""
        )
        # Discharge annotation. When the candidate's arxiv id matches a
        # prior Issue (Outrider-opened or maintainer-opened), surface
        # the Issue # + state + source inline so the LLM sees the dedup
        # signal next to the candidate it's weighing.
        discharged_suffix = ""
        if c.arxiv_id:
            versionless = _arxiv_versionless(c.arxiv_id) or c.arxiv_id
            entry = discharged.get(versionless) or discharged.get(c.arxiv_id)
            if entry:
                src = entry.get("source") or "outrider"
                src_tag = "[Outrider]" if src == "outrider" else "[Maintainer]"
                discharged_suffix = (
                    f"  ✗ already filed: Issue #{entry['number']} "
                    f"({entry['state']}) {src_tag} — do NOT pick"
                )
        blocks.append(
            f"[{i}] {c.paper_title}  "
            f"(arxiv {c.arxiv_id or 'n/a'}, relevance {c.relevance_score:.2f}, "
            f"tier {c.tier}){discharged_suffix}{family_line}\n"
            f"    why surfaced: {(c.reasoning or '(none)')[:600]}\n"
            f"    abstract: {abstract[:400]}"
            f"{license_line}"
        )
    return "\n\n".join(blocks)


# ─── Selection-pass exploration telemetry ──────────────────────────────────
#
# The selection pass explores the target repo (searches + file reads) before
# committing to a verdict. An agent can reach the right files yet stop before
# reading enough lines to verify the integration shape. These helpers parse
# the agent's tool transcript into per-run coverage (searches, file reads,
# lines seen), compute a context-efficiency proxy, and optionally gate a
# verdict reached on too little reading. The coverage and efficiency fields
# are added to the run's JSON output for later analysis.

# Shell-segment separators: `||`/`&&` before `|` so the two-char ops win.
_SHELL_SEGMENT_RE = re.compile(r"\|\||&&|;|\||\n")

# `<path>.py:<line>` citations in the verdict reasoning — the context-
# efficiency numerator. Anchored to a boundary so it doesn't fire mid-token.
_CITATION_RE = re.compile(r"""(?:^|[\s(`'"=>])([\w./-]+\.py):(\d+)""")


def _classify_shell_segment(seg: str) -> str | None:
    """Classify one shell segment as ``"search"``, ``"file_read"``, or None.

    Segment-aware so a batched command (``grep …; sed -n FILE; cmd | head``)
    is scored stage-by-stage: the search and the file read each count, and a
    pipe-fed pager/filter reading stdin (``… | head``, ``… | grep``) counts
    as neither. ``gh-graph`` is navigation, not a content read — also neither.
    """
    seg = seg.strip()
    if not seg:
        return None
    try:
        toks = shlex.split(seg)
    except ValueError:
        toks = seg.split()
    if not toks:
        return None
    binname = os.path.basename(toks[0])
    args = toks[1:]
    nonopt = [a for a in args if not a.startswith("-")]
    if binname == "gh":
        if args[:2] == ["search", "code"] or (args and args[0] == "code-search"):
            return "search"
        if args and args[0] == "api":
            joined = " ".join(args)
            if "/contents/" in joined or joined.rstrip().endswith("/contents"):
                return "file_read"
        return None
    if binname == "gh-graph":
        return None
    if binname in ("grep", "rg", "ag"):
        # pattern + path(s) → searching files; pattern alone → reads stdin.
        return "search" if len(nonopt) >= 2 else None
    if binname == "cat":
        return "file_read" if nonopt else None
    if binname in ("head", "tail"):
        # a non-numeric positional is a file; bare/`-n N` reads stdin.
        return "file_read" if any(not a.isdigit() for a in nonopt) else None
    if binname == "sed":
        # `sed -n '1,50p' FILE` carries script + file; script alone = stdin.
        return "file_read" if len(nonopt) >= 2 else None
    return None


def _classify_shell_command(cmd: str) -> list[str]:
    """All non-None segment classifications for a Bash command string."""
    if not cmd:
        return []
    out: list[str] = []
    for seg in _SHELL_SEGMENT_RE.split(cmd):
        cls = _classify_shell_segment(seg)
        if cls:
            out.append(cls)
    return out


def _classify_tool_use(name: str, inp: dict) -> list[str]:
    """Classifications for one ``tool_use`` block (Bash, Read, Grep, …)."""
    if name == "Bash":
        return _classify_shell_command((inp or {}).get("command") or "")
    if name in ("Read", "WebFetch"):
        return ["file_read"]
    if name in ("Grep", "Glob"):
        return ["search"]
    return []


def _count_result_lines(content: object) -> int:
    """Line count of a ``tool_result`` payload (string or text-block list)."""
    if content is None:
        return 0
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        text = str(content)
    return len(text.splitlines()) if text else 0


def _selection_coverage_from_events(events: list[dict]) -> dict:
    """Parse a stream-json transcript into per-run exploration coverage.

    Pairs each file-read ``tool_use`` with its ``tool_result`` by id so
    ``visible_lines`` reflects content the agent actually saw. Returns
    ``searches`` / ``file_reads`` / ``visible_lines`` / ``search_to_read_ratio``.
    """
    searches = 0
    file_reads = 0
    visible_lines = 0
    read_ids: set[str] = set()
    for ev in events:
        msg = ev.get("message") if isinstance(ev, dict) else None
        content = (msg or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                classes = _classify_tool_use(
                    block.get("name") or "", block.get("input") or {}
                )
                searches += classes.count("search")
                reads = classes.count("file_read")
                file_reads += reads
                if reads and block.get("id"):
                    read_ids.add(block["id"])
            elif btype == "tool_result":
                if block.get("tool_use_id") in read_ids:
                    visible_lines += _count_result_lines(block.get("content"))
    coverage = {
        "searches": searches,
        "file_reads": file_reads,
        "visible_lines": visible_lines,
        "search_to_read_ratio": round(searches / max(file_reads, 1), 2),
    }
    # Exploration-structure dimension (arXiv:2606.11976): classify the same
    # ordered stream as linear (one read per step, single subsystem) vs
    # non-linear/domain-scoped (branching across subsystems). Rides along as
    # telemetry beside the count/ratio dimensions; gating still keys off
    # visible_lines.
    if structure_enabled():
        coverage["exploration_structure"] = exploration_structure_from_events(
            events
        )
    return coverage


def _selection_context_efficiency(text: str, visible_lines: int) -> float:
    """Context-efficiency proxy: distinct ``path:line`` citations in the
    verdict reasoning over total visible lines. A high-coverage /
    low-efficiency run = "read a lot, used little." 0.0 when nothing was
    cited.
    """
    if not text:
        return 0.0
    pairs = set(_CITATION_RE.findall(text))
    return round(len(pairs) / max(visible_lines, 1), 4)


def _apply_coverage_gate(
    data: dict, coverage: dict, *, higher_floor: bool
) -> dict:
    """Flag (and, in enforce mode, block) an under-explored verdict.

    Gates on ``visible_lines`` — the content-grounding signal, robust to how
    the agent batches its shell calls; ``file_reads`` / ``searches`` ride along
    as telemetry only. Mode via ``REMYX_SELECTION_COVERAGE_GATE``:
    ``observe`` (default) flags without blocking; ``enforce`` downgrades an
    under-explored pick to a skip (``chosen_index=-1``), which the caller
    routes through the existing ``skipped_by_selection_verification`` status
    so the user-facing step summary is unchanged; ``off`` disables the check
    entirely. The under-explored verdict is recorded on ``coverage`` (unless
    ``off``) for internal telemetry only.
    """
    mode = os.environ.get(
        "REMYX_SELECTION_COVERAGE_GATE", "observe"
    ).lower().strip()
    if mode == "off":
        return data
    if higher_floor:
        floor = int(os.environ.get(
            "REMYX_SELECTION_MIN_VISIBLE_LINES_EXTENSION", "300"))
    else:
        floor = int(os.environ.get(
            "REMYX_SELECTION_MIN_VISIBLE_LINES", "150"))
    under = coverage.get("visible_lines", 0) < floor
    coverage["under_explored"] = under
    coverage["min_visible_lines"] = floor
    if under and mode == "enforce":
        log.info(
            f"  selection coverage gate (enforce): visible_lines="
            f"{coverage.get('visible_lines')} < floor {floor}; downgrading "
            f"verdict to a skip"
        )
        data["chosen_index"] = -1
        data["under_explored"] = True
    return data


def _fallback_candidate(viable: list[Recommendation]) -> Recommendation:
    """Pick the fallback candidate when agentic selection is unavailable.

    Called when the selection call 429s, times out, or returns unparseable
    output. The broad pool from `/papers/recommended` isn't guaranteed to
    be in descending-relevance order at index 0 — the engine occasionally
    seeds the list with diversity picks — so `viable[0]` blindly on
    fallback can land the *lowest*-relevance candidate.

    Highest relevance wins; ties broken by `license_compat`. A permissive
    candidate with a code link (compat=1.00) should beat a no-code-link
    candidate (compat=0.30) when both are equally relevant. Without the
    tiebreaker the winner is order-of-arrival luck (max() returns the
    first element at the max value).
    """
    return max(viable, key=lambda c: (c.relevance_score, c.license_compat))


def _render_environment_hint(env_body: str) -> str:
    """Render the workflow-attached tooling hint block for the selection prompt.

    Injecting ENVIRONMENTS.md at selection time means the selection agent
    can leverage workflow-authored tools (AST search skills, MCP servers,
    custom search) while VERIFYING candidates — the load-bearing grounding
    stage. Empty env_body yields empty string so behavior is unchanged for
    runs without an ENVIRONMENTS.md.
    """
    if not env_body.strip():
        return ""
    return (
        "**Workflow-attached tooling available for verification** "
        "(from ENVIRONMENTS.md; prefer these over generic search when the "
        "described tool fits the task):\n\n"
        f"{env_body}\n\n"
    )


def select_recommendation(
    workdir: Path, package: str, candidates: list[Recommendation],
    target: "Target | None" = None,
    timeout_s: int | None = None,
    discharged_issues: list[dict] | None = None,
    env_body: str = "",
) -> dict | None:
    """Claude pass that picks the most implementable candidate from the
    lookback pool, given the target repo's module layout.

    Returns the parsed JSON ({chosen_index, reasoning, rejected}) or None
    on any failure (single candidate, parse error after retry, out-of-
    range index, timeout, missing CLI). On JSON parse failure this
    function retries once with a format-only reminder before falling
    through. On None, the caller falls back to the highest-relevance
    candidate in the pool — not necessarily index 0, since the broad
    pool isn't guaranteed to be relevance-sorted at position 0.

    This only chooses *which* candidate to implement — it never decides
    PR vs Issue. The chosen candidate still runs the full preflight +
    integration / stub / test / self-review gate chain, any of which can
    downgrade to an Issue.
    """
    if len(candidates) <= 1:
        return None
    layout = _repo_layout_manifest(workdir, package)
    repo_fullname = target.repo if target is not None else "<unknown>"
    issues = discharged_issues or []
    discharged_index = _discharged_index(issues)

    # Cross-run learning prior: load .remyx/repo_intel.yaml if maintain-state
    # is on. Injected inline into the selection prompt as compact priors on
    # landing zones + rejected shapes + exploration budget. Empty string
    # when maintain-state is off or the fork has no accumulated learning.
    maintain_state = (
        (os.environ.get("INPUT_MAINTAIN_STATE") or "").strip().lower()
        in ("true", "1", "yes")
    )
    intel_block = ""
    already_dispatched_block = ""
    intel: dict | None = None
    if maintain_state:
        intel = _load_fork_repo_intel(workdir)
        if intel is not None:
            intel_block = _render_repo_intel_for_selection(intel)
            already_dispatched_block = _render_already_dispatched_for_selection(intel)
            dispatched_count = len(_extract_dispatched_arxivs(intel))
            log.info(
                "  → selection: threading repo_intel priors "
                "(%d zones, %d rejected shapes, %d prior dispatches)",
                len(intel.get("observed_landing_zones") or []),
                len(intel.get("rejected_shapes") or []),
                dispatched_count,
            )

    prompt = (
        _SELECTION_PROMPT_TEMPLATE
        .replace("__REPO_FULLNAME__", repo_fullname)
        .replace(
            "__DISCHARGED_PAPERS__",
            _render_discharged_papers(issues),
        )
        .replace(
            "__CANDIDATES__",
            _render_candidate_brief(candidates, discharged=discharged_index),
        )
        .replace("__LAYOUT__", layout)
        .replace("__ENVIRONMENT_HINT__", _render_environment_hint(env_body))
        .replace("__REPO_INTEL__", intel_block)
        .replace("__ALREADY_DISPATCHED__", already_dispatched_block)
    )
    # Bound the agentic flow — selection is verification, not a full
    # implementation session. 25 turns covers a few `gh code-search` +
    # file-read rounds across multiple candidates + the final JSON;
    # observed via eval that 15 is too tight on repos with zero open
    # Issues (the loop spends turns hunting context that doesn't exist).
    max_turns = int(os.environ.get("REMYX_SELECTION_MAX_TURNS", "25"))
    # Wall-clock budget for the selection pass. Default 480s gives the
    # agentic loop room for 20-25 verification turns including code
    # searches + per-candidate contract checks. History:
    #   - 180s was too tight after the v1.3.4 / v1.3.5 prompt extensions
    #     (observed on remyxai/VQASynth run #7 on 2026-06-04).
    #   - 360s held until v1.6.5 (CONTEXT.md orientation block) +
    #     v1.6.6 (code-override carve-out) extended the prompt and
    #     introduced extra per-candidate verification work; the
    #     2026-06-17 remyxai/outrider self-dogfood run on a 34-candidate
    #     pool hit the 360s ceiling and fell back to the top-ranked
    #     candidate, losing the override mechanism.
    # 480s preserves the same headroom-per-candidate ratio at the new
    # prompt size; further bumps should track new feature land that
    # extends the agent's per-candidate verification cost.
    if timeout_s is None:
        # Selection inherits the run's claude-timeout budget by default —
        # same pattern as preflight (v1.6.28) and audit (v1.6.29). On
        # slower non-default backends the legacy 480s default was tight
        # enough to time out agentic selection passes and fall back to
        # the top-ranked candidate, silently losing the verification
        # signal. REMYX_SELECTION_TIMEOUT_S stays as an env-var escape
        # hatch for CI scenarios that want a tighter ceiling.
        timeout_s = int(
            os.environ.get("REMYX_SELECTION_TIMEOUT_S", "")
            or (target.claude_timeout_s if target is not None else 480)
        )
    log.info(
        f"  → agentic selection over {len(candidates)} candidates "
        f"(max-turns={max_turns}, timeout={timeout_s}s)"
    )
    ok, output, events = _run_claude_oneshot_streaming(
        workdir, prompt, timeout_s, max_turns=max_turns,
    )
    # Retry-on-empty-output: observed in the wild that the Claude CLI
    # occasionally returns ok=False with a truly empty output — a
    # transient failure mode distinct from timeouts / CLI-gone /
    # rate-limit-hit (which return ok=False with meaningful error
    # text). Immediate re-invocation typically recovers. Retry only
    # when the failure signal itself is empty; leave signaled failures
    # (timeout messages, stderr echoes) to fall through as before —
    # those are infra problems that don't benefit from a retry.
    if not ok and not (output or "").strip():
        log.warning(
            "  selection call returned empty output (ok=False, no error "
            "text); retrying once with reduced max-turns before falling "
            "back to top-ranked candidate"
        )
        retry_ok, retry_output, retry_events = _run_claude_oneshot_streaming(
            workdir, prompt, min(timeout_s, 600),
            max_turns=max(10, max_turns // 2),
        )
        if retry_ok or (retry_output or "").strip():
            ok, output, events = retry_ok, retry_output, retry_events
            log.info("  selection: empty-output retry recovered")
    if not ok:
        log.warning(f"  selection call failed: {output[:200] or '(empty)'}; "
                    f"falling back to top-ranked candidate")
        return None
    data = _extract_json_object(output)
    if data is None:
        # The model sometimes finishes its reasoning out loud instead of
        # emitting the JSON contract — observed in the wild on a run that
        # had clearly identified the right candidate but never wrote the
        # `{"chosen_index": ...}` object. Retry once with an appended
        # format-only reminder; the agentic context is already warm from
        # the first attempt so a short budget is enough to format an
        # answer. If the retry also fails, fall through to the existing
        # fallback path.
        log.warning(f"  selection: couldn't parse JSON; raw: {output[:300]!r}; "
                    f"retrying with format-only reminder")
        retry_prompt = (
            prompt
            + "\n\n--- OUTPUT FORMAT REMINDER ---\n"
              "Your previous response was prose. You already did the "
              "verification work — do NOT re-verify, do NOT call any tools. "
              "Respond NOW with only the JSON object specified above — no "
              "prose, no preamble, no explanation, no markdown fences. The "
              "first character of your response must be `{` and the last "
              "must be `}`."
        )
        # Retry budget: ~5 max-turns × ~10-15s per turn for the Claude API
        # round-trip puts the floor around 50-75s before the agent has any
        # time to compose the final JSON. 180s leaves enough headroom for
        # a slow API response while still capping cost; the prior 90s
        # default was tight enough that complex selection pools (30+
        # candidates with embedded paper context) routinely timed out.
        retry_timeout = int(
            os.environ.get("REMYX_SELECTION_RETRY_TIMEOUT_S", "180")
        )
        retry_max_turns = int(
            os.environ.get("REMYX_SELECTION_RETRY_MAX_TURNS", "5")
        )
        ok, output, events = _run_claude_oneshot_streaming(
            workdir, retry_prompt, retry_timeout, max_turns=retry_max_turns,
        )
        if not ok:
            log.warning(f"  selection retry failed: {output[:200]}; "
                        f"falling back to top-ranked candidate")
            return None
        data = _extract_json_object(output)
        if data is None:
            log.warning(f"  selection retry: still couldn't parse JSON; "
                        f"raw: {output[:300]!r}; falling back")
            return None
        log.info("  selection: JSON-parse retry succeeded")
    # Exploration-coverage telemetry. Computed from the transcript
    # of whichever attempt produced the parseable verdict, then attached to
    # `data` BEFORE the branch logic so every return path (in-pool, extension,
    # external -2, skip -1) carries it. The gate may downgrade an under-
    # explored pick to -1 in enforce mode; observe (default) only records.
    coverage = _selection_coverage_from_events(events)
    reasoning_text = " ".join(
        str(data.get(k) or "") for k in (
            "reasoning", "verification_summary",
            "chosen_call_site", "proposed_call_site",
        )
    )
    context_efficiency = _selection_context_efficiency(
        reasoning_text, coverage["visible_lines"]
    )
    try:
        _idx_raw = int(data.get("chosen_index"))
    except (TypeError, ValueError):
        _idx_raw = None
    _higher_floor = (
        (data.get("integration_shape") or "").lower().strip() == "extension"
        or _idx_raw == -2
    )
    _apply_coverage_gate(data, coverage, higher_floor=_higher_floor)
    data["selection_coverage"] = coverage
    data["selection_context_efficiency"] = context_efficiency
    _struct = coverage.get("exploration_structure") or {}
    log.info(
        f"  selection coverage: {coverage['searches']} searches, "
        f"{coverage['file_reads']} file reads, "
        f"{coverage['visible_lines']} lines visible, "
        f"context-efficiency {context_efficiency} "
        f"(under_explored={coverage.get('under_explored')}, "
        f"structure={_struct.get('structure', 'n/a')}, "
        f"domains={_struct.get('domains', 0)})"
    )
    try:
        idx = int(data.get("chosen_index"))
    except (TypeError, ValueError):
        log.warning(f"  selection: chosen_index not an int "
                    f"({data.get('chosen_index')!r}); falling back")
        return None
    # Extension-shape picks need extra schema fields beyond
    # the base contract: team_direction_signal + proposed_call_site.
    # Without them, the pick fails the four-gate verification we
    # documented to the model; treat as malformed.
    shape = (data.get("integration_shape") or "").lower().strip()
    if shape == "extension":
        tds = (data.get("team_direction_signal") or "").strip()
        pcs = (data.get("proposed_call_site") or "").strip()
        if not tds or not pcs:
            log.warning(
                f"  selection: integration_shape='extension' but missing "
                f"required fields (team_direction_signal={tds!r}, "
                f"proposed_call_site={pcs!r}); falling back to skip-by-"
                f"verification"
            )
            data["chosen_index"] = -1
            return data
        # Extension floor: tier=high AND relevance >= 0.85 (gate 4 of the
        # four-gate verification). The 0.85 threshold (down from the
        # original 0.90) admits high-tier candidates that fall just under
        # the old hard 0.90 cut — the 0.85-0.90 boundary band where
        # several legitimate extension picks were being rejected on
        # relevance alone. Gates 1-3 carry the structural-fit load; this
        # gate is a "ranker put this candidate in the top band" sanity
        # check, not a second pass on relevance. Only validate when
        # chosen_index >= 0; external extension picks (-2) don't have a
        # pool candidate to check against.
        if idx >= 0 and 0 <= idx < len(candidates):
            cand = candidates[idx]
            if cand.tier.lower() != "high" or cand.relevance_score < 0.85:
                log.warning(
                    f"  selection: extension pick [{idx}] "
                    f"{cand.paper_title[:50]}… fails extension floor "
                    f"(tier={cand.tier!r}, relevance={cand.relevance_score:.2f}); "
                    f"extension requires tier=high AND relevance>=0.85; "
                    f"falling back to skip-by-verification"
                )
                data["chosen_index"] = -1
                return data
        log.info(
            f"  selection: extension pick — direction signal: {tds[:100]!r}, "
            f"adjacent call site: {pcs[:80]!r}"
        )
    # Re-pick enforcement: when the agent picked an arxiv that has
    # already been dispatched on this fork (per the "Already-dispatched"
    # section threaded into the prompt when maintain-state is on), it
    # must set is_re_pick=true AND provide a non-empty re_pick_justification.
    #
    # Two-sided check:
    #   1. Server-side detection — cross-reference the selected candidate's
    #      arxiv (versionless-normalized) against the dispatched-arxivs list
    #      extracted from repo_intel. If the pick IS a re-pick but the model
    #      DIDN'T flag it, coerce is_re_pick=true so the justification check
    #      below fires. This catches the case where the model saw the
    #      Already-dispatched section but ignored it.
    #   2. Justification check — if is_re_pick=true (either model-set or
    #      server-flagged) without a non-empty re_pick_justification, coerce
    #      chosen_index=-1 so the run skips rather than redoing prior work.
    is_re_pick = bool(data.get("is_re_pick"))
    re_pick_just = (data.get("re_pick_justification") or "").strip()
    if not is_re_pick and 0 <= idx < len(candidates) and intel is not None:
        # Server-side detection using the intel dict already loaded above
        # (no second fetch). Falls through as no-op when maintain-state
        # is off (intel is None).
        picked_arxiv = _arxiv_versionless(candidates[idx].arxiv_id or "")
        dispatched_arxivs = {
            _arxiv_versionless(d["arxiv"])
            for d in _extract_dispatched_arxivs(intel)
            if d.get("arxiv")
        }
        if picked_arxiv and picked_arxiv in dispatched_arxivs:
            log.warning(
                "  selection: model picked already-dispatched arxiv %s "
                "without setting is_re_pick=true; treating as unjustified "
                "duplicate-work attempt",
                picked_arxiv,
            )
            is_re_pick = True
            data["is_re_pick"] = True
            data["server_flagged_re_pick"] = True

    if is_re_pick:
        if not re_pick_just:
            log.warning(
                "  selection: is_re_pick without re_pick_justification "
                "(server-flagged=%s); falling back to skip-by-verification "
                "to avoid duplicate work",
                data.get("server_flagged_re_pick", False),
            )
            data["chosen_index"] = -1
            data["re_pick_justification"] = ""
            return data
        log.info(
            "  selection: re-pick of prior-dispatched arxiv — justification: %r",
            re_pick_just[:200],
        )
        data["re_pick_justification"] = re_pick_just

    # Code-override audit: when the chosen
    # candidate has no code link (compat <= 0.30) AND the agent
    # populated code_override_justification, validate the override is
    # restricted to the eligible archetypes (addition / simplification)
    # per the prompt contract. If the agent overrode for replacement
    # or extension, the override is invalid — fall back to skip rather
    # than silently allowing a no-code pick in an archetype where the
    # bar should be higher.
    #
    # When the override is empty AND the candidate is no-code, the
    # current behavior holds — the agent is expected to either justify
    # explicitly via this field or reject in `rejected[]`. We don't
    # auto-fail no-code picks without an explicit justification field;
    # this is the carve-out path, not a hard requirement.
    override_just = (data.get("code_override_justification") or "").strip()
    # Normalize: drop empty / whitespace-only justification so downstream
    # consumers don't have to treat "field present but blank" as a special
    # case. Same effect as the field being absent.
    if "code_override_justification" in data and not override_just:
        data.pop("code_override_justification", None)
    if idx >= 0 and 0 <= idx < len(candidates) and override_just:
        cand_compat = getattr(candidates[idx], "license_compat", 1.0)
        if cand_compat > 0.3:
            log.warning(
                f"  selection: code_override_justification set on a "
                f"candidate with code link (compat={cand_compat:.2f}); "
                f"justification ignored — override only applies to "
                f"no-code-link candidates"
            )
            data.pop("code_override_justification", None)
        elif shape not in ("addition", "simplification"):
            log.warning(
                f"  selection: code_override_justification set but "
                f"integration_shape={shape!r} (must be addition or "
                f"simplification); rejecting override"
            )
            data["chosen_index"] = -1
            return data
        else:
            log.info(
                f"  selection: code-override fired — archetype={shape}, "
                f"justification={override_just[:160]!r}"
            )

    # Agentic selection may surface an out-of-pool candidate via
    # broadening-search (chosen_index: -2). Validate the required
    # external_* fields are present; if they're missing, the agent
    # tried to use the extended schema but didn't honor the contract —
    # treat as a malformed selection and fall back to skip.
    if idx == -2:
        external_arxiv = (data.get("external_arxiv_id") or "").strip()
        external_title = (data.get("external_title") or "").strip()
        external_query = (data.get("external_query_used") or "").strip()
        if not external_arxiv or not external_title:
            log.warning(
                f"  selection: chosen_index=-2 but missing required "
                f"external_* fields "
                f"(arxiv={external_arxiv!r}, title={external_title!r}); "
                f"falling back to skip-by-verification"
            )
            data["chosen_index"] = -1
            return data
        log.info(
            f"  selection: external pick {external_arxiv} "
            f"'{external_title[:60]}' via query {external_query!r}"
        )
        return data
    # Agentic selection may explicitly reject every candidate after
    # verification (returns chosen_index: -1). Surface as a structured
    # signal — the caller treats it as "skip this run" rather than
    # falling back to the top-ranked candidate (the whole point of the
    # verification step is that the candidates failed it).
    if idx == -1:
        log.info(f"  selection: every candidate failed verification — "
                 f"{(data.get('reasoning') or '')[:160]}")
        data["chosen_index"] = -1
        return data
    if not (0 <= idx < len(candidates)):
        log.warning(f"  selection: chosen_index {idx} out of range "
                    f"[0,{len(candidates)}); falling back")
        return None
    data["chosen_index"] = idx
    log.info(f"  selection: candidate [{idx}] "
             f"{candidates[idx].paper_title[:50]}… — "
             f"{(data.get('reasoning') or '')[:120]}")
    return data


def self_review_diff(
    workdir: Path, timeout_s: int = 180
) -> dict | None:
    """Second Claude pass over the diff. Returns the parsed JSON or None.

    Never raises and never blocks: a failure here just means the PR
    won't get the self-review section. The integration / stub-density
    checks are the load-bearing gates.

    ``timeout_s`` defaults to 180 for direct callers (kept for backwards
    compatibility with tests and ad-hoc invocations); the production call
    site in ``process_target`` passes ``target.claude_timeout_s`` so a
    customer who bumped ``claude-timeout`` (large monorepo / slower
    backend) gets the same headroom on self-review as on implementation.
    """
    try:
        diff_proc = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=workdir, capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        log.warning(f"  self-review: git diff failed ({e}); skipping")
        return None
    diff = diff_proc.stdout
    if not diff.strip():
        return None
    # Cap diff size at ~80KB to keep the prompt cheap and well under
    # any context limit the headless CLI imposes.
    if len(diff) > 80_000:
        diff = diff[:80_000] + "\n... (truncated)"
    prompt = _SELF_REVIEW_PROMPT_TEMPLATE.replace("__DIFF__", diff)
    log.info(f"  → self-review pass (diff={len(diff)} bytes)")
    ok, output = _run_claude_oneshot(workdir, prompt, timeout_s)
    if not ok:
        log.warning(f"  self-review call failed: {output[:200]}")
        return None
    data = _extract_json_object(output)
    if data is None:
        log.warning(f"  self-review: couldn't parse JSON; raw: {output[:300]!r}")
        return None
    return data


# Class-coded emoji shared by the Issue/PR body license section and the
# step summary's license-verdict line, so the two surfaces never disagree
# on severity color.
_LICENSE_CLASS_EMOJI = {
    "permissive": "🟢",
    "copyleft": "🟡",
    "nc": "🔴",
    "missing": "🔴",
    "no-code-link": "🟡",
    "unknown": "⚪",
}


def _license_enrichment_ran(rec: Recommendation) -> bool:
    """True when the license gate populated any signal on ``rec``.

    Every field at its dataclass default means enrichment never ran
    (env opt-out, or a caller that bypasses query_remyx_candidates) —
    renderers should omit the license verdict rather than report a
    misleading "unknown".
    """
    return bool(
        rec.paper_github_url or rec.paper_huggingface_url
        or rec.paper_license
        or rec.license_class not in ("unknown", "")
        or rec.license_compat != 0.0
    )


def _render_license_section(rec: Recommendation) -> str:
    """Render the License & code availability block for the PR/Issue body.

    Returns ``"\n"`` when no enrichment ran (every signal is at its
    dataclass default — env opt-out or callers that bypass
    query_remyx_candidates). Otherwise renders a short status block
    with a class-coded emoji and a one-line note so the maintainer
    reads it at a glance.

    Deliberately a sibling of ``_render_engineering_section``: the
    license verdict and the engineering verdict are two independent
    calls a maintainer must be able to read separately — an A++
    engineering analysis fused with a wrong license flag means the
    reader can miss either one.
    """
    if not _license_enrichment_ran(rec):
        return "\n"
    emoji = _LICENSE_CLASS_EMOJI.get(rec.license_class, "⚪")
    note = {
        "permissive": "Permissive license — safe to adopt.",
        "copyleft":
            "Copyleft license — review compatibility against this repo's "
            "license before merging.",
        "nc":
            "Non-commercial / no-derivatives license — **adoption blocked** "
            "for commercial or relicensed use.",
        "missing":
            "**No LICENSE file detected** — no legal permission to "
            "redistribute or modify the code. Treat as blocking until "
            "upstream adds a license.",
        "no-code-link":
            "No code repository surfaced — couldn't fetch a LICENSE to "
            "evaluate. Worth confirming the paper has an open release "
            "before investing in adoption.",
        "unknown":
            "Unrecognized license — manual review needed.",
    }.get(rec.license_class, "Unrecognized license class.")
    # Render both code + model URLs when present so the maintainer can
    # see what adoption surface the gate actually inspected.
    code_lines = []
    if rec.paper_github_url:
        code_lines.append(f"- **Code**: {rec.paper_github_url}")
    if rec.paper_huggingface_url:
        code_lines.append(f"- **Model card**: {rec.paper_huggingface_url}")
    if not code_lines:
        code_lines.append(
            "- **Code / model**: no repository or model URL surfaced in "
            "the paper, recommendation envelope, or arxiv abstract page."
        )
    source_suffix = (
        f", source: `{rec.license_source}`" if rec.license_source else ""
    )
    family_line = (
        f"\n_{rec.family_summary}_\n" if rec.family_summary else ""
    )
    return (
        "\n## License & code availability\n\n"
        f"{emoji} {note}\n\n"
        + "\n".join(code_lines) + "\n"
        f"- **License**: `{rec.paper_license or '(none detected)'}` "
        f"(class: `{rec.license_class}`, compat: "
        f"{rec.license_compat:.2f}{source_suffix})\n"
        f"{family_line}"
        "\n"
    )


def _render_engineering_section(
    *,
    integration_shape: str = "",
    contract_match: str = "",
    migration_cost: str = "",
    team_direction_signal: str = "",
    proposed_call_site: str = "",
) -> str:
    """Render the Engineering verdict block for Issue bodies.

    Sibling of ``_render_license_section`` — the engineering call
    (call site, contract match, migration cost) and the license call
    must read as two adjacent, independent verdicts rather than
    interleaved prose, so a maintainer can take one without the other
    (e.g. a great swap proposal under a blocking license stays findable
    if upstream relicenses). Returns ``""`` when no field carries
    signal so callers skip the section silently.
    """
    rows = []
    if integration_shape.strip():
        rows.append(f"- **Integration shape**: {integration_shape.strip()}")
    if contract_match.strip():
        rows.append(f"- **Contract match**: {contract_match.strip()}")
    if migration_cost.strip():
        rows.append(f"- **Migration cost**: {migration_cost.strip()}")
    if team_direction_signal.strip():
        rows.append(
            f"- **Team-direction signal**: {team_direction_signal.strip()}"
        )
    if proposed_call_site.strip():
        rows.append(f"- **Proposed call site**: {proposed_call_site.strip()}")
    if not rows:
        return ""
    return "## Engineering verdict\n\n" + "\n".join(rows) + "\n"


def _record_verdict_fields(result: dict, rec: Recommendation) -> None:
    """Thread the chosen candidate's license axis onto the result dict.

    The step summary renders a license-verdict line adjacent to the
    engineering verdict from these fields. Skipped entirely when the
    license gate never ran, so the summary degrades silently instead
    of reporting a misleading "unknown".
    """
    if not _license_enrichment_ran(rec):
        return
    result["license_class"] = rec.license_class
    result["license_compat"] = rec.license_compat
    result["paper_license"] = rec.paper_license


def _render_self_review_section(review: dict) -> str:
    """Render the self-review JSON into a PR-body section prepended above
    the test results. Always returns a complete Markdown block ending
    in a blank line."""
    # Prefer the value-first keys; fall back to the legacy ones so an older
    # model response still renders.
    delivered = review.get("delivered") or review.get("implemented") or []
    scoped_out = review.get("scoped_out") or review.get("stubbed") or []
    call_site = review.get("call_site") or "(unspecified)"
    summary = (review.get("honest_summary") or "").strip()
    is_orphan = review.get("is_orphan") is True

    def _bullets(items: list) -> str:
        if not items:
            return "_(none reported)_"
        return "\n".join(f"- {x}" for x in items)

    parts = [
        "## What this PR delivers",
        "",
        f"**Call site**: `{call_site}`",
        "",
        "**Delivers (from the paper)**:",
        _bullets(delivered),
        "",
        "**Intentionally out of scope** (not needed for this contribution):",
        _bullets(scoped_out),
    ]
    if is_orphan:
        parts += [
            "",
            "> ⚠ **Self-review flagged this as orphan-shaped.** The coding "
            "agent concluded that no pre-existing entry point invokes the "
            "new code (at most its own tests). If this is a library API "
            "addition meant to be imported by external callers, that's by "
            "design; if this is application code, the wiring may be "
            "incomplete — review before merging.",
        ]
    if summary:
        parts += ["", f"_{summary}_"]
    parts.append("")
    return "\n".join(parts)


# ─── Validation ────────────────────────────────────────────────────────────


# Build-artifact paths that show up in `git status` as side-effects of
# running tests / imports during the Claude Code session — not intentional
# changes. Filtered out before the allowlist check.
_BUILD_ARTIFACT_SUBSTRINGS = (
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".coverage",
)
_BUILD_ARTIFACT_SUFFIXES = (".pyc", ".pyo")


def changed_files(workdir: Path) -> list[str]:
    """Files Claude Code modified or created (vs HEAD), excluding build-
    artifact side-effects (__pycache__, .pytest_cache, *.pyc, etc.).

    Without this filter, pytest's bytecode cache shows up as 'untracked'
    files in git status and gets the run rejected for path-allowlist
    violations even though Claude never intentionally wrote them."""
    # --untracked-files=all lists individual files inside a newly-created
    # directory instead of collapsing them to "newdir/". Without it, a new
    # file in a brand-new dir (e.g. a first-ever tests/ folder) shows up as
    # the directory, which the path-allowlist and integration/invocation
    # checks can't reason about per-file.
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=workdir, capture_output=True, text=True, check=True,
    )
    paths = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        # Format: "XY path/to/file" — XY is status flags
        p = line[3:].strip()
        # Status output can quote paths that contain spaces; strip the quotes.
        if p.startswith('"') and p.endswith('"'):
            p = p[1:-1]
        if any(sub in p for sub in _BUILD_ARTIFACT_SUBSTRINGS):
            continue
        if any(p.endswith(suf) for suf in _BUILD_ARTIFACT_SUFFIXES):
            continue
        paths.append(p)
    return paths


def path_matches_glob(path: str, patterns: list[str]) -> bool:
    """Simple glob matcher. `**` matches any number of path segments,
    INCLUDING zero; `*` matches within a segment.

    fnmatch alone treats `*` as crossing `/`, so `tests/**/*.py` only
    matches when there's at least one intermediate dir — it rejects a
    top-level `tests/test_foo.py`, which is exactly the shape the §3 test
    gate expects. We test three normalizations per pattern so the
    zero-segment case matches too:
      - the raw pattern,
      - `**` → `*`            (collapse to single star),
      - `**/` → ``            (drop the segment entirely, so
                               `tests/**/*.py` also matches `tests/x.py`).
    """
    import fnmatch
    # Case-insensitive: `fnmatch.fnmatch` is case-sensitive on Linux, which
    # rejected e.g. a repo's `README.MD` against the `README.md` allowlist
    # entry and threw away an otherwise-valid PR.
    lower_path = path.lower()
    for p in patterns:
        variants = {p, p.replace("**", "*"), p.replace("**/", "")}
        if any(fnmatch.fnmatch(lower_path, v.lower()) for v in variants):
            return True
    return False


def effective_allowlist(target: Target, package: str) -> list[str]:
    """The default allowlist globs (with `{package}` filled in) PLUS any
    extra globs the customer passed via `guardrails-allowlist`.

    The customer input EXTENDS the defaults — it does not replace them. The
    old `target.guardrails_allowlist or [defaults]` short-circuit silently
    dropped the defaults (`.remyx-recommendation/**`, `*.py`, `README.md`)
    the moment any extra glob was supplied, which then flagged the agent's
    own scaffolding files as violations.
    """
    base = [g.format(package=package) for g in DEFAULT_ALLOWLIST_GLOBS]
    extra = [g for g in (target.guardrails_allowlist or []) if g not in base]
    return base + extra


def validate_changes(workdir: Path, target: Target, package: str) -> tuple[bool, list[str]]:
    """Returns (passed_allowlist, violations)."""
    allowlist = effective_allowlist(target, package)
    paths = changed_files(workdir)
    violations = []
    for p in paths:
        if path_matches_glob(p, ALWAYS_BLOCKED):
            violations.append(f"BLOCKED: {p}")
            continue
        if not path_matches_glob(p, allowlist):
            violations.append(f"NOT IN ALLOWLIST: {p}")
    return (not violations, violations)


# ─── Integration / stub-density / test-integration validators ──────────────
#
# These run AFTER the path-allowlist check passes. They enforce the
# "ready-to-ship PRs" shape: a small wiring edit to an existing file
# that calls into a new capability-named module, with at least one
# test that touches an existing module, and a non-stub-dominated new
# module. Failing any of these routes the run to Issue instead of PR.


def _file_is_new(workdir: Path, path: str) -> bool:
    """True if `path` did not exist at HEAD (i.e. Claude created it)."""
    result = subprocess.run(
        ["git", "ls-tree", "HEAD", "--", path],
        cwd=workdir, capture_output=True, text=True, check=False,
    )
    return not result.stdout.strip()


def _is_test_file(path: str) -> bool:
    """True iff `path` looks like a pytest-collected test file.

    Recognizes the two conventions pytest itself uses for auto-discovery:
    files under a ``tests/`` or ``test/`` directory anywhere in the tree,
    and files whose basename starts with ``test_``. Both cover the common
    layouts; nothing else (fixtures, conftest, helper modules) counts.

    Used by the integration-check invocation gate to exempt ``test_*``
    functions from the "must be invoked from another changed file"
    requirement — pytest is the invoker, and it lives outside the diff.
    """
    parts = path.split("/")
    if any(seg in ("tests", "test") for seg in parts[:-1]):
        return True
    basename = parts[-1]
    return basename.startswith("test_") and basename.endswith(".py")


def _diff_line_changes(workdir: Path, path: str) -> tuple[int, int]:
    """Return (added, deleted) lines for `path` vs HEAD."""
    result = subprocess.run(
        ["git", "diff", "--numstat", "HEAD", "--", path],
        cwd=workdir, capture_output=True, text=True, check=False,
    )
    out = result.stdout.strip()
    if not out:
        return 0, 0
    parts = out.split("\t", 2)
    if len(parts) < 2:
        return 0, 0
    try:
        added = int(parts[0]) if parts[0] != "-" else 0
        deleted = int(parts[1]) if parts[1] != "-" else 0
    except ValueError:
        return 0, 0
    return added, deleted


def _head_source(workdir: Path, path: str) -> str:
    """Source of `path` at HEAD, or '' if it didn't exist there."""
    r = subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        cwd=workdir, capture_output=True, text=True, check=False,
    )
    return r.stdout if r.returncode == 0 else ""


def _public_callables(src: str) -> set[str]:
    """Names of public functions, methods, and classes defined in `src`.

    Methods are included by their bare name because an invocation
    `obj.method(...)` is matched on the attribute name (see _called_names).
    Underscore-prefixed names are treated as private and ignored.
    """
    names: set[str] = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.add(node.name)
    return names


def _called_names(src: str) -> set[str]:
    """Names appearing in a call position in `src`: `foo(...)` yields
    'foo', `obj.foo(...)` yields 'foo'."""
    called: set[str] = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return called
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                called.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                called.add(fn.attr)
    return called


def _added_callables(workdir: Path, path: str) -> set[str]:
    """Public callables defined in the working-tree `path` that were not
    defined at HEAD — the functions / methods / classes this diff adds."""
    if not path.endswith(".py"):
        return set()
    try:
        current = (workdir / path).read_text()
    except OSError:
        return set()
    now = _public_callables(current)
    if _file_is_new(workdir, path):
        return now
    return now - _public_callables(_head_source(workdir, path))


def check_integration(
    workdir: Path, target: Target, package: str
) -> tuple[bool, list[str]]:
    """Reject scaffold-shaped runs — code that's added but never called.

    Pass criteria — ALL of:
      * Number of new .py files under {package}/ ≤ MAX_NEW_PACKAGE_FILES.
      * If the diff adds any new public function / method / class, at least
        one of them must be INVOKED from a different changed file. This
        proves the new code is wired into a call site rather than merely
        defined — and it covers both shapes: a brand-new module (called
        from a modified existing file) and methods/functions bolted onto an
        existing file (called from elsewhere in the diff). An import alone
        no longer counts; there must be an actual call.

    A newly-added symbol can only be reached by code also added/modified in
    this run (otherwise it would have been a NameError before), so scanning
    the changed set is sufficient. A test counts as a call site here — the
    code at least runs; whether a *production* path reaches it is the
    self-review reachability pass's job (§4).

    Historical note: a per-file line-count cap on edits to existing files
    was removed after observation that it produced false-negatives on
    legitimate paper implementations (large-but-focused rewrites of
    trainer loss functions, big test additions matching the paper's
    property-test surface). Scope discipline is now enforced downstream
    by the convention pass, which uses graded signal against the repo's
    own PR history rather than a hardcoded line ceiling.

    Returns (passed, [violations]).
    """
    paths = changed_files(workdir)
    pkg_prefix = f"{package}/"

    new_pkg_files = [
        p for p in paths
        if p.startswith(pkg_prefix) and p.endswith(".py") and _file_is_new(workdir, p)
    ]

    violations: list[str] = []

    if len(new_pkg_files) > MAX_NEW_PACKAGE_FILES:
        violations.append(
            f"too many new files in {package}/: {len(new_pkg_files)} > "
            f"{MAX_NEW_PACKAGE_FILES}"
        )

    # Invocation check. Every newly-added callable, keyed by the file that
    # defines it, must be called from some OTHER changed file.
    #
    # Test-file exemption: ``test_*`` functions inside test files are invoked
    # by pytest, not by other diff files, so requiring another changed file to
    # call them is a false-positive gate. Coverage-only refinement PRs (all
    # additions are new tests exercising baseline code) hit this gate today
    # even though the tests DO wire into real call sites — just ones on the
    # baseline branch, not in the current diff. Strip ``test_*`` names in
    # test-shaped files from the added-callables accounting so those diffs
    # pass the check without loosening the gate for production code.
    changed_py = [p for p in paths if p.endswith(".py")]
    added_by_file: dict[str, set[str]] = {}
    for p in changed_py:
        added = _added_callables(workdir, p)
        if not added:
            continue
        if _is_test_file(p):
            added = {n for n in added if not n.startswith("test_")}
            if not added:
                continue
        added_by_file[p] = added

    if added_by_file:
        calls_by_file: dict[str, set[str]] = {}
        for p in changed_py:
            try:
                calls_by_file[p] = _called_names((workdir / p).read_text())
            except OSError:
                continue
        integrated: set[str] = set()
        for def_file, names in added_by_file.items():
            for call_file, calls in calls_by_file.items():
                if call_file == def_file:
                    continue
                integrated |= names & calls
        if not integrated:
            all_added = sorted({n for ns in added_by_file.values() for n in ns})
            shown = ", ".join(all_added[:8]) + ("…" if len(all_added) > 8 else "")
            violations.append(
                f"none of the newly-added functions/methods/classes are "
                f"invoked from another changed file — the diff defines code "
                f"nothing calls ({shown}). Wire the new capability into a "
                f"real call site (an existing module, a stage driver, or at "
                f"least a test that exercises it) or open as Issue."
            )

    return (not violations, violations)


def _is_stub_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Heuristic: is this function body just a placeholder?

    Treated as a stub:
      - body is a single `pass`
      - body is a single `...` (Ellipsis expression)
      - body is a single `raise NotImplementedError(...)`
      - body is docstring-only (no executable statements after it)

    Not treated as a stub:
      - return statements (even `return None`)
      - real expressions / calls
      - control flow
    """
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    if not body:
        return True
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    if (isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is Ellipsis):
        return True
    if isinstance(stmt, ast.Raise) and stmt.exc is not None:
        exc = stmt.exc
        name = None
        if isinstance(exc, ast.Name):
            name = exc.id
        elif isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
            name = exc.func.id
        if name == "NotImplementedError":
            return True
    return False


def check_stub_density(
    workdir: Path, package: str
) -> tuple[bool, float, list[str]]:
    """Returns (passes, density, examples).

    `passes` is False iff the fraction of stub function bodies across
    NEW .py files in `{package}/` ≥ STUB_DENSITY_DOWNGRADE_THRESHOLD.
    Modified existing files aren't included — the wiring edits there
    are small by design.
    """
    pkg_prefix = f"{package}/"
    new_files = [
        workdir / p for p in changed_files(workdir)
        if p.startswith(pkg_prefix)
        and p.endswith(".py")
        and _file_is_new(workdir, p)
    ]
    if not new_files:
        return True, 0.0, []

    stub_count = 0
    total = 0
    examples: list[str] = []
    for fp in new_files:
        try:
            tree = ast.parse(fp.read_text(), filename=str(fp))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                total += 1
                if _is_stub_body(node):
                    stub_count += 1
                    if len(examples) < 5:
                        examples.append(f"{fp.name}:{node.name}")
    if total == 0:
        return True, 0.0, []
    density = stub_count / total
    return (density < STUB_DENSITY_DOWNGRADE_THRESHOLD, density, examples)


def check_tests_touch_existing_modules(
    workdir: Path, package: str
) -> tuple[bool, list[str]]:
    """If new package modules were added, at least one new test file must
    import from a non-new module in `{package}/`. Pure self-tests of the
    new file don't prove integration.

    No new package modules → vacuously passes (the integration is
    edits-only and the regular pytest gate is sufficient).

    Returns (passed, [example_existing_imports_seen]).
    """
    paths = changed_files(workdir)
    pkg_prefix = f"{package}/"
    new_pkg_files = [
        p for p in paths
        if p.startswith(pkg_prefix) and p.endswith(".py") and _file_is_new(workdir, p)
    ]
    if not new_pkg_files:
        return True, []

    new_pkg_stems = {Path(p).stem for p in new_pkg_files}

    # The gate also passes when the new capability is wired into the
    # package's existing surface — a pre-existing (non-test) package module
    # edited in this run imports the new module (e.g. a new exported nn
    # layer registered in `__init__.py`, or called by an existing module).
    # That is a genuine public-API integration even when the new test only
    # exercises the new module directly, so it shouldn't be demoted to an
    # Issue. (check_integration already proved the new code is invoked.)
    edited_existing_pkg = [
        workdir / p for p in paths
        if p.startswith(pkg_prefix) and p.endswith(".py")
        and not _file_is_new(workdir, p)
    ]
    for ef in edited_existing_pkg:
        try:
            tree = ast.parse(ef.read_text(), filename=str(ef))
        except (SyntaxError, OSError):
            continue
        referenced: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    referenced.add(node.module.rsplit(".", 1)[-1])
                for a in node.names:
                    referenced.add(a.name.rsplit(".", 1)[-1])
            elif isinstance(node, ast.Import):
                for a in node.names:
                    referenced.add(a.name.rsplit(".", 1)[-1])
        if referenced & new_pkg_stems:
            return True, [f"wired into existing module {ef.name}"]

    new_test_files = [
        workdir / p for p in paths
        if p.startswith("tests/") and p.endswith(".py") and _file_is_new(workdir, p)
    ]
    if not new_test_files:
        return False, []

    existing_imports: list[str] = []
    for tf in new_test_files:
        try:
            tree = ast.parse(tf.read_text(), filename=str(tf))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and (
                    node.module == package or node.module.startswith(f"{package}.")
                ):
                    rest = node.module[len(package):].lstrip(".")
                    head = rest.split(".")[0] if rest else ""
                    if head and head not in new_pkg_stems:
                        existing_imports.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == package or alias.name.startswith(f"{package}."):
                        rest = alias.name[len(package):].lstrip(".")
                        head = rest.split(".")[0] if rest else ""
                        if head and head not in new_pkg_stems:
                            existing_imports.append(alias.name)
    return (bool(existing_imports), existing_imports[:5])


def _classify_pytest(returncode: int, output: str) -> str:
    """Map a pytest run to "passed" | "failed" | "unvalidated".

    "unvalidated" means pytest could not actually exercise the change — no
    tests were collected (exit 5) or collection blew up on a missing
    dependency / import error. CI runners commonly install pytest but not
    the target repo's full dependency set (torch, tensorboard, …), so a
    collection ImportError is an environment limitation, NOT a code failure,
    and must not be reported as one.
    """
    if returncode == 0:
        return "passed"
    low = output.lower()
    real_failure = (
        " failed" in low
        or "assertionerror" in low
        or "= failures =" in low
    )
    if real_failure:
        return "failed"
    if returncode == 5:                       # no tests collected
        return "unvalidated"
    collection_markers = (
        "modulenotfounderror",
        "importerror",
        "error during collection",
        "errors during collection",
        "interrupted:",
    )
    if any(m in low for m in collection_markers):
        return "unvalidated"
    return "failed"


def run_tests(workdir: Path, timeout_s: int = 300) -> tuple[str, str]:
    """Run pytest. Returns (status, output) where status is one of
    "passed" | "failed" | "unvalidated" (see _classify_pytest)."""
    log.info(f"  → running pytest in {workdir}")
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "-q", "--maxfail=3"],
            cwd=workdir, capture_output=True, text=True, timeout=timeout_s,
        )
        output = (result.stdout or "") + ("\n--- STDERR ---\n" + result.stderr if result.stderr else "")
        return _classify_pytest(result.returncode, output), output[-3000:]
    except subprocess.TimeoutExpired:
        return "failed", f"pytest timed out after {timeout_s}s"
    except Exception as e:
        return "failed", f"pytest invocation failed: {e}"


# ─── PR opening ────────────────────────────────────────────────────────────


def detect_default_branch(workdir: Path) -> str:
    """The repo's default branch — the branch HEAD points at right after a
    fresh clone (e.g. `main` or `master`). Falls back to `main`.

    Hardcoding `main` failed on `master`-default repos: the PR base 404'd
    and the commit_and_push sanity check saw `origin/main` MISSING and
    aborted. Detect it once and thread it through.

    Prefers ``refs/remotes/origin/HEAD`` over the local ``HEAD`` so the
    answer stays correct when INPUT_START_FROM_REF has
    swapped the working checkout to a non-default ref. The remote's
    HEAD is set at clone time and doesn't move on local checkouts.
    Falls back to local HEAD for local-only test repos where no remote
    is configured.
    """
    r = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=workdir, capture_output=True, text=True, check=False,
    )
    name = r.stdout.strip()
    if name.startswith("origin/"):
        return name[len("origin/"):]
    if name:
        return name
    # No remote HEAD (e.g. local-only test repo) — fall back to local HEAD.
    r = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=workdir, capture_output=True, text=True, check=False,
    )
    return r.stdout.strip() or "main"


def open_pr(
    target: Target, branch: str, title: str, body: str, draft: bool,
    base: str = "main",
) -> tuple[str, int]:
    """Open a PR on the target repo; returns ``(pr_url, pr_number)``.

    The number is threaded back so recommend mode can hand it to the inline
    refinement chain (fidelity → convention → test all key off the PR
    number) without a second API round-trip to look it up.
    """
    log.info(f"  → opening {'draft' if draft else ''} PR on {target.repo} "
             f"(base={base})")
    pr = gh_api("POST", f"/repos/{target.repo}/pulls", {
        "title": title,
        "head": branch,
        "base": base,
        "body": body,
        "draft": draft,
    })
    return pr["html_url"], pr["number"]


def open_issue(
    target: Target, title: str, body: str, *, footer_override: str = "",
) -> tuple[str, int]:
    """Open a discussion Issue on the target repo. Returns ``(url, number)``.

    The default footer attributes the Issue to the coding agent's
    Issue-mode election (the original use case). When the actual route
    is different — preflight downgrade, self-review orphan, integration
    gate, etc. — callers pass ``footer_override`` so the attribution
    reflects the real reason. Pass an empty string to keep the default;
    pass any other string to substitute the whole footer line.
    """
    if footer_override:
        footer = footer_override
    else:
        footer = (
            f"_Opened by the [Remyx Recommendation]({CANONICAL_ATTRIBUTION_URL}) "
            f"orchestrator — the coding agent elected Issue-mode rather "
            f"than scaffolding a PR for this paper._"
        )
    # Re-engagement lever. Outrider treats a paper as discharged once
    # any Outrider Issue exists for it — open or closed. The maintainer's
    # lever for re-engaging is to reopen the Issue; documenting that in
    # every Issue body ensures the mechanism is visible at the moment
    # the maintainer decides whether to close or keep open.
    reengage_note = (
        "_Reopen this Issue if you want Outrider to revisit this "
        "paper later. While it stays closed, the orchestrator "
        "will not re-recommend the same paper._"
    )
    full_body = f"{body}\n\n---\n\n{footer}\n\n{reengage_note}"
    # publish=branch — do not file the Issue; capture the fully-formatted
    # LEAD content so process_target can render it into the step summary
    # instead. Team promotes manually via ``gh issue create`` later if they
    # decide the substance warrants it. Same short-circuit path fires for
    # every open_issue caller (intentional Issue route + all downgrade
    # helpers) since they all flow through here.
    publish_mode = (os.environ.get("INPUT_PUBLISH") or "pr").strip().lower()
    if publish_mode == "branch":
        raise LeadCapturedInBranchMode(title, full_body)
    log.info(f"  → opening Issue on {target.repo}")
    payload = {"title": title, "body": full_body}
    try:
        issue = gh_api("POST", f"/repos/{target.repo}/issues", payload)
    except RuntimeError as e:
        # GitHub disables the Issues tab on forks (and some repos) by
        # default → POST /issues returns HTTP 410 "Issues has been
        # disabled". Enable it and retry once rather than failing the run.
        msg = str(e)
        if "Issues has been disabled" in msg or "HTTP 410" in msg:
            log.warning("  Issues disabled on repo; attempting to enable")
            try:
                gh_api("PATCH", f"/repos/{target.repo}", {"has_issues": True})
                issue = gh_api("POST", f"/repos/{target.repo}/issues", payload)
            except RuntimeError as patch_err:
                # PATCH /repos requires `administration: write`, which the
                # scoped App installation token doesn't carry. Raise the
                # documented graceful-skip exception so process_target can
                # convert it to a `skipped_issues_disabled` status instead
                # of a generic error.
                if "403" in str(patch_err) or "Resource not accessible" in str(patch_err):
                    raise IssuesDisabledError(
                        f"Issues tab disabled on {target.repo} and the bot's "
                        f"installation token lacks admin scope to enable it. "
                        f"Enable Issues manually: "
                        f"`gh repo edit {target.repo} --enable-issues`."
                    ) from patch_err
                raise
        else:
            raise
    return issue["html_url"], int(issue["number"])


def parse_issue_fallback_file(path: Path) -> tuple[str, str]:
    """Parse Claude's OPEN_AS_ISSUE.md into (title, body). The expected
    shape is:

        # Title: short description
        (optional subtitle)

        ## Why this paper is interesting ...

    First H1 (with optional 'Title:' prefix) becomes the Issue title;
    everything after is the body. Falls back to a generic title if no
    H1 is found."""
    text = path.read_text().strip()
    lines = text.splitlines()
    title = ""
    body_start = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("# "):
            inner = s[2:].strip()
            if inner.lower().startswith("title:"):
                inner = inner[len("title:"):].strip()
            title = inner
            body_start = i + 1
            break
    if not title:
        title = "Remyx Recommendation: paper needs team discussion"
    body = "\n".join(lines[body_start:]).strip()
    return title, body


def commit_and_push(
    workdir: Path, branch: str, title: str, repo: str, base_branch: str = "main",
) -> None:
    """Stage all changes, commit, and push the branch to origin.

    The branch's final commit is (re)created through the GitHub git data
    API with the App installation token so it's attributed to
    remyx-ai[bot] AND carries the green "Verified" badge — the same
    mechanism the engine's setup PR.``.

    Two classes of files are scrubbed before staging so they don't end
    up in the PR even when the target repo's .gitignore doesn't cover
    them:

      - Build-artifact directories (__pycache__, .pytest_cache,
        .mypy_cache, .ruff_cache) — side-effects of running tests /
        imports during the Claude session.
      - The orchestrator's own bundle directory (.remyx-recommendation)
        — these are briefing material the action wrote for Claude to
        read; SPEC.md / PAPER.md / GUARDRAILS.md / INVOCATION.md
        duplicate content already in the PR body and add noise to the
        diff.
    """
    # Sanity check: make sure local HEAD still equals its expected upstream
    # before we branch. If Claude (or pytest) disturbed the git state during
    # the session — `git checkout --orphan`, `rm -rf .git`, `git init`,
    # whatever — local HEAD can diverge from the remote default, and the
    # subsequent `git checkout -b branch` produces a root-commit branch with
    # no history in common with it. The PR-creation API then rejects with
    # HTTP 422. Fail fast with a clear error instead.
    #
    # Refinement mode: when INPUT_START_FROM_REF is set the
    # session started from a non-default ref, so its expected upstream is
    # origin/<start-from-ref>, not origin/<base_branch>. The eventual PR
    # still targets base_branch (main / master); only the commit ancestry
    # sanity check changes.
    expected_ref = (
        (os.environ.get("INPUT_START_FROM_REF") or "").strip() or base_branch
    )
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workdir, capture_output=True, text=True, check=True,
    ).stdout.strip()
    try:
        remote_sha = subprocess.run(
            ["git", "rev-parse", f"origin/{expected_ref}"],
            cwd=workdir, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        remote_sha = ""
    if not remote_sha or head_sha != remote_sha:
        raise RuntimeError(
            f"local HEAD ({head_sha[:8]}) doesn't match "
            f"origin/{expected_ref} ({(remote_sha or 'MISSING')[:8]}) — git "
            f"state was disturbed during the session. Refusing to commit; "
            f"would produce a root-commit branch and fail at PR creation."
        )

    subprocess.run(["git", "checkout", "-b", branch], cwd=workdir, check=True)

    # Scrub build artifacts (pytest bytecode caches, mypy/ruff caches).
    # IMPORTANT: prune .git/ from the traversal. Branch names that
    # contain `/` create directories under .git/refs/heads/, and we
    # name our branches `remyx-recommendation/<arxiv_id>`. Any pattern
    # that happens to match a directory name inside .git/ would let
    # `rm -rf` wipe a branch ref and produce an orphan root-commit —
    # which then 422s at PR creation with "no history in common with
    # main." Pruning .git/ is the load-bearing safety here.
    for pat in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"):
        subprocess.run(
            ["find", ".", "-path", "./.git", "-prune",
             "-o", "-type", "d", "-name", pat,
             "-exec", "rm", "-rf", "{}", "+"],
            cwd=workdir, check=False,
        )

    # Bundle dir is always at the top level; remove explicitly so we
    # never have to walk into it with find. The bundle files were
    # briefing material for Claude (SPEC.md, INVOCATION.md, etc.) —
    # they duplicate the PR body and have no business in the commit.
    bundle_path = workdir / BUNDLE_DIR_NAME
    if bundle_path.exists():
        shutil.rmtree(bundle_path, ignore_errors=True)

    subprocess.run(["git", "add", "-A"], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-m", title], cwd=workdir, check=True)

    # Delete any orphan branch with the same name from the remote before
    # pushing. Two reasons:
    #   1. The existing-PR dedup gate already skipped if an OPEN PR for
    #      this branch exists. By the time we get here, any remote branch
    #      with the same name is from a CLOSED PR and is safe to remove.
    #   2. `--force` push from a shallow clone (we use --depth 20)
    #      confuses GitHub's PR validator — it treats the pushed branch
    #      as rooted ("no history in common with main") and refuses PR
    #      creation. Delete-then-plain-push avoids the force entirely.
    # `check=False` because a non-existent branch is the common case and
    # the delete is a no-op there.
    subprocess.run(
        ["git", "push", "origin", "--delete", branch],
        cwd=workdir, check=False, capture_output=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=workdir, check=True,
    )

    # Re-author the pushed commit through the git data API so the branch
    # head is a remyx-ai[bot]-attributed, "Verified" commit. The local
    # commit above only existed to build the tree object and ship it (plus
    # its blobs) to the remote; we now point the branch at an identical
    # tree wrapped in an API-created commit. `head_sha` is the base we
    # branched from (asserted == origin/<base_branch> above), so it's the
    # new commit's sole parent.
    _recommit_via_api(workdir, repo, branch, title, parent_sha=head_sha)


def _recommit_via_api(
    workdir: Path, repo: str, branch: str, title: str, parent_sha: str,
) -> None:
    """Replace ``branch``'s head with a bot-authored, signed commit.

    Creates a commit via ``POST /repos/{repo}/git/commits`` with the App
    installation token (``gh_api`` already uses it) pointing at HEAD's
    tree, then fast-forwards-or-force the branch ref to it. With the App
    token and no explicit author/committer, GitHub stamps both as the
    bot and signs the commit — identical to the engine's setup-PR path.

    Best-effort: if the API path fails (e.g. only the fallback
    GITHUB_TOKEN is available, or contents:write is missing), the branch
    keeps the already-pushed local commit so the PR still opens. The
    local git identity is set to the bot in prepare_workdir, so even that
    fallback commit is attributed to remyx-ai[bot] — just not Verified.
    """
    try:
        tree_sha = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=workdir, capture_output=True, text=True, check=True,
        ).stdout.strip()
        # No author/committer → GitHub uses the token's identity (the bot)
        # for both and signs the commit. Mirrors the engine passing
        # committer=None on the contents API.
        commit = gh_api("POST", f"/repos/{repo}/git/commits", {
            "message": title,
            "tree": tree_sha,
            "parents": [parent_sha],
        })
        new_sha = commit["sha"]
        # force=True: the new commit is a sibling of the pushed local
        # commit (same parent, same tree, different author), not a
        # descendant, so a plain update would be rejected as non-ff.
        gh_api("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {
            "sha": new_sha,
            "force": True,
        })
        log.info(f"  → re-authored {branch} head as remyx-ai[bot] "
                 f"(verified): {new_sha[:8]}")
    except Exception as e:  # noqa: BLE001 — degrade gracefully, never block the PR
        log.warning(
            f"  → could not re-author commit via API ({e}); keeping the "
            f"pushed commit. PR will open but the commit won't be Verified."
        )


# ─── Downgrade-to-Issue helper ─────────────────────────────────────────────


def _capture_implementation_diff(
    workdir: Path, max_bytes: int = 50_000,
) -> str:
    """Stage everything in ``workdir`` and return the diff against HEAD.

    Used by downgrade paths that fire *after* the coding agent has
    written real code — without this, the implementation is silently
    thrown away when the orchestrator routes to Issue instead of PR.
    The workdir is a tempdir about to be cleaned up, so the `git add`
    side-effect doesn't bleed anywhere.

    Returns ``""`` on any failure (git not on PATH, no HEAD to diff
    against, etc.) — diff inclusion is best-effort and must never block
    the Issue-opening path. Truncates at ``max_bytes`` with a footer
    line indicating the truncation so the rendered Markdown is still
    valid and the maintainer knows the patch isn't complete.
    """
    try:
        subprocess.run(
            ["git", "add", "-A"], cwd=workdir, check=True,
            capture_output=True, timeout=30,
        )
        # Exclude the orchestrator's scratchpad files from the user-facing
        # diff. `.remyx-recommendation/` holds CONTEXT.md, GUARDRAILS.md,
        # INVOCATION.md, PAPER.md, SPEC.md — internal agent prompts that
        # leak orchestrator phrasing into the Issue body otherwise. The
        # pathspec exclusion runs inside git, so the diff captured is
        # already clean — no post-parse filtering needed.
        proc = subprocess.run(
            [
                "git", "diff", "--staged", "--",
                ".", ":(exclude).remyx-recommendation",
            ],
            cwd=workdir,
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return ""
        diff = proc.stdout or ""
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        log.debug(f"  diff capture for {workdir} failed: {e}")
        return ""
    if len(diff) > max_bytes:
        cut = diff[:max_bytes].rstrip()
        diff = (
            cut
            + f"\n\n…[diff truncated at {max_bytes:,} bytes; "
              f"original was {len(diff):,} bytes]\n"
        )
    return diff


def _render_implementation_diff_section(diff: str) -> str:
    """Wrap a captured diff in the Markdown section the Issue body uses.

    Empty diff renders to ``""`` so the caller doesn't need to gate the
    section. A ``<details>`` collapse keeps the section unobtrusive in
    the rendered Issue but immediately expandable for review. The
    fence is ``diff`` so GitHub colors additions / deletions natively.
    """
    diff = (diff or "").strip()
    if not diff:
        return ""
    line_count = diff.count("\n") + 1
    return (
        "\n## Proposed implementation\n\n"
        "The coding agent wrote a working draft before the downgrade "
        "gate fired. Apply locally with `git apply` after saving the "
        "block below.\n\n"
        f"<details>\n<summary>Diff ({line_count} lines)</summary>\n\n"
        f"```diff\n{diff}\n```\n\n"
        "</details>\n\n"
    )


def _render_selection_note_section(selection_note: str) -> str:
    """Render the "Why this candidate" rationale for downgrade Issues.

    Parity with the PR body's selection section — gives the maintainer
    the *why this paper from the lookback pool* answer that's currently
    only visible in PR bodies and the step summary. Empty when the note
    is missing or is the parenthetical fallback string (e.g. "(selection
    pass unavailable …)") that would render as a non-explanation.
    """
    note = (selection_note or "").strip()
    if not note or note.startswith("("):
        return ""
    return (
        f"## Why this candidate (selected from the lookback pool)\n\n"
        f"{note}\n\n"
    )


def _render_selection_rejected_section(
    selection_rejected: list[dict] | None,
) -> str:
    """Render the "what else did Outrider consider" collapsed details
    block. Mirrors the step-summary surface so a reviewer reading only
    the Issue body can still see which alternatives were rejected and
    why. Empty when the list is missing.
    """
    items = selection_rejected or []
    if not items:
        return ""
    lines: list[str] = []
    lines.append(
        "## What else Outrider considered this run\n\n"
        f"<details><summary>{len(items)} other candidate(s) "
        f"considered and rejected</summary>\n"
    )
    for r in items[:10]:
        arxiv = (r.get("arxiv_id") or "").strip()
        title = (r.get("title") or "(untitled)")[:120]
        reason = (r.get("reason") or "")[:240]
        if arxiv:
            lines.append(f"- [`{arxiv}`](https://arxiv.org/abs/{arxiv}) — {title}")
        else:
            lines.append(f"- {title}")
        if reason:
            lines.append(f"  - _{reason}_")
    if len(items) > 10:
        lines.append(f"- _…and {len(items) - 10} more_")
    lines.append("\n</details>\n\n")
    return "\n".join(lines)


def _open_downgrade_issue(
    target: Target, rec: Recommendation, reason: str, detail: str,
    implementation_diff: str = "",
    *,
    tldr: str = "",
    engineering_section: str = "",
    selection_note: str = "",
    selection_rejected: list[dict] | None = None,
    skip_paper_reasoning_section: bool = False,
    suppress_suggested_experiment: bool = False,
    replacement_experiment: str = "",
    footer_override: str = "",
    # publish=branch preservation: when the downgrade fires AFTER the
    # coding agent produced code (integration / stub-density / diff-risk
    # / test-integration / self-review-orphan), callers pass workdir +
    # branch + base_branch so branch-mode can commit_and_push the agent's
    # work and raise BranchPushedFromDowngrade instead of filing an Issue.
    # Pre-coding callers (selection-time / preflight) omit these — the
    # sentinel path via open_issue (LeadCapturedInBranchMode) handles them.
    workdir: Optional[Path] = None,
    branch: str = "",
    base_branch: str = "main",
) -> tuple[str, int]:
    """Open an Issue when an automated gate downgrades a PR-candidate.

    Used for preflight / integration / stub-density / test-integration /
    self-review-orphan / substitution branches in process_target. The
    body explains why this paper is interesting (so the team keeps the
    discovery signal) and why we didn't open a PR (so the routing
    decision is auditable). When the downgrade fires *after* the coding
    agent wrote code, callers pass ``implementation_diff`` so the
    maintainer can review and apply the work instead of re-deriving it.

    Optional kwargs (added in v1.4.5 to tighten reviewer triage):

      tldr: at-a-glance one-paragraph summary; opens the body when set
      engineering_section: pre-rendered "## Engineering verdict" block
        (_render_engineering_section). Rendered immediately above the
        license section so the two verdicts read as adjacent,
        independent calls.
      selection_note: "Why this candidate from the pool" rationale —
        parity with PR-body selection section. Skips parenthetical
        fallback strings.
      selection_rejected: per-candidate rejection list (same shape as
        in the step summary). Renders as a collapsed details block.
      skip_paper_reasoning_section: when True (preflight case), skip
        the orchestrator's own "Why this paper" section because the
        preflight's `detail` already covers the topic in depth.
      suppress_suggested_experiment: when True (preflight case where
        the paper's suggested experiment was judged hollow), omit
        the orchestrator's "Suggested experiment" section.
      replacement_experiment: substitute for the paper's suggested
        experiment when non-empty (and not suppressed). Used by
        preflight to redirect the reviewer toward a viable slice.
      footer_override: per-route attribution line. When empty the
        default attributes to coding-agent Issue-mode election (the
        legacy default); callers pass the routing-specific text.
    """
    # publish=branch short-circuit for post-coding downgrades. When the
    # coding agent produced code (workdir + branch supplied), preserve that
    # work by pushing the branch to the fork and raising the sentinel that
    # process_target catches. The downgrade reason + detail travel with the
    # sentinel so the step summary can render the "would have been blocked
    # under publish=pr" context. Team reviews the branch + reasoning,
    # decides whether to promote to PR or delete.
    publish_mode = (os.environ.get("INPUT_PUBLISH") or "pr").strip().lower()
    if publish_mode == "branch" and workdir is not None and branch:
        try:
            commit_and_push(
                workdir, branch, format_pr_title(rec),
                repo=target.repo, base_branch=base_branch,
            )
        except Exception as e:
            log.warning(f"  branch push failed under publish=branch: {e}")
        branch_url = f"https://github.com/{target.repo}/tree/{branch}"
        raise BranchPushedFromDowngrade(branch, branch_url, reason, detail)

    title = format_pr_title(rec)

    sections: list[str] = []
    sections.append(
        f"**Recommended paper**: "
        f"[{rec.paper_title}](https://arxiv.org/abs/{rec.arxiv_id})\n"
        f"**Confidence**: {rec.tier} "
        f"(Remyx relevance {rec.relevance_score:.2f})\n"
        f"**Research interest**: {rec.interest_name or '(unnamed)'}\n"
        f"\n---\n"
    )

    if tldr.strip():
        sections.append(f"\n## TL;DR\n\n{tldr.strip()}\n")

    # Engineering verdict then license verdict, adjacent — two
    # independent calls, not interleaved prose.
    if engineering_section.strip():
        sections.append(engineering_section.rstrip() + "\n")

    license_section = _render_license_section(rec)
    if license_section.strip():
        sections.append(license_section.rstrip() + "\n")

    selection_section = _render_selection_note_section(selection_note)
    if selection_section:
        sections.append(selection_section)

    if not skip_paper_reasoning_section:
        sections.append(
            f"## Why this paper is interesting for the team\n\n"
            f"{rec.reasoning or '(no reasoning provided)'}\n"
        )

    # Suggested experiment — either the paper's original, the preflight's
    # replacement, or omitted entirely when suppressed and no replacement
    # was supplied. The replacement-experiment path lets preflight
    # override hollow suggestions without contradicting itself in the
    # body.
    experiment_text = (replacement_experiment or "").strip()
    if not experiment_text and not suppress_suggested_experiment:
        experiment_text = (rec.suggested_experiment or "").strip()
    if experiment_text:
        sections.append(f"## Suggested experiment\n\n{experiment_text}\n")

    diff_section = _render_implementation_diff_section(implementation_diff)
    if diff_section.strip():
        sections.append(diff_section.rstrip() + "\n")

    sections.append(
        f"## Why the orchestrator opened an Issue instead of a PR\n\n"
        f"**{reason}**\n\n"
        f"{detail}\n"
    )

    rejected_section = _render_selection_rejected_section(selection_rejected)
    if rejected_section:
        sections.append(rejected_section)

    body = "\n".join(s for s in sections if s)
    return open_issue(target, title, body, footer_override=footer_override)


# ─── Main per-target loop ──────────────────────────────────────────────────


def _enrich_selection_rejected(
    raw: list, viable: "list[Recommendation]"
) -> list[dict]:
    """Map selection-pass `{index, why}` entries to structured per-paper
    rejection records using the viable-candidates list as the index target.

    Each enriched entry carries:

      - ``arxiv_id`` — paper identity (engine resolves additional metadata)
      - ``title`` — for the local step-summary renderer
      - ``reason`` — free-form one-line agent rationale (rich enough for
        downstream LLM-classification or embedding-based clustering)
      - ``license_class`` — the candidate's license classification at run
        time (``permissive`` / ``copyleft`` / ``nc`` / ``missing`` /
        ``no-code-link`` / ``unknown``). Structured so cross-customer
        analysis can measure "rejection rate by license_class" without
        parsing prose
      - ``license_compat`` — float in [0, 1] from the same per-paper
        classifier; pairs with ``license_class`` for finer slicing

    Used by both the happy-path (chosen_index ≥ 0) and the all-rejected path
    (chosen_index = -1) so downstream consumers (the $GITHUB_STEP_SUMMARY
    renderer, the engine-side ``recommendation_runs`` telemetry, any
    external tooling parsing the result dict) get self-describing entries.

    The license fields are the empirically-load-bearing axis identified by
    cross-portfolio analysis (21 of 25 high-tier rejected
    candidates were ``no-code-link``). Including them in the per-rejected
    record makes that signal queryable at engine scale rather than only
    visible in the per-run step summary.
    """
    enriched = []
    for r in raw:
        idx = r.get("index")
        if isinstance(idx, int) and 0 <= idx < len(viable):
            cand = viable[idx]
            enriched.append({
                "arxiv_id": cand.arxiv_id,
                "title": cand.paper_title,
                "reason": r.get("why", ""),
                "license_class": cand.license_class,
                "license_compat": cand.license_compat,
            })
        else:
            enriched.append({
                "arxiv_id": "",
                "title": "(candidate index out of range)",
                "reason": r.get("why", ""),
                "license_class": "unknown",
                "license_compat": 0.0,
            })
    return enriched


def _compact_selection_rejected_for_telemetry(
    enriched: list[dict] | None,
    max_entries: int = 50,
    max_reason_chars: int = 300,
) -> list[dict] | None:
    """Compact projection of ``_enrich_selection_rejected``'s output for
    the engine-side telemetry payload (``recommendation_runs.selection_rejected``).

    Differences from the local step-summary representation:

      - ``title`` is dropped — engine resolves paper metadata by
        ``arxiv_id``, no need to duplicate per-run
      - ``reason`` is truncated to ``max_reason_chars`` to keep the
        payload bounded even on runs that produce 30+ rejections with
        rich rationales
      - Top-level list is capped at ``max_entries`` defensively (the
        agent's max-turns ceiling makes >30 rejections rare, but a
        misbehaving model could in principle emit a much larger list)

    Returns ``None`` when no rejections were captured, so the engine
    can distinguish "rejection list was empty" from "rejection list
    wasn't shipped this version" via the column being null vs ``[]``.
    """
    if not enriched:
        return None
    compact = []
    for entry in enriched[:max_entries]:
        reason = (entry.get("reason") or "")[:max_reason_chars]
        compact.append({
            "arxiv_id": entry.get("arxiv_id", ""),
            "license_class": entry.get("license_class", "unknown"),
            "license_compat": entry.get("license_compat", 0.0),
            "reason": reason,
        })
    return compact


def _compact_string_list_for_telemetry(
    items: list[str] | None,
    max_entries: int = 50,
    max_chars: int = 300,
) -> list[str] | None:
    """Bound a list-of-strings field for the engine telemetry payload.

    Used for unbounded list fields the agent can produce (integration
    violations, lint findings) so a misbehaving run can't bloat the
    posted row. Returns ``None`` for ``None`` / non-list input (engine
    can distinguish "field wasn't shipped" from "field was empty").
    """
    if items is None:
        return None
    if not isinstance(items, list):
        return None
    return [str(s)[:max_chars] for s in items[:max_entries]]


def _candidate_enrichment(candidates: "list[Recommendation]") -> list[dict]:
    """Per-candidate code/model/license metadata for the run-telemetry payload.

    Lets the recommendation service fill gaps in a paper's stored metadata from
    what this run resolved. ``license_compat`` is omitted on purpose: it's
    scored relative to the target repository, not a property of the paper.
    Only candidates with a resolved code/model URL or license are included, to
    keep the payload small.
    """
    out = []
    for c in candidates:
        if not c.arxiv_id:
            continue
        if not (c.paper_github_url or c.paper_huggingface_url or c.paper_license):
            continue
        out.append({
            "arxiv_id": c.arxiv_id,
            "github": c.paper_github_url or "",
            "huggingface": c.paper_huggingface_url or "",
            "paper_license": c.paper_license or "",
            "license_source": c.license_source or "",
            "license_class": c.license_class or "",
        })
    return out


def _resolve_external_candidate(selection: dict) -> "Recommendation | None":
    """Construct a synthetic Recommendation from the selection pass's
    external_* fields. Used when chosen_index = -2 — selection surfaced
    an out-of-pool candidate via `remyxai search query`.

    The minimum required fields (arxiv_id, paper_title) come from the
    search hit; the rest are filled with reasonable defaults. There's no
    engine `paper` envelope to consult, so no relevance_score / tier from
    the ranker — these are deliberately marked as broadening-search
    provenance so downstream consumers can distinguish external from
    in-pool picks.

    Returns None when the required external_* fields are missing — caller
    should fall back to `chosen_index: -1` semantics in that case.
    """
    arxiv = (selection.get("external_arxiv_id") or "").strip()
    title = (selection.get("external_title") or "").strip()
    query = (selection.get("external_query_used") or "").strip()
    if not arxiv or not title:
        return None
    return Recommendation(
        paper_title=title,
        arxiv_id=arxiv,
        tier="high",          # external picks are deliberate; signal is strong
        z_score=0.0,          # legacy field; unused
        spec_md="",           # legacy field; unused
        paper_abstract="",
        domain_summary="",
        raw_paper_md="",
        relevance_score=0.0,  # not from ranker
        reasoning=(
            f"External pick surfaced via `remyxai search query "
            f"{query!r}` — not in the engine's recommendation pool for "
            f"this interest, but verified to match the contract the "
            f"selection pass identified."
        ),
        suggested_experiment="(see contract_match + migration_cost below)",
        interest_name="(via broadening-search)",
    )


def process_target(target: Target) -> dict:
    """Run the full discovery + implementation loop for one target.
    Returns a status dict suitable for logging / Slack notify.

    Routing summary — every path leads to either a PR, an Issue, or a
    skip:

        skipped_low_confidence            — tier below min_confidence
        skipped_open_artifact             — an open Remyx PR or Issue
                                            from a prior run still
                                            exists on the target; the
                                            cadence guard avoids
                                            stacking unresolved work
        skipped_pr_exists                 — every candidate already has an
                                            open PR (or a mix of open PRs/Issues)
        skipped_issue_exists              — every candidate already has an
                                            open Remyx Issue

        issue_opened_preflight            — pre-flight (§6) routed to Issue
                                            before invoking implementation
        issue_opened                      — Claude wrote OPEN_AS_ISSUE.md
        issue_opened_no_integration       — integration validator (§2): the
                                            diff adds code nothing invokes
        issue_opened_stub_density         — stub-density validator (§3) rejected
        issue_opened_no_test_integration  — test gate (§3) found no test that
                                            imports an existing module
        issue_opened_self_review          — self-review (§4): new code is an
                                            orphan, unreachable from production

        issue_opened_substitution         — agentic selection identified a
                                            replacement / pipeline-
                                            simplification candidate (vs.
                                            additive drop-in), OR
                                            surfaced an out-of-pool
                                            candidate via broadening-
                                            search (chosen_index = -2);
                                            routed to Issue because the
                                            swap needs dep changes the
                                            PR guardrails block

        rejected_path_violations          — Claude touched out-of-bounds paths
        skipped_by_selection_verification — agentic selection verified every
                                            ranker candidate and rejected
                                            them all (structural mismatch
                                            against the repo's actual
                                            modules)
        skipped_test_failure              — draft_mode=never and tests failed
        claude_failed                     — Claude CLI exited non-zero

        pr_opened / pr_opened_draft       — happy path
    """
    result: dict = {"repo": target.repo, "status": "unknown"}

    # 1. Cadence guard — cheapest gate, before any candidate work or
    #    checkout. Time-decayed: skip only if the most recently opened
    #    Remyx artifact on the target is *younger* than rate_limit_days.
    #    Older open artifacts age out of the throttle window (real
    #    maintainers leave Issues open for weeks; the action should
    #    resume cadence rather than mute the repo indefinitely).
    #    Engagement (merge/close) still clears the gate immediately.
    #    Per-paper dedup (further down) handles same-recommendation retries.
    if target.rate_limit_days > 0:
        age = _most_recent_open_artifact_age_days(target)
        if age is not None and age < target.rate_limit_days:
            log.info(
                f"  cadence guard: most recent open Remyx artifact is "
                f"{age}d old (< rate_limit_days={target.rate_limit_days}); "
                f"skipping. Older open artifacts no longer block."
            )
            result["status"] = "skipped_open_artifact"
            result["most_recent_artifact_age_days"] = age
            return result

    # 1b. Issues-disabled pre-flight — GitHub disables the Issues tab on
    #     forks (and some repos) by default; without it, every Issue-route
    #     artifact this run might emit (high-risk downgrade, no-integration,
    #     stub-density, self-review, substitution, preflight, OPEN_AS_ISSUE)
    #     fails at the POST /issues step. The scoped App token can't
    #     re-enable Issues (PATCH /repos requires `administration: write`,
    #     not granted), so a single GET here saves a full selection +
    #     scaffold pass that would be wasted at the Issue step. The race
    #     where Issues get disabled mid-run is caught by IssuesDisabledError
    #     below (see open_issue + the except handler before the finally).
    try:
        repo_meta = gh_api("GET", f"/repos/{target.repo}")
    except RuntimeError as e:
        log.warning(f"  repo metadata fetch failed ({e}); assuming Issues enabled")
        repo_meta = {"has_issues": True}
    if not repo_meta.get("has_issues", True):
        log.info(f"  Issues disabled on {target.repo}; skipping. "
                 f"Enable: gh repo edit {target.repo} --enable-issues")
        result["status"] = "skipped_issues_disabled"
        return result

    # 2. Query the candidate pool over the lookback window (default: the
    #    past week). The old flow took only papers[0], wasting the
    #    lookback; we keep the whole pool so the selection pass can pick
    #    the most implementable candidate.
    #
    #    Pin-method short-circuit: when the user passes a method query
    #    (or a literal arxiv_id) via `pin-method`, resolve it to a single
    #    /search/assets envelope and use that asset as the sole
    #    candidate — bypassing both the interest's recommendation pool
    #    and the LLM selection pass. The downstream pin_arxiv check at
    #    §4 then picks this candidate naturally (no extra plumbing).
    if target.pin_arxiv:
        # Short-circuit: fetch the pinned paper directly, skip the ranker
        # pool entirely (bypasses /papers/recommended, refine queries, and
        # per-candidate license enrichment on the full pool). Explicit
        # "use THIS paper" intent doesn't need the ranker's opinion.
        asset = _remyx_get_asset(target.pin_arxiv)
        if asset is None:
            log.info(f"  ✗ skipped_pin_arxiv_not_found: pin-arxiv "
                     f"{target.pin_arxiv!r} not found in Remyx catalog")
            result["status"] = "skipped_pin_arxiv_not_found"
            result["pin_arxiv_requested"] = target.pin_arxiv
            return result
        rec = _asset_to_recommendation(
            asset, refine_query=f"pin-arxiv:{target.pin_arxiv}",
            fallback_interest_name="(pin-arxiv)",
            interest_context="",
            experiment_history="",
        )
        log.info(
            f"  → pin-arxiv {target.pin_arxiv!r} resolved to "
            f"{rec.paper_title[:60]}… (skipped ranker pool)"
        )
        candidates = [rec]
        result["pin_arxiv_resolution"] = {
            "arxiv_id": rec.arxiv_id,
            "title": rec.paper_title,
        }
    elif target.search_method:
        asset = _resolve_pin_method(target.search_method)
        if asset is None:
            log.info(f"  ✗ skipped_no_method_match: pin-method "
                     f"{target.search_method!r} resolved to no asset")
            result["status"] = "skipped_no_method_match"
            result["search_method"] = target.search_method
            return result
        rec = _asset_to_recommendation(
            asset, refine_query=f"search-method:{target.search_method}",
            fallback_interest_name="(pin-method)",
            interest_context="",
            experiment_history="",
        )
        log.info(
            f"  → search-method {target.search_method!r} resolved to "
            f"{rec.arxiv_id} ({rec.paper_title[:60]}…)"
        )
        candidates = [rec]
        result["search_method"] = target.search_method
        result["search_method_resolution"] = {
            "query": target.search_method,
            "arxiv_id": rec.arxiv_id,
            "title": rec.paper_title,
        }
        # Reduce pin-method to pin_arxiv so the existing pinning logic at
        # §4 selects this candidate without a parallel code path. The
        # original search_method string is preserved in result["search_method"]
        # for the step summary.
        target.pin_arxiv = rec.arxiv_id
    else:
        candidates = query_remyx_candidates(target)
    # License + github/HF enrichment parity between the pool path and the
    # pin-arxiv / search-method fast-paths. `query_remyx_candidates` already
    # applies `_enrich_candidate_licenses` internally; the fast-paths bypass
    # that call and would otherwise send unenriched candidates through
    # preflight (empty paper_github_url → fidelity falls back to
    # paper-anchored mode; license_class stays "" → downstream renderers
    # show unknown license verdict). Idempotent — no-op when the candidate
    # already has license/URL fields populated by the pool path.
    if (target.pin_arxiv or target.search_method) and candidates:
        if os.environ.get("REMYX_LICENSE_GATE", "1") != "0":
            _enrich_candidate_licenses(candidates, target)
    result["candidates_returned"] = len(candidates)
    # Pool-composition + license-distribution telemetry.
    # Post-dedup counts (query_remyx_candidates coalesces families before
    # returning). Carried on the result dict so the step summary can
    # surface them and the weekly summary can aggregate across runs;
    # these are also the fields engine-side run telemetry will persist.
    broad_n, refine_n = _pool_composition(candidates)
    result["broad_pool_size"] = broad_n
    result["refine_pool_size"] = refine_n
    if _RUN_REFINE_QUERIES:
        result["refine_queries"] = list(_RUN_REFINE_QUERIES)
    if os.environ.get("REMYX_LICENSE_GATE", "1") != "0":
        result["license_class_counts"] = _license_class_counts(candidates)

    # 3. Per-candidate gates. Drop anything below the confidence tier or
    #    already in flight — an open PR for its branch, OR an open Remyx
    #    Issue for the paper. The Issue check matters with a longer
    #    lookback: a sticky top candidate that keeps routing to Issue would
    #    otherwise be re-selected every run and reopen a duplicate Issue.
    #    Symmetric discharge: a paper is considered addressed
    #    once *any* Outrider Issue exists for it, open or closed. Open
    #    means "still in flight" and closed means "the team has made a
    #    call" — both signal "stop re-recommending." Reopen the Issue
    #    to re-engage. Running this BEFORE the clone preserves the
    #    "don't check out the repo if nothing is actionable"
    #    optimization the single-pick flow had.
    min_required = TIER_RANK.get(target.min_confidence.lower(), 2)
    # `open_issues` is misnamed historically — it now carries the full
    # discharge set: Outrider-opened Issues (any state) PLUS maintainer-
    # opened Issues that reference an arxiv id in their body. The
    # broader invariant is "a paper has been put in front of the team;
    # don't waste budget re-deriving it" regardless of who opened the
    # Issue.
    open_issues = _all_discharge_issues(target)
    # Pin-method / pin-arxiv = user explicitly named the paper; their
    # intent overrides the discharge throttle (which is otherwise a
    # "don't keep re-recommending what Outrider already pitched" guard
    # for the normal selection flow). Without this override, A/B-style
    # re-runs, demo re-takes, and "improve the prior artifact" workflows
    # would always skip with skipped_issue_exists. PR-collision check
    # below stays active — that's a real safety property.
    pin_override = bool(target.search_method or target.pin_arxiv)
    viable: list[Recommendation] = []
    dropped_low_conf = 0
    dropped_pr_exists = 0
    dropped_issue_exists = 0
    for c in candidates:
        if TIER_RANK.get(c.tier.lower(), 0) < min_required:
            dropped_low_conf += 1
            continue
        c_branch = format_branch_name(c)
        if existing_pr_for(target, c_branch):
            dropped_pr_exists += 1
            continue
        if not pin_override:
            prior_issue = issue_for_paper(open_issues, c)
            if prior_issue:
                dropped_issue_exists += 1
                continue
        viable.append(c)

    if not viable:
        # Nothing actionable. Prefer the most specific skip reason.
        if dropped_low_conf and not dropped_pr_exists and not dropped_issue_exists:
            result["status"] = "skipped_low_confidence"
            log.info(f"  ✗ no candidate at/above min {target.min_confidence}; "
                     f"skipping")
        elif dropped_issue_exists and not dropped_pr_exists:
            result["status"] = "skipped_issue_exists"
            log.info(f"  ✗ all {dropped_issue_exists} candidate(s) already "
                     f"have prior Outrider Issues (open or closed); skipping")
        else:
            # PR dedup, or a mix of open PRs and prior Issues.
            result["status"] = "skipped_pr_exists"
            log.info(f"  ✗ all candidates already in flight "
                     f"({dropped_pr_exists} open PRs, "
                     f"{dropped_issue_exists} prior Issues); skipping")
        return result

    log.info(f"  ✓ {len(viable)} viable candidate(s) "
             f"(dropped {dropped_low_conf} low-confidence, "
             f"{dropped_pr_exists} open PRs, "
             f"{dropped_issue_exists} prior Issues)")

    # Per-candidate code/model/license metadata for the run-telemetry payload.
    # Set once here (viable is final) so every return path carries it.
    result["candidate_enrichment"] = _candidate_enrichment(viable)

    # 4. Workdir + selection. Clone first (the selection pass needs the
    #    repo's module layout), then let Claude pick the candidate most
    #    directly implementable against this repo. Selection only chooses
    #    WHICH paper — the PR-vs-Issue decision stays with the gates below.
    workdir = prepare_workdir(target)
    try:
        package = detect_package_name(workdir)
        default_branch = detect_default_branch(workdir)
        # Load ENVIRONMENTS.md once, early, so the selection pass sees
        # workflow-attached tooling before it verifies candidates. Thread
        # the body through to both selection and write_spec_bundle to
        # avoid a second load.
        env_body = _load_environments_md(workdir)
        log.info(f"  detected package: {package}  default branch: {default_branch}")

        pinned_idx = None
        if target.pin_arxiv:
            pinned_idx = next(
                (i for i, c in enumerate(viable) if c.arxiv_id == target.pin_arxiv),
                None,
            )
            if pinned_idx is None:
                # Defensive: the pin-arxiv fast-path at §2 injects the
                # pinned paper directly, so this branch is effectively
                # unreachable. Log + skip if it ever fires.
                log.error(
                    f"  ✗ pin-arxiv {target.pin_arxiv!r} unexpectedly "
                    f"absent from viable pool (fast-path bug?); skipping"
                )
                result["status"] = "skipped_pin_arxiv_not_found"
                result["pin_arxiv_requested"] = target.pin_arxiv
                return result
        if pinned_idx is not None:
            rec = viable[pinned_idx]
            # pin-method reduces to pin-arxiv internally (see §2); surface
            # the original user-facing source in the reasoning so step
            # summaries / telemetry are honest about which input drove the pin.
            if target.search_method:
                result["selection_reasoning"] = (
                    f"(pinned via search-method={target.search_method!r} → "
                    f"{target.pin_arxiv})"
                )
            else:
                result["selection_reasoning"] = (
                    f"(pinned via pin-arxiv={target.pin_arxiv})"
                )
            log.info(f"  ✓ pinned candidate [{pinned_idx}] {rec.paper_title[:50]}…")
        else:
            selection = select_recommendation(
                workdir, package, viable, target=target,
                discharged_issues=open_issues,
                env_body=env_body,
            )
            # Attach exploration-coverage telemetry once, before the branch
            # handling — every downstream path returns this same `result`, so
            # the fields ride along regardless of the verdict, and the run's
            # JSON output carries them for later analysis.
            if selection is not None:
                if "selection_coverage" in selection:
                    result["selection_coverage"] = selection["selection_coverage"]
                if "selection_context_efficiency" in selection:
                    result["selection_context_efficiency"] = (
                        selection["selection_context_efficiency"]
                    )
                # Code-override audit field — attached
                # once before the branch logic so every downstream path
                # (in-pool, extension, external, skip) carries it. Field
                # is only set when the agent populated and validated it
                # (`select_recommendation` drops it on contract violations
                # and on non-no-code candidates).
                if selection.get("code_override_justification"):
                    result["selection_code_override_justification"] = (
                        selection["code_override_justification"]
                    )
                # Re-pick telemetry — surface on the result dict so
                # downstream analysis can tell which dispatches picked
                # already-dispatched arxivs (with justification) apart
                # from novel picks.
                if selection.get("is_re_pick"):
                    result["selection_is_re_pick"] = True
                    result["selection_re_pick_justification"] = (
                        selection.get("re_pick_justification", "")
                    )
            if selection is not None and selection.get("chosen_index") == -1:
                # Agentic selection rejected every candidate after verification
                # (or, in coverage-gate enforce mode, an under-explored pick
                # was downgraded to a skip). Either way the user-facing outcome
                # is the same skip status — the under-explored reason is kept
                # only in the internal selection_coverage telemetry, never the
                # user-facing step summary.
                result["status"] = "skipped_by_selection_verification"
                result["selection_reasoning"] = selection.get("reasoning", "")
                result["selection_rejected"] = _enrich_selection_rejected(
                    selection.get("rejected") or [], viable
                )
                # Confabulation check: if the reasoning cites paths not
                # present in the workdir, an operator scanning the step
                # summary should see the mismatch prominently before
                # trusting a "rejected all candidates" verdict.
                path_check = _check_selection_paths(workdir, result["selection_reasoning"])
                result["selection_reasoning_paths"] = path_check
                if path_check["cited"] and not path_check["verified"]:
                    log.warning(
                        "  ⚠ selection reasoning cites %d path(s) — 0 verified in workdir; possible confabulation",
                        len(path_check["cited"]),
                    )
                if selection.get("under_explored"):
                    log.info("  ✗ skipped (coverage gate: under-explored)")
                else:
                    log.info("  ✗ skipped_by_selection_verification: every "
                             "candidate failed verification")
                return result
            if selection is not None and selection.get("chosen_index") == -2:
                # External pick — selection surfaced an out-of-pool candidate
                # via broadening-search. Construct a synthetic Recommendation
                # from the external_* fields and route straight to an
                # `issue_opened_substitution` Issue (PR track is blocked by
                # guardrails for any out-of-pool candidate; deps change).
                external_rec = _resolve_external_candidate(selection)
                if external_rec is None:
                    # Defensive — select_recommendation already validates the
                    # external_* fields are present, so this branch is reached
                    # only on programmer error.
                    result["status"] = "skipped_by_selection_verification"
                    result["selection_reasoning"] = (
                        "(external pick proposed but required external_* "
                        "fields were missing)"
                    )
                    return result
                rec = external_rec
                result["selection_reasoning"] = selection.get("reasoning", "")
                result["selection_rejected"] = _enrich_selection_rejected(
                    selection.get("rejected") or [], viable
                )
                result["selection_external_arxiv_id"] = (
                    selection.get("external_arxiv_id", "")
                )
                result["selection_external_query_used"] = (
                    selection.get("external_query_used", "")
                )
                # Dedup gate for external picks. Engine-pool candidates are
                # filtered against existing Outrider Issues at the viability
                # gate above, but a broadening-search pick is born inside the
                # selection pass and never passes through that gate. Without
                # this check the same paper gets re-recommended on every run.
                # Symmetric: matches against any prior Outrider Issue (open
                # or closed). `open_issues` is misnamed at this point — it
                # now carries the all-state set.
                existing_issue = issue_for_paper(open_issues, rec)
                if existing_issue is not None:
                    issue_state = existing_issue.get("state", "open")
                    result["status"] = "skipped_external_issue_exists"
                    result["existing_issue_url"] = existing_issue.get(
                        "html_url", ""
                    )
                    result["existing_issue_state"] = issue_state
                    state_phrase = (
                        "open Issue" if issue_state == "open"
                        else "closed Issue (team resolved)"
                    )
                    log.info(
                        f"  ✗ skipped_external_issue_exists: external pick "
                        f"{rec.arxiv_id} already has {state_phrase} "
                        f"{existing_issue.get('html_url', '')}"
                    )
                    return result
                shape = (selection.get("integration_shape") or "simplification").lower().strip()
                result["selection_integration_shape"] = shape
                shape_label = {
                    "addition":       "out-of-pool addition",
                    "replacement":    "out-of-pool drop-in replacement",
                    "simplification": "out-of-pool pipeline simplification",
                    "extension":      "out-of-pool extension (new capability)",
                }.get(shape, "out-of-pool substitution")
                # Extension-shape picks thread the new schema fields
                # into the result so the downgrade Issue body and step
                # summary can surface them. REQUIRED for shape=extension;
                # absent on other shapes by design.
                if shape == "extension":
                    result["selection_team_direction_signal"] = (
                        selection.get("team_direction_signal", "")
                    )
                    result["selection_proposed_call_site"] = (
                        selection.get("proposed_call_site", "")
                    )
                contract_match = selection.get("contract_match", "")
                migration_cost = selection.get("migration_cost", "")
                result["selection_contract_match"] = contract_match
                result["selection_migration_cost"] = migration_cost
                # Engineering axis rendered as its own section (adjacent
                # to the license section in the body) instead of fused
                # into the routing prose. Extension
                # picks use different schema fields: team_direction_signal
                # and proposed_call_site instead of contract_match /
                # migration_cost (which don't apply when there's no
                # existing call site).
                if shape == "extension":
                    tds = selection.get("team_direction_signal", "")
                    pcs = selection.get("proposed_call_site", "")
                    engineering_section = _render_engineering_section(
                        integration_shape=shape_label,
                        team_direction_signal=tds or "(none reported)",
                        proposed_call_site=pcs or "(none reported)",
                    )
                    detail = (
                        f"_Selection reasoning_: "
                        f"{selection.get('reasoning', '')}\n\n"
                        f"This candidate proposes a NEW capability the "
                        f"repository does not currently have. The selection "
                        f"pass verified that the team has signaled openness "
                        f"to this capability via the direction signal "
                        f"in the Engineering verdict above (an RFC, a "
                        f"README roadmap item, or a CONTEXT.md investment "
                        f"pattern). Opening as an "
                        f"Issue rather than a PR because there is no "
                        f"existing call site to integrate against — this "
                        f"is a proposal for the maintainer to weigh, not a "
                        f"drop-in implementation."
                    )
                else:
                    engineering_section = _render_engineering_section(
                        integration_shape=shape_label,
                        contract_match=contract_match or "(none reported)",
                        migration_cost=migration_cost or "(none reported)",
                    )
                    detail = (
                        f"_Selection reasoning_: {selection.get('reasoning', '')}\n\n"
                        f"This candidate was surfaced via `remyxai search query "
                        f"{selection.get('external_query_used', '')!r}` — it is NOT "
                        f"in the engine's recommendation pool for this interest. "
                        f"The selection pass identified it via broadening-search "
                        f"after verifying that no in-pool candidate cleanly fits "
                        f"the contract the maintainer thread or search context "
                        f"pointed at. Opening as an Issue (rather than a draft PR) "
                        f"because external picks need dependency changes that "
                        f"fall outside the PR guardrails."
                    )
                _record_verdict_fields(result, rec)
                issue_url, issue_number = _open_downgrade_issue(
                    target, rec,
                    reason=f"Selection identified an {shape_label} candidate",
                    detail=detail,
                    engineering_section=engineering_section,
                    selection_note=selection.get("reasoning", ""),
                    selection_rejected=result.get("selection_rejected"),
                    footer_override=(
                        f"_Opened by the [Remyx Recommendation]"
                        f"({CANONICAL_ATTRIBUTION_URL}) orchestrator. "
                        f"Selection identified an out-of-pool {shape_label} "
                        f"via broadening-search; routed to Issue because "
                        f"external picks need dependency changes that fall "
                        f"outside the PR guardrails._"
                    ),
                )
                result.update({
                    "paper": rec.paper_title,
                    "arxiv": rec.arxiv_id,
                    "tier": rec.tier,
                    "recommendation_id": rec.recommendation_id or None,
                    "candidates_considered": len(viable),
                    "status": "issue_opened_substitution",
                    "issue_url": issue_url,
                    "issue_number": issue_number,
                })
                log.info(
                    f"  ✓ issue_opened_substitution ({shape}, external): "
                    f"{issue_url}"
                )
                return result
            if selection is not None:
                rec = viable[selection["chosen_index"]]
                result["selection_reasoning"] = selection.get("reasoning", "")
                result["selection_rejected"] = _enrich_selection_rejected(
                    selection.get("rejected") or [], viable
                )
                # The former substitution guard fired here on
                # `shape in ("replacement", "simplification")` and short-
                # circuited to Issue. Removed: the workflow's path allowlist
                # (`DEFAULT_ALLOWLIST_GLOBS`) already blocks dep files, and
                # `check_integration()` measures oversized existing-file
                # edits and orphan additions on the actual diff — replacing
                # the shape label with measured evidence.
                shape = (selection.get("integration_shape") or "addition").lower().strip()
                result["selection_integration_shape"] = shape
                result["selection_contract_match"] = (
                    selection.get("contract_match", "")
                )
                result["selection_migration_cost"] = (
                    selection.get("migration_cost", "")
                )
            else:
                rec = _fallback_candidate(viable)
                result["selection_reasoning"] = (
                    "(selection pass unavailable — used highest-relevance "
                    "candidate as fallback)"
                )
        result.update({
            "paper": rec.paper_title,
            "arxiv": rec.arxiv_id,
            "tier": rec.tier,
            "recommendation_id": rec.recommendation_id or None,
            "candidates_considered": len(viable),
        })
        _record_verdict_fields(result, rec)
        log.info(f"  ✓ selected: [{rec.tier}] {rec.paper_title}")

        # Catalog-reference-confidence probe (fires on every run, not just
        # runs where the fidelity gate itself fires). Cheap GitHub Contents
        # API sniff of the linked repo's README — emits confidence tier +
        # signals into the run summary so catalog data-quality issues
        # (paper-to-github mislinks like 2503.14432v2 → microsoft/JARVIS)
        # can be tracked in aggregate independent of Mode / publish setting.
        if rec.paper_github_url:
            try:
                cat_conf, cat_signals = _sniff_reference_confidence_remote(
                    rec.paper_github_url, rec.arxiv_id, rec.paper_title,
                )
            except Exception as e:  # noqa: BLE001 — telemetry never fails a run
                log.warning(f"  ⚠ catalog reference sniff raised: {e}")
                cat_conf, cat_signals = "unknown", {}
            result["catalog_reference_confidence"] = cat_conf
            result["catalog_reference_signals"] = cat_signals
            log.info(
                f"  → catalog-reference-confidence: {cat_conf} "
                f"(paper_github_url={rec.paper_github_url})"
            )
        else:
            result["catalog_reference_confidence"] = "no_reference"
            result["catalog_reference_signals"] = {}

        # Confabulation check on selection reasoning (see comment in the
        # skipped-by-verification branch). Runs while workdir is still
        # available; step-summary renderer picks it up.
        selection_reasoning_str = result.get("selection_reasoning") or ""
        if selection_reasoning_str and not selection_reasoning_str.startswith("("):
            path_check = _check_selection_paths(workdir, selection_reasoning_str)
            result["selection_reasoning_paths"] = path_check
            if path_check["cited"] and not path_check["verified"]:
                log.warning(
                    "  ⚠ selection reasoning cites %d path(s) — 0 verified in workdir; possible confabulation",
                    len(path_check["cited"]),
                )

        # Duplicate-work enforcement — process_target-level guard that
        # catches both agentic-selection picks AND fallback picks (when
        # the selection Claude call fails and _fallback_candidate runs).
        #
        # Bypass hierarchy (ordered — first match wins, guard doesn't fire):
        #   1. INPUT_PIN_ARXIV set — user explicitly requested this paper
        #   2. INPUT_START_FROM_REF set — refinement flow, compounding by design
        #   3. INPUT_LEAD_CONTENT set — user/orchestrator provided scope for this run
        #   4. selection_is_re_pick=true with justification — model self-flagged
        #
        # Otherwise, if picked arxiv is in observed_landing_zones, coerce
        # to skipped_arxiv_already_landed. Prevents fallback path from
        # bypassing intel-aware re-pick machinery on transient selection
        # failures (observed z.ai empty-output cases).
        _maintain_state = (
            (os.environ.get("INPUT_MAINTAIN_STATE") or "").strip().lower()
            in ("true", "1", "yes")
        )
        if _maintain_state:
            _pin_arxiv = (os.environ.get("INPUT_PIN_ARXIV") or "").strip()
            _start_from_ref = (os.environ.get("INPUT_START_FROM_REF") or "").strip()
            _lead_content = (os.environ.get("INPUT_LEAD_CONTENT") or "").strip()
            _bypass_reason = None
            if _pin_arxiv:
                _bypass_reason = f"pin-arxiv={_pin_arxiv!r} explicitly set"
            elif _start_from_ref:
                _bypass_reason = f"start-from-ref={_start_from_ref!r} — refinement flow"
            elif _lead_content:
                _bypass_reason = "lead-content set — orchestrator provided scope"
            elif result.get("selection_is_re_pick"):
                _bypass_reason = "selection self-flagged is_re_pick with justification"

            if not _bypass_reason:
                _intel = _load_fork_repo_intel(workdir)
                if _intel is not None:
                    _picked = _arxiv_versionless(rec.arxiv_id or "")
                    _dispatched = {
                        _arxiv_versionless(d["arxiv"])
                        for d in _extract_dispatched_arxivs(_intel)
                        if d.get("arxiv")
                    }
                    if _picked and _picked in _dispatched:
                        log.warning(
                            "  ⚠ duplicate-work guard: picked arxiv %s "
                            "already has a landing in .remyx/repo_intel.yaml "
                            "on this fork (fallback path bypassed re-pick "
                            "enforcement); skipping this dispatch",
                            _picked,
                        )
                        result["status"] = "skipped_arxiv_already_landed"
                        result["skipped_arxiv"] = _picked
                        return result
            else:
                log.info(
                    "  → duplicate-work guard bypassed: %s (proceeding with "
                    "implementation regardless of prior landing)",
                    _bypass_reason,
                )
                result["duplicate_work_guard_bypass_reason"] = _bypass_reason

        # 5. Spec bundle for the chosen candidate. Thread the selection
        # rationale through so pre-flight and the implementer evaluate the
        # same scoped framing the selection pass reasoned about.
        branch = format_branch_name(rec)
        # Collision-free branch naming: when the derived slug already
        # exists on the fork (e.g. a prior dispatch of the same paper
        # produced a branch that never got a PR), append -v2..-vN so
        # this dispatch doesn't force-push over the earlier branch.
        # Selection layer's re-pick justification is what legitimizes
        # a re-pick in the first place; this handles the push side.
        branch = _apply_branch_collision_suffix(target, branch)
        # 4.5. Research phase (opt-in via INPUT_STAGED_SYNTHESIS). Runs a
        # bounded research-only Claude Code invocation before the coding
        # session, producing web_findings.json in the briefing bundle dir.
        # Coding session (and downstream refinement chain) read that
        # artifact as structured context alongside SPEC.md.
        #
        # Falls back to classic single-invocation flow when the research
        # phase fails — no web_findings.json written, INVOCATION.md
        # skips the ref block, coding session runs as today.
        staged = (os.environ.get("INPUT_STAGED_SYNTHESIS") or "").strip().lower()
        if staged in ("true", "1", "yes"):
            log.info("  → staged-synthesis enabled; running research phase before coding session")
            write_research_invocation(workdir, rec, target)
            research_ok, research_log = invoke_research_phase(
                workdir, timeout_s=min(600, target.claude_timeout_s),
            )
            result["research_phase_ok"] = research_ok
            result["research_log_tail"] = research_log[-1000:]

        write_spec_bundle(
            workdir, target, rec, package,
            selection_note=result.get("selection_reasoning", ""),
            env_body=env_body,
        )

        # Repo-intel telemetry — surface whether the fork's
        # .remyx/repo_intel.yaml was loaded into the coding session so
        # A/B validation runs can measure adaptive-behavior deltas
        # against dispatches without maintain-state.
        _repo_intel_path = workdir / BUNDLE_DIR_NAME / "REPO_INTEL.md"
        if _repo_intel_path.exists():
            result["repo_intel_loaded"] = True
            result["repo_intel_bytes"] = _repo_intel_path.stat().st_size
        else:
            result["repo_intel_loaded"] = False

        # 5.5. Pre-flight Issue routing (§6). Cheap Claude pass that
        # decides PR vs Issue before we spend the implementation budget.
        # Failures here fall through — they don't block the PR path.
        # Shares the implementation call's ceiling so a customer who
        # bumped claude-timeout for a large monorepo (or a slower
        # non-Anthropic backend) gets the same headroom on preflight
        # without having to know about a separate knob.
        preflight = preflight_routing(
            workdir, package, timeout_s=target.claude_timeout_s,
        )
        result["preflight_decision"] = (
            preflight.get("decision") if preflight else "(skipped)"
        )
        if preflight and preflight.get("decision") == "ISSUE":
            issue_title_inner = (
                preflight.get("issue_title")
                or f"{rec.paper_title}: needs team discussion"
            )
            issue_body_inner = (
                preflight.get("issue_body")
                or preflight.get("reasoning")
                or ""
            )
            # Compose the preflight detail. Promotes "Pre-flight
            # reasoning" from a buried italicized tail into a proper
            # heading — it's the load-bearing "why this didn't ship as
            # a PR" answer the maintainer needs at a glance.
            preflight_detail = (
                f"### Why this didn't ship as a PR\n\n"
                f"{preflight.get('reasoning', '(no reasoning provided)')}\n\n"
                f"{issue_body_inner}"
            )
            # Architecture-add Issues gain an HF Hub checkpoint
            # availability block, replacing "Is a checkpoint forthcoming?" as
            # an open question with a run-time-checked fact — sourced from
            # HF's canonical arxiv-paper linkage index, not a heuristic.
            if _is_architecture_add_shape(issue_body_inner):
                linkage = _fetch_hf_paper_linkage(rec.arxiv_id)
                preflight_detail += _format_hf_checkpoint_section(linkage)
            # Convert agent-inferred sibling claims ("sits alongside
            # `X`") into reader-verifiable enumerations by grepping the
            # workdir for definitions sharing X's distinctive suffix.
            preflight_detail = _enrich_body_with_convention_precedents(
                preflight_detail, workdir,
            )
            issue_url, issue_number = _open_downgrade_issue(
                target, rec,
                reason="Pre-flight routed to Issue before implementation",
                detail=preflight_detail,
                tldr=preflight.get("tldr", ""),
                selection_note=result.get("selection_reasoning", ""),
                selection_rejected=result.get("selection_rejected"),
                # Preflight's `issue_body` already covers the "what
                # the paper offers" angle in depth; skipping the
                # scaffolding's parallel section avoids the duplicate
                # "Why this paper is interesting for the team" header
                # that v1.4.4 and earlier rendered.
                skip_paper_reasoning_section=True,
                # The paper's suggested experiment frequently
                # contradicts what preflight just rejected. Suppress
                # it; preflight can supply a replacement via the new
                # JSON field when a viable smaller slice exists.
                suppress_suggested_experiment=True,
                replacement_experiment=preflight.get("replacement_experiment", ""),
                footer_override=(
                    f"_Opened by the [Remyx Recommendation]"
                    f"({CANONICAL_ATTRIBUTION_URL}) orchestrator. "
                    f"Pre-flight routed this paper to Issue before the "
                    f"coding agent ran — see the reasoning above for "
                    f"what would need to change to scaffold it as a PR._"
                ),
            )
            # Override the body title with the preflight's title — it's
            # more specific than the generic paper title.
            result["status"] = "issue_opened_preflight"
            result["issue_url"] = issue_url
            result["issue_number"] = issue_number
            log.info(f"  ✓ issue_opened_preflight: {issue_url}")
            return result

        # 6. Claude Code
        ok, claude_log = invoke_claude_code(
            workdir, timeout_s=target.claude_timeout_s
        )
        result["claude_exit_ok"] = ok
        # Always retain the Claude log tail — useful for diagnosing
        # silent-success-but-broken-state outcomes (e.g. orphan branch,
        # missing files), not just hard failures.
        result["claude_log_tail"] = claude_log[-1000:]
        if not ok:
            result["status"] = "claude_failed"
            return result

        # 6.5. Claude may have elected Issue-mode instead of writing code.
        issue_file = workdir / ISSUE_FALLBACK_FILENAME
        if issue_file.exists():
            log.info(f"  → Claude elected Issue-mode "
                     f"({ISSUE_FALLBACK_FILENAME} present); opening Issue")
            issue_title_inner, issue_body_inner = parse_issue_fallback_file(issue_file)
            issue_title = f"{PR_TITLE_PREFIX} {issue_title_inner}"
            # Architecture-add Issues gain a checkpoint-availability
            # block sourced from HF's arxiv-paper linkage index, replacing the
            # "Is a checkpoint forthcoming?" question with a resolved fact.
            checkpoint_block = ""
            if _is_architecture_add_shape(issue_body_inner):
                linkage = _fetch_hf_paper_linkage(rec.arxiv_id)
                checkpoint_block = _format_hf_checkpoint_section(linkage)
            issue_body = (
                f"**Recommended paper**: "
                f"[{rec.paper_title}](https://arxiv.org/abs/{rec.arxiv_id})\n"
                f"**Confidence**: {rec.tier} "
                f"(Remyx relevance {rec.relevance_score:.2f})\n"
                f"**Research interest**: {rec.interest_name or '(unnamed)'}\n"
                f"{_render_license_section(rec)}"
                f"{checkpoint_block}"
                f"\n---\n\n"
                f"{issue_body_inner}"
            )
            # Enrich with verified in-repo convention precedents
            # for any sibling claims the agent cited in its Issue body.
            issue_body = _enrich_body_with_convention_precedents(
                issue_body, workdir,
            )
            issue_url, issue_number = open_issue(target, issue_title, issue_body)
            result["status"] = "issue_opened"
            result["issue_url"] = issue_url
            result["issue_number"] = issue_number
            log.info(f"  ✓ issue_opened: {issue_url}")
            return result

        # 7. Path allowlist enforcement.
        passed_allowlist, violations = validate_changes(workdir, target, package)
        if not passed_allowlist:
            result["status"] = "rejected_path_violations"
            result["violations"] = violations
            log.warning(f"  ✗ path violations: {violations}")
            return result

        # 7.5. Integration validator (§2). Rejects scaffold-shaped runs:
        # new module added with no existing-file edit referencing it,
        # too many new files, or oversized edits to existing files.
        passed_integration, int_violations = check_integration(
            workdir, target, package
        )
        if not passed_integration:
            result["integration_violations"] = int_violations
            log.warning(f"  ✗ integration check failed: {int_violations}")
            issue_url, issue_number = _open_downgrade_issue(
                target, rec,
                reason="No real integration with the existing codebase",
                detail=(
                    "The implementation either added new modules without "
                    "wiring them into an existing call site, added too many "
                    "new files, or rewrote an existing file too aggressively. "
                    "Specifics:\n\n"
                    + "\n".join(f"- {v}" for v in int_violations)
                ),
                implementation_diff=_capture_implementation_diff(workdir),
                selection_note=result.get("selection_reasoning", ""),
                selection_rejected=result.get("selection_rejected"),
                footer_override=(
                    f"_Opened by the [Remyx Recommendation]"
                    f"({CANONICAL_ATTRIBUTION_URL}) orchestrator. The "
                    f"coding agent wrote code but the integration gate "
                    f"caught that it isn't wired into an existing call "
                    f"site — routed to Issue so the team can decide on "
                    f"the wiring._"
                ),
                workdir=workdir, branch=branch, base_branch=default_branch,
            )
            result["status"] = "issue_opened_no_integration"
            result["issue_url"] = issue_url
            result["issue_number"] = issue_number
            return result

        # 7.6. Stub density (§3). Routes to Issue if the new module's
        # public surface is dominated by pass / NotImplementedError /
        # empty bodies — i.e. the paper's contribution isn't really
        # present.
        density_ok, density, stub_examples = check_stub_density(workdir, package)
        result["stub_density"] = density
        if not density_ok:
            log.warning(
                f"  ✗ stub density {density:.0%} ≥ "
                f"{STUB_DENSITY_DOWNGRADE_THRESHOLD:.0%}; downgrading to Issue"
            )
            issue_url, issue_number = _open_downgrade_issue(
                target, rec,
                reason=(
                    f"New module is mostly unimplemented "
                    f"({density:.0%} of function bodies are stubs)"
                ),
                detail=(
                    "The orchestrator's coding agent produced a module "
                    "whose public surface is dominated by `pass`, "
                    "`raise NotImplementedError`, or docstring-only "
                    "bodies. This usually means the paper's primary "
                    "contribution requires infra the repo doesn't have, "
                    "or there's no clear call site to extend.\n\n"
                    "Examples of stub bodies in the draft:\n\n"
                    + "\n".join(f"- `{e}`" for e in stub_examples)
                ),
                implementation_diff=_capture_implementation_diff(workdir),
                selection_note=result.get("selection_reasoning", ""),
                selection_rejected=result.get("selection_rejected"),
                footer_override=(
                    f"_Opened by the [Remyx Recommendation]"
                    f"({CANONICAL_ATTRIBUTION_URL}) orchestrator. The "
                    f"coding agent wrote a module but most of its public "
                    f"surface is stubs — routed to Issue rather than "
                    f"shipping a hollow PR._"
                ),
                workdir=workdir, branch=branch, base_branch=default_branch,
            )
            result["status"] = "issue_opened_stub_density"
            result["issue_url"] = issue_url
            result["issue_number"] = issue_number
            return result

        # 7.7. Diff Risk Score (RADAR). Calibrated static-diff risk band
        # over features the funnel already computed (files, lines, new
        # callables, critical-file edits, test impact). "high" routes to a
        # human-review Issue; "elevated" is handled at §10 (PR, forced draft).
        risk = score_diff_risk(workdir, package)
        result["diff_risk_score"] = risk.score
        result["diff_risk_band"] = risk.band
        result["diff_risk_factors"] = risk.factors
        if risk.band == "high":
            log.warning(f"  ✗ diff risk {risk.score:.2f} ≥ "
                        f"{DIFF_RISK_ISSUE_THRESHOLD:.2f} (high); → Issue")
            issue_url, issue_number = _open_downgrade_issue(
                target, rec,
                reason=(f"Diff Risk Score {risk.score:.2f} exceeds the "
                        f"auto-land threshold ({DIFF_RISK_ISSUE_THRESHOLD:.2f})"),
                detail=(
                    "A calibrated static-diff risk score placed this change in "
                    "the **high** band, where RADAR mandates human review over "
                    "auto-landing. The implementation is attached for a "
                    "maintainer to land manually.\n\n" + render_risk_detail(risk)
                ),
                implementation_diff=_capture_implementation_diff(workdir),
                selection_note=result.get("selection_reasoning", ""),
                selection_rejected=result.get("selection_rejected"),
                footer_override=(
                    f"_Opened by the [Remyx Recommendation]"
                    f"({CANONICAL_ATTRIBUTION_URL}) orchestrator. The diff's "
                    f"calibrated risk crossed the auto-land threshold — routed "
                    f"to Issue for human review per RADAR's risk-aware policy._"
                ),
                workdir=workdir, branch=branch, base_branch=default_branch,
            )
            result["status"] = "issue_opened_high_risk"
            result["issue_url"] = issue_url
            result["issue_number"] = issue_number
            return result

        # 8. Tests
        tests_status, test_output = run_tests(workdir)
        result["tests_status"] = tests_status
        tests_passed = tests_status == "passed"
        result["tests_passed"] = tests_passed

        # 8.5. Test-touches-existing-modules gate (§3). If new package
        # modules were added, at least one new test must import from a
        # non-new module in the package — otherwise tests are pure
        # self-tests and don't prove integration.
        #
        # Behavior is controlled by `target.test_integration_policy`:
        #   - "off"    → skip the gate entirely (relies on the other
        #                validators to keep PRs honest)
        #   - "soft"   → gate failure annotates the PR body with a
        #                warning section but does NOT demote to Issue
        #   - "strict" → (default) gate failure demotes to Issue, as before
        if target.test_integration_policy == "off":
            result["tests_touch_existing"] = True   # vacuous: gate skipped
            result["test_integration_gate"] = "skipped"
        else:
            tests_touch_existing, existing_imports = (
                check_tests_touch_existing_modules(workdir, package)
            )
            result["tests_touch_existing"] = tests_touch_existing
            if not tests_touch_existing:
                if target.test_integration_policy == "soft":
                    log.warning(
                        "  ⚠ no new test imports from an existing module — "
                        "policy=soft, opening PR with a warning"
                    )
                    result["test_integration_gate"] = "soft_failed"
                else:  # "strict"
                    log.warning(
                        "  ✗ no new test imports from an existing module — "
                        "tests only self-test the new file"
                    )
                    issue_url, issue_number = _open_downgrade_issue(
                        target, rec,
                        reason=(
                            "New tests don't touch any pre-existing module"
                        ),
                        detail=(
                            "A new module was added, but none of the new test "
                            "files import from a pre-existing module in "
                            f"`{package}/`. Pure self-tests of the new file "
                            "don't prove the integration runs against existing "
                            "pipeline outputs."
                        ),
                        implementation_diff=_capture_implementation_diff(workdir),
                        selection_note=result.get("selection_reasoning", ""),
                        selection_rejected=result.get("selection_rejected"),
                        footer_override=(
                            f"_Opened by the [Remyx Recommendation]"
                            f"({CANONICAL_ATTRIBUTION_URL}) orchestrator. "
                            f"The coding agent's tests only self-test the "
                            f"new module — none of them import from any "
                            f"pre-existing module in the package, so the "
                            f"integration is unproven. Routed to Issue."
                            f"_"
                        ),
                        workdir=workdir, branch=branch, base_branch=default_branch,
                    )
                    result["status"] = "issue_opened_no_test_integration"
                    result["issue_url"] = issue_url
                    result["issue_number"] = issue_number
                    return result

        # 9. Self-review (§4). Second Claude pass over the diff. Renders
        # a "What this PR actually does" section into the PR body; if the
        # new code is an orphan (unreachable from any production path), it
        # routes to Issue. This is a REACHABILITY check, not a triviality
        # one — stub density (§3) already covers "the code is too thin".
        # Shares the implementation call's ceiling (same rationale as
        # preflight above): a customer who bumped claude-timeout for a
        # large monorepo or a slower non-Anthropic backend should get the
        # same headroom on review without learning about a separate knob.
        review = self_review_diff(workdir, timeout_s=target.claude_timeout_s)
        result["self_review"] = review or {}
        if review and review.get("is_orphan") is True:
            # Surface the classification but don't veto the PR here — the
            # measurement-based gates upstream (path allowlist,
            # check_integration invocation check, tests-touch-existing-
            # modules) already caught orphan scaffolding on real diff
            # evidence; if the run got this far, those passed. The
            # self-review verdict rides along in the PR body so the
            # maintainer sees the orphan framing prominently and can
            # close a scaffold-shaped PR fast, without the pipeline
            # single-handedly downgrading a legitimate library API
            # addition on a boolean flag.
            log.warning(
                "  ⚠ self-review flagged is_orphan=True — surfaced in the "
                "PR body; upstream measurement-based gates already passed"
            )
        review_section = _render_self_review_section(review) if review else ""

        # 10. Draft determination. "unvalidated" (tests couldn't run in CI,
        # e.g. the runner lacks the repo's deps) is NOT a failure: never-mode
        # opens a draft rather than skipping, and on_test_failure drafts it.
        if target.draft_mode == "always":
            draft = True
        elif target.draft_mode == "never":
            if tests_status == "failed":
                result["status"] = "skipped_test_failure"
                result["test_output_tail"] = test_output[-500:]
                return result
            draft = tests_status != "passed"
        else:                                # "on_test_failure"
            draft = tests_status != "passed"

        # RADAR risk-aware safety: an "elevated"-band diff stays a draft even
        # when tests pass, so a human reviews before it lands. Low-risk diffs
        # are unaffected; "high" already routed to an Issue above.
        if result.get("diff_risk_band") == "elevated":
            draft = True

        # 11. Commit + push + PR
        pr_title = format_pr_title(rec)
        pr_body = build_pr_body(
            target, rec, tests_status, test_output,
            review_section=review_section,
            selection_note=result.get("selection_reasoning", ""),
            test_integration_warning=(
                result.get("test_integration_gate") == "soft_failed"
            ),
        )
        commit_and_push(
            workdir, branch, pr_title, repo=target.repo, base_branch=default_branch
        )

        # REMYX-186: Pre-PR fidelity gate.
        # Run fidelity on the local branch BEFORE opening the PR. If
        # substantive deviations from the reference are found, either
        # patch the branch (REMYX-185, scoped-down single-attempt) or
        # skip publication entirely. Fabricated artifacts never become
        # public.
        prepub_verdict = _run_pre_pr_fidelity_check(
            rec, target, workdir, pr_title, pr_body, base_branch=default_branch,
            self_review=review,
        )
        result["prepub_fidelity"] = {
            "status": prepub_verdict.get("status"),
            "items_count": prepub_verdict.get("items_count", 0),
            "needs_judgment": prepub_verdict.get("needs_judgment", False),
        }
        if prepub_verdict.get("coverage_section"):
            _append_to_step_summary(prepub_verdict["coverage_section"])

        if prepub_verdict.get("needs_judgment"):
            # Under publish=branch, skip the fabrication-patch attempt entirely.
            # The patch is designed to make the diff PR-ready; branch mode
            # doesn't need PR-readiness — the team reviews the raw agent
            # output on the branch and decides. Surface the fidelity flags as
            # informational content in the step summary + return with the
            # branch-pushed status. The branch itself is already on the fork
            # (commit_and_push ran before the fidelity gate).
            _pb_mode = (os.environ.get("INPUT_PUBLISH") or "pr").strip().lower()
            if _pb_mode == "branch":
                _branch_url = f"https://github.com/{target.repo}/tree/{branch}"
                result["status"] = "branch_pushed_no_pr"
                result["branch"] = branch
                result["branch_url"] = _branch_url
                result["pr_title"] = pr_title
                result["would_have_downgraded_reason"] = (
                    f"pre-PR fidelity flagged {prepub_verdict.get('items_count', 0)} "
                    f"items for judgment"
                )
                log.info(
                    f"  ✓ {result['status']}: {_branch_url} "
                    f"(no PR opened; publish=branch; fidelity flagged "
                    f"{prepub_verdict.get('items_count', 0)} items — see step summary)"
                )
                _append_to_step_summary(
                    "## Branch mode — pre-PR fidelity flagged items, branch preserved\n\n"
                    f"Branch pushed to fork: [`{branch}`]({_branch_url})\n\n"
                    f"The pre-PR fidelity gate flagged "
                    f"{prepub_verdict.get('items_count', 0)} item(s) as "
                    "`needs_judgment` — under `publish=pr` this would have "
                    "triggered a fabrication-patch attempt and (if that "
                    "failed) skipped publication. Under `publish=branch` the "
                    "coding agent's raw output is preserved as a branch so "
                    "the team can review the flagged items directly and "
                    "decide whether they're real fabrication or deliberate "
                    "scope carve-outs.\n\n"
                    "See the coverage matrix above for per-item detail. "
                    "Promote via `gh pr create` if the flags reflect "
                    "scope-carve-out rather than fabrication, or delete via "
                    f"`git push origin --delete {branch}` if not."
                )
                return result
            log.info("  → pre-PR fidelity flagged fabrication; attempting one patch")
            patched = _attempt_pre_pr_fidelity_patch(
                workdir,
                prepub_verdict["matrix"],
                prepub_verdict.get("reference_url", ""),
                timeout_s=target.claude_timeout_s,
            )
            if patched:
                # Append the patch as a second commit on the existing
                # branch. Can't reuse `commit_and_push` — its safety
                # guard requires HEAD == origin/main.
                #
                # Extra step: the first commit_and_push re-authored the
                # remote branch head via the GitHub API (produces a
                # bot-verified commit with same tree, different author
                # metadata). Local HEAD is still at the pre-reauth
                # commit — a plain push of a local descendant would be
                # non-fast-forward. `fetch + reset --soft` aligns local
                # HEAD to the remote's API commit without touching the
                # working tree (patch changes stay uncommitted); the
                # subsequent commit is a clean fast-forward.
                #
                # Explicit refspec (``+<branch>:refs/remotes/origin/<branch>``)
                # so ``refs/remotes/origin/<branch>`` actually gets populated
                # locally — a bare ``git fetch origin <branch>`` only updates
                # ``FETCH_HEAD``, and the subsequent ``origin/<branch>``
                # rev-parse resolves the remote-tracking ref, not FETCH_HEAD.
                # Without the explicit refspec this consistently 500s on the
                # reset with "ambiguous argument 'origin/<branch>': unknown
                # revision or path not in the working tree."
                try:
                    subprocess.run(
                        ["git", "fetch", "origin",
                         f"+{branch}:refs/remotes/origin/{branch}"],
                        cwd=workdir, check=True, capture_output=True, text=True,
                    )
                    subprocess.run(
                        ["git", "reset", "--soft", f"origin/{branch}"],
                        cwd=workdir, check=True, capture_output=True, text=True,
                    )
                    subprocess.run(
                        ["git", "commit", "-am", "Fidelity remediation"],
                        cwd=workdir, check=True, capture_output=True, text=True,
                    )
                    subprocess.run(
                        ["git", "push", "origin", branch],
                        cwd=workdir, check=True, capture_output=True, text=True,
                    )
                    log.info(f"  ✓ pushed patch commit on {branch}")
                except subprocess.CalledProcessError as e:
                    stderr = (e.stderr or "").strip()
                    log.warning(f"  ⚠ patch commit/push failed: {stderr[-300:]}")
                    result["status"] = "skipped_fidelity_fabrication"
                    result["error"] = f"patch commit failed: {stderr[-200:]}"
                    return result
                # Thread the coding session's self_review through so the
                # remediation-pass fidelity check evaluates against the same
                # ``mode_cited`` / ``substitutions`` / ``scoped_out`` context
                # the first-pass check used. Without this, the remediation
                # pass silently downgrades to ``mode-1, subs=0, scoped_out=0``
                # and re-flags every legitimate Mode-2 substitution as
                # fabrication — hard-to-shift outcome for any Mode-2 refinement
                # that triggers judgment on the first pass.
                revised_verdict = _run_pre_pr_fidelity_check(
                    rec, target, workdir, pr_title, pr_body,
                    base_branch=default_branch,
                    self_review=review,
                )
                result["prepub_fidelity_after_patch"] = {
                    "status": revised_verdict.get("status"),
                    "items_count": revised_verdict.get("items_count", 0),
                    "needs_judgment": revised_verdict.get("needs_judgment", False),
                }
                if revised_verdict.get("coverage_section"):
                    _append_to_step_summary(revised_verdict["coverage_section"])
                if revised_verdict.get("needs_judgment"):
                    result["status"] = "skipped_fidelity_fabrication_after_patch"
                    log.info(
                        f"  ✗ {result['status']}: fidelity still flags "
                        f"{revised_verdict['items_count']} items after patch"
                    )
                    return result
                log.info("  ✓ patch resolved fidelity findings — continuing to PR")
            else:
                result["status"] = "skipped_fidelity_fabrication"
                log.info(
                    f"  ✗ {result['status']}: patch attempt failed or no edits; "
                    "no PR opened"
                )
                return result

        # publish=branch — skip PR creation entirely. The branch is already
        # pushed to the fork by commit_and_push; team reviews the branch via
        # `gh` / GitHub UI and runs `gh pr create` themselves when ready to
        # ship. Coordination + evidence land in the step summary + workflow
        # artifact, never on the branch tree — that keeps promoted PRs clean
        # (no metadata files bleeding into the maintainer's diff).
        publish_mode = (os.environ.get("INPUT_PUBLISH") or "pr").strip().lower()
        if publish_mode == "branch":
            branch_url = f"https://github.com/{target.repo}/tree/{branch}"
            result["status"] = "branch_pushed_no_pr"
            result["branch"] = branch
            result["branch_url"] = branch_url
            result["pr_title"] = pr_title
            log.info(f"  ✓ {result['status']}: {branch_url} (no PR opened; publish=branch)")
            _append_to_step_summary(
                "## Branch mode — no PR opened\n\n"
                f"Branch pushed to fork: [`{branch}`]({branch_url})\n\n"
                "The team reviews this branch and runs `gh pr create` when "
                "ready to ship. Coordination signal + evidence are in this "
                "workflow's step summary and downloadable artifacts (not on "
                "the branch tree)."
            )
            return result

        pr_url, pr_number = open_pr(
            target, branch, pr_title, pr_body, draft=draft, base=default_branch
        )
        result["status"] = "pr_opened_draft" if draft else "pr_opened"
        result["pr_url"] = pr_url
        result["pr_number"] = pr_number
        log.info(f"  ✓ {result['status']}: {pr_url}")
        return result

    except BranchPushedFromDowngrade as e:
        # publish=branch — a post-coding downgrade fired (integration /
        # stub-density / diff-risk / test-integration / self-review-orphan)
        # after the coding agent produced code. Instead of filing an Issue,
        # the branch has been pushed to the fork; the downgrade reason +
        # detail are surfaced as informational content in the step summary.
        # Team reviews the branch's code + reasoning, decides whether to
        # promote to PR (`gh pr create`) or delete the branch.
        result["status"] = "branch_pushed_no_pr"
        result["branch"] = e.branch
        result["branch_url"] = e.branch_url
        result["would_have_downgraded_reason"] = e.reason
        log.info(
            f"  ✓ {result['status']}: {e.branch_url} "
            f"(no Issue filed; publish=branch; downgrade reason: {e.reason})"
        )
        _append_to_step_summary(
            "## Branch mode — downgrade suppressed, branch preserved\n\n"
            f"Branch pushed to fork: [`{e.branch}`]({e.branch_url})\n\n"
            f"**Downgrade reason (informational)**: {e.reason}\n\n"
            "Under `publish=pr`, this run would have opened an Issue with "
            "the diff attached instead of a Draft PR, because a downstream "
            "check flagged the implementation. Under `publish=branch` the "
            "coding agent's output is preserved as a branch on the fork so "
            "the team can review it directly and decide whether the "
            "flagged concern is a real blocker.\n\n"
            "**Detail:**\n\n"
            f"{e.detail}\n\n"
            "Promote via `gh pr create` if the branch is worth shipping "
            "despite the flag, or delete via `git push origin --delete "
            f"{e.branch}` if not."
        )
        return result

    except LeadCapturedInBranchMode as e:
        # publish=branch — an Issue would have been filed (either the
        # intentional Issue route or one of the downgrade helpers). No
        # Issue lands on the target repo; the LEAD content is rendered
        # into the step summary so the team can review it and file the
        # Issue manually via ``gh issue create`` if the substance warrants.
        # Zero maintainer attention consumed until the team acts.
        result["status"] = "lead_captured_no_issue"
        result["lead_title"] = e.title
        log.info(
            f"  ✓ {result['status']}: '{e.title}' "
            f"(no Issue filed; publish=branch)"
        )
        _append_to_step_summary(
            "## LEAD captured — no Issue filed (branch mode)\n\n"
            f"**Would-be Issue title**: {e.title}\n\n"
            "---\n\n"
            f"{e.body}\n\n"
            "---\n\n"
            f"To file this Issue on `{target.repo}`, run:\n\n"
            "```bash\n"
            f"gh issue create --repo {target.repo} \\\n"
            f"  --title \"{e.title}\" \\\n"
            "  --body-file <(...paste the LEAD content above...)\n"
            "```\n\n"
            "Or copy the title + body into the GitHub web UI. The step "
            "summary above contains the exact content the maintainer "
            "would have seen if the Issue had been filed automatically."
        )
        return result

    except IssuesDisabledError as e:
        # Race: Issues were enabled at the pre-flight check but got disabled
        # mid-run, so open_issue's PATCH-to-enable hit 403. Catch here so
        # the run ends with a graceful skipped_issues_disabled status
        # instead of a generic error — selection-pass + scaffold work is
        # already done; surfacing the actionable hint is the best we can
        # do for this rare path.
        log.warning(f"  ↪ Issues became disabled mid-run: {e}")
        result["status"] = "skipped_issues_disabled"
        result["error"] = str(e)
        return result

    except OutboundSecretError as e:
        # The v1.6.4 outbound-body scrubber fired — assembled PR / Issue /
        # comment body contained content matching credential patterns,
        # and the API request was refused at gh_api before any data left
        # the runner. Route to the dedicated `aborted_secret_in_payload`
        # status (in FAILURE_EXIT_STATUSES → red in CI) so the operator
        # sees this as a real abort requiring investigation, not a
        # graceful skip. Surface the path + matched-pattern identifiers
        # in the result so step_summary + run telemetry can render the
        # diagnostic without parsing the message string. Match content
        # itself is never propagated — only path + pattern names + the
        # length diagnostic emitted by `_scrub_outbound_payload`.
        log.error(
            f"  ↪ outbound-secret-scrubber aborted the request at "
            f"field {e.path!r} (patterns={e.patterns})"
        )
        result["status"] = "aborted_secret_in_payload"
        result["error"] = str(e)
        result["scrubber_path"] = e.path
        result["scrubber_patterns"] = e.patterns
        return result

    finally:
        # Repo-intel write path — fires on every process_target exit
        # (success or exception) so terminal states landed in `result`
        # get merged into the fork's .remyx/repo_intel.yaml when
        # INPUT_MAINTAIN_STATE is on. No-op otherwise. Never fails the
        # run — errors logged and swallowed inside the helper.
        try:
            _update_fork_repo_intel(target, result, workdir)
        except Exception:  # noqa: BLE001 — telemetry write never blocks the run
            log.exception("  ⚠ repo_intel write hook raised; swallowed")

        # Clean up tmpdir unless DEBUG_KEEP_WORKDIR set
        if not os.environ.get("DEBUG_KEEP_WORKDIR"):
            shutil.rmtree(workdir, ignore_errors=True)


def build_pr_body(
    target: Target,
    rec: Recommendation,
    tests_status: str,
    test_output: str,
    review_section: str = "",
    selection_note: str = "",
    test_integration_warning: bool = False,
) -> str:
    tier_emoji = {"high": "🟢", "moderate": "🟡", "low": "🟠", "noise": "🔴"}.get(rec.tier, "⚪")
    if tests_status == "passed":
        test_section_inner = "### Test results\n\n✅ All tests passed.\n"
    elif tests_status == "unvalidated":
        test_section_inner = (
            "### Test results\n\nℹ️ Tests could not run in CI — the runner "
            "lacks this repo's dependencies (a collection/import error, not "
            "a code failure). Run the suite locally to validate.\n\n"
            f"```\n{test_output[-1000:]}\n```\n"
        )
    else:
        test_section_inner = (
            "### Test results\n\n⚠️ Tests did not pass. PR opened as draft "
            f"for review.\n\n```\n{test_output[-1000:]}\n```\n"
        )
    # Soft-mode test-integration warning, rendered when the gate failed
    # but the run was kept as a PR per `test-integration-policy: soft`.
    # Sits above the test section so reviewers see the integration caveat
    # before the green checkmark.
    if test_integration_warning:
        warning_block = (
            "### ⚠️ Test integration not validated\n\n"
            "New tests only self-test the new module — no new test imports "
            "from a pre-existing module in the package. This is typically "
            "fine for standalone-module contributions (new layer, kernel, "
            "component), but if a clear integration path exists, consider "
            "adding a test that exercises the wiring edit.\n\n"
            "_PR opened via `test-integration-policy: soft`._\n"
        )
    else:
        warning_block = ""

    # Self-review section (§4) goes ABOVE the test section so reviewers
    # see "what this PR actually does vs. what's stubbed" before the
    # green checkmark.
    test_section = (
        f"{warning_block}{review_section}\n{test_section_inner}"
        if review_section else
        f"{warning_block}{test_section_inner}"
    )
    # Selection rationale: why this candidate was picked from the lookback
    # pool over higher-ranked ones. Empty (just the section break) when the
    # pool had one candidate or the selection pass was unavailable.
    selection_section = (
        f"\n## Why this candidate (selected from the lookback pool)\n\n"
        f"{selection_note}\n"
        if selection_note and not selection_note.startswith("(")
        else "\n"
    )
    return _PR_BODY_TEMPLATE.format(
        paper_title=rec.paper_title,
        arxiv_id=rec.arxiv_id,
        tier_emoji=tier_emoji,
        tier=rec.tier,
        relevance_score=rec.relevance_score,
        interest_name=rec.interest_name or "(unnamed)",
        reasoning=rec.reasoning or "(no reasoning provided)",
        selection_section=selection_section,
        license_section=_render_license_section(rec),
        suggested_experiment=rec.suggested_experiment or "(none)",
        test_section=test_section,
        attribution_url=CANONICAL_ATTRIBUTION_URL,
    )


# ─── Entry point ───────────────────────────────────────────────────────────


def _require_env(name: str) -> str:
    """Read a required env var or exit with a clear error."""
    v = os.environ.get(name, "").strip()
    if not v:
        log.error(
            f"Required env var {name!r} is empty or unset. "
            f"Check the action's `with:` block (for INPUT_* vars) or "
            f"the workflow's `env:` / `secrets:` block (for "
            f"REMYX_API_KEY, ANTHROPIC_API_KEY, GITHUB_TOKEN)."
        )
        sys.exit(2)
    return v


def _optional_env(name: str, default: str) -> str:
    return (os.environ.get(name) or "").strip() or default


def build_target_from_env() -> Target:
    """Read the action inputs from env vars and build a single Target.

    GitHub Actions composite actions surface `inputs.foo` as the env
    var `INPUT_FOO` to subprocesses (the case is normalized to upper
    when passing through; action.yml is responsible for the mapping).
    The action's `runs.steps` block sets these explicitly to be
    portable across composite / Docker / JavaScript action types.
    """
    repo = _require_env("TARGET_REPO")
    interest_id = _require_env("INPUT_INTEREST_ID")

    draft_mode = _optional_env("INPUT_DRAFT_MODE", "always")
    if draft_mode not in DRAFT_MODES:
        log.error(
            f"INPUT_DRAFT_MODE={draft_mode!r} is invalid. "
            f"Must be one of {DRAFT_MODES}."
        )
        sys.exit(2)

    rate_limit_raw = _optional_env("INPUT_RATE_LIMIT_DAYS", "7")
    try:
        rate_limit_days = int(rate_limit_raw)
    except ValueError:
        log.error(
            f"INPUT_RATE_LIMIT_DAYS={rate_limit_raw!r} is not an integer."
        )
        sys.exit(2)

    guardrails_raw = _optional_env("INPUT_GUARDRAILS_ALLOWLIST", "")
    guardrails_allowlist = (
        [p.strip() for p in guardrails_raw.split(",") if p.strip()]
        if guardrails_raw
        else []
    )

    test_integration_policy = _optional_env(
        "INPUT_TEST_INTEGRATION_POLICY", "strict"
    ).strip().lower()
    if test_integration_policy not in TEST_INTEGRATION_POLICIES:
        log.error(
            f"INPUT_TEST_INTEGRATION_POLICY={test_integration_policy!r} "
            f"is invalid. Must be one of {TEST_INTEGRATION_POLICIES}."
        )
        sys.exit(2)

    timeout_raw = _optional_env("INPUT_CLAUDE_TIMEOUT", "900")
    try:
        claude_timeout_s = int(timeout_raw)
    except ValueError:
        log.error(f"INPUT_CLAUDE_TIMEOUT={timeout_raw!r} is not an integer.")
        sys.exit(2)

    # Inline refinement chain toggle. Default on; any of the usual falsey
    # spellings disables it so cost-sensitive customers can opt out via
    # `chain: false` in the action's `with:` block.
    chain_enabled = _optional_env("INPUT_CHAIN", "true").strip().lower() not in (
        "false", "0", "no", "off",
    )

    return Target(
        repo=repo,
        interest_id=interest_id,
        min_confidence=_optional_env("INPUT_MIN_CONFIDENCE", "moderate"),
        rate_limit_days=rate_limit_days,
        draft_mode=draft_mode,
        guardrails_allowlist=guardrails_allowlist,
        test_integration_policy=test_integration_policy,
        claude_timeout_s=claude_timeout_s,
        pin_arxiv=_optional_env("INPUT_PIN_ARXIV", ""),
        search_method=_optional_env("INPUT_SEARCH_METHOD", ""),
        model_base_url=_optional_env("INPUT_MODEL_BASE_URL", ""),
        chain_enabled=chain_enabled,
        notes="",
    )


# ─── Weekly Discussion summary ─────────────────────────────────────────────
#
# A rolling weekly digest of Outrider's work on the target repo, posted as
# a comment on a designated GitHub Discussion. Opt-in: fires only when the
# action is invoked with `mode: weekly-summary` AND REMYX_WEEKLY_DISCUSSION_ID
# is set. Data source: the GitHub Actions API + per-run logs (the engine
# engine-side `recommendation_runs` table is the preferred long-term
# carrier; `_fetch_week_runs` is the seam to swap when it ships). Runs whose
# logs have aged out of retention are listed without details rather than
# silently dropped. One Claude call drafts the interpretive sections; on
# failure the post degrades to data-only.

WEEKLY_WINDOW_DAYS = 7


def _resolve_discussion_id(target: Target, raw: str) -> str:
    """Resolve REMYX_WEEKLY_DISCUSSION_ID to a GraphQL node ID.

    Accepts either the node ID itself (``D_kwDO…``) or a plain Discussion
    number — numbers are friendlier to copy from the Discussion URL, so
    resolve them via one GraphQL query. Raises RuntimeError when a number
    doesn't match any Discussion on the target repo.
    """
    raw = raw.strip()
    if not raw.isdigit():
        return raw
    owner, _, name = target.repo.partition("/")
    data = gh_graphql(
        "query($owner: String!, $name: String!, $number: Int!) {"
        " repository(owner: $owner, name: $name) {"
        " discussion(number: $number) { id } } }",
        {"owner": owner, "name": name, "number": int(raw)},
    )
    node = (data.get("repository") or {}).get("discussion") or {}
    disc_id = node.get("id") or ""
    if not disc_id:
        raise RuntimeError(
            f"Discussion #{raw} not found on {target.repo} — check "
            f"REMYX_WEEKLY_DISCUSSION_ID / the weekly-discussion-id input."
        )
    return disc_id


def _post_discussion_comment(discussion_id: str, body: str) -> str:
    """Post ``body`` as a comment on the Discussion; return the comment URL.

    Posts with the active token first — the self-minted remyx[bot] token
    when available, so the digest is bot-authored by default. When that
    token can't post Discussions (the App's Discussions permission isn't
    granted/accepted on this install yet → GraphQL "Resource not
    accessible"), falls back to the workflow's GITHUB_TOKEN so the digest
    still ships — authored by github-actions[bot] rather than failing
    the run.
    """
    mutation = (
        "mutation($id: ID!, $body: String!) {"
        " addDiscussionComment(input: {discussionId: $id, body: $body}) {"
        " comment { url } } }"
    )
    variables = {"id": discussion_id, "body": body}
    try:
        data = gh_graphql(mutation, variables)
    except RuntimeError as e:
        fallback = os.environ.get("GITHUB_TOKEN", "").strip()
        permission_denied = (
            "Resource not accessible" in str(e)
            or "FORBIDDEN" in str(e)
            or "403" in str(e)
        )
        if not (fallback and fallback != _github_token() and permission_denied):
            raise
        log.warning(
            f"  weekly: active token can't post Discussions "
            f"({str(e)[:120]}); retrying with GITHUB_TOKEN. Grant the "
            f"Remyx App 'Discussions: Read and write' for a bot-authored "
            f"digest."
        )
        data = gh_graphql(mutation, variables, token=fallback)
    comment = (data.get("addDiscussionComment") or {}).get("comment") or {}
    return comment.get("url") or ""


def _fetch_prior_digest_excerpt(discussion_id: str, max_chars: int = 3000) -> str:
    """Most recent prior digest comment on the host Discussion, truncated.

    Fed to the narrative call so research-stream trends can make
    week-over-week claims ("up from 1 candidate last week") with zero
    storage — the Discussion thread IS the history. Best-effort: ``""``
    when the lookup fails or no prior digest exists.
    """
    try:
        data = gh_graphql(
            "query($id: ID!) { node(id: $id) { ... on Discussion {"
            " comments(last: 5) { nodes { body } } } } }",
            {"id": discussion_id},
        )
    except Exception as e:
        log.debug(f"  weekly: prior-digest fetch failed: {e}")
        return ""
    nodes = (
        ((data.get("node") or {}).get("comments") or {}).get("nodes")
    ) or []
    # `last: 5` returns oldest→newest; scan newest first.
    for node in reversed(nodes):
        body = (node or {}).get("body") or ""
        if "Outrider weekly" in body:
            return body[:max_chars]
    return ""


_LOG_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z ?")


def _extract_run_summary(log_text: str) -> dict | None:
    """Parse the RUN SUMMARY JSON object out of a raw Actions job log.

    GitHub prefixes every log line with an ISO timestamp; strip it, find
    the last ``=== RUN SUMMARY ===`` marker, then collect from the first
    ``{`` to the matching column-0 ``}`` (json.dumps(indent=2) shape).
    Returns None when no marker / unparseable — callers treat that as
    "not an Outrider run" or "log truncated".
    """
    if "=== RUN SUMMARY ===" not in log_text:
        return None
    lines = [_LOG_TIMESTAMP_RE.sub("", l) for l in log_text.splitlines()]
    marker_idx = max(
        i for i, l in enumerate(lines) if "=== RUN SUMMARY ===" in l
    )
    buf: list[str] = []
    for line in lines[marker_idx + 1:]:
        if not buf:
            if line.startswith("{"):
                buf.append(line)
            continue
        buf.append(line)
        if line.startswith("}"):
            break
    if not buf:
        return None
    try:
        parsed = json.loads("\n".join(buf))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _fetch_run_log_text(repo: str, run_id: int) -> str | None:
    """Download one workflow run's log archive and return the text of the
    member containing the RUN SUMMARY marker (or the largest member when
    none matches — callers re-check). Returns None when the archive is
    gone (aged out of retention → HTTP 410/404) or unreadable."""
    token = _github_token()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/logs",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "feature-finder-orchestrator",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            blob = r.read()
        archive = zipfile.ZipFile(io.BytesIO(blob))
        for info in archive.infolist():
            if not info.filename.endswith(".txt"):
                continue
            text = archive.read(info).decode("utf-8", errors="replace")
            if "=== RUN SUMMARY ===" in text:
                return text
        return ""
    except Exception as e:
        log.debug(f"  weekly: log fetch for run {run_id} failed: {e}")
        return None


def _fetch_week_runs(target: Target, since: "dt.datetime") -> list[dict]:
    """Completed Outrider runs on the target repo since ``since``.

    Returns entries ``{"run": <Actions run envelope>, "summary": dict|None}``,
    newest first. A run counts as an Outrider run when its log contains the
    RUN SUMMARY marker; when the log has aged out, the workflow name/path
    containing "outrider" is the fallback signal and the entry carries
    ``summary=None`` (rendered as "details unavailable" — an honest gap,
    never a silent drop). Weekly-summary runs themselves are excluded.
    """
    created = urllib.parse.quote(f">={since.strftime('%Y-%m-%d')}")
    resp = gh_api(
        "GET",
        f"/repos/{target.repo}/actions/runs?created={created}&per_page=100",
    )
    runs = resp.get("workflow_runs") or []
    total = resp.get("total_count") or len(runs)
    if total > len(runs):
        log.info(f"  weekly: repo had {total} runs this window; "
                 f"only the first {len(runs)} were fetched")
    entries: list[dict] = []
    for run in runs:
        if run.get("status") != "completed":
            continue
        log_text = _fetch_run_log_text(target.repo, run.get("id"))
        if log_text is None:
            name_path = (
                (run.get("name") or "") + (run.get("path") or "")
            ).lower()
            if "outrider" in name_path:
                entries.append({"run": run, "summary": None})
            continue
        summary = _extract_run_summary(log_text)
        if summary is None:
            continue  # not an Outrider run
        if summary.get("mode") == "weekly-summary":
            continue  # don't aggregate the digest runs themselves
        entries.append({"run": run, "summary": summary})
    entries.sort(key=lambda e: e["run"].get("created_at") or "", reverse=True)
    return entries


def _aggregate_week(entries: list[dict]) -> dict:
    """Mechanical aggregation across the week's run entries.

    Everything here is data, not interpretation — the one Claude call in
    ``_draft_weekly_narrative`` works from this dict. Costs are summed
    only over runs whose logs parsed; ``unverified_runs`` counts the
    retention gaps so the digest never reports an estimate as exact.
    """
    agg: dict = {
        "rows": [],
        "n_runs": len(entries),
        "n_success": 0,
        "n_failed": 0,
        "n_artifacts": 0,
        "n_skips": 0,
        "n_errors": 0,
        "artifact_statuses": {},
        "status_counts": {},
        "verified_cost": 0.0,
        "unverified_runs": 0,
        "license_class_counts": {},
        "refine_queries": [],
        "selection_quotes": [],
        # Distinct candidates the selection pass saw this week — title +
        # outcome (chosen, or the rejection reason). Deduped across runs
        # (the same pool repeats on daily crons); the research-stream
        # trends in the narrative call are drawn from this corpus.
        "candidates": [],
    }
    seen_candidates: set[str] = set()
    for e in entries:
        run, summary = e["run"], e["summary"]
        date = (run.get("created_at") or "")[:10]
        if run.get("conclusion") == "success":
            agg["n_success"] += 1
        else:
            agg["n_failed"] += 1
        if summary is None:
            agg["unverified_runs"] += 1
            if run.get("conclusion") != "success":
                agg["n_errors"] += 1
            agg["rows"].append({
                "date": date,
                "status": "(outside log retention — details unavailable)",
                "output": "—",
            })
            continue
        status = summary.get("status", "unknown")
        agg["status_counts"][status] = agg["status_counts"].get(status, 0) + 1
        artifact = summary.get("pr_url") or summary.get("issue_url") or ""
        if artifact:
            agg["n_artifacts"] += 1
            agg["artifact_statuses"][status] = (
                agg["artifact_statuses"].get(status, 0) + 1
            )
            number = artifact.rstrip("/").split("/")[-1]
            output = f"[#{number}]({artifact})"
        else:
            if status.startswith("skipped"):
                agg["n_skips"] += 1
            else:
                agg["n_errors"] += 1
            output = "No artifact"
        agg["rows"].append({"date": date, "status": status, "output": output})
        agg["verified_cost"] += float(summary.get("cost_usd") or 0.0)
        for cls, n in (summary.get("license_class_counts") or {}).items():
            agg["license_class_counts"][cls] = (
                agg["license_class_counts"].get(cls, 0) + int(n)
            )
        agg["refine_queries"].extend(summary.get("refine_queries") or [])
        reasoning = (summary.get("selection_reasoning") or "").strip()
        if status == "skipped_by_selection_verification" and reasoning:
            agg["selection_quotes"].append(reasoning)
        # Candidate corpus — entries are newest-first, so the first
        # occurrence carries the most recent outcome for that paper.
        chosen_key = (
            summary.get("arxiv") or summary.get("paper") or ""
        ).strip().lower()
        if artifact and chosen_key and chosen_key not in seen_candidates:
            seen_candidates.add(chosen_key)
            agg["candidates"].append({
                "title": (summary.get("paper") or "")[:120],
                "outcome": f"chosen → {status}",
            })
        for r in summary.get("selection_rejected") or []:
            key = (
                (r.get("arxiv_id") or "") or (r.get("title") or "")
            ).strip().lower()
            if not key or key in seen_candidates:
                continue
            seen_candidates.add(key)
            agg["candidates"].append({
                "title": (r.get("title") or "")[:120],
                "outcome": f"rejected — {(r.get('reason') or '')[:160]}",
            })
    if len(agg["candidates"]) > 60:
        log.info(f"  weekly: candidate corpus capped at 60 "
                 f"(of {len(agg['candidates'])} distinct)")
        agg["candidates"] = agg["candidates"][:60]
    return agg


_WEEKLY_NARRATIVE_PROMPT_TEMPLATE = """\
You are drafting the interpretive sections of Outrider's weekly digest
for the repository __REPO__. Below: the week's aggregated run data
(including the distinct candidate papers the selection pass saw), the
open Outrider artifacts awaiting maintainer review, and — when present —
last week's digest. Your output supplements the data sections; do NOT
restate them.

Aggregated run data (JSON)
--------------------------
__AGG_JSON__

Open Outrider artifacts awaiting review (number, title, body excerpt)
---------------------------------------------------------------------
__OPEN_ITEMS__

Last week's digest (excerpt; may be empty)
------------------------------------------
__PRIOR_DIGEST__

Produce strictly this JSON object (no prose wrapper):
{
  "verdict_bullets": ["...", ...],
  "refine_themes": [{"theme": "...", "queries": N, "hit_rate": "..."}, ...],
  "patterns": ["...", ...],
  "research_trends": ["...", ...],
  "next_actions": {"<artifact number>": "<short next action>", ...}
}

Style — every string is a terse fragment, NOT a full sentence. No
trailing periods. The reader is skimming; distill each point to its
essence.

Rules:
- verdict_bullets: 2-3 fragments on what the selection pass did this
  week — what it anchored on, what it rejected and why. Verbatim
  reasoning quotes are rendered separately; never paraphrase them.
  Quotes exist only for verification-skip runs, so their absence in a
  week with PR/Issue outcomes is normal — NOT a wiring failure.
- refine_themes: cluster the refine queries into themes; per-theme query
  count + one-phrase hit-rate assessment. Empty list when no queries.
- patterns: 3-5 entries about operating Outrider better. Format:
  "**Noun phrase** — evidence → concrete maintainer action". Evidence
  MUST cite numbers from the data.
- research_trends: 2-4 entries on the research themes moving through
  this repo's recommendation stream (NOT arxiv at large — the pool is
  shaped by this repo's interest). Format: "**Theme** — N of M
  candidates, what it means for this repo". Only claim a trend with
  >= 2 supporting candidates; always cite the counts. Use last week's
  digest for week-over-week deltas only when it actually supports the
  claim. Do NOT duplicate content between patterns and research_trends:
  patterns = operate the tool, research_trends = the field.
- next_actions: for each open artifact whose body makes the next step
  obvious (a flag to flip, a license to re-check, a question to answer),
  a short action fragment. Omit artifacts with no clear next action.
"""


def _draft_weekly_narrative(
    agg: dict, open_items: list[dict], prior_digest: str = "",
) -> dict | None:
    """One Claude call drafting the interpretive sections. None on any
    failure — the digest degrades to data-only, never blocks the post."""
    items_block = "\n".join(
        f"#{it.get('number')} {it.get('title', '')}\n"
        f"  {' '.join((it.get('body') or '').split())[:1200]}"
        for it in open_items
    ) or "(none open)"
    prompt = (
        _WEEKLY_NARRATIVE_PROMPT_TEMPLATE
        .replace("__REPO__", agg.get("repo", ""))
        .replace("__AGG_JSON__", json.dumps(agg, indent=2)[:20000])
        .replace("__OPEN_ITEMS__", items_block)
        .replace("__PRIOR_DIGEST__", prior_digest or "(no prior digest)")
    )
    timeout_s = int(os.environ.get("REMYX_WEEKLY_TIMEOUT_S", "180"))
    max_turns = int(os.environ.get("REMYX_WEEKLY_MAX_TURNS", "3"))
    with tempfile.TemporaryDirectory(prefix="outrider-weekly-") as tmp:
        ok, output = _run_claude_oneshot(
            Path(tmp), prompt, timeout_s, max_turns=max_turns,
        )
    if not ok:
        log.warning(f"  weekly: narrative call failed: {output[:200]}")
        return None
    data = _extract_json_object(output)
    if not isinstance(data, dict):
        log.warning(f"  weekly: narrative JSON unparseable: {output[:200]!r}")
        return None
    return data


def _short_artifact_title(title: str, max_len: int = 70) -> str:
    """Checklist-friendly title: prefix stripped, word-boundary truncated."""
    t = (title or "").strip()
    if t.startswith(PR_TITLE_PREFIX):
        t = t[len(PR_TITLE_PREFIX):].strip()
    if len(t) > max_len:
        t = t[:max_len].rsplit(" ", 1)[0].rstrip(",;:·-") + "…"
    return t


def _month_day(iso: str) -> str:
    """``2026-06-10T…`` → ``Jun 10``; ``""`` when unparseable."""
    try:
        d = dt.datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"{d.strftime('%b')} {d.day}"


def _drafted_fragments(drafted: dict, key: str) -> list[str]:
    """Non-empty string fragments under ``key``, or [] for any bad shape."""
    raw = drafted.get(key)
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _group_run_rows_by_date(rows: list[dict]) -> list[tuple[str, str, str]]:
    """Collapse per-run rows into one table row per date.

    A daily-cron week produces 7+ near-identical rows; one row per date
    with per-status counts (``\\`error\\` ×3``) keeps the collapsed run
    log skimmable without dropping the audit trail. Output cell joins
    the date's artifact links, ``—`` when none.
    """
    by_date: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        date = row["date"][5:] if len(row["date"]) == 10 else row["date"]
        if date not in by_date:
            by_date[date] = {"order": [], "counts": {}, "outputs": []}
            order.append(date)
        g = by_date[date]
        s = row["status"]
        if s not in g["counts"]:
            g["order"].append(s)
        g["counts"][s] = g["counts"].get(s, 0) + 1
        out = row.get("output") or ""
        if out and out not in ("No artifact", "—"):
            g["outputs"].append(out)
    grouped = []
    for date in order:
        g = by_date[date]
        status_cell = " · ".join(
            f"`{s}`" + (f" ×{g['counts'][s]}" if g["counts"][s] > 1 else "")
            for s in g["order"]
        )
        grouped.append((date, status_cell, " · ".join(g["outputs"]) or "—"))
    return grouped


def _compose_weekly_markdown(
    window_start: "dt.datetime",
    window_end: "dt.datetime",
    agg: dict,
    open_items: list[dict],
    drafted: dict | None,
    lifecycle_events: list[dict] | None = None,
    newly_viable: list[dict] | None = None,
) -> str:
    """Assemble the Discussion-comment body.

    Layout: interpretive sections lead — patterns, then research-stream
    trends — followed by a checkable review list of open artifacts; the
    mechanical data sections support below, with the full run log
    collapsed so a long week doesn't turn the digest into a scroll.
    Everything renders as fragments, not sentences. The verbatim
    selection-reasoning quote is copied as-is into a blockquote — never
    paraphrased (it's the most maintainer-impactful content). Drafted
    sections slot in when the narrative call succeeded; the digest is
    data-only otherwise.
    """
    drafted = drafted or {}
    lines: list[str] = []
    lines.append(
        f"## 🧭 Outrider weekly — "
        f"{window_start.strftime('%b')} {window_start.day} → "
        f"{window_end.strftime('%b')} {window_end.day}"
    )
    lines.append("")

    # Stat line — zero segments are dropped; cost is exact when every
    # run's log parsed and explicitly partial otherwise (never report an
    # estimate as exact).
    segments = [f"**{agg['n_runs']} runs**"]
    if agg.get("n_artifacts"):
        statuses = set(agg.get("artifact_statuses") or {})
        plural = agg["n_artifacts"] != 1
        if statuses == {"pr_opened_draft"}:
            label = "draft PRs" if plural else "draft PR"
        elif statuses and all(s.startswith("pr_opened") for s in statuses):
            label = "PRs" if plural else "PR"
        elif statuses and all(s.startswith("issue_opened") for s in statuses):
            label = "Issues" if plural else "Issue"
        else:
            label = "artifacts" if plural else "artifact"
        segments.append(f"✅ {agg['n_artifacts']} {label}")
    if agg.get("n_skips"):
        n = agg["n_skips"]
        segments.append(f"⏭️ {n} skip{'s' if n != 1 else ''}")
    if agg.get("n_errors"):
        n = agg["n_errors"]
        segments.append(f"❌ {n} error{'s' if n != 1 else ''}")
    cost_seg = f"💸 ${agg['verified_cost']:.2f} verified"
    if agg["unverified_runs"]:
        cost_seg += (
            f" · {agg['unverified_runs']} run(s) outside log retention "
            f"not counted"
        )
    segments.append(cost_seg)
    lines.append(" · ".join(segments))

    patterns = _drafted_fragments(drafted, "patterns")
    if patterns:
        lines += ["", "### ⚡ Patterns worth your attention", ""]
        lines += [f"{i}. {p}" for i, p in enumerate(patterns, start=1)]

    trends = _drafted_fragments(drafted, "research_trends")
    if trends:
        lines += ["", "### 📈 In the research stream", ""]
        lines += [f"- {t}" for t in trends]

    # Lifecycle events on Outrider Issues/PRs in the past 7 days.
    # Section omitted entirely when no events occurred.
    if lifecycle_events:
        lines += _render_lifecycle_events_section(lifecycle_events, window_end)

    # Newly-viable recommendations — previously blocked at the license
    # gate, now resolve to a permissive license. Sibling category to
    # lifecycle events above; same omit-when-empty shape.
    if newly_viable:
        lines += _render_newly_viable_section(newly_viable)

    if open_items:
        next_actions = drafted.get("next_actions")
        if not isinstance(next_actions, dict):
            next_actions = {}
        lines += ["", "### 📥 Awaiting your review", ""]
        for it in open_items:
            number = it.get("number")
            url = it.get("html_url") or ""
            entry = (
                f"- [ ] [#{number}]({url}) "
                f"{_short_artifact_title(it.get('title') or '')}"
            )
            opened = _month_day(it.get("created_at") or "")
            if opened:
                entry += f" · {opened}"
            action = str(next_actions.get(str(number), "") or "").strip()
            if action:
                entry += f" — next: {action}"
            lines.append(entry)

    lines += ["", "### 🔍 Selection-pass verdicts", ""]
    bullets = _drafted_fragments(drafted, "verdict_bullets")
    if bullets:
        lines += [f"- {b}" for b in bullets]
    elif agg["status_counts"]:
        lines += [
            f"- {n} run(s) ended `{s}`"
            for s, n in sorted(agg["status_counts"].items())
        ]
    else:
        lines.append("- No completed Outrider runs in this window")
    if agg["selection_quotes"]:
        # Verbatim, most recent first — the rejection reasoning is the
        # most maintainer-impactful content; never paraphrase it.
        lines.append("")
        for quote_line in agg["selection_quotes"][0].splitlines():
            lines.append(f"> {quote_line}")

    themes = drafted.get("refine_themes") or []
    if isinstance(themes, list) and any(isinstance(t, dict) for t in themes):
        lines += ["", "### 🔭 Refine-query themes the audit pass explored", ""]
        for t in themes:
            if not isinstance(t, dict):
                continue
            n_q = t.get("queries", "")
            unit = "query" if str(n_q) == "1" else "queries"
            lines.append(
                f"- {t.get('theme', '')} — {n_q} {unit} "
                f"· {t.get('hit_rate', '')}"
            )
    elif agg["refine_queries"]:
        lines += ["", "### 🔭 Refine queries the audit pass explored", ""]
        seen_q: set[str] = set()
        for q in agg["refine_queries"]:
            if q not in seen_q:
                seen_q.add(q)
                lines.append(f"- `{q}`")

    if agg["license_class_counts"]:
        lines += [
            "", "### ⚖️ License gate findings", "",
            f"`{_format_license_class_counts(agg['license_class_counts'])}`",
        ]

    if agg["rows"]:
        lines += [
            "", "<details>",
            f"<summary>📋 Full run log ({agg['n_runs']} "
            f"run{'s' if agg['n_runs'] != 1 else ''})</summary>", "",
            "| Date | Status | Output |", "|---|---|---|",
        ]
        for date, status_cell, output_cell in _group_run_rows_by_date(
            agg["rows"]
        ):
            lines.append(f"| {date} | {status_cell} | {output_cell} |")
        lines += ["", "</details>"]

    lines += [
        "", "---", "",
        "<sub>Outrider weekly-summary · data: GitHub Actions API + run "
        "logs · out-of-retention runs listed without details</sub>",
    ]
    return "\n".join(lines)


# ─── Lifecycle events for Outrider-authored artifacts ─────────────────────


def _is_bot_actor(user: dict | None) -> bool:
    """True if a GitHub API ``user`` dict belongs to a bot account.

    Filters our own follow-ups out of "lifecycle events" the weekly
    summary surfaces — comments by ``remyx-ai[bot]`` or
    ``github-actions[bot]`` aren't new signal for the maintainer.
    """
    if not user:
        return True
    if (user.get("type") or "").lower() == "bot":
        return True
    login = (user.get("login") or "").lower()
    return login in {"remyx-ai[bot]", "github-actions[bot]", "app/remyx-ai"}


def _is_outrider_artifact(item: dict) -> bool:
    """True if a GitHub Issue/PR was opened by Outrider.

    Matches both historical artifacts (``[Remyx Recommendation]`` title
    prefix) and new-format artifacts (body marker from the orchestrator-
    built PR body footer).
    """
    title = item.get("title") or ""
    body = item.get("body") or ""
    head_ref = (((item.get("pull_request") or {}).get("head") or {}).get("ref")
                if item.get("pull_request") else None)
    head_ref = head_ref or ((item.get("head") or {}).get("ref") if item.get("head") else "")
    return (
        title.startswith(PR_TITLE_PREFIX)
        or "Remyx Recommendation" in body
        or (head_ref and head_ref.startswith(BRANCH_PREFIX))
    )


def _parse_iso(s: str) -> "dt.datetime | None":
    """Parse a GitHub ISO-8601 timestamp; return None on failure."""
    if not s:
        return None
    try:
        # GitHub returns 2026-06-12T15:30:00Z; Python's fromisoformat
        # handles the Z suffix from 3.11+ but we replace for safety.
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _relative_when(when: "dt.datetime", now: "dt.datetime") -> str:
    """Format a recent past timestamp as 'today' / 'yesterday' / 'N days ago'.

    Caps at 7 days since that's the weekly window — anything older would
    be a bug (or a freshly-discovered event in a stale artifact).
    """
    delta = now - when
    days = delta.days
    if days < 0:
        return "just now"
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def _lifecycle_events_for_outrider_artifacts(
    target: Target, window_start: "dt.datetime", window_end: "dt.datetime",
    max_events: int = 10,
) -> list[dict]:
    """Detect state-change events on Outrider-authored Issues/PRs in the window.

    Single-pass over the target repo's recently-updated issues+PRs (the
    REST ``issues`` endpoint returns both with ``pull_request`` set on
    PRs), filtered to Outrider-authored. For each, emit events that
    occurred in ``[window_start, window_end]``:

    - Issue/PR closed or reopened
    - PR merged (state=closed AND merged_at within window)
    - New comments from non-bot actors
    - PR reviews from non-bot actors

    Returns events sorted by recency (newest first), capped at
    ``max_events`` so the digest doesn't balloon. Terminal events
    (merged/closed) are prioritized over intermediate ones when the cap
    is reached.
    """
    since_iso = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        items = gh_api(
            "GET",
            f"/repos/{target.repo}/issues"
            f"?state=all&since={since_iso}&per_page=100",
        ) or []
    except Exception as e:
        log.debug(f"  lifecycle events fetch for {target.repo} failed: {e}")
        return []

    events: list[dict] = []
    for item in items:
        if not _is_outrider_artifact(item):
            continue

        number = item.get("number")
        title = item.get("title") or ""
        html_url = item.get("html_url") or ""
        is_pr = item.get("pull_request") is not None
        kind_prefix = "PR" if is_pr else "Issue"

        # Terminal state events
        if item.get("state") == "closed":
            closed_at = _parse_iso(item.get("closed_at") or "")
            if closed_at and window_start <= closed_at <= window_end:
                # PR merged is a distinct, higher-signal terminal event
                merged_at_str = (item.get("pull_request") or {}).get("merged_at")
                merged_at = _parse_iso(merged_at_str) if merged_at_str else None
                if merged_at and window_start <= merged_at <= window_end:
                    events.append({
                        "number": number, "title": title, "url": html_url,
                        "kind_prefix": kind_prefix, "kind": "merged",
                        "when": merged_at, "actor": "maintainer",
                        "priority": 0,  # terminal events go first
                    })
                else:
                    closed_by = (item.get("closed_by") or {}).get("login") or "maintainer"
                    events.append({
                        "number": number, "title": title, "url": html_url,
                        "kind_prefix": kind_prefix, "kind": "closed",
                        "when": closed_at, "actor": closed_by,
                        "priority": 0,
                    })

        # Recently-opened Outrider artifacts also count as activity
        # (the maintainer wants to see "Outrider opened #N this week").
        created_at = _parse_iso(item.get("created_at") or "")
        if created_at and window_start <= created_at <= window_end:
            events.append({
                "number": number, "title": title, "url": html_url,
                "kind_prefix": kind_prefix, "kind": "opened",
                "when": created_at, "actor": "Outrider",
                "priority": 2,  # informational
            })

        # Comments from non-bot actors in the window
        try:
            comments = gh_api(
                "GET",
                f"/repos/{target.repo}/issues/{number}/comments"
                f"?since={since_iso}&per_page=50",
            ) or []
        except Exception:
            comments = []
        for c in comments:
            user = c.get("user") or {}
            if _is_bot_actor(user):
                continue
            when = _parse_iso(c.get("created_at") or "")
            if not when or when < window_start or when > window_end:
                continue
            body = (c.get("body") or "").strip()
            # First non-empty line as a one-glance summary
            summary_line = next(
                (ln.strip() for ln in body.splitlines() if ln.strip()), ""
            )
            events.append({
                "number": number, "title": title, "url": html_url,
                "kind_prefix": kind_prefix, "kind": "comment",
                "when": when, "actor": user.get("login") or "?",
                "summary": summary_line[:120],
                "priority": 1,
            })

    # Sort by (priority asc, when desc) — terminal events first within
    # each artifact, then newest events overall.
    events.sort(key=lambda e: (e.get("priority", 9), -(e["when"].timestamp())))
    return events[:max_events]


def _render_lifecycle_events_section(
    events: list[dict], now: "dt.datetime",
) -> list[str]:
    """Render the Outrider-artifact lifecycle events as markdown lines.

    Returns ``[]`` when no events were detected — caller skips the
    section header entirely (no "nothing happened" noise).
    """
    if not events:
        return []
    lines: list[str] = [
        "",
        "### 🔁 Recent activity on Outrider Issues/PRs",
        "",
    ]
    for e in events:
        when_label = _relative_when(e["when"], now)
        actor = e.get("actor") or "?"
        kind = e.get("kind")
        prefix = e.get("kind_prefix") or "Item"
        number = e.get("number")
        url = e.get("url")
        short_title = _short_artifact_title(e.get("title") or "")[:80]
        head = f"- [{prefix} #{number}]({url}) ({short_title})"
        if kind == "merged":
            lines.append(f"{head} — merged {when_label}")
        elif kind == "closed":
            lines.append(f"{head} — closed by @{actor} {when_label}")
        elif kind == "opened":
            lines.append(f"{head} — opened by Outrider {when_label}")
        elif kind == "comment":
            summary = (e.get("summary") or "").rstrip(":") or "(no summary)"
            lines.append(
                f"{head} — comment by @{actor} {when_label}: {summary}"
            )
        else:
            lines.append(f"{head} — {kind} {when_label}")
    return lines


# ─── License-watch: previously-blocked candidates becoming viable ─────────


# Regex for the license line that ``_render_license_section`` always
# writes into an Outrider Issue/PR body:
#   - **License**: `<spdx>` (class: `<class>`, compat: <compat>[, source: `<src>`])
_LICENSE_BODY_LINE_RE = re.compile(
    r"\*\*License\*\*:\s*`(?P<spdx>[^`]*)`"
    r"\s*\(class:\s*`(?P<klass>[^`]+)`,"
    r"\s*compat:\s*(?P<compat>[0-9.]+)"
    r"(?:,\s*source:\s*`(?P<source>[^`]+)`)?\)",
    re.IGNORECASE,
)

# Regex for the code-URL line(s). Outrider writes either
#   - **Code**: <github url>
# or
#   - **Model card**: <hf url>
# (or both). When neither is found the body has a "no repository" note.
_CODE_BODY_LINE_RE = re.compile(
    r"\*\*Code\*\*:\s*(?P<url>https?://github\.com/\S+)",
    re.IGNORECASE,
)
_MODEL_BODY_LINE_RE = re.compile(
    r"\*\*Model card\*\*:\s*(?P<url>https?://huggingface\.co/\S+)",
    re.IGNORECASE,
)


def _parse_license_state_from_issue_body(body: str) -> dict | None:
    """Extract the license-at-recommendation snapshot from an Issue body.

    Returns a dict with ``spdx``, ``klass``, ``compat`` (float),
    ``code_url`` / ``model_url`` (optional), and ``source`` (optional).

    Two paths:

    - **Structured**: matches the License line that ``_render_license_section``
      writes ("**License**: ``<spdx>`` (class: ``<klass>``, compat: <c>...)").
      This is the normal path for current-format Outrider Issues.
    - **Body-scan fallback**: when the structured line is absent (older
      Issue formats from before license enrichment was always-on), scan
      the body for any GitHub or HuggingFace URLs and synthesize a
      "no-enrichment" snapshot with ``klass="no-enrichment"`` and
      ``compat=0.30`` (treated as blocked, same severity as
      ``no-code-link``). The license-watch can then re-check the
      upstream URL and surface the Issue if the current state is
      permissive.

    Returns ``None`` only when neither path yields anything usable.
    """
    if not body:
        return None
    m = _LICENSE_BODY_LINE_RE.search(body)
    if m:
        try:
            compat = float(m.group("compat"))
        except (TypeError, ValueError):
            return None
        snap: dict = {
            "spdx": (m.group("spdx") or "").strip(),
            "klass": (m.group("klass") or "").strip(),
            "compat": compat,
            "source": m.group("source"),
        }
        code = _CODE_BODY_LINE_RE.search(body)
        if code:
            snap["code_url"] = code.group("url").rstrip(".,")
        model = _MODEL_BODY_LINE_RE.search(body)
        if model:
            snap["model_url"] = model.group("url").rstrip(".,")
        return snap

    # Fallback: no structured License section, but the body may still
    # reference a code URL the agent linked elsewhere (e.g. older Issue
    # bodies opened before license enrichment was always-on, or agent-
    # written Issue bodies that linked the repo from a free-form section).
    # ``_extract_github_urls`` / ``_extract_huggingface_urls`` return
    # ``owner/repo`` slugs; prepend the host so downstream consumers
    # (``_recheck_outrider_license_state``) get the full URL shape they
    # expect.
    github_slugs = [
        s for s in _extract_github_urls(body)
        # Skip Remyx's own URLs — these appear in the orchestrator
        # footer / call-to-action lines, not the paper's reference repo.
        if not s.lower().startswith(("remyxai/", "smellslikeml/"))
    ]
    hf_slugs = _extract_huggingface_urls(body)
    if not github_slugs and not hf_slugs:
        return None
    snap = {
        "spdx": "",
        "klass": "no-enrichment",
        "compat": 0.30,
        "source": "body-scan",
    }
    if github_slugs:
        snap["code_url"] = f"https://github.com/{github_slugs[0]}"
    if hf_slugs:
        snap["model_url"] = f"https://huggingface.co/{hf_slugs[0]}"
    return snap


def _recheck_outrider_license_state(snap: dict) -> dict | None:
    """Re-fetch license status for a recommendation given its snapshot.

    ``snap`` is the output of ``_parse_license_state_from_issue_body``.
    Re-resolves the upstream LICENSE via the existing helpers and
    returns a parallel dict with the *current* spdx/klass/compat.
    Returns ``None`` when no code URL is known and no fresh URL can be
    discovered (nothing to check).
    """
    code_url = snap.get("code_url")
    model_url = snap.get("model_url")

    fresh: dict = {"spdx": "", "klass": "missing", "compat": 0.0, "source": ""}
    if code_url:
        owner_repo = code_url.split("github.com/", 1)[-1].rstrip("/")
        owner_repo = "/".join(owner_repo.split("/")[:2])
        try:
            spdx = _fetch_repo_license(owner_repo)
        except Exception:
            spdx = ""
        fresh["spdx"] = (spdx or "").strip()
        fresh["klass"] = _classify_license(fresh["spdx"])
        fresh["source"] = "github"
    if model_url and (not code_url or fresh["klass"] in ("missing", "unknown")):
        owner_model = model_url.split("huggingface.co/", 1)[-1].rstrip("/")
        try:
            spdx = _fetch_hf_license(owner_model)
        except Exception:
            spdx = ""
        if spdx:
            fresh["spdx"] = spdx.strip()
            fresh["klass"] = _classify_license(fresh["spdx"])
            fresh["source"] = "hf"
    if not code_url and not model_url:
        # The previous snapshot was "no code link found." Nothing to
        # re-check until the agent re-discovers a URL — out of scope
        # for the in-band watch.
        return None
    # Score against permissive target (the conservative default; the
    # interest's actual target class isn't easily reachable here).
    fresh["compat"] = _license_compat_score(fresh["klass"], "permissive")
    return fresh


def _is_license_newly_viable(prev: dict, curr: dict) -> bool:
    """True if the recommendation transitioned from blocked to viable.

    "Blocked" = compat < 0.50 (no-code-link, missing, or nc) at the
    recommendation snapshot. "Viable" = compat >= 1.00 at the re-check
    (permissive only — copyleft into a permissive target stays a
    yellow flag, not a green one, and shouldn't fire as "newly viable").
    """
    return prev["compat"] < 0.5 <= 1.0 <= curr["compat"]


def _arxiv_id_from_outrider_body(body: str) -> str:
    """Extract the arxiv id from an Outrider Issue/PR body.

    The body always links the paper as ``https://arxiv.org/abs/<id>``
    near the top (set by the PR/Issue templates). Returns "" when no
    arxiv link is found.
    """
    if not body:
        return ""
    m = re.search(r"https?://arxiv\.org/abs/([\w./-]+)", body)
    return (m.group(1) if m else "").rstrip(").,")


def _discover_code_url_from_comments(
    target: Target, issue_number: int,
) -> str | None:
    """Scan an Outrider Issue's comments for a code URL the agent didn't
    surface in the body.

    Maintainer-written discussion often names the upstream code repo
    even when the original recommendation came through as ``no-code-link``
    (e.g. a licensing-audit comment that enumerates the upstream
    repo's missing LICENSE). Returns the first non-Remyx GitHub URL
    found, or ``None``.

    Best-effort: any API failure returns ``None`` so the caller
    proceeds without the comment-discovered URL.
    """
    if not issue_number:
        return None
    try:
        comments = gh_api(
            "GET",
            f"/repos/{target.repo}/issues/{issue_number}/comments?per_page=50",
        ) or []
    except Exception:
        return None
    for c in comments:
        body = c.get("body") or ""
        slugs = [
            s for s in _extract_github_urls(body)
            if not s.lower().startswith(("remyxai/", "smellslikeml/"))
        ]
        if slugs:
            return f"https://github.com/{slugs[0]}"
    return None


def _newly_viable_outrider_artifacts(
    target: Target, max_items: int = 5,
) -> list[dict]:
    """Iterate open Outrider Issues; surface ones whose license transitioned
    from blocked to viable since recommendation time.

    Lookup order for each Issue:

    1. Parse the structured License line from the body (current-format
       Outrider Issues written by ``_render_license_section``).
    2. Fallback: scan the body for any GitHub/HF URLs (older-format
       Issues where the URL appears outside a structured section).
    3. Fallback: scan the Issue's comments for a GitHub URL maintainers
       referenced after recommendation time (e.g. licensing-audit
       comments that name the upstream repo).

    Returns a list of dicts: ``number``, ``title``, ``url``,
    ``arxiv_id``, ``prev`` snapshot, ``curr`` snapshot — capped at
    ``max_items`` so the digest doesn't balloon if many recommendations
    were unblocked in the same week.
    """
    try:
        items = _remyx_issues(target, state="open") or []
    except Exception as e:
        log.debug(f"  newly-viable fetch for {target.repo} failed: {e}")
        return []

    out: list[dict] = []
    for item in items:
        body = item.get("body") or ""
        prev = _parse_license_state_from_issue_body(body)
        # Comment-scan fallback: when the body parse fails OR when the
        # parsed snapshot has no code URL to re-check against (e.g.
        # a no-code-link snapshot), look in the comments for one.
        if (prev is None
                or (not prev.get("code_url") and not prev.get("model_url"))):
            discovered = _discover_code_url_from_comments(
                target, item.get("number"),
            )
            if discovered:
                if prev is None:
                    prev = {
                        "spdx": "", "klass": "no-enrichment",
                        "compat": 0.30, "source": "comments-scan",
                    }
                else:
                    prev["source"] = (
                        f"{prev.get('source') or 'body'}+comments-scan"
                    )
                prev["code_url"] = discovered

        if not prev:
            continue
        # Only re-check Issues that were blocked at recommendation time.
        if prev["compat"] >= 1.0:
            continue
        try:
            curr = _recheck_outrider_license_state(prev)
        except Exception as e:
            log.debug(
                f"  recheck failed for issue #{item.get('number')}: {e}"
            )
            continue
        if not curr:
            continue
        if not _is_license_newly_viable(prev, curr):
            continue
        out.append({
            "number": item.get("number"),
            "title": item.get("title") or "",
            "url": item.get("html_url") or "",
            "arxiv_id": _arxiv_id_from_outrider_body(body),
            "prev": prev,
            "curr": curr,
        })
        if len(out) >= max_items:
            break
    return out


def _render_newly_viable_section(transitions: list[dict]) -> list[str]:
    """Render the "Newly viable recommendations" markdown lines.

    Returns ``[]`` when no transitions — caller skips the section header
    entirely (same shape as the lifecycle-events renderer).
    """
    if not transitions:
        return []
    lines: list[str] = [
        "",
        "### 🟢 Newly viable recommendations",
        "",
        "Recommendations previously blocked at the license/code-availability "
        "gate now resolve to a permissive license. Worth reconsidering:",
        "",
    ]
    for t in transitions:
        prev = t["prev"]
        curr = t["curr"]
        prev_label = (
            f"`{prev['spdx']}`" if prev["spdx"]
            else "no declared license"
        )
        prev_klass = prev["klass"]
        curr_spdx = curr["spdx"] or "(detected)"
        lines.append(
            f"- [Issue #{t['number']}]({t['url']}) "
            f"{_short_artifact_title(t['title'])[:80]} — "
            f"upstream now publishes `{curr_spdx}` "
            f"(was: {prev_label}, class `{prev_klass}`, compat "
            f"{prev['compat']:.2f}). Re-run selection to confirm "
            f"structural fit, then decide whether to draft a PR."
        )
    return lines


def run_weekly_summary(target: Target) -> dict:
    """Aggregate the past week's runs and post the digest to the
    configured Discussion. The weekly-mode counterpart to
    ``process_target`` — main() routes here on ``mode: weekly-summary``."""
    result: dict = {
        "repo": target.repo, "mode": "weekly-summary", "status": "unknown",
    }
    raw_id = os.environ.get("REMYX_WEEKLY_DISCUSSION_ID", "").strip()
    if not raw_id:
        result["status"] = "weekly_summary_skipped_no_discussion_id"
        log.info("  ✗ weekly-summary mode invoked without "
                 "REMYX_WEEKLY_DISCUSSION_ID; nothing to post to")
        return result
    discussion_id = _resolve_discussion_id(target, raw_id)
    window_end = dt.datetime.now(dt.timezone.utc)
    window_start = window_end - dt.timedelta(days=WEEKLY_WINDOW_DAYS)
    log.info(f"  → weekly summary over {target.repo} "
             f"({window_start.date()} → {window_end.date()})")
    entries = _fetch_week_runs(target, window_start)
    # Review checklist = open Outrider PRs + Issues, newest first — an
    # idle draft PR is as actionable as an open Issue.
    open_items = sorted(
        _remyx_open_prs(target) + _remyx_issues(target, state="open"),
        key=lambda it: it.get("created_at") or "",
        reverse=True,
    )
    # Lifecycle events on Outrider-authored Issues/PRs in the window —
    # state transitions + maintainer comments since the prior digest.
    # Empty list when nothing changed; the render code
    # then omits the section header entirely.
    lifecycle_events = _lifecycle_events_for_outrider_artifacts(
        target, window_start, window_end,
    )
    # License-watch: re-check upstream license for open Outrider Issues
    # that were originally blocked at the license/code-availability gate;
    # surface ones that transitioned to viable. Sibling
    # category to the lifecycle events above — shares the same surface.
    newly_viable = _newly_viable_outrider_artifacts(target)
    agg = _aggregate_week(entries)
    agg["repo"] = target.repo
    prior_digest = _fetch_prior_digest_excerpt(discussion_id)
    drafted = _draft_weekly_narrative(agg, open_items, prior_digest)
    body = _compose_weekly_markdown(
        window_start, window_end, agg, open_items, drafted,
        lifecycle_events=lifecycle_events,
        newly_viable=newly_viable,
    )
    url = _post_discussion_comment(discussion_id, body)
    result.update({
        "status": "weekly_summary_posted",
        "discussion_comment_url": url,
        "runs_aggregated": len(entries),
        "open_items_listed": len(open_items),
        "narrative_drafted": drafted is not None,
    })
    log.info(f"  ✓ weekly_summary_posted: {url}")
    return result


# ─── Refinement-pass: fidelity audit ───────────────────────────────────────
#
# Triggered by the `outrider:draft` label on a remyx-ai[bot] PR: diffs the
# PR's added/modified code against the paper's reference impl, produces a
# structured Coverage matrix (covered / deferred / deviation) and appends it
# to the PR body as a ``## Coverage`` section.
#
# Inputs (workflow surface):
#   INPUT_PR_NUMBER  — PR number to audit (set by the workflow from the
#                      pull_request event payload)
#
# Output:
#   - PR body updated with ``## Coverage`` section
#   - Label transition: outrider:draft → outrider:fidelity-done
#                                       or  outrider:needs-judgment
#   - status: fidelity_audited / fidelity_skipped_* / fidelity_failed_*

FIDELITY_COVERAGE_SECTION_HEADER = "## Coverage"
FIDELITY_LABEL_TRIGGER = "outrider:draft"
FIDELITY_LABEL_DONE = "outrider:fidelity-done"
FIDELITY_LABEL_NEEDS_JUDGMENT = "outrider:needs-judgment"

_REF_REPO_URL_RE = re.compile(
    # Greedy on owner/repo. Trailing path (/blob/..., /tree/..., #anchor,
    # ?query) is not captured.
    r"https?://github\.com/([\w.-]+/[\w.-]+)",
    re.IGNORECASE,
)

# Marker-prefixed reference URL: ``Reference: <url>``, ``Code: <url>``,
# ``Project page: <url>``, ``Official implementation: <url>``. Tolerates
# markdown formatting on the marker line (``**Code**:``, ``- **Code**:``,
# ``**Reference impl** ([permissive](<url>)):``) by scanning for the first
# ``github.com/<owner>/<repo>`` URL within a short on-line window after the
# marker word — robust to bold, list bullets, parenthesized license tags,
# and same-line annotations, all of which the initial draft's
# ``_render_license_section`` is allowed to add.
_REF_MARKER_RE = re.compile(
    r"(?:reference(?:\s+impl)?|code|project\s+page|official\s+implementation)"
    r"[^\n]{0,120}?https?://github\.com/([\w.-]+/[\w.-]+)",
    re.IGNORECASE,
)

_ARXIV_URL_RE = re.compile(
    r"https?://arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)

# Owners whose URLs are never the paper's reference impl — they're our
# own attribution (Outrider's docs, the engine, the action repo). Without
# this, a body that mentions ``engine.remyx.ai``-class links gets the
# attribution URL picked as the "reference," producing a wasted Phase A
# clone against an unrelated repo.
_REFERENCE_DENY_OWNERS = frozenset({"remyxai"})


def _extract_reference_url_from_pr_body(
    body: str, target_repo: str = ""
) -> tuple[str, str]:
    """Pull out (arxiv_id, reference_repo_url) from a PR body.

    PR bodies in the supported templates carry both a paper link
    (``https://arxiv.org/abs/<id>``) and a reference-impl link
    (``https://github.com/<owner>/<repo>[/blob/...]``) embedded in the
    Description section. Either may be absent. Returns ``("", "")`` for
    anything that can't be parsed.

    Extraction strategy:

    1. Prefer a **marker-prefixed URL** (``Reference:``, ``Code:``,
       ``Project page:``, ``Official implementation:``). When the
       drafting pass renders the paper's known reference URL with this
       marker, the audit picks it up unambiguously regardless of what
       other github URLs are in the body.
    2. Fall back to the **first github URL not owned by an excluded
       owner** — the target's own owner, or any of the action's own
       attribution-link owners (``remyxai/*``).

    The reference URL is normalised to the ``owner/repo`` clone URL —
    the file-specific suffix is preserved separately if the caller
    needs a file-path anchor.
    """
    body = body or ""
    arxiv_id = ""
    m = _ARXIV_URL_RE.search(body)
    if m:
        arxiv_id = m.group(1)

    target_owner = target_repo.split("/", 1)[0].lower() if target_repo else ""
    excluded_owners = _REFERENCE_DENY_OWNERS | ({target_owner} if target_owner else set())

    # Layer 1: marker-prefixed URL. The regex's capture group is already
    # the normalised ``owner/repo`` (no trailing /blob/... path).
    for m in _REF_MARKER_RE.finditer(body):
        owner_repo = m.group(1)
        owner = owner_repo.split("/", 1)[0].lower()
        if owner in excluded_owners:
            continue
        return arxiv_id, f"https://github.com/{owner_repo}"

    # Layer 2: first github URL not owned by an excluded owner.
    for m in _REF_REPO_URL_RE.finditer(body):
        owner_repo = m.group(1)
        owner = owner_repo.split("/", 1)[0].lower()
        if owner in excluded_owners:
            continue
        return arxiv_id, f"https://github.com/{owner_repo}"

    return arxiv_id, ""


_REFERENCE_TITLE_STOPWORDS = frozenset({
    "paper", "with", "from", "this", "that", "into", "using", "based",
    "large", "small", "model", "models", "language", "learning", "framework",
    "system", "systems", "approach", "method", "methods", "study", "novel",
    "efficient", "effective", "better", "generic", "general",
})


def _arxiv_bare_id(arxiv_id: str) -> str:
    """Strip the version suffix from an arxiv ID (``2503.14432v2`` → ``2503.14432``)."""
    if not arxiv_id:
        return ""
    return arxiv_id.strip().lower().split("v")[0]


def _arxiv_id_signals_in_text(text: str, arxiv_id: str) -> bool:
    """Whether ``text`` mentions ``arxiv_id`` via bare/v-suffixed form or an arxiv URL."""
    if not text or not arxiv_id:
        return False
    bare = _arxiv_bare_id(arxiv_id)
    if not bare:
        return False
    pattern = rf"(?:arxiv\.org/(?:abs|pdf)/)?{re.escape(bare)}(?:v\d+)?\b"
    return bool(re.search(pattern, text, re.IGNORECASE))


def _score_reference_confidence(
    ref_dir: Path,
    arxiv_id: str,
    paper_title: str,
) -> tuple[str, dict]:
    """Score whether the cloned reference repo actually implements the paper.

    Returns ``(tier, signals)`` where tier is one of:

    - ``"high"``: arxiv ID appears in README, CITATION file, or the top-3
      largest .py files. Fidelity gate runs strict.
    - ``"medium"``: paper-title tokens (≥3 unique ≥4-char non-stopwords)
      appear in the README. Fidelity gate runs but reports advisory-only.
    - ``"low"``: no signals — likely a catalog mislink. Fidelity soft-skips
      as ``pre_pr_fidelity_reference_mismatch`` and the publication proceeds.

    Motivating case: paper ``2503.14432v2`` (PLAY2PROMPT) had a catalog
    reference of ``microsoft/JARVIS``, but that repo self-identifies with
    arxiv ``2303.17580`` (HuggingGPT). Comparing PLAY2PROMPT code against
    HuggingGPT flagged every legitimate feature as fabrication and drove
    a false-positive ``skipped_fidelity_fabrication_after_patch``.
    """
    signals: dict = {
        "readme_arxiv_id": False,
        "citation_arxiv_id": False,
        "code_arxiv_id": False,
        "readme_title_tokens": False,
        "title_tokens_matched": 0,
    }
    if not ref_dir or not ref_dir.exists() or not arxiv_id:
        return "low", signals

    readme_text = ""
    for candidate in ("README.md", "README.rst", "README.txt", "readme.md", "Readme.md", "README"):
        readme_path = ref_dir / candidate
        if readme_path.exists() and readme_path.is_file():
            try:
                readme_text = readme_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                readme_text = ""
            break

    if readme_text and _arxiv_id_signals_in_text(readme_text, arxiv_id):
        signals["readme_arxiv_id"] = True

    for candidate in ("CITATION.cff", "CITATION.bib", "CITATION", "citation.cff", "citation.bib"):
        cite_path = ref_dir / candidate
        if cite_path.exists() and cite_path.is_file():
            try:
                cite_text = cite_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                cite_text = ""
            if cite_text and _arxiv_id_signals_in_text(cite_text, arxiv_id):
                signals["citation_arxiv_id"] = True
                break

    try:
        py_files = sorted(
            (p for p in ref_dir.rglob("*.py")
             if p.is_file() and ".git" not in p.parts),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )[:3]
        for py_path in py_files:
            try:
                py_text = py_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if _arxiv_id_signals_in_text(py_text, arxiv_id):
                signals["code_arxiv_id"] = True
                break
    except OSError:
        pass

    if signals["readme_arxiv_id"] or signals["citation_arxiv_id"] or signals["code_arxiv_id"]:
        return "high", signals

    if readme_text and paper_title:
        tokens = {w for w in _title_words(paper_title) if w not in _REFERENCE_TITLE_STOPWORDS}
        readme_lower = readme_text.lower()
        matched = sum(1 for tok in tokens if tok in readme_lower)
        signals["title_tokens_matched"] = matched
        if matched >= 3:
            signals["readme_title_tokens"] = True
            return "medium", signals

    return "low", signals


def _sniff_reference_confidence_remote(
    github_url: str,
    arxiv_id: str,
    paper_title: str,
    timeout_s: float = 10.0,
) -> tuple[str, dict]:
    """Cheap GitHub-Contents-API sniff without a full clone.

    Companion to ``_score_reference_confidence``, used to emit
    ``catalog_reference_confidence`` on every run summary — even runs where
    the fidelity gate never fires (Mode 3, publish=branch, no reference
    URL). Fetches only the repository's default README; falls back to
    ``"unknown"`` on any network/parse failure.
    """
    signals: dict = {
        "readme_arxiv_id": False,
        "readme_title_tokens": False,
        "title_tokens_matched": 0,
    }
    if not github_url or "github.com/" not in github_url:
        return "unknown", signals
    match = re.match(r"https?://github\.com/([^/#?]+/[^/#?]+)", github_url)
    if not match:
        return "unknown", signals
    slug = match.group(1).rstrip("/")
    if slug.endswith(".git"):
        slug = slug[:-4]

    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{slug}/readme",
            headers={"Accept": "application/vnd.github.raw",
                     "User-Agent": "outrider-reference-sniff"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            readme_text = resp.read().decode("utf-8", errors="ignore")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return "unknown", signals

    if not readme_text:
        return "low", signals

    if _arxiv_id_signals_in_text(readme_text, arxiv_id):
        signals["readme_arxiv_id"] = True
        return "high", signals

    if paper_title:
        tokens = {w for w in _title_words(paper_title) if w not in _REFERENCE_TITLE_STOPWORDS}
        matched = sum(1 for tok in tokens if tok in readme_text.lower())
        signals["title_tokens_matched"] = matched
        if matched >= 3:
            signals["readme_title_tokens"] = True
            return "medium", signals

    return "low", signals


def _clone_reference_repo(url: str, workdir: Path) -> tuple[bool, Path | None, str]:
    """Shallow-clone ``url`` into ``workdir/reference``. Returns
    (success, path, error_message). The clone is depth-1 since the audit
    only needs the current state of the reference, not its history."""
    if not url:
        return False, None, "no reference URL provided"
    ref_dir = workdir / "reference"
    if ref_dir.exists():
        shutil.rmtree(ref_dir)
    log.info(f"  → cloning reference {url} to {ref_dir}")
    # GIT_TERMINAL_PROMPT=0 fails fast on any auth prompt instead of
    # hanging the runner; public repos clone without credentials, and a
    # private ref isn't supported by this mode.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(ref_dir)],
            check=True,
            capture_output=True,
            timeout=180,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, None, f"clone timed out for {url}"
    except subprocess.CalledProcessError as e:
        return False, None, f"clone failed: {e.stderr.decode()[:500]}"
    return True, ref_dir, ""


def _fetch_pr_metadata(target: Target, pr_number: int) -> dict:
    """Fetch the PR's full metadata via gh_api. Returned dict has the
    standard GitHub PR shape: number, html_url, head/base, title, body,
    user, labels, etc."""
    return gh_api("GET", f"/repos/{target.repo}/pulls/{pr_number}")


def _fetch_pr_diff(target: Target, pr_number: int) -> str:
    """Fetch the unified diff of a PR. GitHub serves it as text/plain
    when Accept: application/vnd.github.v3.diff. gh_api wraps JSON, so
    we use the raw URL path for the diff."""
    token = _github_token()
    url = f"https://api.github.com/repos/{target.repo}/pulls/{pr_number}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github.v3.diff",
        "Authorization": f"Bearer {token}",
        "User-Agent": "remyxai-outrider",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def _build_fidelity_audit_prompt(
    pr_title: str,
    pr_body: str,
    pr_diff: str,
    arxiv_id: str,
    reference_url: str,
    reference_root: Path,
    mode: str = "mode-1",
    substitutions: "list[str] | None" = None,
    scoped_out: "list[str] | None" = None,
) -> str:
    """Compose the fidelity-audit prompt for the one-shot Claude call.

    The prompt instructs Claude to read the reference repo (which has
    been cloned into the workdir alongside the prompt's working
    directory), diff the PR's added code against it, and produce a
    structured Coverage matrix as JSON.

    Output shape (must be valid JSON, parseable by ``_extract_json_object``):

        {
          "summary": "<one-sentence overall verdict>",
          "needs_judgment": <bool>,
          "items": [
            {
              "name": "<paper §-or-feature name>",
              "draft_location": "<file::symbol or null>",
              "reference_location": "<file::symbol or null>",
              "status": "covered" | "deferred" | "deviation",
              "deviation_class": null | "defensible" | "needs-judgment",
              "rationale": "<one to three sentences>"
            },
            ...
          ]
        }
    """
    # Cap each input section so the prompt stays under the context window.
    # The reference repo is at ``./reference/`` relative to Claude's
    # workdir; the agent can explore it via Read/Glob/Grep — we don't
    # try to embed it here.
    diff_excerpt = pr_diff if len(pr_diff) <= 60_000 else (
        pr_diff[:60_000] + f"\n... [diff truncated, total {len(pr_diff)} chars] ...\n"
    )
    body_excerpt = (pr_body or "")[:4000]

    # Mode-aware guidance (REMYX-195). Mode 3 is handled by a separate
    # function; here we branch between Mode 1 (strict) and Mode 2
    # (auxiliary-substitution-tolerant).
    substitutions = substitutions or []
    scoped_out = scoped_out or []
    mode_guidance = ""
    if mode == "mode-2" and substitutions:
        subs_bullets = "\n".join(f"  - {s}" for s in substitutions[:20])
        mode_guidance = f"""

# Mode 2 — adapted port

The coding session cited this as a **Mode 2 (adapted port)** implementation:
the paper's core mechanism is preserved at full fidelity, but auxiliary
components were intentionally substituted with target-native equivalents.
The following substitutions were explicitly declared in the self-review:

{subs_bullets}

Deltas from the reference that match these substitutions are EXPECTED
and should be classified as ``deferred`` (with the substitution named
in the rationale) or ``deviation`` / ``defensible``, NOT
``needs-judgment``. Only flag as ``needs-judgment`` when the deviation
lies OUTSIDE the declared substitutions — i.e. an unexpected change
to the paper's core mechanism itself.
"""
    if scoped_out:
        sco_bullets = "\n".join(f"  - {s}" for s in scoped_out[:20])
        mode_guidance += f"""

# Deliberately scoped out

The self-review declared the following items as intentionally NOT built —
scoping decisions, not fabrication. Items on this list should be
classified as ``deferred`` (cite which entry justifies the deferral),
NOT ``needs-judgment``:

{sco_bullets}
"""

    return f"""You are auditing a draft PR's fidelity to its reference implementation.

# PR under audit

**Title**: {pr_title}

**Paper**: arxiv:{arxiv_id or "(unknown)"}

**Reference impl**: {reference_url or "(unknown)"} (cloned at ./reference/)

## PR body

```
{body_excerpt}
```

## PR diff

```
{diff_excerpt}
```
{mode_guidance}
# Task

Read the reference impl at ./reference/ — explore the files that correspond to
the PR's added/modified code. Then produce a structured Coverage matrix that
classifies each notable piece of the PR as one of:

- **covered**: the PR faithfully implements the reference. Cite both locations.
- **deferred**: the PR intentionally leaves something out of scope. Cite what
  the PR body says about it (e.g., "§4.4 adaptive decay deferred as follow-up").
- **deviation**: the PR diverges from the reference. Classify as either:
  - **defensible**: the deviation is a reasonable adaptation to the target
    repo's conventions or API shape (e.g., advantage-multiply vs PG-loss-multiply
    in PPO-clipped settings; one is mathematically nearly equivalent and matches
    the target framework's idiom)
  - **needs-judgment**: the deviation is substantive and should be a human
    review point (e.g., different default values that change behavior, missing
    a paper-headline mechanism, computing a different mathematical quantity)

Focus on the math/algorithm-bearing pieces — function bodies, numerical
constants, control-flow shape, what's multiplied by what. Don't flag stylistic
differences (variable naming, docstring shape, test framework) as deviations.

# Output

Return ONLY a valid JSON object (no prose before or after):

{{
  "summary": "<one-sentence overall verdict>",
  "needs_judgment": <true if any item.deviation_class == "needs-judgment">,
  "items": [
    {{
      "name": "<paper §-or-feature name>",
      "draft_location": "<file::symbol or null>",
      "reference_location": "<file::symbol or null>",
      "status": "covered" | "deferred" | "deviation",
      "deviation_class": null | "defensible" | "needs-judgment",
      "rationale": "<one to three sentences>"
    }}
  ]
}}
"""


def _local_git_diff(workdir: "Path", base_branch: str = "main") -> str:
    """Return the diff of committed changes on the current branch vs base.

    Called pre-PR-publication so fidelity can audit local branch changes
    without fetching from GitHub. ``workdir`` is the clone that
    ``commit_and_push`` just built on. Returns ``""`` on any git failure —
    caller decides how to degrade.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--no-color", f"{base_branch}..HEAD"],
            cwd=str(workdir), capture_output=True, text=True, timeout=30,
        )
        return result.stdout if result.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError) as e:
        log.debug(f"  local git diff failed: {e}")
        return ""


_MODE_1_TOKENS = ("mode 1", "direct port")
_MODE_2_TOKENS = ("mode 2", "adapted port")
_MODE_3_TOKENS = ("mode 3", "inspired experiment", "inspired adaptation")


def _classify_mode_cited(review: "dict | None") -> str:
    """Read the self-review's ``mode_cited`` field and normalize to one
    of ``'mode-1'`` / ``'mode-2'`` / ``'mode-3'`` / ``''`` (unspecified).

    Free-text tolerant: the coding session may spell the mode as
    ``'Mode 3 (inspired experiment)'``, ``'Mode 3'``, or ``'inspired
    adaptation'``; all normalize to ``'mode-3'``. Returns ``''`` when
    the self-review didn't cite a mode (pre-v1.7.14 shape or Claude
    omitted the field) — caller falls back to strict Mode-1 semantics.
    """
    if not review:
        return ""
    raw = (review.get("mode_cited") or "").lower()
    if not raw:
        return ""
    for tok in _MODE_3_TOKENS:
        if tok in raw:
            return "mode-3"
    for tok in _MODE_2_TOKENS:
        if tok in raw:
            return "mode-2"
    for tok in _MODE_1_TOKENS:
        if tok in raw:
            return "mode-1"
    return ""


def _run_pre_pr_fidelity_check(
    rec: "Recommendation",
    target: "Target",
    workdir: "Path",
    pr_title: str,
    pr_body: str,
    base_branch: str = "main",
    self_review: "dict | None" = None,
) -> dict:
    """Run the fidelity audit on the local branch, before opening a PR.

    Mirrors ``run_fidelity_mode`` but takes local inputs (title/body/diff
    from what's about to be posted) instead of fetching from a PR that
    doesn't exist yet. Returns a verdict dict with ``needs_judgment``,
    ``items_count``, ``coverage_section``, ``status``, ``matrix``.

    Mode-aware routing (REMYX-195): reads ``self_review['mode_cited']``
    to pick the audit shape.

      * **Mode 1 (direct port)** — strict reference-vs-diff comparison.
        Items appearing in ``self_review['scoped_out']`` are cross-
        referenced against the Coverage matrix and downgraded from
        ``needs-judgment`` to ``deferred`` before the verdict.
      * **Mode 2 (adapted port)** — strict on the core mechanism, but
        deviations that match ``self_review['substitutions']`` (auxiliary
        components deliberately swapped for target-native equivalents)
        are treated as ``defensible`` rather than ``needs-judgment``.
        The prompt is augmented with the substitution list so Claude
        knows which deltas are intended.
      * **Mode 3 (inspired experiment)** — the diff does not port the
        paper's method. Method-vs-diff comparison is skipped; instead
        an insight-preservation check verifies that
        ``self_review['reframed_insight']`` (the paper's core insight
        being drawn on) is reflected in the diff's docstrings and
        honest_summary. This replaces the previous
        ``pre_pr_fidelity_skipped_no_reference`` behavior for Mode-3
        outputs, which is now the *normal* case under v1.7.14 rather
        than a fallback edge case.

    When the self-review omits ``mode_cited`` (pre-v1.7.14 runs or
    Claude skipped the field), Mode 1 is assumed — matches historical
    behavior.

    Reference-anchored mode remains the only path when a reference URL
    exists; when none is available AND the mode is not Mode 3, returns
    ``needs_judgment=False`` and lets the flow proceed (equivalent to
    the old post-PR skip behavior).
    """
    mode = _classify_mode_cited(self_review)
    verdict = {
        "needs_judgment": False,
        "items_count": 0,
        "coverage_section": "",
        "status": "pre_pr_fidelity_skipped_no_reference",
        "matrix": None,
        "mode_cited": mode or "mode-1",
    }

    # Mode 3 has its own audit shape — insight-preservation, no reference impl needed.
    if mode == "mode-3":
        return _run_mode3_insight_preservation_check(
            rec, target, workdir, pr_body, self_review, base_branch, verdict,
        )

    _, reference_url = _extract_reference_url_from_pr_body(pr_body, target.repo)
    if not reference_url:
        log.info(f"  → pre-PR fidelity ({verdict['mode_cited']}): no reference URL, skipping")
        return verdict
    verdict["reference_url"] = reference_url

    fid_workdir = Path(tempfile.mkdtemp(prefix="outrider-prepub-fidelity-"))
    log.info(f"  → pre-PR fidelity workdir: {fid_workdir}")
    ok, ref_dir, err = _clone_reference_repo(reference_url, fid_workdir)
    if not ok:
        verdict["status"] = "pre_pr_fidelity_failed_clone"
        verdict["error"] = err
        log.warning(f"  ⚠ pre-PR fidelity clone failed: {err}")
        return verdict

    # Reference-impl sanity check: does the cloned repo actually self-identify
    # as an implementation of this paper? When a catalog mislink points at an
    # unrelated repo (observed: 2503.14432v2 → microsoft/JARVIS, which
    # self-identifies with arxiv 2303.17580), a full fidelity comparison
    # against the wrong code flags every legitimate feature as fabrication.
    confidence, ref_signals = _score_reference_confidence(
        ref_dir, rec.arxiv_id, rec.paper_title,
    )
    verdict["reference_confidence"] = confidence
    verdict["reference_signals"] = ref_signals
    log.info(
        f"  → reference-impl confidence: {confidence} "
        f"(readme_arxiv={ref_signals.get('readme_arxiv_id', False)}, "
        f"citation_arxiv={ref_signals.get('citation_arxiv_id', False)}, "
        f"code_arxiv={ref_signals.get('code_arxiv_id', False)}, "
        f"title_tokens={ref_signals.get('title_tokens_matched', 0)})"
    )
    if confidence == "low":
        verdict["status"] = "pre_pr_fidelity_reference_mismatch"
        log.warning(
            f"  ⚠ pre-PR fidelity: reference {reference_url} shows no "
            f"evidence of implementing arxiv:{rec.arxiv_id}; soft-skipping "
            f"the fidelity gate (publication proceeds; catalog data-quality "
            f"issue surfaced in telemetry)"
        )
        return verdict

    advisory_only = (confidence == "medium")
    if advisory_only:
        log.info(
            f"  → pre-PR fidelity: medium-confidence reference "
            f"(title-token match only); running in advisory mode "
            f"(flags surfaced but non-blocking)"
        )

    diff = _local_git_diff(workdir, base_branch)
    if not diff:
        verdict["status"] = "pre_pr_fidelity_failed_no_diff"
        log.warning("  ⚠ pre-PR fidelity: local git diff empty; skipping")
        return verdict

    substitutions = (self_review or {}).get("substitutions") or []
    scoped_out = (self_review or {}).get("scoped_out") or []
    prompt = _build_fidelity_audit_prompt(
        pr_title=pr_title,
        pr_body=pr_body,
        pr_diff=diff,
        arxiv_id=rec.arxiv_id,
        reference_url=reference_url,
        reference_root=ref_dir,
        mode=verdict["mode_cited"],
        substitutions=substitutions,
        scoped_out=scoped_out,
    )
    log.info(
        f"  → pre-PR fidelity Claude one-shot "
        f"({verdict['mode_cited']}, subs={len(substitutions)}, "
        f"scoped_out={len(scoped_out)}, timeout={target.claude_timeout_s}s)"
    )
    ok, raw = _run_claude_oneshot(
        fid_workdir, prompt, target.claude_timeout_s, max_turns=20,
    )
    if not ok:
        verdict["status"] = "pre_pr_fidelity_failed_claude"
        verdict["error"] = f"Claude non-zero: {raw[-500:]}"
        log.warning(f"  ⚠ pre-PR fidelity Claude failed")
        return verdict

    matrix = _extract_json_object(raw)
    if not matrix or "items" not in matrix:
        verdict["status"] = "pre_pr_fidelity_failed_claude"
        verdict["error"] = f"unparseable JSON: {raw[-500:]}"
        log.warning("  ⚠ pre-PR fidelity: unparseable JSON")
        return verdict

    coverage_section = _render_coverage_matrix(matrix, audit_anchor="reference")
    raw_needs_judgment = bool(matrix.get("needs_judgment", False))

    # Advisory-mode: under medium-confidence reference, flags are surfaced
    # to the team but don't block publication — the reference is likely the
    # right paper (title tokens match) but doesn't self-identify strongly
    # enough to trust the flag count as ground truth.
    effective_needs_judgment = raw_needs_judgment and not advisory_only

    if advisory_only and raw_needs_judgment:
        status = "pre_pr_fidelity_advisory"
    elif effective_needs_judgment:
        status = "pre_pr_fidelity_needs_judgment"
    else:
        status = "pre_pr_fidelity_clean"

    verdict.update({
        "needs_judgment": effective_needs_judgment,
        "items_count": len(matrix.get("items", [])),
        "coverage_section": coverage_section,
        "matrix": matrix,
        "advisory_only": advisory_only,
        "status": status,
    })
    log.info(
        f"  ✓ pre-PR fidelity: {verdict['items_count']} items, "
        f"needs_judgment={effective_needs_judgment}"
        + (f" (advisory-only; raw flag count was {matrix.get('items', [])})"
           if advisory_only and raw_needs_judgment else "")
    )
    return verdict


def _build_mode3_insight_preservation_prompt(
    pr_title: str,
    pr_diff: str,
    reframed_insight: str,
    honest_summary: str,
    delivered: "list[str]",
    arxiv_id: str,
) -> str:
    """Compose the Mode-3 insight-preservation audit prompt.

    Under Mode 3 the diff does NOT port the paper's method — it applies
    the paper's insight to a target-native problem. So the fidelity
    question shifts from *"does the diff match the reference?"* to
    *"does the diff preserve the paper's core insight, and does the
    self-review honestly explain the reframing?"*.
    """
    diff_excerpt = pr_diff if len(pr_diff) <= 60_000 else (
        pr_diff[:60_000] + f"\n... [diff truncated, total {len(pr_diff)} chars] ...\n"
    )
    delivered_bullets = "\n".join(f"  - {d}" for d in (delivered or [])[:10]) or "  (none listed)"
    return f"""You are auditing a Mode-3 (inspired experiment) PR's INSIGHT PRESERVATION.

Mode 3 means: the coding session took the paper's core insight or framing and
implemented a target-native experiment drawing on it. The PR does NOT reproduce
the paper's method. So the usual paper-method-vs-diff comparison is the WRONG
question here — the diff is EXPECTED to diverge from the paper's method by
design.

The right questions:

1. Is the paper's core insight actually preserved in the diff?
2. Does the module's docstring / code comments name the paper it draws on,
   and honestly frame this as an *inspired adaptation* rather than a port?
3. Does the honest_summary explain the reframing?

# PR under audit

**Title**: {pr_title}

**Paper**: arxiv:{arxiv_id or "(unknown)"}

## Self-review's reframed insight (what the paper contributes that this diff draws on)

{reframed_insight}

## Self-review's honest summary

{honest_summary}

## Self-review's `delivered` items

{delivered_bullets}

## Diff

```
{diff_excerpt}
```

# Task

Evaluate whether this PR PRESERVES the paper's insight. Emit a Coverage matrix
whose ``items`` describe the *insight-preservation* checks, not method-diff.
Each item's ``status`` is:

- **covered**: the diff's docstrings / comments / code faithfully embody the
  reframed insight; the reader can trace the paper's idea to the target-native
  implementation via the docstring alone.
- **deferred**: parts of the paper's contribution intentionally NOT implemented
  because Mode 3 leaves them out (the world model, the full training system,
  the paper's specific auxiliary components).
- **deviation**: the diff diverges from the reframed insight itself.
  - **defensible**: an adaptation of the insight to fit the target's shape.
  - **needs-judgment**: the diff has drifted from the insight (e.g. the
    module docstring cites the paper but the code doesn't actually implement
    the insight; or the docstring names the paper but the code is unrelated).

Also produce ``insight_check`` items with these three specific verifications:

- ``docstring_cites_paper`` — does the new module's docstring name the paper
  (arxiv id or title)?
- ``docstring_frames_as_inspired`` — does it honestly frame this as inspired /
  adapted rather than claiming to be a port?
- ``code_embodies_insight`` — does the code path actually implement the reframed
  insight, or just add scaffolding that doesn't do the work?

Return ONLY a valid JSON object (no prose before or after):

{{
  "summary": "<one-sentence overall verdict on insight preservation>",
  "needs_judgment": <true if any item.deviation_class == "needs-judgment" or
                     any insight_check.status == "fail">,
  "items": [
    {{
      "name": "<what's being checked, e.g. 'insight: value-overestimation as gating signal'>",
      "draft_location": "<file::symbol or null>",
      "reference_location": null,
      "status": "covered" | "deferred" | "deviation",
      "deviation_class": null | "defensible" | "needs-judgment",
      "rationale": "<one to three sentences>"
    }}
  ],
  "insight_check": {{
    "docstring_cites_paper":       "pass" | "fail",
    "docstring_frames_as_inspired": "pass" | "fail",
    "code_embodies_insight":        "pass" | "fail",
    "rationale": "<one to three sentences>"
  }}
}}
"""


def _run_mode3_insight_preservation_check(
    rec: "Recommendation",
    target: "Target",
    workdir: "Path",
    pr_body: str,
    self_review: dict,
    base_branch: str,
    verdict: dict,
) -> dict:
    """Mode-3 fidelity: verify the paper's reframed insight is preserved
    in the diff, rather than comparing the diff to a reference impl.

    Returns the same verdict shape as ``_run_pre_pr_fidelity_check``,
    but with ``status`` values ``pre_pr_fidelity_mode3_insight_preserved``
    (clean), ``pre_pr_fidelity_mode3_needs_judgment`` (drift detected),
    or ``pre_pr_fidelity_mode3_skipped_no_insight`` (self-review didn't
    fill in ``reframed_insight``).
    """
    reframed_insight = (self_review.get("reframed_insight") or "").strip()
    honest_summary = (self_review.get("honest_summary") or "").strip()
    delivered = self_review.get("delivered") or []

    if not reframed_insight:
        log.info(
            "  → pre-PR fidelity (mode-3): no reframed_insight in self-review; "
            "skipping (Claude may have omitted the field)"
        )
        verdict["status"] = "pre_pr_fidelity_mode3_skipped_no_insight"
        return verdict

    diff = _local_git_diff(workdir, base_branch)
    if not diff:
        verdict["status"] = "pre_pr_fidelity_failed_no_diff"
        log.warning("  ⚠ pre-PR fidelity (mode-3): local git diff empty; skipping")
        return verdict

    prompt = _build_mode3_insight_preservation_prompt(
        pr_title=format_pr_title(rec),
        pr_diff=diff,
        reframed_insight=reframed_insight,
        honest_summary=honest_summary,
        delivered=delivered,
        arxiv_id=rec.arxiv_id,
    )
    log.info(
        f"  → pre-PR fidelity (mode-3) insight-preservation Claude one-shot "
        f"(timeout={target.claude_timeout_s}s)"
    )
    audit_workdir = Path(tempfile.mkdtemp(prefix="outrider-mode3-fidelity-"))
    ok, raw = _run_claude_oneshot(
        audit_workdir, prompt, target.claude_timeout_s, max_turns=10,
    )
    if not ok:
        verdict["status"] = "pre_pr_fidelity_failed_claude"
        verdict["error"] = f"Claude non-zero: {raw[-500:]}"
        log.warning("  ⚠ pre-PR fidelity (mode-3) Claude failed")
        return verdict

    matrix = _extract_json_object(raw)
    if not matrix or "items" not in matrix:
        verdict["status"] = "pre_pr_fidelity_failed_claude"
        verdict["error"] = f"unparseable JSON: {raw[-500:]}"
        log.warning("  ⚠ pre-PR fidelity (mode-3): unparseable JSON")
        return verdict

    coverage_section = _render_coverage_matrix(matrix, audit_anchor="insight")
    # Insight-check failures also promote to needs_judgment.
    insight_check = matrix.get("insight_check") or {}
    insight_fail = any(
        insight_check.get(k) == "fail"
        for k in ("docstring_cites_paper", "docstring_frames_as_inspired",
                  "code_embodies_insight")
    )
    needs_judgment = bool(matrix.get("needs_judgment", False)) or insight_fail

    verdict.update({
        "needs_judgment": needs_judgment,
        "items_count": len(matrix.get("items", [])),
        "coverage_section": coverage_section,
        "matrix": matrix,
        "insight_check": insight_check,
        "status": (
            "pre_pr_fidelity_mode3_needs_judgment" if needs_judgment
            else "pre_pr_fidelity_mode3_insight_preserved"
        ),
    })
    log.info(
        f"  ✓ pre-PR fidelity (mode-3): {verdict['items_count']} items, "
        f"insight_check={insight_check}, needs_judgment={needs_judgment}"
    )
    return verdict


def _attempt_pre_pr_fidelity_patch(
    workdir: "Path",
    coverage_matrix: dict,
    reference_url: str,
    timeout_s: int = 900,
) -> bool:
    """Invoke Claude Code with fidelity findings to patch the local branch.

    Bounded single-attempt remediation: Claude gets the coverage matrix
    of flagged deviations + the reference-repo URL as context and edits
    the branch's files to resolve them. Caller re-runs fidelity after.

    Returns ``True`` if Claude exited cleanly AND touched files in the
    workdir (so re-fidelity is worth running). ``False`` on Claude
    failure or no edits — caller should skip publication.
    """
    if not coverage_matrix or not coverage_matrix.get("items"):
        return False
    flagged = [
        it for it in coverage_matrix["items"]
        if isinstance(it, dict) and "deviation" in str(it.get("status", "")).lower()
    ]
    if not flagged:
        return False

    # Compose a Claude Code prompt file that Claude reads on startup.
    findings_md = "\n".join(
        f"- **{it.get('name', '(unnamed)')}** ({it.get('status', '')}): "
        f"{it.get('rationale', '')[:400]}"
        for it in flagged
    )
    patch_brief = f"""# Fidelity remediation brief

Fidelity audit against the reference implementation ({reference_url})
flagged {len(flagged)} substantive deviations in the current branch.
Fix them by editing the branch's files to match the reference.

**Constraints**:
- Match the reference's algorithm, defaults, and mechanism — do not invent
- If the paper claim in the PR body is wrong, correct it — reference is
  ground truth
- Do not add new scope (no unrelated features, no drive-by refactors)
- Keep the diff surface small — the goal is to resolve these findings,
  not rewrite the module

**Flagged items**:
{findings_md}

Read the reference codebase (already cloned as `./reference/`) as needed.
Apply the fixes; changes are staged by the caller.
"""
    # Write the patch brief to INVOCATION.md — the file `invoke_claude_code`
    # reads at startup. The original INVOCATION.md was consumed by the earlier
    # implementation session; overwriting it here is safe because no downstream
    # reader in the pre-PR flow needs the original content.
    brief_path = workdir / ".remyx-recommendation" / "INVOCATION.md"
    brief_path.parent.mkdir(exist_ok=True)
    brief_path.write_text(patch_brief)

    # Record file mtimes to detect edits
    before = {
        p: p.stat().st_mtime
        for p in workdir.rglob("*.py")
        if ".git" not in p.parts and ".remyx-recommendation" not in p.parts
    }

    log.info(f"  → pre-PR fidelity patch attempt ({len(flagged)} deviations, timeout={timeout_s}s)")
    ok, log_tail = invoke_claude_code(workdir, timeout_s=timeout_s)
    if not ok:
        log.warning(f"  ⚠ patch attempt failed: {log_tail[-300:]}")
        return False

    # Did anything actually change?
    after = {
        p: p.stat().st_mtime
        for p in workdir.rglob("*.py")
        if ".git" not in p.parts and ".remyx-recommendation" not in p.parts
    }
    touched = any(after.get(p, 0) > before.get(p, 0) for p in after)
    if not touched:
        log.warning("  ⚠ patch attempt: no .py files touched")
        return False
    log.info(f"  ✓ patch attempt applied edits — re-running fidelity")
    return True


def _build_fidelity_audit_prompt_paper_anchored(
    pr_title: str,
    pr_body: str,
    pr_diff: str,
    arxiv_id: str,
    paper_text: str,
) -> str:
    """Compose the paper-anchored fidelity-audit prompt.

    Used as Phase A's degraded mode when the paper has no public
    reference impl to clone — the audit anchors against the paper's
    title + abstract (fetched via ``_fetch_arxiv_abstract_text``)
    instead of a reference codebase. Lower precision than the
    reference-anchored variant (abstracts can be vague on
    implementation-level details), but materially better than skipping
    the entire chain.

    Output schema matches ``_build_fidelity_audit_prompt`` so the same
    ``_render_coverage_matrix`` renderer + downstream consumers handle
    both modes. The ``reference_location`` field is interpreted as
    "paper section/equation" when paper-anchored (the renderer doesn't
    care; the value is opaque markdown).
    """
    diff_excerpt = pr_diff if len(pr_diff) <= 60_000 else (
        pr_diff[:60_000] + f"\n... [diff truncated, total {len(pr_diff)} chars] ...\n"
    )
    body_excerpt = (pr_body or "")[:4000]
    paper_excerpt = paper_text[:8000]

    return f"""You are auditing a draft PR's fidelity to the paper it claims to implement.

# PR under audit

**Title**: {pr_title}

**Paper**: arxiv:{arxiv_id or "(unknown)"}

**Audit mode**: paper-anchored (no public reference impl available — comparing the diff against the paper's described method instead of a concrete reference codebase).

## Paper text (title + abstract)

```
{paper_excerpt}
```

## PR body

```
{body_excerpt}
```

## PR diff

```
{diff_excerpt}
```

# Task

Compare the diff against the paper's described method. Produce a structured
Coverage matrix that classifies each notable piece of the PR as one of:

- **covered**: the PR implements something the paper explicitly describes.
  Cite the paper's claim (a phrase from the abstract or a section reference
  if mentioned in the PR body) and the PR location.
- **deferred**: the PR intentionally leaves something out of scope. Cite what
  the PR body says about it (e.g., "§4.4 adaptive decay deferred as follow-up").
- **deviation**: the PR implements something the paper does not describe, or
  implements it in a way the paper's abstract contradicts. Classify as:
  - **defensible**: a reasonable adaptation when the paper is silent on
    implementation specifics (e.g., the abstract describes a "filter" without
    fixing the algorithm; the diff picks a standard choice).
  - **needs-judgment**: substantive deviation from a claim the paper makes
    (e.g., different numerical constants, missing a paper-headline mechanism,
    computing a different mathematical quantity than the abstract describes).

The abstract is a method-summary surface — it tells you what the paper claims
to do at a high level, but not the implementation-level specifics. When the
abstract is silent on a detail in the diff, treat that as **deferred** (out
of scope of what the abstract covers) rather than a deviation. Reserve
**needs-judgment** for items where the diff clearly conflicts with what the
abstract claims.

# Output

Return ONLY a valid JSON object (no prose before or after):

{{
  "summary": "<one-sentence overall verdict>",
  "needs_judgment": <true if any item.deviation_class == "needs-judgment">,
  "items": [
    {{
      "name": "<paper claim or feature name>",
      "draft_location": "<file::symbol or null>",
      "reference_location": "<paper section/claim or null>",
      "status": "covered" | "deferred" | "deviation",
      "deviation_class": null | "defensible" | "needs-judgment",
      "rationale": "<one to three sentences>"
    }}
  ]
}}
"""


def _render_coverage_matrix(matrix: dict, audit_anchor: str = "reference") -> str:
    """Render the Claude-emitted Coverage matrix as a markdown section
    to append to the PR body. Renders as a table with one row per item
    plus a summary line, an audit-anchor line (sets precision
    expectations for the maintainer), and a needs-judgment callout.

    ``audit_anchor`` is one of ``"reference"`` (Phase A clone-and-diff
    against the paper's reference impl — higher precision) or
    ``"paper"`` (Phase A degraded mode anchored against the paper's
    abstract text only — lower precision). The line is omitted for
    unknown values to keep older call sites round-trippable.
    """
    summary = matrix.get("summary", "(no summary)")
    needs_judgment = bool(matrix.get("needs_judgment", False))
    items = matrix.get("items", [])

    lines = [FIDELITY_COVERAGE_SECTION_HEADER, ""]
    lines.append(f"_{summary}_")
    lines.append("")
    if audit_anchor == "reference":
        lines.append("_Audit anchor: reference implementation (diff vs. cloned reference codebase)._")
        lines.append("")
    elif audit_anchor == "paper":
        lines.append(
            "_Audit anchor: paper abstract (no public reference impl available — "
            "this audit compares the diff to the paper's described method at the "
            "abstract level, which is less precise than a code-level comparison)._"
        )
        lines.append("")
    elif audit_anchor == "insight":
        lines.append(
            "_Audit anchor: paper insight (Mode 3 — inspired experiment). "
            "This diff does not port the paper's method; the check validates that "
            "the paper's core insight is preserved in the diff's docstrings and "
            "code path, and that the self-review honestly frames the reframing._"
        )
        lines.append("")
    if needs_judgment:
        lines.append("> ⚠️ One or more items below need human judgment — see rows marked `deviation (needs-judgment)`.")
        lines.append("")

    if not items:
        lines.append("_No items extracted from the reference comparison._")
        return "\n".join(lines)

    lines.append("| Item | Status | Draft location | Reference location | Rationale |")
    lines.append("|---|---|---|---|---|")
    for it in items:
        name = (it.get("name") or "").replace("|", "\\|")
        status_raw = (it.get("status") or "unknown")
        dev_class = it.get("deviation_class") or ""
        status = f"{status_raw} ({dev_class})" if dev_class else status_raw
        draft_loc = (it.get("draft_location") or "—").replace("|", "\\|")
        ref_loc = (it.get("reference_location") or "—").replace("|", "\\|")
        rationale = (it.get("rationale") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {name} | {status} | `{draft_loc}` | `{ref_loc}` | {rationale} |")
    lines.append("")
    lines.append(
        f"_Generated by [Outrider fidelity audit]"
        f"({CANONICAL_ATTRIBUTION_URL}) at {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}._"
    )
    return "\n".join(lines)


def _append_to_step_summary(markdown: str) -> None:
    """Append markdown to ``$GITHUB_STEP_SUMMARY``.

    Refinement-pass phases write their per-run output here rather than to
    the PR body — the body should be 100% governed by the upstream-repo
    convention-extraction (Phase B's job), and our per-phase metadata
    belongs on the action's run-summary panel where it can be inspected
    without polluting the maintainer's PR-body conventions.
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(markdown.rstrip() + "\n\n")
    except OSError as e:
        log.warning(f"Could not append to $GITHUB_STEP_SUMMARY: {e}")


def _append_or_replace_section(body: str, header: str, section_content: str) -> str:
    """Idempotently append (or replace) a top-level section in a PR body.

    A section is delimited by its ``## Header`` line on top, and either
    the next ``# `` / ``## `` heading or end-of-body on the bottom. If
    ``header`` is already present, the existing section is replaced
    in-place; otherwise the new section is appended at the end with a
    ``---`` separator.

    Used by every refinement phase that writes to the PR body so re-runs
    are idempotent and the section ordering is stable.
    """
    body = body or ""
    if header in body:
        idx = body.index(header)
        tail = body[idx + len(header):]
        next_h = re.search(r"\n(##? )", tail)
        if next_h:
            return body[:idx] + section_content + tail[next_h.start():]
        return body[:idx] + section_content
    sep = "\n\n---\n\n" if body.strip() else ""
    return body + sep + section_content


def _append_or_replace_coverage(body: str, coverage_section: str) -> str:
    """Backward-compat wrapper for Phase A's Coverage-section update."""
    return _append_or_replace_section(body, FIDELITY_COVERAGE_SECTION_HEADER, coverage_section)


def _drop_pr_draft_state(target: Target, pr_number: int) -> bool:
    """Mark a draft PR as ready-for-review via the GraphQL mutation
    ``markPullRequestReadyForReview``. The REST endpoint
    (``PATCH /repos/{repo}/pulls/{n}``) silently ignores the ``draft``
    field — only the GraphQL mutation actually transitions the state.

    Returns True on success, False on any error (non-fatal — the
    label transition is the primary signal either way).
    """
    pr = gh_api("GET", f"/repos/{target.repo}/pulls/{pr_number}")
    if not pr.get("draft"):
        return True   # already non-draft, idempotent
    node_id = pr.get("node_id", "")
    if not node_id:
        return False
    mutation = (
        "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){"
        "pullRequest{isDraft}}}"
    )
    try:
        gh_graphql(mutation, {"id": node_id})
        return True
    except RuntimeError as e:
        log.warning(f"  ! could not drop draft state on PR #{pr_number}: {e}")
        return False


def _update_pr_body(target: Target, pr_number: int, new_body: str) -> None:
    """PATCH the PR body via gh_api."""
    gh_api("PATCH", f"/repos/{target.repo}/pulls/{pr_number}", {"body": new_body})


def _add_pr_label(target: Target, pr_number: int, label: str) -> None:
    """Add a label to the PR. Idempotent — a 422 (already-present) is
    swallowed since the post-condition is what we want."""
    try:
        gh_api(
            "POST",
            f"/repos/{target.repo}/issues/{pr_number}/labels",
            {"labels": [label]},
        )
    except RuntimeError as e:
        if "422" in str(e) or "already_exists" in str(e):
            return
        raise


def _remove_pr_label(target: Target, pr_number: int, label: str) -> None:
    """Remove a label from the PR. Swallows 404 (already-absent)."""
    try:
        gh_api(
            "DELETE",
            f"/repos/{target.repo}/issues/{pr_number}/labels/{urllib.parse.quote(label)}",
        )
    except RuntimeError as e:
        if "404" in str(e):
            return
        raise


def run_fidelity_audit(target: Target) -> dict:
    """Diff a remyx-ai[bot] PR against its paper's reference impl, post
    a Coverage matrix to the PR body, and transition labels.

    The PR number is read from ``INPUT_PR_NUMBER`` (set by the
    workflow's ``pull_request`` event payload). The reference URL is
    extracted from the PR body using the standard arxiv + github
    embedding shape Outrider produces.

    Returns a status dict suitable for the action's $GITHUB_OUTPUT and
    the telemetry post. Status values:

        fidelity_audited                       — happy path; Coverage
                                                 matrix posted (reference
                                                 impl anchor)
        fidelity_audited_needs_judgment        — happy path + at least
                                                 one item flagged for
                                                 human review (reference
                                                 impl anchor)
        fidelity_audited_paper_anchored        — happy path; Coverage
                                                 matrix posted (paper
                                                 abstract anchor — used
                                                 when no public reference
                                                 impl is available)
        fidelity_audited_paper_anchored_needs_judgment — paper-anchored
                                                 happy path + needs-judgment
        fidelity_skipped_no_pr                 — INPUT_PR_NUMBER empty
        fidelity_skipped_not_bot               — PR not authored by
                                                 remyx-ai[bot]
        fidelity_skipped_no_reference          — couldn't extract a
                                                 reference URL AND no
                                                 arxiv id available for
                                                 the paper-anchored
                                                 degraded mode
        fidelity_failed_clone                  — reference clone failed
        fidelity_failed_claude                 — Claude call failed or
                                                 returned unparseable JSON
    """
    result: dict = {"repo": target.repo, "mode": "fidelity", "status": "unknown"}

    pr_number_raw = os.environ.get("INPUT_PR_NUMBER", "").strip()
    if not pr_number_raw:
        result["status"] = "fidelity_skipped_no_pr"
        log.info("  ✗ fidelity mode invoked without INPUT_PR_NUMBER")
        return result
    try:
        pr_number = int(pr_number_raw)
    except ValueError:
        result["status"] = "fidelity_skipped_no_pr"
        log.error(f"  ✗ INPUT_PR_NUMBER={pr_number_raw!r} is not an integer")
        return result
    result["pr_number"] = pr_number

    log.info(f"  → fidelity audit on {target.repo}#{pr_number}")

    try:
        pr = _fetch_pr_metadata(target, pr_number)
    except RuntimeError as e:
        result["status"] = "fidelity_failed_claude"
        result["error"] = f"could not fetch PR: {e}"
        return result

    # Bot-author gate — we only audit remyx-ai[bot] PRs to avoid
    # touching human-opened PRs that happened to get the label.
    author = (pr.get("user") or {}).get("login", "")
    if author != "remyx-ai[bot]":
        result["status"] = "fidelity_skipped_not_bot"
        result["error"] = f"PR author is {author!r}, not remyx-ai[bot]"
        log.info(f"  ✗ PR #{pr_number} authored by {author!r}; skipping")
        return result

    body = pr.get("body") or ""
    title = pr.get("title") or ""
    arxiv_id, reference_url = _extract_reference_url_from_pr_body(body, target.repo)
    result["arxiv_id"] = arxiv_id

    # Workdir layout: when reference-anchored, ./reference/ holds the
    # cloned ref impl and Claude's cwd is the workdir root so it can
    # Read/Glob/Grep into it. In paper-anchored mode the workdir is
    # still set up (the Claude one-shot needs a cwd) but ./reference/
    # is absent — the prompt embeds the paper text directly.
    workdir = Path(tempfile.mkdtemp(prefix="outrider-fidelity-"))
    log.info(f"  → workdir: {workdir}")

    audit_anchor: str  # "reference" | "paper"
    reference_confidence = "unknown"
    reference_signals: dict = {}
    if reference_url:
        result["reference_url"] = reference_url
        ok, ref_dir, err = _clone_reference_repo(reference_url, workdir)
        if not ok:
            result["status"] = "fidelity_failed_clone"
            result["error"] = err
            log.error(f"  ✗ {err}")
            return result

        # Reference-impl sanity check (same tiering as the pre-PR gate).
        # Extracts a paper title from the PR title for fallback matching;
        # the arxiv ID match is the strong signal.
        reference_confidence, reference_signals = _score_reference_confidence(
            ref_dir, arxiv_id, title,
        )
        result["reference_confidence"] = reference_confidence
        result["reference_signals"] = reference_signals
        log.info(
            f"  → reference-impl confidence: {reference_confidence} "
            f"(readme_arxiv={reference_signals.get('readme_arxiv_id', False)}, "
            f"citation_arxiv={reference_signals.get('citation_arxiv_id', False)}, "
            f"code_arxiv={reference_signals.get('code_arxiv_id', False)}, "
            f"title_tokens={reference_signals.get('title_tokens_matched', 0)})"
        )
        if reference_confidence == "low":
            # Reference is a catalog mislink — skip the Claude one-shot and
            # transition labels through so downstream chain phases proceed.
            # Post an advisory PR comment so reviewers know the audit was
            # skipped for a legitimate data-quality reason, not a fabrication.
            result["status"] = "fidelity_skipped_reference_mismatch"
            _add_pr_label(target, pr_number, FIDELITY_LABEL_DONE)
            _remove_pr_label(target, pr_number, FIDELITY_LABEL_TRIGGER)
            _append_to_step_summary(
                "## Fidelity audit — skipped (reference-impl mismatch)\n\n"
                f"The catalog reference (`{reference_url}`) shows no evidence "
                f"of implementing arxiv:{arxiv_id} — no arxiv ID in the README, "
                f"CITATION file, or top code files, and paper-title tokens "
                f"below the medium-confidence threshold. Comparing the PR "
                f"diff against this reference would produce false-positive "
                f"fabrication flags. Skipping the audit; the PR is preserved "
                f"as-is for reviewer inspection.\n\n"
                f"*Catalog data-quality issue — the arxiv-id → github-url "
                f"mapping should be corrected upstream.*"
            )
            log.info(
                f"  ✓ {result['status']}: reference {reference_url} does not "
                f"self-identify with arxiv:{arxiv_id}; audit skipped, PR "
                f"preserved, labels transitioned"
            )
            return result

        try:
            pr_diff = _fetch_pr_diff(target, pr_number)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            result["status"] = "fidelity_failed_claude"
            result["error"] = f"could not fetch PR diff: {e}"
            return result
        prompt = _build_fidelity_audit_prompt(
            pr_title=title,
            pr_body=body,
            pr_diff=pr_diff,
            arxiv_id=arxiv_id,
            reference_url=reference_url,
            reference_root=ref_dir,
        )
        audit_anchor = "reference"
        log.info(
            f"  → reference-anchored audit ({reference_url})"
            + ("; advisory-only (medium confidence)"
               if reference_confidence == "medium" else "")
        )
    else:
        # Phase A degraded mode: no public reference impl available, but
        # we may still have the paper's title + abstract from arxiv. The
        # audit compares the diff against the paper's described method
        # at the abstract level — lower precision than a code-anchored
        # comparison, but materially better than skipping the entire
        # chain (B + C still run after this).
        paper_text = _fetch_arxiv_abstract_text(arxiv_id) if arxiv_id else ""
        if not paper_text:
            result["status"] = "fidelity_skipped_no_reference"
            result["error"] = (
                "no reference URL extracted from PR body and no arxiv "
                "abstract available for paper-anchored audit"
            )
            log.info(
                f"  ✗ no reference URL in PR #{pr_number} body and no "
                f"arxiv abstract for {arxiv_id!r}; skipping"
            )
            return result
        try:
            pr_diff = _fetch_pr_diff(target, pr_number)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            result["status"] = "fidelity_failed_claude"
            result["error"] = f"could not fetch PR diff: {e}"
            return result
        prompt = _build_fidelity_audit_prompt_paper_anchored(
            pr_title=title,
            pr_body=body,
            pr_diff=pr_diff,
            arxiv_id=arxiv_id,
            paper_text=paper_text,
        )
        audit_anchor = "paper"
        log.info(f"  → paper-anchored audit (arxiv:{arxiv_id}, no reference impl)")

    log.info(f"  → Claude one-shot audit (timeout={target.claude_timeout_s}s)")
    ok, raw = _run_claude_oneshot(workdir, prompt, target.claude_timeout_s, max_turns=20)
    if not ok:
        result["status"] = "fidelity_failed_claude"
        result["error"] = f"Claude returned non-zero: {raw[-500:]}"
        return result

    matrix = _extract_json_object(raw)
    if not matrix or "items" not in matrix:
        result["status"] = "fidelity_failed_claude"
        result["error"] = f"Claude returned unparseable JSON: {raw[-500:]}"
        return result

    coverage_section = _render_coverage_matrix(matrix, audit_anchor=audit_anchor)
    # Per-phase output goes to the action run-summary panel rather than the
    # PR body; the body is reserved for content the convention pass aligns
    # to upstream conventions.
    _append_to_step_summary(coverage_section)

    raw_needs_judgment = bool(matrix.get("needs_judgment", False))
    # Advisory-mode: under medium-confidence reference the audit ran but
    # its flag count is not authoritative — reference doesn't self-identify
    # with the paper strongly enough to trust judgment as ground truth.
    # Surface the coverage matrix + flag count in the run summary but
    # don't apply the needs-judgment label that would gate reviewer flow.
    advisory_only = (reference_confidence == "medium")
    effective_needs_judgment = raw_needs_judgment and not advisory_only

    if effective_needs_judgment:
        _add_pr_label(target, pr_number, FIDELITY_LABEL_NEEDS_JUDGMENT)
        result["status"] = (
            "fidelity_audited_paper_anchored_needs_judgment"
            if audit_anchor == "paper"
            else "fidelity_audited_needs_judgment"
        )
    elif advisory_only and raw_needs_judgment:
        result["status"] = "fidelity_audited_advisory"
        _append_to_step_summary(
            "\n\n*Audit above is **advisory-only** — reference-impl "
            f"self-identifies only weakly with arxiv:{arxiv_id} "
            "(title-token match; no explicit arxiv ID in README/CITATION/"
            "code). Flags surfaced for reviewer awareness but the "
            "`needs-judgment` label is withheld to avoid blocking the "
            "chain on a probably-mislinked reference.*"
        )
    else:
        result["status"] = (
            "fidelity_audited_paper_anchored"
            if audit_anchor == "paper"
            else "fidelity_audited"
        )
    result["audit_anchor"] = audit_anchor
    result["advisory_only"] = advisory_only

    # Transition the trigger label → fidelity-done so the convention pass
    # workflow picks it up.
    _add_pr_label(target, pr_number, FIDELITY_LABEL_DONE)
    _remove_pr_label(target, pr_number, FIDELITY_LABEL_TRIGGER)

    result["items_count"] = len(matrix.get("items", []))
    result["needs_judgment"] = needs_judgment
    result["coverage_summary"] = matrix.get("summary", "")
    result["pr_url"] = pr.get("html_url", "")
    log.info(
        f"  ✓ {result['status']}: {result['items_count']} items, "
        f"needs_judgment={needs_judgment}"
    )
    return result


# ─── Refinement-pass: convention alignment ─────────────────────────────────
#
# Triggered by the `outrider:fidelity-done` label. Extracts target-repo
# conventions from recent merged PRs, identifies misalignments in the draft,
# runs an agentic Claude Code patching session bounded to convention-shaped
# changes (PR body shape, file placement, test idioms, docs entries —
# explicitly NOT algorithm changes), force-pushes the result with the bot's
# verified attribution, and posts an evidence comment listing the patterns
# extracted + the PRs they were derived from.
#
# Input (workflow surface):
#   INPUT_PR_NUMBER — PR number to refine
#
# Output:
#   - PR head branch force-pushed with convention-aligned commit(s)
#   - PR comment posted listing extracted patterns + evidence PRs
#   - Label transition: outrider:fidelity-done → outrider:convention-done
#   - status: convention_aligned / convention_skipped_* / convention_failed_*

CONVENTION_LABEL_TRIGGER = "outrider:fidelity-done"
CONVENTION_LABEL_DONE = "outrider:convention-done"
CONVENTION_MAX_REFERENCE_PRS = 30


# Paths the convention pass is allowed to modify. Anything else the agent
# stages is reverted before commit so the patch is bounded to actually-
# convention-shaped changes (PR-aligned tests, docs, the README index, etc.).
# Order matters — the deny list is consulted first.
_CONVENTION_DENY_PATTERNS = (
    f"{BUNDLE_DIR_NAME}/",  # our own scaffolding bundle
    "version.txt",
    "VERSION",
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "setup.py",
    "pyproject.toml",
    "MANIFEST.in",
    ".github/",
    "Dockerfile",
)
_CONVENTION_DENY_RE = re.compile(
    # Language-variant READMEs (README_ja.md, README_zh-CN.md, etc.). Plain
    # README.md is allowed — that's where the project index lives.
    r"(^|/)README_[a-zA-Z][a-zA-Z0-9_\-]*\.md$",
    re.IGNORECASE,
)
_CONVENTION_ALLOW_PATTERNS = (
    ".py",            # any python file (where most convention patches land)
    ".md",            # docs / paper-index / README.md
    "tests/",         # test idioms
    "docs/",          # documentation conventions
    ".yml", ".yaml",  # CI / config sometimes carries the convention
    ".toml",          # config (but pyproject.toml is in the deny list above)
)

# Path-prefix → allowed-extension table for repos that ship example scripts
# as a documented part of their feature-rollout convention (e.g. OpenRLHF's
# examples/scripts/train_*.sh shell wrappers). Each entry says "files under
# this prefix may carry this extension even if the extension wouldn't be
# allow-listed globally." Generic shell files outside these prefixes stay
# out of scope.
_CONVENTION_PREFIX_ALLOW = (
    ("examples/scripts/", (".sh", ".py")),
    ("examples/python/", (".py", ".md")),
)


def _partition_convention_staged_paths(
    staged: list[str],
) -> tuple[list[str], list[str]]:
    """Split staged file paths into (in_scope, out_of_scope) by the
    convention-pass allow/deny rules. Deny list is consulted first; any
    path matching a deny pattern is out-of-scope regardless of whether
    it would otherwise be allowed."""
    in_scope: list[str] = []
    out_of_scope: list[str] = []
    for path in staged:
        if any(path.startswith(p) or f"/{p}" in f"/{path}/" for p in _CONVENTION_DENY_PATTERNS):
            out_of_scope.append(path)
            continue
        if _CONVENTION_DENY_RE.search(path):
            out_of_scope.append(path)
            continue
        # Prefix-scoped allow rules (e.g. examples/scripts/*.sh).
        prefix_match = False
        for prefix, exts in _CONVENTION_PREFIX_ALLOW:
            if path.startswith(prefix) and any(path.endswith(ext) for ext in exts):
                in_scope.append(path)
                prefix_match = True
                break
        if prefix_match:
            continue
        if any(path.endswith(p) or p in path for p in _CONVENTION_ALLOW_PATTERNS):
            in_scope.append(path)
        else:
            out_of_scope.append(path)
    return in_scope, out_of_scope


def _resolve_upstream_for_conventions(target: Target) -> str:
    """Return the repo whose merged-PR corpus encodes the conventions
    we should align to. For forks this is the parent; otherwise the
    target itself."""
    try:
        meta = gh_api("GET", f"/repos/{target.repo}")
    except RuntimeError:
        return target.repo
    parent = (meta.get("parent") or {}).get("full_name", "")
    if parent and meta.get("fork"):
        return parent
    return target.repo


def _fetch_recent_merged_prs(repo: str, limit: int) -> list[dict]:
    """Return the most-recently-merged PRs on ``repo``, newest first.

    Returns a list of dicts with ``number``, ``title``, ``body``,
    ``merged_at``, ``additions``, ``deletions``, ``files`` (list of
    ``{filename, status, additions, deletions}``). ``files`` is capped
    at 40 entries per PR to bound the convention-extraction prompt size.
    """
    # GitHub's list API doesn't sort by merged_at; we use the search
    # API which does, scoped to merged state on the repo.
    q = f"repo:{repo} is:pr is:merged"
    search = gh_api("GET", f"/search/issues?q={urllib.parse.quote(q)}&sort=updated&order=desc&per_page={limit}")
    prs: list[dict] = []
    for item in search.get("items", [])[:limit]:
        num = item["number"]
        try:
            pr = gh_api("GET", f"/repos/{repo}/pulls/{num}")
        except RuntimeError:
            continue
        try:
            files = gh_api("GET", f"/repos/{repo}/pulls/{num}/files?per_page=40")
        except RuntimeError:
            files = []
        prs.append({
            "number": num,
            "title": pr.get("title", ""),
            "body": (pr.get("body") or "")[:6000],
            "merged_at": pr.get("merged_at", ""),
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "files": [
                {"filename": f.get("filename", ""),
                 "status": f.get("status", ""),
                 "additions": f.get("additions", 0),
                 "deletions": f.get("deletions", 0)}
                for f in files[:40]
            ],
        })
    return prs


def _build_convention_extraction_prompt(
    upstream_repo: str, recent_prs: list[dict]
) -> str:
    """Compose the prompt for the convention-extraction Claude one-shot."""
    pr_blocks = []
    for pr in recent_prs:
        files_summary = "\n".join(
            f"  - {f['filename']}  ({f['status']}, +{f['additions']}/-{f['deletions']})"
            for f in pr["files"]
        )
        pr_blocks.append(
            f"### #{pr['number']} — {pr['title']}\n"
            f"merged: {pr['merged_at'][:10]}  +{pr['additions']}/-{pr['deletions']}\n\n"
            f"**Body:**\n```\n{pr['body']}\n```\n\n"
            f"**Files changed:**\n{files_summary}\n"
        )
    pr_corpus = "\n\n".join(pr_blocks)

    return f"""You are extracting contributor conventions from a repo's recently-merged PRs.

Target repo: {upstream_repo}
Corpus: {len(recent_prs)} most-recently-merged PRs.

# Recent merged PRs

{pr_corpus}

# Task

Identify the conventions that a new contributor's PR should match for it to
read as natively-shaped to this repo. Focus on patterns the PR-corpus
demonstrates repeatedly (not one-off choices). For each pattern, cite the PR
numbers it's derived from.

Categories to inspect (skip any that don't have clear patterns in this
corpus):

- **pr_body_template**: required sections in the PR body (e.g. "What does
  this PR do?", "Before submitting", "Who can review?", "AI writing
  disclosure", etc.) and their typical content.
- **code_placement**: when a new feature/method/option is added, where does
  it land? Specific directories, files, classes, dispatch patterns
  (e.g. `loss_type` dispatch in GRPOTrainer, `register_X` decorators,
  config dataclass field placement).
- **test_idiom**: how are new options tested? Parametrize patterns, test
  file naming, test class structure, fixture conventions.
- **docs_idiom**: where do new features get documented? Top-level docs
  index files, README mentions, paper-index entries, docstring shape.
- **ai_disclosure**: does the corpus show explicit AI-usage disclosure
  blocks? If yes, what's the canonical shape?

# Output

Return ONLY valid JSON (no prose before or after):

{{
  "patterns": [
    {{
      "category": "<one of: pr_body_template | code_placement | test_idiom | docs_idiom | ai_disclosure>",
      "description": "<one-to-three sentences describing the pattern>",
      "evidence_pr_numbers": [<int>, ...],
      "canonical_example": "<a short code/markdown snippet illustrating the pattern, or null>"
    }},
    ...
  ]
}}

Limit yourself to the 5 most-load-bearing patterns. Skip patterns that only
appear in 1-2 PRs.
"""


def _build_misalignment_prompt(
    pr_title: str, pr_body: str, pr_diff: str, patterns: dict
) -> str:
    """Compose the prompt that takes (draft PR, extracted conventions)
    and returns a list of actionable misalignment fixes."""
    pattern_lines = []
    for p in patterns.get("patterns", []):
        pattern_lines.append(
            f"- **{p.get('category')}**: {p.get('description')}\n"
            f"  Evidence: PRs {p.get('evidence_pr_numbers', [])}\n"
            f"  Example:\n```\n{(p.get('canonical_example') or '')[:1500]}\n```"
        )
    patterns_block = "\n\n".join(pattern_lines)

    diff_excerpt = pr_diff if len(pr_diff) <= 40_000 else (
        pr_diff[:40_000] + f"\n... [truncated; total {len(pr_diff)} chars] ...\n"
    )

    return f"""You are reviewing a draft PR for alignment with extracted target-repo conventions.

# Target-repo conventions

{patterns_block}

# Draft PR

**Title**: {pr_title}

## PR body

```
{(pr_body or "")[:4000]}
```

## PR diff

```
{diff_excerpt}
```

# Task

For each convention above, decide whether the draft is *aligned*, *misaligned*,
or *not applicable*. For misaligned items, propose a concrete patch action
(file to edit, what to add/change, expected outcome). Limit yourself to
convention-shape patches — **DO NOT propose algorithmic or numerical-value
changes**. Algorithm fidelity is a separate concern.

# Output

Return ONLY valid JSON (no prose):

{{
  "summary": "<one-sentence overall verdict>",
  "actions": [
    {{
      "category": "<which convention category>",
      "verdict": "aligned | misaligned | n/a",
      "patch_action": "<concrete instruction for the patching agent, or null if aligned>",
      "files_likely_touched": ["<path>", ...]
    }},
    ...
  ]
}}
"""


def _render_convention_evidence_comment(
    upstream_repo: str,
    patterns: dict,
    misalignments: dict,
    applied_patches_summary: str,
    pr_body_updated: bool = False,
    pr_body_rationale: str = "",
    pr_body_skip_reason: str = "",
) -> str:
    """Produce the PR comment that surfaces what conventions were extracted
    + which were applied, with evidence PR links so a reviewer can verify."""
    lines = ["## Convention pass", ""]
    lines.append(f"_{misalignments.get('summary', '(no summary)')}_")
    lines.append("")
    lines.append(f"Conventions extracted from `{upstream_repo}`'s recent merged PRs:")
    lines.append("")
    for p in patterns.get("patterns", []):
        cat = p.get("category", "?")
        desc = p.get("description", "")
        evidence = p.get("evidence_pr_numbers", [])
        evidence_md = ", ".join(
            f"[#{n}](https://github.com/{upstream_repo}/pull/{n})" for n in evidence[:5]
        )
        lines.append(f"- **{cat}** — {desc}")
        if evidence_md:
            lines.append(f"  _Evidence:_ {evidence_md}")
    lines.append("")
    lines.append("### Patches applied")
    lines.append("")
    body_line = ""
    if pr_body_updated:
        body_line = f"- **PR body**: rewritten to match `pr_body_template` convention ({pr_body_rationale})."
    elif pr_body_skip_reason:
        body_line = f"- **PR body**: rewrite skipped — {pr_body_skip_reason}."
    if body_line:
        lines.append(body_line)
    if applied_patches_summary.strip():
        lines.append(applied_patches_summary)
    elif not body_line:
        lines.append("_No code patches applied (draft already aligned with extracted conventions, or no actionable convention-shape diffs identified)._")
    lines.append("")
    lines.append(
        f"_Generated by [Outrider convention pass]"
        f"({CANONICAL_ATTRIBUTION_URL}) at {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}._"
    )
    return "\n".join(lines)


def _build_pr_body_rewrite_prompt(
    pr_title: str, current_body: str, pr_body_pattern: dict, all_patterns: dict
) -> str:
    """Compose the prompt for a focused Claude one-shot that rewrites
    the PR body to match the extracted pr_body_template convention.

    The rewrite must preserve the existing ``## Coverage`` section
    verbatim (Phase A's audit output stays intact), keep the same factual
    content, and only restructure the body to use the headings + sections
    the convention demands.
    """
    other_patterns = "\n".join(
        f"- **{p.get('category')}**: {p.get('description', '')[:240]}"
        for p in all_patterns.get("patterns", [])
        if p.get("category") != "pr_body_template"
    )
    canonical = pr_body_pattern.get("canonical_example") or ""
    description = pr_body_pattern.get("description", "")

    return f"""You are rewriting a draft PR's body to match the target repo's PR body conventions.

# PR being refined

**Title**: {pr_title}

# Current PR body

```
{current_body[:8000]}
```

# Convention to apply

{description}

**Canonical example:**

```
{canonical[:3000]}
```

# Other conventions (for context, do not address here)

{other_patterns}

# Task

The canonical example is the **primary structure** of the rewritten body —
a maintainer reading the new body should see the same headings, in the
same order, as the canonical example. Outrider's draft preamble (paper
provenance, selection reasoning, suggested experiment, license metadata,
footer promos) is **scaffolding**, not first-class content: it should be
folded into the canonical sections where it maps, and relegated to a
single collapsed block where it doesn't.

Produce an updated PR body that:

1. **Treats the canonical example as the primary structure.** The
   rewritten body's first-class sections are the canonical example's
   sections, in its order. If the canonical has ``## Description`` /
   ``## Usage`` / ``## Checklist``, those become the body's primary
   reading surface — not appended below a stack of Outrider sections.
2. **Folds the current body's facts INTO the canonical sections** rather
   than duplicating them. If the current body has an Outrider-style
   "What this PR delivers" or delivery-summary section AND the canonical
   has ``## Description``, MERGE the delivery content into ``## Description``
   — do not keep both. Same for usage snippets, test results, etc.: the
   canonical's section owns the content; the Outrider version is
   absorbed, not stacked.
3. **Relegates Outrider-only context to ONE collapsed block.** Content
   from the current body that doesn't map to any canonical section
   (paper-provenance blockquote, "Why this paper for this team", "Why
   this candidate", license-class metadata, "Suggested experiment",
   self-summary paragraphs, footer promos like "Want eval-on-every-PR?")
   goes inside a single ``<details><summary>Discovery context</summary>``
   block placed at the bottom of the body, ABOVE any ``## Coverage``
   section. Do not interleave these as ``##`` headings in the primary
   surface. If there is no such content, omit the block.
4. **Compresses attribution to one italicized line** placed immediately
   above the Discovery-context block (or above Coverage if the block was
   omitted): ``_Drafted by [Outrider](https://github.com/remyxai/outrider)
   — paper: [arXiv:<id>](<arxiv-url>)._``. Do not preserve the original
   blockquote attribution stack.
5. **Preserves the existing ``## Coverage`` section verbatim** if
   present — that section is the fidelity-audit output and must not be
   modified or moved. Keep it at the very end of the body.
6. **Preserves the paper's reference repo URL** if present. The original
   body may carry a ``- **Code**: https://github.com/...`` line (or
   similar ``Reference:`` / ``Project page:`` / ``Official implementation:``
   marker) under a License-or-availability section. **Do not silently
   drop the URL.** Place it inside the Discovery-context details block
   as ``Reference: https://github.com/<owner>/<repo>``; if the block was
   omitted, append that single line at the bottom of the body above any
   ``## Coverage`` section. The fidelity audit (Phase A) reads this URL
   to clone the paper's reference; without it, the audit silently skips
   on re-runs.
7. **Reuses substantive facts** from the current body — code locations,
   behavior callouts, intentional-out-of-scope notes, test results — do
   not invent new claims and do not drop these. The Outrider framing
   ("Drafted by an autonomous discovery loop", confidence tier,
   research-interest name, "Implementation by" lines, italic self-summary)
   is framing, not substance; it does not need to be preserved beyond
   the one-line attribution and the Discovery-context block.
8. **Does not add boilerplate** that isn't in the canonical example
   beyond the one-line attribution and the Discovery-context block (no
   extra "Generated by..." footers, no horizontal rules unless the
   canonical example shows them).

# Output

Return ONLY valid JSON (no prose before or after):

{{
  "updated_body": "<the full new PR body in markdown>",
  "rationale": "<one-sentence note on what structurally changed>"
}}
"""


def _apply_pr_body_convention_update(
    target: Target,
    pr_number: int,
    current_body: str,
    pr_body_pattern: dict,
    all_patterns: dict,
    pr_title: str,
    timeout_s: int,
    workdir: Path,
) -> tuple[bool, str, str]:
    """Run the PR-body rewrite one-shot and PATCH the PR body.

    Returns ``(ok, rationale, error)``. On non-fatal failure (Claude
    failed, JSON unparseable, PATCH rejected) returns ``ok=False`` with
    an error message — the caller decides whether to continue with the
    file-patch step.
    """
    prompt = _build_pr_body_rewrite_prompt(
        pr_title, current_body, pr_body_pattern, all_patterns
    )
    ok, raw = _run_claude_oneshot(workdir, prompt, timeout_s, max_turns=4)
    if not ok:
        return False, "", f"body-rewrite Claude call failed: {raw[-300:]}"
    rewrite = _extract_json_object(raw)
    if not rewrite or "updated_body" not in rewrite:
        return False, "", f"body-rewrite returned unparseable JSON: {raw[-300:]}"
    new_body = rewrite["updated_body"]
    # Sanity check: if a Coverage section existed in the input, it must
    # still exist verbatim in the output. Drop the rewrite if Phase A's
    # audit was clobbered.
    if FIDELITY_COVERAGE_SECTION_HEADER in current_body:
        if FIDELITY_COVERAGE_SECTION_HEADER not in new_body:
            return False, "", "body-rewrite dropped the ## Coverage section"
    # Sanity check: if the input had a non-self-owner github URL the
    # fidelity audit could anchor against, the rewrite must keep at least
    # one such URL reachable. Stripping the License-or-availability
    # section without preserving the reference URL would leave Phase A
    # with no anchor on re-runs (the normal chain runs Phase A before
    # this rewrite, but re-running Phase A afterwards must still work).
    _, current_ref = _extract_reference_url_from_pr_body(current_body, target.repo)
    if current_ref:
        _, new_ref = _extract_reference_url_from_pr_body(new_body, target.repo)
        if not new_ref:
            return False, "", (
                f"body-rewrite dropped the reference URL ({current_ref}); "
                f"Phase A would lose the fidelity anchor"
            )
    # Sanity check: if the input carried an arXiv link (paper provenance),
    # the rewrite must keep one. The relegate-to-details rule pushes
    # provenance into a collapsed block but does not drop it — a rewrite
    # that strips the arXiv anchor entirely is treating provenance as
    # boilerplate, which we reject.
    current_arxiv = _ARXIV_URL_RE.search(current_body)
    if current_arxiv:
        if not _ARXIV_URL_RE.search(new_body):
            return False, "", (
                f"body-rewrite dropped the arXiv link ({current_arxiv.group(0)}); "
                f"paper provenance must remain reachable"
            )
    try:
        _update_pr_body(target, pr_number, new_body)
    except RuntimeError as e:
        return False, "", f"PR body PATCH failed: {e}"
    return True, rewrite.get("rationale", "structurally aligned to convention"), ""


def _build_convention_patch_invocation(
    patterns: dict, misalignments: dict, pr_title: str
) -> str:
    """Compose the INVOCATION.md content that drives the Claude Code
    patching session.

    The patching session edits files in the cloned PR head branch (cwd =
    workdir). The agent reads the misalignment list, makes the smallest
    edits needed to align with the extracted conventions, and STAGES the
    changes for commit. The runner handles commit + push.
    """
    actions = [a for a in misalignments.get("actions", []) if a.get("verdict") == "misaligned"]
    if not actions:
        return ""

    action_lines = []
    for a in actions:
        files = ", ".join(a.get("files_likely_touched", []) or [])
        action_lines.append(
            f"- **{a.get('category')}**: {a.get('patch_action', '')}\n"
            f"  Files to consider: {files or '(any relevant)'}\n"
        )
    actions_block = "\n".join(action_lines)

    pattern_lines = []
    for p in patterns.get("patterns", []):
        if p.get("canonical_example"):
            pattern_lines.append(
                f"### {p.get('category')}\n\n{p.get('description', '')}\n\n"
                f"Canonical example:\n```\n{p['canonical_example'][:2000]}\n```"
            )
    patterns_block = "\n\n".join(pattern_lines)

    return f"""# Convention-alignment patches

PR under refinement: "{pr_title}"

You are running in the cloned PR head branch (cwd = the workdir). Your task
is to apply small, convention-shape patches to align this draft with the
target repo's contributor conventions. Use the Read, Edit, Glob, and Grep
tools as needed.

## Scope guard — read carefully

- **DO** patch: code files (`*.py`) where the convention is about *placement
  or naming*, the project's main `README.md` (only if the convention is a
  documented-feature pattern), test files in `tests/`, documentation pages
  in `docs/`, paper-index entries.
- **DO NOT** touch any of:
  - `.remyx-recommendation/` — our own scaffolding, not part of the PR's
    surface; leave alone even if you see references to it.
  - `version.txt`, `VERSION`, `package-lock.json`, `yarn.lock`, `*.lock`,
    `setup.py`, `pyproject.toml`, `MANIFEST.in`, `Dockerfile` — release /
    packaging surfaces, not contributor conventions.
  - `README_<lang>.md` (Japanese / Chinese / etc. language variants) —
    they're maintained by native-language maintainers and shouldn't be
    updated by a programmatic patcher. The main `README.md` may be edited.
  - `.github/workflows/` — CI is out of this pass's scope.
- **DO NOT** patch: algorithm logic, numerical constants, default values,
  control flow of math-bearing functions, what's multiplied by what. Those
  are algorithm-fidelity concerns and are out of this pass's scope.

If a misalignment is ambiguous between "convention" and "algorithm", skip it
and leave a note in your final summary. **It is strictly better to skip an
unclear patch than to make one that touches paper-anchored math.**

## Extracted conventions

{patterns_block}

## Misalignments to address

{actions_block}

## After patching

Stage all changes with ``git add -A``. Do not commit — the orchestrator
handles the commit and force-push with the bot's verified attribution.

Print a brief markdown summary of what you actually changed (file paths +
one-line per file), bullet-listed, in your final response. The runner uses
this summary verbatim in the PR comment that explains the patches.
"""


def run_convention_pass(target: Target) -> dict:
    """Phase B — diff a remyx-ai[bot] PR against the target repo's recent
    merged-PR conventions, patch convention-shape misalignments via an
    agentic session, force-push, and transition labels.

    Status values:
        convention_aligned                  — patches applied (or no
                                              misalignments found)
        convention_skipped_no_pr            — INPUT_PR_NUMBER empty
        convention_skipped_not_bot          — PR not authored by remyx-ai[bot]
        convention_skipped_no_corpus        — upstream has too few merged PRs
                                              to extract patterns from
        convention_failed_extraction        — pattern extraction Claude call
                                              failed
        convention_failed_misalignment      — misalignment Claude call failed
        convention_failed_patch             — Claude Code patch session failed
        convention_failed_push              — git push step failed
    """
    result: dict = {"repo": target.repo, "mode": "convention", "status": "unknown"}

    pr_number_raw = os.environ.get("INPUT_PR_NUMBER", "").strip()
    if not pr_number_raw:
        result["status"] = "convention_skipped_no_pr"
        log.info("  ✗ convention mode invoked without INPUT_PR_NUMBER")
        return result
    try:
        pr_number = int(pr_number_raw)
    except ValueError:
        result["status"] = "convention_skipped_no_pr"
        return result
    result["pr_number"] = pr_number
    log.info(f"  → convention pass on {target.repo}#{pr_number}")

    try:
        pr = _fetch_pr_metadata(target, pr_number)
    except RuntimeError as e:
        result["status"] = "convention_failed_extraction"
        result["error"] = f"could not fetch PR: {e}"
        return result

    author = (pr.get("user") or {}).get("login", "")
    if author != "remyx-ai[bot]":
        result["status"] = "convention_skipped_not_bot"
        result["error"] = f"PR author is {author!r}, not remyx-ai[bot]"
        return result

    upstream_repo = _resolve_upstream_for_conventions(target)
    log.info(f"  → resolving conventions from {upstream_repo}")
    result["upstream_repo"] = upstream_repo

    recent_prs = _fetch_recent_merged_prs(upstream_repo, CONVENTION_MAX_REFERENCE_PRS)
    if len(recent_prs) < 5:
        result["status"] = "convention_skipped_no_corpus"
        result["error"] = f"only {len(recent_prs)} merged PRs found on {upstream_repo}"
        return result
    log.info(f"  → corpus: {len(recent_prs)} recent merged PRs")

    workdir = Path(tempfile.mkdtemp(prefix="outrider-convention-"))
    log.info(f"  → workdir: {workdir}")

    # Pattern extraction (one-shot)
    log.info("  → extracting conventions (one-shot)")
    extraction_prompt = _build_convention_extraction_prompt(upstream_repo, recent_prs)
    ok, raw = _run_claude_oneshot(workdir, extraction_prompt, target.claude_timeout_s, max_turns=8)
    if not ok:
        result["status"] = "convention_failed_extraction"
        result["error"] = f"extraction call failed: {raw[-500:]}"
        return result
    patterns = _extract_json_object(raw)
    if not patterns or "patterns" not in patterns:
        result["status"] = "convention_failed_extraction"
        result["error"] = f"extraction returned unparseable JSON: {raw[-500:]}"
        return result
    result["patterns_count"] = len(patterns.get("patterns", []))
    log.info(f"  → extracted {result['patterns_count']} patterns")

    # Fetch draft PR diff for misalignment analysis
    try:
        pr_diff = _fetch_pr_diff(target, pr_number)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        result["status"] = "convention_failed_misalignment"
        result["error"] = f"could not fetch PR diff: {e}"
        return result

    # Misalignment analysis (one-shot)
    log.info("  → identifying misalignments (one-shot)")
    misalignment_prompt = _build_misalignment_prompt(
        pr.get("title", ""), pr.get("body", "") or "", pr_diff, patterns,
    )
    ok, raw = _run_claude_oneshot(workdir, misalignment_prompt, target.claude_timeout_s, max_turns=8)
    if not ok:
        result["status"] = "convention_failed_misalignment"
        result["error"] = f"misalignment call failed: {raw[-500:]}"
        return result
    misalignments = _extract_json_object(raw)
    if not misalignments or "actions" not in misalignments:
        result["status"] = "convention_failed_misalignment"
        result["error"] = f"misalignment returned unparseable JSON: {raw[-500:]}"
        return result
    actionable = [a for a in misalignments.get("actions", []) if a.get("verdict") == "misaligned"]
    result["actionable_count"] = len(actionable)
    log.info(f"  → {len(actionable)} actionable misalignments")

    patches_summary = ""

    # PR-body rewrites are handled separately from file patches: the PR
    # body lives in GitHub metadata, not in the cloned repo, so the
    # agentic patch session can't touch it. We do a focused one-shot +
    # gh PATCH here and remove the pr_body_template action from the
    # list passed to the agent.
    pr_body_action = next(
        (a for a in actionable if a.get("category") == "pr_body_template"), None,
    )
    pr_body_pattern = next(
        (p for p in patterns.get("patterns", []) if p.get("category") == "pr_body_template"),
        None,
    )
    if pr_body_action and pr_body_pattern:
        log.info("  → rewriting PR body to match pr_body_template convention")
        ok, rationale, err = _apply_pr_body_convention_update(
            target=target,
            pr_number=pr_number,
            current_body=pr.get("body") or "",
            pr_body_pattern=pr_body_pattern,
            all_patterns=patterns,
            pr_title=pr.get("title", ""),
            timeout_s=target.claude_timeout_s,
            workdir=workdir,
        )
        if ok:
            log.info(f"  ✓ PR body updated: {rationale}")
            result["pr_body_updated"] = True
            result["pr_body_rationale"] = rationale
            # Drop the PR-body action from the agent's queue so it doesn't
            # try to also restructure code in pursuit of the same fix.
            actionable = [a for a in actionable if a is not pr_body_action]
        else:
            log.warning(f"  ! PR body rewrite skipped (non-fatal): {err}")
            result["pr_body_updated"] = False
            result["pr_body_skip_reason"] = err
    else:
        result["pr_body_updated"] = False

    if actionable:
        # Clone the PR head branch (we need to patch + push to it).
        head_repo = (pr.get("head") or {}).get("repo", {}).get("full_name") or target.repo
        head_ref = (pr.get("head") or {}).get("ref", "")
        if not head_ref:
            result["status"] = "convention_failed_patch"
            result["error"] = "could not resolve PR head branch"
            return result

        log.info(f"  → cloning PR head {head_repo}:{head_ref}")
        clone_workdir = workdir / "draft"
        token = _github_token()
        clone_url = f"https://x-access-token:{token}@github.com/{head_repo}.git"
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", head_ref, clone_url, str(clone_workdir)],
                check=True, capture_output=True, timeout=300, env=env,
            )
        except subprocess.CalledProcessError as e:
            result["status"] = "convention_failed_patch"
            result["error"] = f"clone PR head failed: {e.stderr.decode()[:500]}"
            return result

        # Capture the parent SHA before any changes — this is the commit
        # the new bot-authored commit will descend from.
        parent_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, cwd=str(clone_workdir),
        ).stdout.strip()

        # Write the INVOCATION for Claude Code
        bundle_dir = clone_workdir / BUNDLE_DIR_NAME
        bundle_dir.mkdir(parents=True, exist_ok=True)
        invocation = _build_convention_patch_invocation(
            patterns, misalignments, pr.get("title", ""),
        )
        (bundle_dir / "INVOCATION.md").write_text(invocation)

        # Run the agentic patch loop
        log.info(f"  → invoking Claude Code patch session (timeout={target.claude_timeout_s}s)")
        ok, patch_output = invoke_claude_code(clone_workdir, timeout_s=target.claude_timeout_s)
        if not ok:
            result["status"] = "convention_failed_patch"
            result["error"] = f"patch session failed: {patch_output[-500:]}"
            return result
        patches_summary = patch_output

        # Inspect staged changes
        try:
            staged_raw = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                check=True, capture_output=True, text=True, cwd=str(clone_workdir),
            ).stdout.strip()
        except subprocess.CalledProcessError as e:
            result["status"] = "convention_failed_patch"
            result["error"] = f"git diff --cached failed: {e.stderr[:500]}"
            return result

        all_staged = staged_raw.splitlines() if staged_raw else []

        # Scope filter: only keep changes that match convention-shape paths.
        # Drop our own scaffolding (BUNDLE_DIR_NAME/), language-variant READMEs,
        # version files, lock files — anything the agent shouldn't have touched
        # but might have. Out-of-scope edits are reverted via git restore so
        # the commit only carries the legitimate convention patches.
        in_scope, out_of_scope = _partition_convention_staged_paths(all_staged)
        if out_of_scope:
            log.info(f"  → reverting {len(out_of_scope)} out-of-scope files: {out_of_scope[:5]}")
            try:
                subprocess.run(
                    ["git", "restore", "--staged", "--worktree", *out_of_scope],
                    check=True, capture_output=True, text=True, cwd=str(clone_workdir),
                )
            except subprocess.CalledProcessError as e:
                log.warning(f"  ! restore of out-of-scope paths failed (continuing): {e.stderr[:300]}")
        result["files_touched"] = in_scope
        result["files_dropped_out_of_scope"] = out_of_scope

        if in_scope:
            log.info(f"  → {len(in_scope)} files patched in-scope: {in_scope[:5]}")

            # Auto-fix lint-trivial issues the patching agent may have
            # introduced (unused imports, trailing whitespace, ruff format
            # gaps, etc.) before the commit lands. Non-trivial issues
            # (e.g. B002 double-unary-minus) are NOT auto-fixed and remain
            # for the test gate to surface — that's the right division of
            # labor: convention pass cleans up the cheap stuff, test gate
            # gates on what's genuinely actionable.
            py_in_scope = [p for p in in_scope if p.endswith(".py")]
            if py_in_scope:
                # Ensure ruff is available on the runner (not in action.yml's
                # baseline install set; cheap to install here).
                subprocess.run(
                    ["python", "-m", "pip", "install", "--quiet",
                     "--disable-pip-version-check", "ruff"],
                    capture_output=True, timeout=60,
                )
                log.info(
                    f"  → ruff --fix + ruff format on {len(py_in_scope)} patched .py files"
                )
                try:
                    subprocess.run(
                        ["ruff", "check", "--fix", "--exit-zero",
                         "--no-cache", *py_in_scope],
                        cwd=str(clone_workdir),
                        capture_output=True, text=True, timeout=120,
                    )
                    subprocess.run(
                        ["ruff", "format", "--no-cache", *py_in_scope],
                        cwd=str(clone_workdir),
                        capture_output=True, text=True, timeout=120,
                    )
                    # Re-stage anything ruff modified so the commit picks
                    # the fixes up. Idempotent if ruff made no changes.
                    subprocess.run(
                        ["git", "add", *py_in_scope],
                        check=True, cwd=str(clone_workdir),
                        capture_output=True, timeout=30,
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                    log.warning(f"  ! ruff auto-fix step failed (continuing): {e}")

            commit_msg = (
                f"chore: align PR with target-repo conventions\n\n"
                f"Convention-shape patches extracted from {upstream_repo}'s "
                f"recent merged PRs. Algorithm logic is left untouched. "
                f"Ruff auto-fixed lint-trivial issues on patched files."
            )

            # Local commit so HEAD's tree carries the new content. The
            # subsequent _recommit_via_api wraps that tree in an API-created
            # commit signed by the bot installation token (Verified badge).
            try:
                subprocess.run(
                    ["git", "commit", "-m", commit_msg],
                    check=True, capture_output=True, text=True, cwd=str(clone_workdir),
                )
            except subprocess.CalledProcessError as e:
                result["status"] = "convention_failed_push"
                result["error"] = f"local commit failed: {e.stderr[:500]}"
                return result

            # Push first so origin has the branch at our new commit, then
            # re-author via API to get the Verified badge.
            try:
                subprocess.run(
                    ["git", "push", "--force-with-lease", "origin", head_ref],
                    check=True, capture_output=True, text=True, cwd=str(clone_workdir),
                    env=env,
                )
            except subprocess.CalledProcessError as e:
                result["status"] = "convention_failed_push"
                result["error"] = f"git push failed: {e.stderr[:500]}"
                return result

            try:
                _recommit_via_api(
                    clone_workdir, head_repo, head_ref, commit_msg,
                    parent_sha=parent_sha,
                )
            except Exception as e:
                # _recommit_via_api degrades gracefully (logs warning, keeps
                # the pushed commit) so we only land here on a programming
                # error. Don't fail the whole run — the patches are pushed.
                log.warning(f"  ! API re-author failed (Verified badge missed): {e}")
        else:
            log.info("  → no in-scope staged changes after filtering; skipping push")

    # Evidence — to the action run-summary panel only. The PR body
    # itself is owned by the convention-driven rewrite performed earlier
    # in this run; appending an `## Convention pass` H2 here would
    # contradict the body's upstream-shape, which is the whole point of
    # this phase.
    log.info("  → writing convention-pass evidence to step summary")
    evidence_md = _render_convention_evidence_comment(
        upstream_repo, patterns, misalignments, patches_summary,
        pr_body_updated=bool(result.get("pr_body_updated")),
        pr_body_rationale=result.get("pr_body_rationale", ""),
        pr_body_skip_reason=result.get("pr_body_skip_reason", ""),
    )
    _append_to_step_summary(evidence_md)

    # Label transition
    _add_pr_label(target, pr_number, CONVENTION_LABEL_DONE)
    _remove_pr_label(target, pr_number, CONVENTION_LABEL_TRIGGER)

    result["status"] = "convention_aligned"
    result["summary"] = misalignments.get("summary", "")
    result["pr_url"] = pr.get("html_url", "")
    log.info(
        f"  ✓ {result['status']}: {result.get('actionable_count', 0)} actionable, "
        f"{len(result.get('files_touched', []))} files patched"
    )
    return result


# ─── Refinement-pass: Issue-route convention alignment ────────────────────
#
# Parallel to the PR-route convention pass above, but for Outrider Issues.
# When recommend mode routes to an Issue (issue_opened_preflight,
# issue_opened_self_review, issue_opened_substitution, etc.), this pass
# reads the target repo's .github/ISSUE_TEMPLATE/ files, picks the best-
# fitting template via a Claude one-shot, and rewrites the Issue body to
# match that template's shape — folding Outrider scaffolding into matching
# sections and relegating non-mapping content to a single <details> block.
# Issues have no diff, no draft state, and no Phase C, so this is a single
# Claude call + a PATCH to the Issue body — much cheaper than the PR-route
# convention pass.
#
# Input (workflow surface):
#   INPUT_ISSUE_NUMBER — Issue number to refine (set by the inline
#                        dispatcher or a standalone `mode: issue-convention`
#                        workflow_dispatch)
#
# Output:
#   - Issue body PATCHed with the rewritten content
#   - Label applied: outrider:issue-convention-done
#   - status: issue_convention_aligned / issue_convention_aligned_no_fitting_template
#             / issue_convention_skipped_no_templates / issue_convention_failed_*

ISSUE_CONVENTION_LABEL_DONE = "outrider:issue-convention-done"

# Template-kind heuristic keywords. The picker filters out templates whose
# kind is 'bug' or 'question' before passing the candidate set to Claude —
# Outrider's paper-pitch shape would be a bad fit for either, and rewriting
# as a bug report (lerobot's case in test forks) would actively harm the
# Issue's reception.
# Priority order: bug + new_model are checked before feature so that
# specific contributions get specific kinds. e.g. "New dataset request"
# matches `dataset` (new_model) before `request` (feature).
_TEMPLATE_KIND_KEYWORDS = {
    "bug": ("bug", "crash", "error", "broken", "regression", "fault"),
    "new_model": ("model", "dataset", "benchmark", "evaluation", "eval"),
    "feature": ("feature", "enhancement", "improvement", "request"),
    "question": ("question", "help", "discussion", "support", "how do"),
}

# Frontmatter / Issue-form metadata extractors. Both formats expose `name`
# and `description` as top-level keys; regex extraction is robust enough
# for the kind-classifier without pulling in a YAML dependency. Markdown
# frontmatter is delimited by `---` on its own line; Issue Forms have no
# such delimiter (the YAML is the whole file).
_TEMPLATE_NAME_RE = re.compile(r'^name:\s*(.+?)\s*$', re.MULTILINE | re.IGNORECASE)
_TEMPLATE_DESCRIPTION_RE = re.compile(r'^description:\s*(.+?)\s*$', re.MULTILINE | re.IGNORECASE)

# Filenames to skip when walking .github/ISSUE_TEMPLATE/. config.yml is
# GitHub's metadata file (controls "blank issue allowed" + contact links),
# not a template itself.
_ISSUE_TEMPLATE_SKIP_FILES = frozenset({"config.yml", "config.yaml"})

# Accepted template file extensions. mteb uses .yaml, ag2/dspy/lerobot use
# .yml, haystack uses .md — discovery walks all three.
_ISSUE_TEMPLATE_EXTENSIONS = (".md", ".yml", ".yaml")


def _fetch_issue_templates_from_repo(target_repo: str) -> list[dict]:
    """Return the parsed Issue templates from ``.github/ISSUE_TEMPLATE/``.

    Each element of the returned list is a dict shaped like:

        {
          "filename": "feature_request.yml",
          "name": "Feature Request",          # from frontmatter or YAML top
          "description": "Suggest a feature", # may be empty
          "kind": "feature",                  # heuristic classification
          "raw_content": "<full file body>",
        }

    Returns ``[]`` when the directory is missing or empty, or when every
    file is `config.yml`-style metadata. Never raises — the caller falls
    back to the no-templates path on an empty list.
    """
    try:
        listing = gh_api("GET", f"/repos/{target_repo}/contents/.github/ISSUE_TEMPLATE")
    except RuntimeError as e:
        # 404 → no template directory; that's a normal case, not an error.
        log.debug(f"  no ISSUE_TEMPLATE/ on {target_repo}: {e}")
        return []
    if not isinstance(listing, list):
        return []

    templates: list[dict] = []
    for entry in listing:
        if entry.get("type") != "file":
            continue
        filename = entry.get("name", "")
        if filename in _ISSUE_TEMPLATE_SKIP_FILES:
            continue
        if not filename.endswith(_ISSUE_TEMPLATE_EXTENSIONS):
            continue
        # Fetch the raw content. The Contents API returns base64 when the
        # file is small; for files > 1MB it returns a download_url instead
        # — Issue templates are always tiny so we only handle the base64
        # path here.
        content_b64 = entry.get("content", "")
        if not content_b64:
            # Some API responses elide content for directory listings;
            # fetch the file directly in that case.
            try:
                file_meta = gh_api(
                    "GET",
                    f"/repos/{target_repo}/contents/.github/ISSUE_TEMPLATE/{filename}",
                )
                content_b64 = file_meta.get("content", "")
            except RuntimeError:
                continue
        try:
            raw = base64.b64decode(content_b64).decode("utf-8", errors="replace")
        except (ValueError, UnicodeDecodeError):
            continue

        name = ""
        description = ""
        m_name = _TEMPLATE_NAME_RE.search(raw)
        if m_name:
            name = m_name.group(1).strip().strip('"').strip("'")
        m_desc = _TEMPLATE_DESCRIPTION_RE.search(raw)
        if m_desc:
            description = m_desc.group(1).strip().strip('"').strip("'")

        templates.append({
            "filename": filename,
            "name": name or filename,
            "description": description,
            "kind": _classify_template_kind(name, description),
            "raw_content": raw,
        })
    return templates


def _classify_template_kind(name: str, description: str) -> str:
    """Heuristic classifier — returns ``bug | feature | new_model | question | other``.

    Walks the kind keywords in priority order (bug > feature > new_model > question).
    Bug + question kinds are filtered out by the picker since Outrider's
    paper-pitch shape doesn't fit either. ``new_model`` covers the mteb-style
    "add a new model / dataset / benchmark" templates that are the strongest
    Outrider fit when present.
    """
    haystack = f"{name} {description}".lower()
    for kind, keywords in _TEMPLATE_KIND_KEYWORDS.items():
        if any(kw in haystack for kw in keywords):
            return kind
    return "other"


def _filter_eligible_templates(templates: list[dict]) -> list[dict]:
    """Drop templates whose kind doesn't fit Outrider's paper-pitch shape.

    ``bug`` and ``question`` templates would actively harm the rewrite —
    forcing a "describe the bug" / "what's your question" shape onto a
    paper recommendation is a worse outcome than the default body. Keeps
    ``feature`` / ``new_model`` / ``other``.
    """
    return [t for t in templates if t.get("kind") not in ("bug", "question")]


def _build_issue_body_rewrite_prompt(
    issue_title: str, current_body: str, eligible_templates: list[dict]
) -> str:
    """Compose the Claude one-shot that picks the best-fitting Issue template
    and rewrites the Issue body to match it.

    Combines the picker + body-rewrite into one call (vs two) since both
    operate on the same inputs and an isolated picker doesn't materially
    constrain the rewrite's quality. The LLM picks template_id by best
    structural match against the Outrider body and produces the rewritten
    body in the same JSON response.
    """
    template_blocks = []
    for t in eligible_templates[:6]:  # cap to keep the prompt bounded
        template_blocks.append(
            f"### Template: `{t['filename']}` (kind: {t['kind']})\n\n"
            f"**Name**: {t['name']}\n"
            f"**Description**: {t.get('description') or '(no description)'}\n\n"
            f"**Raw file:**\n\n```\n{t['raw_content'][:2500]}\n```"
        )
    templates_section = "\n\n".join(template_blocks) if template_blocks else "(none — only bug/question templates were available)"

    return f"""You are rewriting a draft Issue's body to match the target repo's Issue template conventions.

# Issue being refined

**Title**: {issue_title}

# Current Issue body

```
{current_body[:8000]}
```

# Eligible templates from the target repo's `.github/ISSUE_TEMPLATE/`

{templates_section}

# Task

The Outrider Issue body above carries the orchestrator's draft scaffolding
(paper provenance, "Why this paper is interesting for the team", "Why this
candidate", license metadata, "Suggested experiment", "Intentionally out
of scope", routing-reason sections). That scaffolding is **secondary
context**, not first-class content. The target repo's Issue templates are
the **primary structure** — a maintainer reading the rewritten Issue
should see the template's shape, not the Outrider sections.

Pick the **best-fitting template** from the eligible set. "Best fit" means
the template whose `name` / `description` / body fields most closely match
the Outrider Issue's actual ask:

- If a template specifically targets the kind of contribution Outrider is
  proposing (e.g. `new_model.yaml` for a paper-anchored model addition),
  prefer it over a generic `feature_request` template.
- If no template is a clear fit (e.g. only generic `feature` templates
  exist for what is structurally a model-addition Issue), pick the closest
  match and note the fit-quality in the rationale.
- If the eligible set is empty, return `(none)` for `TEMPLATE_ID` — the
  body is rewritten with only the scaffolding-collapse rule applied (no
  upstream structure to fold into).

Produce an updated Issue body that:

1. **Treats the picked template as the primary structure.** Use its
   section headings (or its YAML Issue Form labels rendered as `**Bold
   field name**` lines, in the order the form declares them) as the
   body's primary reading surface. For YAML Issue Forms, render each
   `body[].attributes.label` as a `**Label**` line followed by the
   matching content from the Outrider body.
2. **Folds the current body's facts INTO the template's fields.** The
   Outrider "Why this paper is interesting" / "Suggested experiment" /
   delivery-rationale content fills the template's `Description` /
   `Problem` / `Solution` / similar fields. Quote arxiv IDs, GitHub
   URLs, and license metadata into the template's dedicated fields if
   any exist (e.g. mteb's `Arxiv link` input).
3. **Relegates Outrider-only context to ONE collapsed block.** Sections
   from the current body that don't map to any template field (the
   discovery-loop blockquote, "Why this candidate" selection reasoning,
   "What else Outrider considered" rejection list, paper-provenance
   confidence/relevance lines, footer promos) go inside a single
   ``<details><summary>Discovery context</summary>`` block placed at
   the bottom of the body. If there's no such content, omit the block.
4. **Compresses attribution to one italicized line** placed immediately
   above the Discovery-context block (or at the very bottom if the
   block was omitted): ``_Drafted by [Outrider](https://github.com/remyxai/outrider)
   — paper: [arXiv:<id>](<arxiv-url>)._``
5. **Preserves the paper's reference repo URL** if present. If the
   picked template has a dedicated code/link field, put it there.
   Otherwise put it inside the Discovery-context block as
   ``Reference: https://github.com/<owner>/<repo>``.
6. **Reuses substantive facts** — paper title, arxiv id, call-site
   notes, the routing-reason section ("Why the orchestrator opened an
   Issue instead of a PR"), the re-engagement note ("Reopen this Issue
   if you want Outrider to revisit this paper later"). Do not invent
   new claims; do not drop the routing-reason content (it's auditable
   evidence of why this is an Issue, not a PR) — fold it into the
   template's discussion/context field if available, otherwise keep it
   as a clearly-labeled section above the Discovery-context block.
7. **Does not add boilerplate** that isn't in the picked template
   beyond the one-line attribution and the Discovery-context block.

# Output format

Return ONLY a delimited response in EXACTLY this format (no prose before
or after, no markdown code fences):

===TEMPLATE_ID===
<filename of picked template, or (none) if no template fits>
===RATIONALE===
<one-sentence note on what structurally changed>
===UPDATED_BODY===
<the full new Issue body in markdown, verbatim — may contain any
characters including newlines, quotes, braces, etc.>
===END===

The delimited format is required because the body is long markdown that
would be hard to escape in JSON. Do not wrap the response in code fences;
do not add prose before the first marker or after the last marker. The
exact section markers (``===TEMPLATE_ID===``, ``===RATIONALE===``,
``===UPDATED_BODY===``, ``===END===``) must each appear on their own line.
"""


def _update_issue_body(target: Target, issue_number: int, new_body: str) -> None:
    """PATCH the Issue body via the GitHub Issues API."""
    gh_api("PATCH", f"/repos/{target.repo}/issues/{issue_number}", {"body": new_body})


# Section markers used by the Issue-body-rewrite Claude one-shot's delimited
# output format. JSON-wrapped output is brittle when the body is long markdown
# (one unescaped quote in a 30k-token response trashes the parse); a delimited
# format lets the body contain any chars including quotes, braces, and
# newlines without any escaping concerns.
_ISSUE_REWRITE_MARKER_TEMPLATE_ID = "===TEMPLATE_ID==="
_ISSUE_REWRITE_MARKER_RATIONALE = "===RATIONALE==="
_ISSUE_REWRITE_MARKER_UPDATED_BODY = "===UPDATED_BODY==="
_ISSUE_REWRITE_MARKER_END = "===END==="


def _parse_issue_rewrite_response(raw: str) -> dict | None:
    """Parse the delimited Issue-body-rewrite response.

    Expects sections in order: TEMPLATE_ID, RATIONALE, UPDATED_BODY, END.
    Returns ``{template_id, rationale, updated_body}`` on success, or
    ``None`` if any marker is missing or out-of-order. Empty
    ``UPDATED_BODY`` is also treated as a parse failure — the rewrite
    must produce a body.

    ``(none)`` and the literal string ``null`` in TEMPLATE_ID are both
    normalised to an empty string (meaning "no template was picked"); the
    orchestrator treats both as the "no-fitting-template" outcome.
    """
    if not raw:
        return None
    try:
        t_start = raw.index(_ISSUE_REWRITE_MARKER_TEMPLATE_ID)
        r_start = raw.index(_ISSUE_REWRITE_MARKER_RATIONALE)
        b_start = raw.index(_ISSUE_REWRITE_MARKER_UPDATED_BODY)
        # END is optional — the body extends to EOF if absent.
        try:
            e_start = raw.index(_ISSUE_REWRITE_MARKER_END, b_start)
        except ValueError:
            e_start = len(raw)
    except ValueError:
        return None
    # Order check.
    if not (t_start < r_start < b_start <= e_start):
        return None
    template_id = raw[t_start + len(_ISSUE_REWRITE_MARKER_TEMPLATE_ID):r_start].strip()
    rationale = raw[r_start + len(_ISSUE_REWRITE_MARKER_RATIONALE):b_start].strip()
    updated_body = raw[b_start + len(_ISSUE_REWRITE_MARKER_UPDATED_BODY):e_start].strip()
    if not updated_body:
        return None
    # Normalise the no-pick sentinels.
    if template_id.lower() in ("(none)", "null", "none", "", "<none>"):
        template_id = ""
    return {
        "template_id": template_id,
        "rationale": rationale,
        "updated_body": updated_body,
    }


def _apply_issue_body_convention_update(
    target: Target,
    issue_number: int,
    current_body: str,
    eligible_templates: list[dict],
    issue_title: str,
    timeout_s: int,
    workdir: Path,
) -> tuple[bool, str, str, str]:
    """Run the Issue-body rewrite one-shot and PATCH the Issue body.

    Returns ``(ok, picked_template_id, rationale, error)``. On non-fatal
    failure returns ``ok=False`` with an error message; the caller treats
    that as ``issue_convention_failed_claude`` and moves on.
    """
    prompt = _build_issue_body_rewrite_prompt(
        issue_title, current_body, eligible_templates,
    )
    ok, raw = _run_claude_oneshot(workdir, prompt, timeout_s, max_turns=4)
    if not ok:
        return False, "", "", f"issue-body-rewrite Claude call failed: {raw[-300:]}"
    rewrite = _parse_issue_rewrite_response(raw)
    if not rewrite:
        return False, "", "", (
            f"issue-body-rewrite returned unparseable delimited response: "
            f"{raw[-300:]}"
        )
    new_body = rewrite["updated_body"]
    # Sanity check: if the input carried an arXiv link (paper provenance),
    # the rewrite must keep one. Same guard as the PR-route Phase B.
    current_arxiv = _ARXIV_URL_RE.search(current_body)
    if current_arxiv:
        if not _ARXIV_URL_RE.search(new_body):
            return False, "", "", (
                f"issue-body-rewrite dropped the arXiv link "
                f"({current_arxiv.group(0)}); paper provenance must "
                f"remain reachable"
            )
    try:
        _update_issue_body(target, issue_number, new_body)
    except RuntimeError as e:
        return False, "", "", f"Issue body PATCH failed: {e}"
    return (
        True,
        rewrite.get("template_id") or "",
        rewrite.get("rationale", "structurally aligned to Issue template"),
        "",
    )


def run_issue_convention_pass(target: Target) -> dict:
    """Phase B (Issue route) — fold Outrider scaffolding into the target
    repo's Issue template shape.

    Status values:
        issue_convention_aligned                       — picked a template
                                                         and rewrote the body
        issue_convention_aligned_no_fitting_template   — templates exist but
                                                         none fit Outrider's
                                                         paper-pitch shape
                                                         (e.g. bug-only sets);
                                                         rewrote with scaffolding
                                                         collapsed
        issue_convention_skipped_no_templates          — no .github/ISSUE_TEMPLATE/
                                                         directory; rewrote with
                                                         scaffolding collapsed
        issue_convention_skipped_no_issue              — INPUT_ISSUE_NUMBER empty
        issue_convention_skipped_not_bot               — Issue not authored by
                                                         remyx-ai[bot]
        issue_convention_failed_claude                 — Claude call failed or
                                                         returned unparseable JSON
        issue_convention_failed_patch                  — PATCH to Issue body
                                                         failed
    """
    result: dict = {"repo": target.repo, "mode": "issue-convention", "status": "unknown"}

    issue_number_raw = os.environ.get("INPUT_ISSUE_NUMBER", "").strip()
    if not issue_number_raw:
        result["status"] = "issue_convention_skipped_no_issue"
        log.info("  ✗ issue-convention mode invoked without INPUT_ISSUE_NUMBER")
        return result
    try:
        issue_number = int(issue_number_raw)
    except ValueError:
        result["status"] = "issue_convention_skipped_no_issue"
        log.error(f"  ✗ INPUT_ISSUE_NUMBER={issue_number_raw!r} is not an integer")
        return result
    result["issue_number"] = issue_number
    log.info(f"  → issue convention pass on {target.repo}#{issue_number}")

    try:
        issue = gh_api("GET", f"/repos/{target.repo}/issues/{issue_number}")
    except RuntimeError as e:
        result["status"] = "issue_convention_failed_claude"
        result["error"] = f"could not fetch Issue: {e}"
        return result

    author = (issue.get("user") or {}).get("login", "")
    if author != "remyx-ai[bot]":
        result["status"] = "issue_convention_skipped_not_bot"
        result["error"] = f"Issue author is {author!r}, not remyx-ai[bot]"
        log.info(f"  ✗ Issue #{issue_number} authored by {author!r}; skipping")
        return result

    issue_title = issue.get("title") or ""
    current_body = issue.get("body") or ""

    upstream_repo = _resolve_upstream_for_conventions(target)
    log.info(f"  → fetching issue templates from {upstream_repo}")
    templates = _fetch_issue_templates_from_repo(upstream_repo)
    result["templates_found"] = len(templates)

    if not templates:
        # The "no templates at all" path. We still want to clean up the
        # Outrider scaffolding (collapse into <details>) even without a
        # canonical structure to fold into — partial improvement is still
        # an improvement. The rewrite prompt handles the empty-templates
        # case by setting template_id=null and applying only the
        # scaffolding-collapse rule.
        workdir = Path(tempfile.mkdtemp(prefix="outrider-issue-convention-"))
        log.info(f"  → workdir: {workdir} (no templates — scaffolding-collapse only)")
        ok, picked, rationale, err = _apply_issue_body_convention_update(
            target, issue_number, current_body, [], issue_title,
            target.claude_timeout_s, workdir,
        )
        if not ok:
            result["status"] = "issue_convention_failed_claude"
            result["error"] = err
            return result
        result["status"] = "issue_convention_skipped_no_templates"
        result["rationale"] = rationale
        _add_pr_label(target, issue_number, ISSUE_CONVENTION_LABEL_DONE)
        log.info(f"  ✓ {result['status']}: {rationale}")
        return result

    eligible = _filter_eligible_templates(templates)
    result["templates_eligible"] = len(eligible)
    result["templates_filtered_kinds"] = sorted({
        t["kind"] for t in templates if t.get("kind") in ("bug", "question")
    })

    workdir = Path(tempfile.mkdtemp(prefix="outrider-issue-convention-"))
    log.info(f"  → workdir: {workdir}")
    log.info(
        f"  → {len(templates)} templates found, "
        f"{len(eligible)} eligible after kind-filter"
    )

    ok, picked_template_id, rationale, err = _apply_issue_body_convention_update(
        target, issue_number, current_body,
        eligible if eligible else [],  # if all filtered, prompt sees empty set
        issue_title,
        target.claude_timeout_s, workdir,
    )
    if not ok:
        if "PATCH failed" in err:
            result["status"] = "issue_convention_failed_patch"
        else:
            result["status"] = "issue_convention_failed_claude"
        result["error"] = err
        log.error(f"  ✗ {result['status']}: {err}")
        return result

    result["picked_template"] = picked_template_id
    result["rationale"] = rationale

    if not eligible:
        # Templates existed but the kind-filter dropped them all (e.g.
        # bug-only sets like lerobot). The body was still rewritten with
        # scaffolding collapsed, but no upstream template was applied.
        result["status"] = "issue_convention_aligned_no_fitting_template"
    elif not picked_template_id:
        # Eligible templates existed but the LLM declined to pick one
        # (judged none a fit). Same outcome shape as the no-fitting case.
        result["status"] = "issue_convention_aligned_no_fitting_template"
    else:
        result["status"] = "issue_convention_aligned"

    _add_pr_label(target, issue_number, ISSUE_CONVENTION_LABEL_DONE)
    result["issue_url"] = issue.get("html_url", "")
    log.info(
        f"  ✓ {result['status']} "
        f"(template={picked_template_id or '(none)'}): {rationale}"
    )
    return result


# ─── Refinement-pass: test gate ────────────────────────────────────────────
#
# Triggered after the convention pass completes. Runs lint + targeted tests
# on the PR's added/modified files, writes a result panel to the action's
# run-summary, drops the PR's draft state on a passing run (the canonical
# GitHub signal for "ready for review"), and applies the
# ``outrider:test-failed`` label only on a hard failure. PR body is left
# untouched — that's the convention pass's surface.
#
# Heavier validation workloads (CPU smoke pass, benchmarks, LLM judges) are
# deferred to a forthcoming "validate" phase that opts in via outrider.yaml
# in the target repo; this phase sticks to checks that fit in a stock runner
# without needing GPU or special-cased infra.

TEST_LABEL_TRIGGER = "outrider:convention-done"
TEST_LABEL_FAILED = "outrider:test-failed"

_TEST_LINT_TIMEOUT_S = 120
_TEST_PYTEST_TIMEOUT_S = 240
_TEST_DEP_INSTALL_TIMEOUT_S = 480  # 8 min upper bound; most pure-Python repos finish in <2 min


# PyPI distribution names commonly imported under a different top-level
# package name. The AST scan returns import-name; pip needs dist-name.
_IMPORT_TO_DIST = {
    "yaml": "PyYAML",
    "PIL": "pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "Crypto": "pycryptodome",
    "OpenSSL": "pyOpenSSL",
    "bs4": "beautifulsoup4",
    "google": "google-cloud-core",  # ambiguous; better than nothing
}

# Top-level imports that are part of the Python stdlib or always-installed
# on a stock GH Actions runner — never need pip install.
_STDLIB_OR_PREINSTALLED = frozenset({
    # stdlib (representative subset; more added below dynamically)
    "abc", "argparse", "ast", "asyncio", "base64", "bisect", "builtins",
    "bz2", "collections", "configparser", "contextlib", "copy", "csv",
    "ctypes", "dataclasses", "datetime", "decimal", "difflib", "dis",
    "doctest", "email", "enum", "errno", "fileinput", "fnmatch",
    "fractions", "functools", "gc", "getopt", "getpass", "gettext", "glob",
    "gzip", "hashlib", "heapq", "hmac", "html", "http", "imp", "importlib",
    "inspect", "io", "ipaddress", "itertools", "json", "keyword", "linecache",
    "locale", "logging", "lzma", "math", "mimetypes", "multiprocessing",
    "numbers", "operator", "os", "pathlib", "pdb", "pickle", "platform",
    "plistlib", "pprint", "queue", "random", "re", "secrets", "select",
    "shlex", "shutil", "signal", "site", "smtplib", "socket", "sqlite3",
    "ssl", "stat", "statistics", "string", "stringprep", "struct",
    "subprocess", "sys", "tarfile", "tempfile", "textwrap", "threading",
    "time", "timeit", "tkinter", "token", "tokenize", "trace", "traceback",
    "types", "typing", "unicodedata", "unittest", "urllib", "uuid",
    "warnings", "weakref", "webbrowser", "xml", "xmlrpc", "zipfile",
    "zipimport", "zlib",
    # Pre-installed on the GH Actions runner via action.yml
    "pytest", "ruff", "remyxai",
})

# Packages whose build step requires torch to be importable (PEP 517 build
# isolation defeats this when ``pip install -e .`` builds them in a fresh
# env). Pre-installing CPU torch BEFORE attempting full install handles the
# common ML-repo pattern.
_TORCH_BUILD_DEPENDENTS = frozenset({
    "flash_attn", "flash-attn", "flash_attn_2",
    "mamba_ssm", "mamba-ssm",
    "causal_conv1d", "causal-conv1d",
    "xformers",
    "deepspeed",
    "apex",
    "triton",
})


def _extract_external_imports(
    workdir: Path, test_files: list[str]
) -> tuple[list[str], list[str]]:
    """AST-walk the touched test files and return
    ``(external_imports, local_imports)``.

    A top-level import name is classified as ``local`` if a same-named
    directory or ``.py`` file exists at the repo root (i.e., the import
    resolves from ``PYTHONPATH=<workdir>`` without pip install). Otherwise
    it's ``external`` and goes on the pip install list. Stdlib and
    runner-preinstalled names are filtered out of both lists.
    """
    external: set[str] = set()
    local: set[str] = set()
    repo_top_level = {p.stem for p in workdir.iterdir() if p.is_dir() or p.suffix == ".py"}
    for rel in test_files:
        path = workdir / rel
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in _STDLIB_OR_PREINSTALLED:
                        continue
                    if top in repo_top_level:
                        local.add(top)
                    else:
                        external.add(top)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    top = node.module.split(".")[0]
                    if top in _STDLIB_OR_PREINSTALLED:
                        continue
                    if top in repo_top_level:
                        local.add(top)
                    else:
                        external.add(top)
    return sorted(external), sorted(local)


def _install_focused_test_deps(
    workdir: Path, external_imports: list[str]
) -> tuple[bool, str]:
    """Install just the external Python packages the touched test files
    actually import, plus any torch-build-required deps. Skips the full
    target-repo install entirely; tests resolve local imports via the
    ``PYTHONPATH=<workdir>`` set in :func:`_run_test_pytest`.

    Multi-strategy: if any torch-build-dependent package is in the
    extracted imports, pre-install CPU torch first via PyTorch's CPU
    wheel index so the dependent package can build.

    Returns ``(installed, summary)``.
    """
    if not external_imports:
        return True, "no external imports detected in touched test files"

    # Map import names to PyPI dist names where they differ.
    dist_names = [_IMPORT_TO_DIST.get(name, name) for name in external_imports]

    # Decide whether to pre-install torch from the CPU wheel index.
    needs_torch_first = (
        "torch" in external_imports
        or any(name.replace("-", "_") in _TORCH_BUILD_DEPENDENTS for name in external_imports)
    )

    install_log: list[str] = []
    env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}

    if needs_torch_first and "torch" in dist_names:
        # Move torch to its own install step so we can pin the CPU index.
        dist_names = [n for n in dist_names if n != "torch"]
        log.info("  → installing torch (CPU wheel) before other deps")
        try:
            proc = subprocess.run(
                ["python", "-m", "pip", "install", "--quiet",
                 "--index-url", "https://download.pytorch.org/whl/cpu",
                 "torch"],
                capture_output=True, text=True,
                timeout=_TEST_DEP_INSTALL_TIMEOUT_S,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return False, f"torch CPU-wheel install timed out after {_TEST_DEP_INSTALL_TIMEOUT_S}s"
        if proc.returncode != 0:
            return False, f"torch CPU-wheel install failed: {(proc.stderr or proc.stdout)[-800:].strip()}"
        install_log.append("torch (CPU wheel)")

    if dist_names:
        log.info(f"  → installing focused deps: {dist_names}")
        try:
            proc = subprocess.run(
                ["python", "-m", "pip", "install", "--quiet", *dist_names],
                capture_output=True, text=True,
                timeout=_TEST_DEP_INSTALL_TIMEOUT_S,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return False, f"focused-deps install timed out after {_TEST_DEP_INSTALL_TIMEOUT_S}s"
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-1200:].strip()
            return False, f"focused-deps install failed (exit {proc.returncode}):\n{tail}"
        install_log.append(", ".join(dist_names))

    return True, "installed: " + "; then ".join(install_log) if install_log else "no install needed"


def _extract_touched_files(pr_diff: str) -> tuple[list[str], list[str]]:
    """Parse a unified diff to extract added/modified file paths.

    Returns ``(python_files, test_files)``. ``test_files`` is the subset
    of python_files whose path matches the conventional test patterns
    (``tests/...``, ``**/test_*.py``, ``**/*_test.py``) so the test
    gate can run a focused pytest invocation.
    """
    py_files: list[str] = []
    for line in (pr_diff or "").splitlines():
        # New file: "+++ b/path/to/file.py"
        if line.startswith("+++ b/") and line.endswith(".py"):
            path = line[6:]
            if path not in py_files:
                py_files.append(path)
    test_files = [
        p for p in py_files
        if p.startswith("tests/") or p.startswith("test/")
        or "/tests/" in p or "/test/" in p
        or Path(p).name.startswith("test_")
        or Path(p).name.endswith("_test.py")
    ]
    return py_files, test_files


def _run_test_lint(
    workdir: Path, touched_py_files: list[str]
) -> tuple[str, str, int]:
    """Run ruff check on PR-touched Python files.

    Returns ``(status, output, issue_count)`` where status is
    ``"passed"`` / ``"failed"`` / ``"unvalidated"`` (no files to check
    or ruff not installable). ``issue_count`` is parsed from ruff's
    summary line when status is failed.
    """
    if not touched_py_files:
        return "unvalidated", "no python files touched by this PR", 0

    # Ensure ruff is available — the existing action.yml installs pytest
    # and remyxai but not ruff; install ad-hoc rather than threading
    # another dep through action.yml.
    install = subprocess.run(
        ["python", "-m", "pip", "install", "--quiet", "ruff"],
        capture_output=True, text=True, timeout=60,
    )
    if install.returncode != 0:
        return "unvalidated", f"could not install ruff: {install.stderr[-300:]}", 0

    existing = [f for f in touched_py_files if (workdir / f).is_file()]
    if not existing:
        return "unvalidated", "no touched .py files found in workdir", 0

    log.info(f"  → running ruff check on {len(existing)} touched .py files")
    try:
        proc = subprocess.run(
            ["ruff", "check", "--no-fix", "--output-format=concise", *existing],
            cwd=workdir, capture_output=True, text=True,
            timeout=_TEST_LINT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return "failed", f"ruff timed out after {_TEST_LINT_TIMEOUT_S}s", 0
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return "passed", output[-2000:].strip() or "All ruff checks passed.", 0
    # Ruff exit 1 = lint issues; exit 2 = ruff itself errored. Count
    # issue lines for the comment summary.
    issue_lines = [l for l in (proc.stdout or "").splitlines() if ":" in l and l.strip()]
    return "failed", output[-2500:], len(issue_lines)


def _run_test_pytest(
    workdir: Path, touched_test_files: list[str]
) -> tuple[str, str]:
    """Run pytest on the PR's added/modified test files.

    ``PYTHONPATH=<workdir>`` is prepended so local imports
    (``from openrlhf.utils import ...``) resolve from the cloned repo
    without requiring a ``pip install -e .`` step — that step is the
    main reason most ML-repo test runs blow up on build-time deps
    (flash-attn / deepspeed wheels that need torch present to build).

    Returns ``(status, output)`` matching ``run_tests``' contract:
    ``"passed"`` / ``"failed"`` / ``"unvalidated"``. ``unvalidated``
    fires when the runner lacks the target repo's full dep set
    (collection ImportError) — informational, doesn't gate the chain.
    """
    if not touched_test_files:
        return "unvalidated", "no test files touched by this PR"

    existing = [f for f in touched_test_files if (workdir / f).is_file()]
    if not existing:
        return "unvalidated", "no touched test files found in workdir"

    log.info(f"  → running pytest on {len(existing)} touched test files")
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath = str(workdir) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    env = {**os.environ, "PYTHONPATH": pythonpath}
    try:
        proc = subprocess.run(
            ["python", "-m", "pytest", "-q", "--maxfail=5", *existing],
            cwd=workdir, capture_output=True, text=True,
            timeout=_TEST_PYTEST_TIMEOUT_S, env=env,
        )
    except subprocess.TimeoutExpired:
        return "failed", f"pytest timed out after {_TEST_PYTEST_TIMEOUT_S}s"
    output = (proc.stdout or "") + ("\n--- STDERR ---\n" + proc.stderr if proc.stderr else "")
    return _classify_pytest(proc.returncode, output), output[-3000:]


def _render_test_section(
    lint_status: str, lint_output: str, lint_issues: int,
    test_status: str, test_output: str,
    touched_py: list[str], touched_tests: list[str],
    package_manager: str,
    deps_installed: bool = False,
    deps_install_summary: str = "",
) -> str:
    """Compose the markdown body section summarising the test gate's findings."""
    icon = {"passed": "✓", "failed": "✗", "unvalidated": "—"}
    summary_parts = [
        f"**Lint** {icon.get(lint_status, '?')} {lint_status}",
        f"**Tests** {icon.get(test_status, '?')} {test_status}",
    ]
    lines = ["## Refinement checks", ""]
    lines.append(" · ".join(summary_parts))
    lines.append("")
    lines.append(f"Touched in this PR: **{len(touched_py)} Python files** "
                 f"({len(touched_tests)} test). Package manager detected: `{package_manager}`. "
                 f"Deps install: {'succeeded' if deps_installed else 'skipped/failed'}.")
    if deps_install_summary:
        lines.append("")
        lines.append(f"_{deps_install_summary[:600]}_")
    lines.append("")

    lines.append(f"### Lint — `{lint_status}`")
    if lint_status == "passed":
        lines.append("All ruff checks passed on PR-touched files.")
    elif lint_status == "unvalidated":
        lines.append(f"_{lint_output}_")
    else:
        lines.append(f"Ruff surfaced **{lint_issues} issues** on PR-touched files:")
        lines.append("")
        lines.append("```")
        lines.append(lint_output)
        lines.append("```")
    lines.append("")

    lines.append(f"### Tests — `{test_status}`")
    if test_status == "passed":
        lines.append(f"`pytest` on touched test files succeeded.")
    elif test_status == "unvalidated":
        if "no test files" in test_output or "no touched test files" in test_output:
            lines.append(f"_{test_output}_")
        else:
            lines.append(
                "Tests could not be exercised — collection failed on a missing "
                "dependency or import error. This is an environment limitation, "
                "not a code failure (a stock runner lacks the target repo's "
                "full ML dependency set). The PR's own changes were not run."
            )
            lines.append("")
            lines.append("<details><summary>collection output (tail)</summary>")
            lines.append("")
            lines.append("```")
            lines.append(test_output[-1500:])
            lines.append("```")
            lines.append("")
            lines.append("</details>")
    else:
        lines.append(f"Pytest run failed:")
        lines.append("")
        lines.append("```")
        lines.append(test_output)
        lines.append("```")
    lines.append("")
    lines.append(
        f"_Generated by [Outrider test gate]"
        f"({CANONICAL_ATTRIBUTION_URL}) at {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}._"
    )
    return "\n".join(lines)


def run_test_gate(target: Target) -> dict:
    """Test gate — lint + targeted pytest on a remyx-ai[bot] PR. Writes
    a result section to the action's run-summary, drops the PR's draft
    state on a passing run (the canonical GitHub signal for "ready for
    review"), and applies ``outrider:test-failed`` on hard failure.

    Status values:
        test_passed                  — lint passed, tests passed-or-unvalidated
        test_failed                  — lint surfaced issues OR tests failed
        test_skipped_no_pr           — INPUT_PR_NUMBER empty
        test_skipped_not_bot         — PR not authored by remyx-ai[bot]
        test_failed_setup            — could not clone PR head
    """
    result: dict = {"repo": target.repo, "mode": "test", "status": "unknown"}

    pr_number_raw = os.environ.get("INPUT_PR_NUMBER", "").strip()
    if not pr_number_raw:
        result["status"] = "test_skipped_no_pr"
        return result
    try:
        pr_number = int(pr_number_raw)
    except ValueError:
        result["status"] = "test_skipped_no_pr"
        return result
    result["pr_number"] = pr_number
    log.info(f"  → test gate on {target.repo}#{pr_number}")

    try:
        pr = _fetch_pr_metadata(target, pr_number)
    except RuntimeError as e:
        result["status"] = "test_failed_setup"
        result["error"] = f"could not fetch PR: {e}"
        return result

    author = (pr.get("user") or {}).get("login", "")
    if author != "remyx-ai[bot]":
        result["status"] = "test_skipped_not_bot"
        return result

    try:
        pr_diff = _fetch_pr_diff(target, pr_number)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        result["status"] = "test_failed_setup"
        result["error"] = f"could not fetch PR diff: {e}"
        return result

    touched_py, touched_tests = _extract_touched_files(pr_diff)
    log.info(f"  → PR touches {len(touched_py)} .py files ({len(touched_tests)} tests)")
    result["touched_py_files"] = touched_py
    result["touched_test_files"] = touched_tests

    # Clone PR head — lint + pytest both need actual files on disk.
    head_repo = (pr.get("head") or {}).get("repo", {}).get("full_name") or target.repo
    head_ref = (pr.get("head") or {}).get("ref", "")
    if not head_ref:
        result["status"] = "test_failed_setup"
        result["error"] = "could not resolve PR head branch"
        return result

    workdir = Path(tempfile.mkdtemp(prefix="outrider-test-"))
    clone_workdir = workdir / "draft"
    token = _github_token()
    clone_url = f"https://x-access-token:{token}@github.com/{head_repo}.git"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    log.info(f"  → cloning PR head {head_repo}:{head_ref}")
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", head_ref, clone_url, str(clone_workdir)],
            check=True, capture_output=True, timeout=300, env=env,
        )
    except subprocess.CalledProcessError as e:
        result["status"] = "test_failed_setup"
        result["error"] = f"clone PR head failed: {e.stderr.decode()[:500]}"
        return result

    pkg_mgr, _ = _detect_verification_stack(clone_workdir)
    result["package_manager"] = pkg_mgr

    # Lint
    lint_status, lint_output, lint_issues = _run_test_lint(clone_workdir, touched_py)
    result["lint_status"] = lint_status
    result["lint_issues"] = lint_issues
    log.info(f"  → lint: {lint_status} ({lint_issues} issues)")

    # Install only what the touched test files actually import — much
    # more likely to succeed on ML repos than a full ``pip install -e .``
    # (which can blow up on heavy build-time deps like flash-attn /
    # deepspeed that need torch present during their PEP 517 build).
    # Local imports resolve via PYTHONPATH=<workdir> in _run_test_pytest.
    external_imports, local_imports = _extract_external_imports(clone_workdir, touched_tests)
    log.info(f"  → touched-test imports: external={external_imports}, local={local_imports}")
    install_ok, install_summary = _install_focused_test_deps(clone_workdir, external_imports)
    result["deps_installed"] = install_ok
    result["deps_install_summary"] = install_summary
    result["test_imports_external"] = external_imports
    result["test_imports_local"] = local_imports
    log.info(f"  → deps install: {'ok' if install_ok else 'failed'} ({install_summary[:120]})")

    # Optional escalation: if focused install failed AND the repo has a
    # pyproject.toml, try a single retry with --no-build-isolation. This
    # picks up repos where torch is already in the runner env (from the
    # focused step above) and the repo's own deps build against it.
    if not install_ok and (clone_workdir / "pyproject.toml").is_file():
        log.info("  → escalating to pip install -e . --no-build-isolation")
        try:
            proc = subprocess.run(
                ["python", "-m", "pip", "install", "--quiet",
                 "--disable-pip-version-check", "--no-build-isolation",
                 "-e", "."],
                cwd=clone_workdir, capture_output=True, text=True,
                timeout=_TEST_DEP_INSTALL_TIMEOUT_S,
            )
            if proc.returncode == 0:
                install_ok = True
                install_summary = "installed via pip install -e . --no-build-isolation (fallback)"
                result["deps_installed"] = True
                result["deps_install_summary"] = install_summary
                log.info(f"  → fallback install succeeded")
        except subprocess.TimeoutExpired:
            log.info("  → fallback install timed out; continuing with unvalidated tests")

    # Tests
    test_status, test_output = _run_test_pytest(clone_workdir, touched_tests)
    result["test_status"] = test_status
    log.info(f"  → tests: {test_status}")

    # Render the result section and write to the action run-summary
    # panel; the PR body is owned by the convention pass's body rewrite
    # and isn't an appropriate surface for per-run check output.
    section_body = _render_test_section(
        lint_status=lint_status, lint_output=lint_output, lint_issues=lint_issues,
        test_status=test_status, test_output=test_output,
        touched_py=touched_py, touched_tests=touched_tests,
        package_manager=pkg_mgr,
        deps_installed=install_ok, deps_install_summary=install_summary,
    )
    _append_to_step_summary(section_body)

    # On a passing run, drop the PR's draft state — that's the canonical
    # GitHub signal for "ready for review", and replaces what was
    # previously a redundant outrider:ready-for-review label.
    # Unvalidated tests (missing deps) don't gate — informational only.
    hard_failure = (lint_status == "failed") or (test_status == "failed")
    if hard_failure:
        _add_pr_label(target, pr_number, TEST_LABEL_FAILED)
        result["status"] = "test_failed"
    else:
        result["status"] = "test_passed"
        ready_ok = _drop_pr_draft_state(target, pr_number)
        result["draft_dropped"] = ready_ok
    _remove_pr_label(target, pr_number, TEST_LABEL_TRIGGER)

    result["pr_url"] = pr.get("html_url", "")
    log.info(
        f"  ✓ {result['status']}: lint={lint_status}, tests={test_status}, "
        f"hard_failure={hard_failure}"
    )
    return result


def _agent_failure_blocks(agent: str, log_tail: str, claude_calls: int) -> list[str]:
    """Render a list of step_summary markdown lines for a ``claude_failed``
    status, dispatching on the agent's log tail.

    Currently agent-specific to Claude Code (Anthropic). When alternative
    agent CLIs land (Aider, Goose, Copilot, Codex), this helper grows a
    per-agent patterns + URLs lookup keyed on ``agent`` — the call site
    in ``_write_step_summary`` doesn't change.
    """
    tail = (log_tail or "").lower()
    lines: list[str] = []
    if "credit balance is too low" in tail:
        lines.append("\n> ### 🪙 Action required: Anthropic credit balance exhausted\n>")
        lines.append(
            f"> All {claude_calls} Claude calls this run failed with "
            "\"Credit balance is too low\"."
        )
        lines.append(
            "> The `ANTHROPIC_API_KEY` secret authenticated — the account "
            "just has no remaining credits."
        )
        lines.append(">")
        lines.append(f"> **Top up at:** {_ANTHROPIC_BILLING_URL}")
        lines.append(">")
        lines.append(
            "> The next scheduled run will retry automatically once "
            "credits are available.\n"
        )
    elif "401" in tail and (
        "authentication" in tail
        or "invalid api key" in tail
        or "invalid x-api-key" in tail
    ):
        lines.append("\n> ### 🔑 Action required: ANTHROPIC_API_KEY secret invalid\n>")
        lines.append(
            "> The key configured as the `ANTHROPIC_API_KEY` repo secret "
            "didn't authenticate."
        )
        lines.append(
            f"> Check the key at {_ANTHROPIC_KEYS_URL} and update the "
            "secret via"
        )
        lines.append("> `gh secret set ANTHROPIC_API_KEY --repo <this-repo>`.\n")
    elif "429" in tail or "rate_limit" in tail or "too many requests" in tail:
        lines.append("\n> ### ⏱️ Rate limited — no action needed\n>")
        lines.append(
            "> The Anthropic API rate-limited this run. The next "
            "scheduled run will retry.\n"
        )
    elif tail:
        lines.append("\n<details><summary>Claude agent failure tail</summary>\n")
        lines.append(f"\n```\n{log_tail[:1500]}\n```\n")
        lines.append("\n</details>\n")
    return lines


def _write_step_summary(result: dict) -> None:
    """Render the run outcome as Markdown into $GITHUB_STEP_SUMMARY.

    GitHub Actions pins this panel at the top of every workflow run
    page — it's the most visible surface and the only one that shows
    cost telemetry to a customer without them having to wire
    downstream consuming steps.

    Sections (only what applies given the result's shape):
      - Headline: status + paper link
      - PR / Issue link if one was opened
      - Engineering verdict + license verdict for the chosen candidate,
        adjacent — two independent calls
      - Pool composition (broad + refine, after dedup) and the pool's
        license-class distribution on one line each
      - Why-this-paper reasoning (collapsed by default for brevity)
      - Cost + tokens
      - Selection rejected candidates (collapsed) for "what else did
        Remyx consider"
      - Error trace if status == error
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    status = result.get("status", "unknown")
    paper = result.get("paper")
    arxiv = result.get("arxiv")
    tier = result.get("tier")
    pr_url = result.get("pr_url")
    issue_url = result.get("issue_url")
    reasoning = result.get("reasoning") or ""
    cost = result.get("cost_usd", 0)
    in_tok = result.get("input_tokens", 0)
    out_tok = result.get("output_tokens", 0)
    cache_in_tok = result.get("cache_read_input_tokens", 0)
    claude_calls = result.get("claude_calls", 0)
    rejected = result.get("selection_rejected") or []
    err = result.get("error")

    # Headline emoji conveys outcome at a glance.
    emoji = {
        "pr_opened":               "🟢",
        "pr_opened_draft":         "🟢",
        "issue_opened":            "🟡",
        "issue_opened_preflight":  "🟡",
        "issue_opened_no_integration":     "🟡",
        "issue_opened_stub_density":       "🟡",
        "issue_opened_no_test_integration": "🟡",
        "issue_opened_self_review":        "🟡",
        "skipped_low_confidence":  "⏭️",
        "skipped_open_artifact":   "⏭️",
        "skipped_issues_disabled": "⏭️",
        "skipped_pr_exists":       "⏭️",
        "skipped_issue_exists":    "⏭️",
        "skipped_external_issue_exists": "⏭️",
        "skipped_by_selection_verification": "⏭️",
        "issue_opened_substitution": "🔁",
        "skipped_test_failure":    "⏭️",
        "claude_failed":           "❌",
        "rejected_path_violations":"❌",
        "error":                   "❌",
        "aborted_secret_in_payload": "🛑",
        "weekly_summary_posted":   "🟢",
        "weekly_summary_skipped_no_discussion_id": "⏭️",
        "weekly_summary_failed":   "❌",
    }.get(status, "ℹ️")

    lines: list[str] = []
    lines.append(f"## {emoji} Remyx Recommendation — `{status}`\n")

    if paper and arxiv:
        tier_str = f" ({tier})" if tier else ""
        lines.append(
            f"**Paper**: [{paper}](https://arxiv.org/abs/{arxiv}){tier_str}\n"
        )
    if pr_url:
        lines.append(f"**PR**: {pr_url}\n")
    if issue_url:
        lines.append(f"**Issue**: {issue_url}\n")
    discussion_url = result.get("discussion_comment_url") or ""
    if discussion_url:
        lines.append(f"**Discussion comment**: {discussion_url}\n")

    # When the dedup gate fires (open OR closed prior Issue), surface
    # the existing-Issue context inline so the maintainer sees at a
    # glance which thread already covers this paper and whether it's
    # still in flight or resolved (symmetric discharge).
    existing_url = result.get("existing_issue_url") or ""
    if existing_url:
        existing_state = result.get("existing_issue_state", "open")
        if existing_state == "closed":
            lines.append(
                f"**Already addressed**: {existing_url} (closed — team "
                f"has resolved). Reopen the Issue to re-engage.\n"
            )
        else:
            lines.append(
                f"**Already in flight**: {existing_url} (open — "
                f"re-validated this run).\n"
            )

    # Engineering verdict + license verdict for the chosen candidate,
    # rendered adjacently so they read as two independent calls — a
    # maintainer should be able to take the engineering analysis and the
    # license risk separately. Each degrades
    # silently when its fields are absent.
    eng_shape = (result.get("selection_integration_shape") or "").strip()
    eng_contract = (result.get("selection_contract_match") or "").strip()
    eng_migration = (result.get("selection_migration_cost") or "").strip()
    eng_tds = (result.get("selection_team_direction_signal") or "").strip()
    eng_pcs = (result.get("selection_proposed_call_site") or "").strip()
    if eng_contract or eng_migration or eng_tds or eng_pcs:
        lines.append("**Engineering verdict**\n")
        if eng_shape:
            lines.append(f"- **Integration shape**: {eng_shape}")
        if eng_contract:
            lines.append(f"- **Contract match**: {eng_contract}")
        if eng_migration:
            lines.append(f"- **Migration cost**: {eng_migration}")
        if eng_tds:
            lines.append(f"- **Team-direction signal**: {eng_tds}")
        if eng_pcs:
            lines.append(f"- **Proposed call site**: {eng_pcs}")
        lines.append("")
    license_class = (result.get("license_class") or "").strip()
    if license_class:
        lic_emoji = _LICENSE_CLASS_EMOJI.get(license_class, "⚪")
        lic_compat = result.get("license_compat", 0.0)
        lic_spdx = result.get("paper_license") or "(none detected)"
        lines.append(
            f"**License verdict**: {lic_emoji} `{lic_spdx}` "
            f"(class: `{license_class}`, compat: {lic_compat:.2f})\n"
        )

    # Pool composition + license-class distribution — run-level context
    # for "what did Outrider actually look at". Surfaces the
    # deep-search contribution and the license gate's coverage at a
    # glance; both degrade silently when the fields are absent.
    broad_n = result.get("broad_pool_size")
    refine_n = result.get("refine_pool_size")
    if broad_n is not None and refine_n is not None and (broad_n + refine_n):
        lines.append(
            f"**Candidate pool**: {broad_n} broad + {refine_n} refine "
            f"candidate(s) considered (after dedup)\n"
        )
    license_counts = result.get("license_class_counts") or {}
    if license_counts:
        lines.append(
            f"**License gate (pool)**: "
            f"{_format_license_class_counts(license_counts)}\n"
        )

    # Selection-pass narrative — "why this candidate (or skip)" from
    # the agentic selection. Distinct from rec.reasoning, which is the
    # per-paper context. For skipped_by_selection_verification there is
    # no paper at all and selection_reasoning is the only meaningful
    # payload — render it open so it's visible without expansion. For
    # other outcomes, collapse it so the cost line stays above the
    # fold. The "(selection pass unavailable — used highest-relevance
    # candidate as fallback)" placeholder is a non-signal and is
    # skipped here.
    selection_reasoning = (result.get("selection_reasoning") or "").strip()
    if (
        selection_reasoning
        and not selection_reasoning.startswith("(selection pass unavailable")
    ):
        open_attr = (
            " open" if status == "skipped_by_selection_verification" else ""
        )
        lines.append(
            f"<details{open_attr}><summary>Why this selection</summary>\n"
        )
        lines.append(f"\n{selection_reasoning}\n")
        lines.append("\n</details>\n")

    # Confabulation signal: if selection reasoning cited paths, show which
    # ones the workdir actually contains. A "0 of N verified" line is a
    # concrete flag that the reasoning may be hallucinated — an operator
    # sees it in the step-summary panel before trusting the verdict.
    path_check = result.get("selection_reasoning_paths") or {}
    cited = path_check.get("cited") or []
    if cited:
        verified = path_check.get("verified") or []
        not_found = path_check.get("not_found") or []
        icon = "✓" if verified and not not_found else ("⚠️" if not verified else "•")
        lines.append(
            f"**Paths cited in selection reasoning**: {icon} {len(verified)} of "
            f"{len(cited)} verified in the workdir\n"
        )
        if verified:
            lines.append("- verified: " + ", ".join(f"`{p}`" for p in verified))
        if not_found:
            lines.append("- ⚠️ not found: " + ", ".join(f"`{p}`" for p in not_found))
        lines.append("")

    if reasoning:
        # Collapse long reasoning into a <details> so the cost line
        # stays above the fold.
        lines.append("<details><summary>Why this paper</summary>\n")
        lines.append(f"\n{reasoning}\n")
        lines.append("\n</details>\n")

    # Cost telemetry — the headline reason this summary exists.
    token_line = f"{in_tok:,} in / {out_tok:,} out"
    if cache_in_tok:
        token_line += f" ({cache_in_tok:,} cache-read)"
    agent = result.get("agent", "Claude Code")
    backend = result.get("model_backend", "Anthropic")
    cost_basis = result.get("cost_basis", "claude_code_envelope")
    # Annotate the cost line when the figure is the CLI's
    # Anthropic-rate estimate applied to a non-Anthropic backend's tokens
    # — accurate token counts, approximate dollars. When we compute from
    # the backend's own rate card (cost_basis == "backend_rate_table"),
    # the dollars are authoritative for that rate sheet.
    if cost_basis == "backend_rate_table":
        cost_note = f" *(computed from {backend} PAYG rates)*"
    elif backend != "Anthropic":
        cost_note = (" *(Anthropic-rate estimate on backend tokens; "
                     "see provider billing for the real number)*")
    else:
        cost_note = ""
    lines.append("\n**Cost & tokens this run**\n")
    lines.append(f"- **Agent**: {agent} → {backend}")
    lines.append(f"- **Cost**: `${cost:.4f}`{cost_note}")
    lines.append(f"- **Tokens**: {token_line}")
    if claude_calls:
        lines.append(f"- **Claude calls**: {claude_calls}")
    lines.append("")

    if rejected:
        lines.append(f"<details><summary>Selection: {len(rejected)} other candidate(s) considered</summary>\n")
        for r in rejected[:10]:
            r_arxiv = (r.get("arxiv_id") or "").strip()
            r_title = (r.get("title") or "(untitled)")[:120]
            r_reason = (r.get("reason") or "")[:200]
            if r_arxiv:
                lines.append(f"- [`{r_arxiv}`](https://arxiv.org/abs/{r_arxiv}) — {r_title}")
            else:
                # No arxiv_id (e.g. defensive path when selection returned
                # an out-of-range index). Render the title without a broken
                # link target.
                lines.append(f"- {r_title}")
            if r_reason:
                lines.append(f"  - _{r_reason}_")
        if len(rejected) > 10:
            lines.append(f"- _…and {len(rejected) - 10} more_")
        lines.append("\n</details>\n")

    if status == "claude_failed":
        lines.extend(_agent_failure_blocks(
            agent="claude",
            log_tail=result.get("claude_log_tail") or "",
            claude_calls=claude_calls,
        ))

    if status == "aborted_secret_in_payload":
        scrubber_path = result.get("scrubber_path") or "(unknown)"
        scrubber_patterns = result.get("scrubber_patterns") or []
        patterns_str = (
            ", ".join(f"`{p}`" for p in scrubber_patterns)
            if scrubber_patterns else "(unspecified)"
        )
        lines.append("\n### 🛑 Outbound credential-scrubber fired\n")
        lines.append(
            f"The assembled payload included content matching credential "
            f"patterns ({patterns_str}) in field `{scrubber_path}`. The API "
            f"request was aborted at the runner — no body left the host. "
            f"This is leak-prevention, not a content issue.\n"
        )
        lines.append(
            "**To investigate**: check the run logs above for the "
            "`outbound-payload scrubber matched` line, which lists "
            "per-pattern match lengths.\n"
        )
        lines.append(
            "* A match length near the regex minimum (32–40 chars for the "
            "bearer pattern) typically indicates a prose false positive — "
            "tighten the pattern or fix the body-assembly path producing "
            "the matching text.\n"
            "* A match length of 40+ chars typically indicates a real "
            "credential. Identify which upstream (agent self-review, test "
            "stdout, pre-flight reasoning) produced it and add a redaction "
            "or env-strip there.\n"
        )

    if err:
        lines.append("\n**Error**\n")
        lines.append(f"```\n{err[:2000]}\n```\n")

    try:
        with open(summary_path, "a") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        log.warning(f"Could not write to $GITHUB_STEP_SUMMARY: {e}")


def _post_run_telemetry(result: dict, target: "Target") -> None:
    """Best-effort POST of this run's telemetry to the engine.

    Captures the per-run output into the engine's ``recommendation_runs`` table
    for later analysis. Never raises and never blocks: any
    failure (no API key, endpoint unreachable, non-2xx) is logged and
    swallowed so a telemetry outage can't break a customer's run. Skipped
    outside GitHub Actions, where there's no ``GITHUB_RUN_ID`` to dedup on.

    The fields are read defensively with ``.get`` — a skipped run that never
    reached the selection pass simply sends nulls for the pool / selection /
    coverage fields.
    """
    run_id_raw = os.environ.get("GITHUB_RUN_ID")
    if not run_id_raw:
        log.debug("  run telemetry: no GITHUB_RUN_ID (local run?); skipping POST")
        return
    try:
        run_id = int(run_id_raw)
    except (TypeError, ValueError):
        log.debug(f"  run telemetry: GITHUB_RUN_ID {run_id_raw!r} not an int; "
                  f"skipping POST")
        return

    reasoning = result.get("selection_reasoning") or ""
    payload = {
        "run_id": run_id,
        # The chosen candidate's engine paper_recommendation UUID, threaded
        # onto `result` when an in-pool candidate is picked; null for skips and
        # out-of-pool (search-surfaced) picks, which have no pool row.
        "recommendation_id": result.get("recommendation_id"),
        "target_repo": target.repo,
        "status": result.get("status"),
        # The run's origin, so downstream analysis can separate standard
        # Outrider runs from other run sources. Override via REMYX_RUN_SOURCE.
        "source": os.environ.get("REMYX_RUN_SOURCE", "outrider"),
        "artifact_url": (
            result.get("pr_url")
            or result.get("issue_url")
            or result.get("discussion_comment_url")
        ),
        "broad_pool_size": result.get("broad_pool_size"),
        "refine_pool_size": result.get("refine_pool_size"),
        "candidates_considered": result.get("candidates_considered"),
        "refine_queries": result.get("refine_queries"),
        "license_class_counts": result.get("license_class_counts"),
        "candidate_enrichment": result.get("candidate_enrichment"),
        "selection_reasoning_excerpt": reasoning[:2048] or None,
        "selection_integration_shape": result.get("selection_integration_shape"),
        "selection_coverage": result.get("selection_coverage"),
        "selection_context_efficiency": result.get("selection_context_efficiency"),
        # Per-rejected-candidate structured rationale. Engine-side
        # ``recommendation_runs.selection_rejected`` (JSONB) accumulates
        # these for cross-customer analysis: baseline code-availability
        # rate, per-(paper, customer) rejection patterns, deeper-research
        # prioritization. Title is
        # omitted from the wire payload — engine resolves it from
        # ``arxiv_id``; per-rejection reason is truncated to keep payload
        # bounded on rich runs.
        "selection_rejected": _compact_selection_rejected_for_telemetry(
            result.get("selection_rejected")
        ),
        "cost_usd": result.get("cost_usd"),
        "input_tokens": result.get("input_tokens"),
        "output_tokens": result.get("output_tokens"),
        "cache_read_input_tokens": result.get("cache_read_input_tokens"),
        "claude_calls": result.get("claude_calls"),
        "num_turns": result.get("num_turns"),
        # Coding-agent identity + backend / cost-basis annotations. These
        # let SQL slice telemetry by which backend served a run (Anthropic
        # vs z.ai (GLM) vs Bedrock vs ...) and whether the dollar figure
        # came from the CLI's envelope or a backend-specific rate table.
        "agent": result.get("agent"),
        "model_backend": result.get("model_backend"),
        "cost_basis": result.get("cost_basis"),
        # Successful envelopes that arrived without a `usage` block.
        # Non-zero means the run's token totals are an under-count.
        "envelopes_without_usage": result.get("envelopes_without_usage"),
        # Preflight / routing decisions made before (or in lieu of) the
        # implementation pass. Together these capture *why* a run ended
        # up as PR / Issue / preflight-Issue / no-integration.
        "preflight_decision": result.get("preflight_decision"),
        "audit_anchor": result.get("audit_anchor"),
        "search_method": result.get("search_method"),
        "search_method_resolution": result.get("search_method_resolution"),
        "selection_proposed_call_site": (
            (result.get("selection_proposed_call_site") or "")[:512] or None
        ),
        "selection_team_direction_signal":
            result.get("selection_team_direction_signal"),
        "selection_contract_match": result.get("selection_contract_match"),
        "selection_migration_cost": result.get("selection_migration_cost"),
        "selection_external_arxiv_id": result.get("selection_external_arxiv_id"),
        "selection_external_query_used": (
            (result.get("selection_external_query_used") or "")[:512] or None
        ),
        "selection_code_override_justification": (
            (result.get("selection_code_override_justification") or "")[:1024]
            or None
        ),
        # Refinement-chain stage outcomes — observability for self-review,
        # convention pass, draft-state flip, test integration gate,
        # diff-risk band, lint/test status, etc. Bool-typed fields stay
        # bool/None; counters stay int/None; verbose lists are bounded.
        "chain": result.get("chain"),
        "self_review": result.get("self_review"),
        "needs_judgment": result.get("needs_judgment"),
        "pr_body_updated": result.get("pr_body_updated"),
        "pr_body_rationale": (
            (result.get("pr_body_rationale") or "")[:2048] or None
        ),
        "draft_dropped": result.get("draft_dropped"),
        "test_integration_gate": result.get("test_integration_gate"),
        "tests_status": result.get("tests_status"),
        "test_status": result.get("test_status"),
        "tests_touch_existing": result.get("tests_touch_existing"),
        "stub_density": result.get("stub_density"),
        "integration_violations": _compact_string_list_for_telemetry(
            result.get("integration_violations")
        ),
        "lint_status": result.get("lint_status"),
        "lint_issues": result.get("lint_issues"),
        "diff_risk_band": result.get("diff_risk_band"),
        "diff_risk_score": result.get("diff_risk_score"),
        "diff_risk_factors": result.get("diff_risk_factors"),
        "coverage_summary": (
            (result.get("coverage_summary") or "")[:2048] or None
        ),
        # Issue-route convention-pass outcomes.
        "picked_template": result.get("picked_template"),
        "templates_eligible": result.get("templates_eligible"),
        "templates_filtered_kinds": result.get("templates_filtered_kinds"),
        "templates_found": result.get("templates_found"),
        "existing_issue_state": result.get("existing_issue_state"),
        "existing_issue_url": result.get("existing_issue_url"),
        # Repo / file-touch telemetry.
        "files_touched": result.get("files_touched"),
        "touched_py_files": result.get("touched_py_files"),
        "touched_test_files": result.get("touched_test_files"),
        "files_dropped_out_of_scope": result.get("files_dropped_out_of_scope"),
        "package_manager": result.get("package_manager"),
        "deps_installed": result.get("deps_installed"),
        "deps_install_summary": result.get("deps_install_summary"),
        # Paper metadata snapshot — arxiv_id is also reachable via the
        # recommendation_id FK, but having it on the row makes SQL slicing
        # by paper trivial.
        "arxiv_id": result.get("arxiv_id"),
        "upstream_repo": result.get("upstream_repo"),
        "paper_license": result.get("paper_license"),
        "license_class": result.get("license_class"),
        "license_compat": result.get("license_compat"),
        "reference_url": result.get("reference_url"),
        "error": result.get("error"),
    }
    try:
        _remyx_post("/api/v1.0/outrider/runs", payload)
        log.info(f"  run telemetry posted (run_id={run_id}, "
                 f"status={payload['status']})")
    except Exception as e:
        log.warning(f"  run telemetry POST failed (non-fatal): {str(e)[:200]}")


def run_refinement_chain(target: Target, pr_number: int) -> dict:
    """Continue a freshly-filed recommend-mode PR into the refinement chain.

    Runs the three phases sequentially on ``pr_number`` —
    ``run_fidelity_audit`` → ``run_convention_pass`` → ``run_test_gate`` —
    so the chain runs by default inside the recommend workflow run rather
    than requiring the customer to deploy the standalone
    outrider-fidelity/convention/test workflows.

    Convention and test only run once fidelity actually audited the PR
    (status ``fidelity_audited*``); a skip/failure short-circuits the
    chain since the downstream phases have nothing meaningful to act on.
    Convention and test run as a pair after that — the test gate is what
    flips the draft to ready, so it runs regardless of the convention
    phase's individual outcome.

    The phase runners read the PR number from ``INPUT_PR_NUMBER`` (the same
    seam the standalone ``mode: <phase>`` workflows use), so we set it here
    before delegating. Returns a dict of per-phase statuses for telemetry
    and the run summary.
    """
    log.info(f"  → continuing into refinement chain on PR #{pr_number}")
    # The phase runners read INPUT_PR_NUMBER from the environment (shared
    # seam with the standalone workflow_dispatch chain). Set it so the
    # inline call targets the PR recommend mode just opened.
    os.environ["INPUT_PR_NUMBER"] = str(pr_number)

    chain: dict = {"pr_number": pr_number}

    log.info("  ─── chain phase: fidelity audit ───")
    fidelity = run_fidelity_audit(target)
    chain["fidelity_status"] = fidelity.get("status")
    if not str(fidelity.get("status", "")).startswith("fidelity_audited"):
        log.info(
            f"  ↪ fidelity did not audit (status={fidelity.get('status')!r}); "
            f"skipping convention + test phases"
        )
        return chain

    log.info("  ─── chain phase: convention pass ───")
    convention = run_convention_pass(target)
    chain["convention_status"] = convention.get("status")

    log.info("  ─── chain phase: test gate ───")
    test = run_test_gate(target)
    chain["test_status"] = test.get("status")
    chain["draft_dropped"] = test.get("draft_dropped", False)

    return chain


def main():
    # Mode dispatch: "recommend" is the classic
    # scout-and-implement run; "weekly-summary" aggregates the past week
    # and posts a digest comment to the configured Discussion. Customers
    # opt in via a second scheduled job passing `mode: weekly-summary`.
    mode = (
        os.environ.get("REMYX_MODE")
        or os.environ.get("INPUT_MODE")
        or "recommend"
    ).strip().lower().replace("_", "-")
    if mode not in (
        "recommend", "weekly-summary", "fidelity", "convention", "test",
        "issue-convention",
    ):
        log.error(f"Unknown mode {mode!r}; must be 'recommend', "
                  f"'weekly-summary', 'fidelity', 'convention', 'test', "
                  f"or 'issue-convention'.")
        sys.exit(2)

    target = build_target_from_env()
    if target.pin_arxiv and target.search_method:
        log.error(
            "pin-arxiv and pin-method are mutually exclusive; set one or "
            "the other, not both."
        )
        sys.exit(2)
    # Plumb the configured backend URL through to the Claude Code subprocess
    # via the existing _CLAUDE_ENV_WHITELIST passthrough. Set it on the
    # parent process env so every Claude invocation in this run inherits the
    # routing — single point of truth, no need to thread `target` into the
    # subprocess-env builder. The customer's ANTHROPIC_BASE_URL set directly
    # in the workflow `env:` block (the pre-input workaround) still works;
    # this input is the documented surface.
    if target.model_base_url:
        os.environ["ANTHROPIC_BASE_URL"] = target.model_base_url
        backend_name, backend_rates = _detect_backend(target.model_base_url)
        if backend_rates is not None:
            cost_note = f"cost computed from {backend_name} rate table"
        else:
            cost_note = (f"cost telemetry is Anthropic-rate estimate "
                         f"(no rate table for {backend_name})")
        log.info(f"  routing Claude Code via {target.model_base_url} "
                 f"({cost_note})")
    # Validate the auth env shape before any agent call. Catches the
    # common misconfigurations (missing var, literal '-' from
    # gh-secret-set ambiguity, whitespace, mutual-exclusion on non-
    # default backends) that otherwise surface as opaque 401s after a
    # full run's worth of clone + spec-bundle work.
    auth_ok, auth_warnings = _validate_claude_auth_env()
    for w in auth_warnings:
        log.warning("  ⚠ auth check: %s", w)
    if not auth_ok:
        sys.exit(2)
    log.info(f"=== {target.repo} ===")
    log.info(f"  interest_id={target.interest_id}")
    if mode == "weekly-summary":
        log.info("  mode=weekly-summary")
        runner = run_weekly_summary
        failure_status = "weekly_summary_failed"
    elif mode == "fidelity":
        pr_number = os.environ.get("INPUT_PR_NUMBER", "").strip()
        log.info(f"  mode=fidelity  pr_number={pr_number}")
        runner = run_fidelity_audit
        failure_status = "fidelity_failed_claude"
    elif mode == "convention":
        pr_number = os.environ.get("INPUT_PR_NUMBER", "").strip()
        log.info(f"  mode=convention  pr_number={pr_number}")
        runner = run_convention_pass
        failure_status = "convention_failed_extraction"
    elif mode == "test":
        pr_number = os.environ.get("INPUT_PR_NUMBER", "").strip()
        log.info(f"  mode=test  pr_number={pr_number}")
        runner = run_test_gate
        failure_status = "test_failed_setup"
    elif mode == "issue-convention":
        issue_number = os.environ.get("INPUT_ISSUE_NUMBER", "").strip()
        log.info(f"  mode=issue-convention  issue_number={issue_number}")
        runner = run_issue_convention_pass
        failure_status = "issue_convention_failed_claude"
    else:
        log.info(f"  min_confidence={target.min_confidence}  "
                 f"draft_mode={target.draft_mode}  "
                 f"rate_limit_days={target.rate_limit_days}")
        runner = process_target
        failure_status = "error"

    _reset_run_cost()
    try:
        result = runner(target)
    except Exception as e:
        log.exception(f"  ✗ unhandled error: {e}")
        result = {"repo": target.repo, "status": failure_status, "error": str(e)}

    # Inline refinement chain: once recommend mode files a draft
    # PR, continue sequentially into fidelity → convention → test on that PR
    # so the chain runs by default — no standalone workflow files required.
    # Gated on `chain_enabled` (action input `chain`, default true) and on a
    # PR actually having opened. Runs before the cost-totals block so the
    # chain's Claude spend rolls into this run's reported totals.
    if (
        mode == "recommend"
        and target.chain_enabled
        and str(result.get("status", "")).startswith("pr_opened")
        and result.get("pr_number")
    ):
        try:
            result["chain"] = run_refinement_chain(target, result["pr_number"])
        except Exception as e:
            log.exception(f"  ✗ refinement chain failed (non-fatal): {e}")
            result["chain"] = {"error": str(e)}
    # Issue-route convention pass: when recommend mode opens an Issue
    # instead of a PR (preflight downgrade, self-review orphan, substitution,
    # etc.), run the Issue-route equivalent of Phase B to align the Issue
    # body to the target repo's ISSUE_TEMPLATE shape. No Phase A (no diff to
    # audit) and no Phase C (no tests to run / no draft state); this is a
    # single body-rewrite call.
    elif (
        mode == "recommend"
        and target.chain_enabled
        and str(result.get("status", "")).startswith("issue_opened")
        and result.get("issue_number")
    ):
        try:
            os.environ["INPUT_ISSUE_NUMBER"] = str(result["issue_number"])
            result["chain"] = {
                "issue_number": result["issue_number"],
                "issue_convention_status": run_issue_convention_pass(target).get("status"),
            }
        except Exception as e:
            log.exception(f"  ✗ issue-convention pass failed (non-fatal): {e}")
            result["chain"] = {"error": str(e)}
    elif mode == "recommend" and not target.chain_enabled:
        log.info("  chain disabled (chain: false); skipping refinement chain")

    # Token/cost totals across every Claude pass this run, captured even when
    # process_target raised.
    result["cost_usd"] = round(_RUN_COST["cost_usd"], 4)
    result["input_tokens"] = _RUN_COST["input_tokens"]
    result["output_tokens"] = _RUN_COST["output_tokens"]
    result["cache_read_input_tokens"] = _RUN_COST["cache_read_input_tokens"]
    result["claude_calls"] = _RUN_COST["claude_calls"]
    result["num_turns"] = _RUN_COST["num_turns"]
    # Coding-agent and model-backend identity. Fixed to Claude Code today;
    # the agent field is the seam for future per-CLI adapters
    # (Aider / Goose / Codex). model_backend tracks the API endpoint the
    # agent talked to — "Anthropic" by default, "z.ai (GLM)" / "AWS
    # Bedrock" / etc. when ANTHROPIC_BASE_URL routes elsewhere. cost_basis
    # tells the step summary whether cost was computed from a known rate
    # card or trusted from the CLI's envelope.
    result["agent"] = "Claude Code"
    result["model_backend"] = _RUN_COST.get("model_backend", "Anthropic")
    result["cost_basis"] = _RUN_COST.get("cost_basis", "claude_code_envelope")
    result["envelopes_without_usage"] = _RUN_COST.get(
        "envelopes_without_usage", 0
    )
    log.info(f"  cost: ${result['cost_usd']} "
             f"({result['input_tokens']} in / {result['output_tokens']} out "
             f"tokens, {result['claude_calls']} claude calls) "
             f"via {result['agent']} → {result['model_backend']}")

    print("\n=== RUN SUMMARY ===")
    print(json.dumps(result, indent=2))

    # Surface key outputs to the GitHub Actions runner so consuming
    # workflows can branch on the result (e.g., notify Slack on
    # pr_opened, alert on rejected_path_violations).
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        try:
            with open(github_output, "a") as f:
                f.write(f"status={result.get('status', 'unknown')}\n")
                if "pr_url" in result:
                    f.write(f"pr_url={result['pr_url']}\n")
                if "pr_number" in result:
                    f.write(f"pr_number={result['pr_number']}\n")
                # Inline refinement-chain phase outcomes (recommend mode with
                # chain enabled). Lets downstream steps branch on whether the
                # draft was dropped to ready, etc.
                chain = result.get("chain") or {}
                if "fidelity_status" in chain:
                    f.write(f"chain_fidelity_status={chain['fidelity_status']}\n")
                if "convention_status" in chain:
                    f.write(f"chain_convention_status={chain['convention_status']}\n")
                if "test_status" in chain:
                    f.write(f"chain_test_status={chain['test_status']}\n")
                if "draft_dropped" in chain:
                    f.write(f"chain_draft_dropped={str(chain['draft_dropped']).lower()}\n")
                if "issue_url" in result:
                    f.write(f"issue_url={result['issue_url']}\n")
                if "discussion_comment_url" in result:
                    f.write(
                        f"discussion_comment_url="
                        f"{result['discussion_comment_url']}\n"
                    )
                if "arxiv" in result:
                    f.write(f"arxiv={result['arxiv']}\n")
                if "tier" in result:
                    f.write(f"tier={result['tier']}\n")
                if "candidates_considered" in result:
                    f.write(f"candidates_considered={result['candidates_considered']}\n")
                if "selection_rejected" in result:
                    f.write(f"selection_rejected={len(result['selection_rejected'])}\n")
                f.write(f"cost_usd={result.get('cost_usd', 0)}\n")
                f.write(f"input_tokens={result.get('input_tokens', 0)}\n")
                f.write(f"output_tokens={result.get('output_tokens', 0)}\n")
        except OSError as e:
            log.warning(f"Could not write to $GITHUB_OUTPUT: {e}")

    # Render a human-readable summary into $GITHUB_STEP_SUMMARY. This is
    # the markdown panel GitHub pins at the top of every workflow run
    # page — by far the most visible surface, and the one place
    # customers see cost telemetry without wiring downstream steps.
    _write_step_summary(result)

    # Best-effort: capture this run's telemetry to the engine for analysis.
    # Only recommend-mode runs map onto the run schema; weekly-summary and
    # fidelity-audit runs are skipped (they have their own outcome surfaces:
    # Discussion comment and PR Coverage section respectively). Never blocks
    # the run.
    if mode == "recommend":
        _post_run_telemetry(result, target)

    # Non-zero exit on genuine failures so the workflow step fails visibly
    # (a green run with no PR/Issue previously masked claude_failed). Issues,
    # skips, and PRs stay green.
    if result.get("status") in FAILURE_EXIT_STATUSES:
        sys.exit(1)


if __name__ == "__main__":
    main()
