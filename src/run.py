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
# Permissive on the target package because §2 of the "ready-to-ship PRs"
# work requires the agent to be able to add small wiring edits to
# existing files (e.g. a 3-line hook in evaluation.py). The post-hoc
# check_integration() validator caps how much can change per existing
# file and rejects runs that only add freestanding modules.
# Python source anywhere in the repo is editable: a wiring edit has to be
# able to reach the real call site, which often lives outside the target
# package (a pipeline/stage driver, an entrypoint module, etc.), and we
# don't want to hard-code any one repo's directory layout. Infra files that
# happen to sit alongside source — container builds, shell scripts,
# dependency/build manifests, CI config — are blocked by ROLE in
# ALWAYS_BLOCKED, which takes precedence. The 50-line edit cap and the
# invocation check in check_integration() keep edits surgical and honest.
DEFAULT_ALLOWLIST_GLOBS = [
    "*.py",
    ".remyx-recommendation/**",
    "**/*.md",               # Markdown anywhere (README, CHANGELOG, docs/,
                             # ADR notes). Diff is text-only and reviewable.
                             # The 50-line edit cap still applies to existing
                             # files; new docs files are uncapped but still
                             # need to be referenced somewhere to land in a PR.
]

# Cap on additions+deletions per pre-existing file. Keeps wiring edits
# small and surgical; rejects runs where Claude rewrote an unrelated
# module under the cover of "integration".
MAX_LINES_PER_EXISTING_FILE = 50

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

Open as Issue if any of these is true:

  - The paper's contribution requires infrastructure the codebase lacks
    (a trainer when the repo is inference-only, a dataset format the
    repo never touches, external checkpoints with no path to load).
  - You cannot point at a specific existing call site to modify.
  - Your implementation would be a freestanding module that no existing
    code imports or calls — i.e. it could be deleted without breaking
    or altering anything in the repo.
  - You'd be inventing the integration rather than slotting into one.

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

Route to ISSUE only if any of these is likely true of THAT scoped slice:

  - Even the scoped implementation requires infrastructure that isn't in
    the repo (a trainer when the repo is inference-only, a data format the
    repo never touches, checkpoints with no loader path).
  - There is no clear call site — no existing module that naturally hosts
    even the scoped contribution (and the selection rationale, if present,
    names none that hold up against the layout).
  - The most realistic implementation would be a freestanding module that
    no existing code would call.

Otherwise route to PR.

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

**Four legitimate integration shapes — classify each candidate you
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
  ]
}

__DISCHARGED_PAPERS__--- Candidates (highest relevance first) ---

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
                     deliberate boundaries, not as what you 'failed' to do.>"
}

Be ruthless about reachability. If the only thing that calls your new code
is a test you added (or nothing at all), set is_orphan=true — the product
never exercises it. If a pre-existing entry point (a pipeline/stage driver,
a CLI, an existing module) now invokes your new code, set is_orphan=false.
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
    # Outbound credential-scrubber abort. Not a graceful skip — the
    # operator needs to investigate the body-assembly path (whatever
    # upstream content produced credential-shaped text). Surfaces red
    # in CI rather than silently green so the signal isn't lost.
    "aborted_secret_in_payload",
}


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
    """
    if rec.paper_title:
        return slugify(rec.paper_title)
    return rec.arxiv_id or "paper-recommendation"


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
3. **Bias your refine queries toward SIMPLIFY / REPLACE / ACCELERATE
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
4. For each under-represented theme that's a genuine fit, draft a single
   keyword-style search query — 4-8 terms, no quotes, no boolean
   operators. The Remyx /search/assets backend is keyword-matched, so
   the strongest signal words should appear first.
5. Output 1-3 queries (no more than 3). Quality beats quantity — if the
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
    prompt = (
        _AUDIT_PROMPT_TEMPLATE
        .replace("__REPO_FULLNAME__", target.repo)
        .replace("__INTEREST_NAME__", interest_name or "(unnamed)")
        .replace("__INTEREST_CONTEXT__", interest_block)
        .replace("__README_CHARS__", "2000")
        .replace("__README__", readme_block)
        .replace("__RECENT_N__", str(len(recent_issues)))
        .replace("__RECENT_ISSUES__", recent_block)
        .replace("__BROAD_N__", str(len(broad_candidates)))
        .replace("__BROAD_BRIEF__", _render_broad_brief(broad_candidates))
    )
    timeout_s = int(os.environ.get("REMYX_AUDIT_TIMEOUT_S", "120"))
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


def open_remyx_artifact_exists(target: Target) -> bool:
    """Return True if any **open** Remyx Recommendation PR or Issue exists
    on the target repo.

    The cadence guard's job is to avoid stacking unresolved work on a
    maintainer who hasn't engaged with the prior recommendation. Once
    they merge or close it, the channel is clear and the next run can
    proceed — engagement IS the signal. Per-paper dedup (a separate
    gate) already prevents the same recommendation from re-surfacing,
    so the cadence guard doesn't need its own time window.

    `target.rate_limit_days > 0` enables this gate; `<= 0` disables it.
    The numeric value is otherwise ignored — preserved as an on/off bit
    so existing `outrider.yml` workflows continue to work unchanged.
    """
    if target.rate_limit_days <= 0:
        return False

    # PRs — identified by branch prefix. Only open PRs block.
    prs = gh_api(
        "GET", f"/repos/{target.repo}/pulls?state=open&per_page=50"
    ) or []
    for pr in prs:
        ref = pr.get("head", {}).get("ref", "")
        if not ref.startswith(BRANCH_PREFIX):
            continue
        log.info(f"  open Remyx PR exists: {pr['html_url']}")
        return True

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
        log.info(f"  open Remyx Issue exists: {it['html_url']}")
        return True

    return False


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


def write_spec_bundle(
    workdir: Path, target: Target, rec: Recommendation, package: str,
    selection_note: str = "",
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
    (bundle / "SPEC.md").write_text(_SPEC_MD_TEMPLATE.format(
        paper_title=rec.paper_title,
        arxiv_id=rec.arxiv_id,
        tier=rec.tier,
        relevance_score=rec.relevance_score,
        interest_name=rec.interest_name or "(unnamed interest)",
        interest_context_block=interest_block,
        reasoning=rec.reasoning or "(no reasoning provided)",
        selection_block=selection_block,
        suggested_experiment=rec.suggested_experiment or "(none)",
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

    (bundle / "INVOCATION.md").write_text(_INVOCATION_MD_TEMPLATE.format(
        package=package,
        attribution_url=CANONICAL_ATTRIBUTION_URL,
        issue_fallback_filename=ISSUE_FALLBACK_FILENAME,
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
    )
    _RUN_REFINE_QUERIES.clear()
    _BOT_TOKEN.update(attempted=False, token="", permissions={})


def _record_claude_usage(env: dict) -> None:
    """Accumulate one `claude --output-format json` envelope's usage."""
    _RUN_COST["claude_calls"] += 1
    _RUN_COST["cost_usd"] += float(env.get("total_cost_usd") or 0.0)
    _RUN_COST["num_turns"] += int(env.get("num_turns") or 0)
    u = env.get("usage") or {}
    _RUN_COST["input_tokens"] += int(u.get("input_tokens") or 0)
    _RUN_COST["output_tokens"] += int(u.get("output_tokens") or 0)
    _RUN_COST["cache_read_input_tokens"] += int(
        u.get("cache_read_input_tokens") or 0
    )


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


def select_recommendation(
    workdir: Path, package: str, candidates: list[Recommendation],
    target: "Target | None" = None,
    timeout_s: int | None = None,
    discharged_issues: list[dict] | None = None,
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
        timeout_s = int(os.environ.get("REMYX_SELECTION_TIMEOUT_S", "480"))
    log.info(
        f"  → agentic selection over {len(candidates)} candidates "
        f"(max-turns={max_turns}, timeout={timeout_s}s)"
    )
    ok, output, events = _run_claude_oneshot_streaming(
        workdir, prompt, timeout_s, max_turns=max_turns,
    )
    if not ok:
        log.warning(f"  selection call failed: {output[:200]}; "
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
      * Each modified existing file's net change ≤
        MAX_LINES_PER_EXISTING_FILE lines.
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

    for p in paths:
        if _file_is_new(workdir, p):
            continue
        added, deleted = _diff_line_changes(workdir, p)
        if added + deleted > MAX_LINES_PER_EXISTING_FILE:
            violations.append(
                f"oversized edit to existing file {p}: +{added}/-{deleted} "
                f"> {MAX_LINES_PER_EXISTING_FILE}"
            )

    # Invocation check. Every newly-added callable, keyed by the file that
    # defines it, must be called from some OTHER changed file.
    changed_py = [p for p in paths if p.endswith(".py")]
    added_by_file: dict[str, set[str]] = {}
    for p in changed_py:
        added = _added_callables(workdir, p)
        if added:
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
    """
    r = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=workdir, capture_output=True, text=True, check=False,
    )
    return r.stdout.strip() or "main"


def open_pr(
    target: Target, branch: str, title: str, body: str, draft: bool,
    base: str = "main",
) -> str:
    """Open a PR on the target repo; returns the PR URL."""
    log.info(f"  → opening {'draft' if draft else ''} PR on {target.repo} "
             f"(base={base})")
    pr = gh_api("POST", f"/repos/{target.repo}/pulls", {
        "title": title,
        "head": branch,
        "base": base,
        "body": body,
        "draft": draft,
    })
    return pr["html_url"]


def open_issue(
    target: Target, title: str, body: str, *, footer_override: str = "",
) -> str:
    """Open a discussion Issue on the target repo. Returns the issue URL.

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
    return issue["html_url"]


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
    # Sanity check: make sure local HEAD still equals origin/<base_branch>
    # before we branch. If Claude (or pytest) disturbed the git state during
    # the session — `git checkout --orphan`, `rm -rf .git`, `git init`,
    # whatever — local HEAD can diverge from the remote default, and the
    # subsequent `git checkout -b branch` produces a root-commit branch with
    # no history in common with it. The PR-creation API then rejects with
    # HTTP 422. Fail fast with a clear error instead. (base_branch is the
    # repo's real default — `main` or `master` — not a hardcoded `main`.)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workdir, capture_output=True, text=True, check=True,
    ).stdout.strip()
    try:
        remote_sha = subprocess.run(
            ["git", "rev-parse", f"origin/{base_branch}"],
            cwd=workdir, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        remote_sha = ""
    if not remote_sha or head_sha != remote_sha:
        raise RuntimeError(
            f"local HEAD ({head_sha[:8]}) doesn't match "
            f"origin/{base_branch} ({(remote_sha or 'MISSING')[:8]}) — git "
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
) -> str:
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
    #    checkout. Skip if any *open* Remyx artifact exists on the
    #    target. Engagement (merge/close) IS the signal that the
    #    channel is clear; the guard exists to avoid stacking
    #    unresolved work, not to enforce a time window. Per-paper
    #    dedup (further down) handles same-recommendation retries.
    if open_remyx_artifact_exists(target):
        result["status"] = "skipped_open_artifact"
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
    candidates = query_remyx_candidates(target)
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
        log.info(f"  detected package: {package}  default branch: {default_branch}")

        pinned_idx = None
        if target.pin_arxiv:
            pinned_idx = next(
                (i for i, c in enumerate(viable) if c.arxiv_id == target.pin_arxiv),
                None,
            )
            if pinned_idx is None:
                log.warning(f"  pin-arxiv {target.pin_arxiv!r} not in viable "
                            f"pool; falling back to the selection pass")
        if pinned_idx is not None:
            rec = viable[pinned_idx]
            result["selection_reasoning"] = (
                f"(pinned via pin-arxiv={target.pin_arxiv})"
            )
            log.info(f"  ✓ pinned candidate [{pinned_idx}] {rec.paper_title[:50]}…")
        else:
            selection = select_recommendation(
                workdir, package, viable, target=target,
                discharged_issues=open_issues,
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
                issue_url = _open_downgrade_issue(
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
                # Substitution routing — replacement / simplification
                # recommendations need dep changes the PR guardrails block
                # (requirements.txt, factory wiring, often test fixtures).
                # Open as an Issue with the contract analysis instead of a
                # half-built PR.
                shape = (selection.get("integration_shape") or "addition").lower().strip()
                result["selection_integration_shape"] = shape
                result["selection_contract_match"] = (
                    selection.get("contract_match", "")
                )
                result["selection_migration_cost"] = (
                    selection.get("migration_cost", "")
                )
                if shape in ("replacement", "simplification"):
                    contract_match = selection.get("contract_match", "")
                    migration_cost = selection.get("migration_cost", "")
                    shape_label = (
                        "drop-in replacement"
                        if shape == "replacement"
                        else "pipeline simplification"
                    )
                    # Engineering axis as its own section, adjacent to the
                    # license section.
                    engineering_section = _render_engineering_section(
                        integration_shape=shape_label,
                        contract_match=contract_match or "(none reported)",
                        migration_cost=migration_cost or "(none reported)",
                    )
                    detail = (
                        f"_Selection reasoning_: {selection.get('reasoning', '')}\n\n"
                        f"This swap touches dependency files and existing module "
                        f"boundaries — changes that fall outside Outrider's "
                        f"auto-PR guardrails. Opening as an Issue so the team "
                        f"can decide whether to merge the upgrade."
                    )
                    _record_verdict_fields(result, rec)
                    issue_url = _open_downgrade_issue(
                        target, rec,
                        reason=f"Selection identified a {shape_label} candidate",
                        detail=detail,
                        engineering_section=engineering_section,
                        selection_note=selection.get("reasoning", ""),
                        selection_rejected=result.get("selection_rejected"),
                        footer_override=(
                            f"_Opened by the [Remyx Recommendation]"
                            f"({CANONICAL_ATTRIBUTION_URL}) orchestrator. "
                            f"Selection identified a {shape_label} that "
                            f"touches dependency files / module boundaries "
                            f"outside the PR guardrails — routed to Issue "
                            f"so the team can decide on the swap._"
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
                    })
                    log.info(f"  ✓ issue_opened_substitution ({shape}): {issue_url}")
                    return result
            else:
                # The broad pool from `/papers/recommended` isn't guaranteed
                # to be in descending-relevance order at index 0 — the
                # engine occasionally seeds the list with diversity picks.
                # Selecting `viable[0]` blindly on fallback can land the
                # *lowest*-relevance candidate in the pool. Pick the actual
                # highest-relevance candidate so a fallback at least gives
                # the maintainer the engine's strongest signal.
                rec = max(viable, key=lambda c: c.relevance_score)
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

        # 5. Spec bundle for the chosen candidate. Thread the selection
        # rationale through so pre-flight and the implementer evaluate the
        # same scoped framing the selection pass reasoned about.
        branch = format_branch_name(rec)
        write_spec_bundle(
            workdir, target, rec, package,
            selection_note=result.get("selection_reasoning", ""),
        )

        # 5.5. Pre-flight Issue routing (§6). Cheap Claude pass that
        # decides PR vs Issue before we spend the implementation budget.
        # Failures here fall through — they don't block the PR path.
        preflight = preflight_routing(workdir, package)
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
            issue_url = _open_downgrade_issue(
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
            issue_body = (
                f"**Recommended paper**: "
                f"[{rec.paper_title}](https://arxiv.org/abs/{rec.arxiv_id})\n"
                f"**Confidence**: {rec.tier} "
                f"(Remyx relevance {rec.relevance_score:.2f})\n"
                f"**Research interest**: {rec.interest_name or '(unnamed)'}\n"
                f"{_render_license_section(rec)}"
                f"\n---\n\n"
                f"{issue_body_inner}"
            )
            issue_url = open_issue(target, issue_title, issue_body)
            result["status"] = "issue_opened"
            result["issue_url"] = issue_url
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
            issue_url = _open_downgrade_issue(
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
            )
            result["status"] = "issue_opened_no_integration"
            result["issue_url"] = issue_url
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
            issue_url = _open_downgrade_issue(
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
            )
            result["status"] = "issue_opened_stub_density"
            result["issue_url"] = issue_url
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
            issue_url = _open_downgrade_issue(
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
            )
            result["status"] = "issue_opened_high_risk"
            result["issue_url"] = issue_url
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
                    issue_url = _open_downgrade_issue(
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
                    )
                    result["status"] = "issue_opened_no_test_integration"
                    result["issue_url"] = issue_url
                    return result

        # 9. Self-review (§4). Second Claude pass over the diff. Renders
        # a "What this PR actually does" section into the PR body; if the
        # new code is an orphan (unreachable from any production path), it
        # routes to Issue. This is a REACHABILITY check, not a triviality
        # one — stub density (§3) already covers "the code is too thin".
        review = self_review_diff(workdir)
        result["self_review"] = review or {}
        if review and review.get("is_orphan") is True:
            log.warning(
                "  ✗ self-review: new code is unreachable from any "
                "production path (orphan); downgrading to Issue"
            )
            summary = review.get("honest_summary") or ""
            # Promote the self-review summary out of italics into a
            # proper sub-heading — it's the most informative part of
            # the body and worth a glance, not a footnote.
            detail_body = (
                "On a second pass over the diff, the coding agent "
                "concluded that no pre-existing entry point or module "
                "invokes the new code — at most its own tests call it. "
                "That's an orphan: the product never exercises it. "
                "(This is about reachability, not whether the code is "
                "trivial — stub density is judged separately.)\n"
            )
            if summary:
                detail_body += (
                    f"\n### Self-review summary\n\n{summary}\n"
                )
            issue_url = _open_downgrade_issue(
                target, rec,
                reason="Self-review judged the new code an orphan (no production call path)",
                detail=detail_body,
                implementation_diff=_capture_implementation_diff(workdir),
                tldr=summary[:240] if summary else "",
                selection_note=result.get("selection_reasoning", ""),
                selection_rejected=result.get("selection_rejected"),
                footer_override=(
                    f"_Opened by the [Remyx Recommendation]"
                    f"({CANONICAL_ATTRIBUTION_URL}) orchestrator. "
                    f"Self-review caught that the new code is wired into "
                    f"a flag no production caller sets — routed to Issue "
                    f"so the team can decide whether to enable it._"
                ),
            )
            result["status"] = "issue_opened_self_review"
            result["issue_url"] = issue_url
            return result
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
        pr_url = open_pr(
            target, branch, pr_title, pr_body, draft=draft, base=default_branch
        )
        result["status"] = "pr_opened_draft" if draft else "pr_opened"
        result["pr_url"] = pr_url
        log.info(f"  ✓ {result['status']}: {pr_url}")
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


# ─── Refinement-pass: Phase A (fidelity audit) ─────────────────────────────
#
# REMYX-140 Phase A. Triggered by the `outrider:draft` label on a remyx-ai[bot]
# PR: diffs the PR's added/modified code against the paper's reference impl,
# produces a structured Coverage matrix (covered / deferred / deviation) and
# appends it to the PR body as a ``## Coverage`` section. Optionally patches
# obviously-mechanical paper-faithful fixes (constant values, missing variant
# flags) via a Claude Code agentic session and force-pushes.
#
# Inputs (workflow surface):
#   INPUT_PR_NUMBER  — PR number to audit (set by workflow from
#                      ``${{ github.event.pull_request.number }}``)
#
# Output (action surface):
#   - PR body updated with ``## Coverage`` section
#   - Label transition: outrider:draft → outrider:fidelity-done
#                                       or  outrider:needs-judgment
#                                       (if any item flagged needs-judgment)
#   - status: fidelity_audited / fidelity_skipped_* / fidelity_failed_*

FIDELITY_COVERAGE_SECTION_HEADER = "## Coverage"
FIDELITY_LABEL_DRAFT = "outrider:draft"
FIDELITY_LABEL_DONE = "outrider:fidelity-done"
FIDELITY_LABEL_NEEDS_JUDGMENT = "outrider:needs-judgment"

_REF_REPO_URL_RE = re.compile(
    r"https?://github\.com/([\w.-]+/[\w.-]+?)(?:/tree/[\w.-]+)?"
    r"(?:/blob/[\w./_-]+)?(?:[/#?][\w./?=&%#-]*)?",
    re.IGNORECASE,
)

_ARXIV_URL_RE = re.compile(
    r"https?://arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)


def _extract_reference_url_from_pr_body(body: str) -> tuple[str, str]:
    """Pull out (arxiv_id, reference_repo_url) from an Outrider PR body.

    Outrider PR bodies of the shape produced by ``build_pr_body`` and the
    similar axolotl/TRL templates have both a paper link
    (``https://arxiv.org/abs/<id>``) and a reference-impl link
    (``https://github.com/<owner>/<repo>[/blob/...]``) embedded in the
    Description section. Either may be absent. Returns ``("", "")`` for
    anything that can't be parsed; the caller decides how to proceed.

    The reference URL is normalised to the ``owner/repo`` clone URL
    (``https://github.com/<owner>/<repo>``) — the file-specific suffix is
    preserved separately if the caller needs a file path anchor (e.g., for
    the Coverage matrix's reference_location field).
    """
    arxiv_id = ""
    m = _ARXIV_URL_RE.search(body or "")
    if m:
        arxiv_id = m.group(1)
    # Pick the first github.com URL that isn't a remyxai/* internal link
    # (those are footer attribution, not reference impl).
    reference_url = ""
    for m in _REF_REPO_URL_RE.finditer(body or ""):
        owner_repo = m.group(1)
        if owner_repo.startswith("remyxai/") or owner_repo.startswith("smellslikeml/"):
            continue
        reference_url = f"https://github.com/{owner_repo}"
        break
    return arxiv_id, reference_url


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
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(ref_dir)],
            check=True,
            capture_output=True,
            timeout=180,
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


def _render_coverage_matrix(matrix: dict) -> str:
    """Render the Claude-emitted Coverage matrix as a markdown section
    to append to the PR body. Renders as a table with one row per item
    plus a summary line + needs-judgment callout."""
    summary = matrix.get("summary", "(no summary)")
    needs_judgment = bool(matrix.get("needs_judgment", False))
    items = matrix.get("items", [])

    lines = [FIDELITY_COVERAGE_SECTION_HEADER, ""]
    lines.append(f"_{summary}_")
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


def _append_or_replace_coverage(body: str, coverage_section: str) -> str:
    """Idempotently append (or replace) the ``## Coverage`` section in
    a PR body. If the body already has a ``## Coverage`` section (from
    a prior fidelity-audit run on the same PR), this replaces it with
    the new content so re-running the audit doesn't produce duplicate
    sections."""
    body = body or ""
    marker = FIDELITY_COVERAGE_SECTION_HEADER
    if marker in body:
        # Replace everything from the existing ## Coverage header up to
        # (but not including) the next H2-or-higher header or end of body.
        idx = body.index(marker)
        # Find the next ## or # header after the existing Coverage section
        tail = body[idx + len(marker):]
        next_h = re.search(r"\n(##? )", tail)
        if next_h:
            return body[:idx] + coverage_section + tail[next_h.start():]
        return body[:idx] + coverage_section
    # Append at the end with a separator
    sep = "\n\n---\n\n" if body.strip() else ""
    return body + sep + coverage_section


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
    """REMYX-140 Phase A — diff a remyx-ai[bot] PR against its reference
    impl, post a Coverage matrix to the PR body, and transition labels.

    The PR number is read from ``INPUT_PR_NUMBER`` (set by the
    workflow's ``pull_request`` event payload). The reference URL is
    extracted from the PR body using the standard arxiv + github
    embedding shape Outrider produces.

    Returns a status dict suitable for the action's $GITHUB_OUTPUT and
    the telemetry post. Status values:

        fidelity_audited                — happy path; Coverage matrix posted
        fidelity_audited_needs_judgment — happy path + at least one item
                                          flagged for human review
        fidelity_skipped_no_pr          — INPUT_PR_NUMBER empty
        fidelity_skipped_not_bot        — PR not authored by remyx-ai[bot]
        fidelity_skipped_no_reference   — couldn't extract a reference URL
        fidelity_failed_clone           — reference clone failed
        fidelity_failed_claude          — Claude call failed or returned
                                          unparseable JSON
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
    arxiv_id, reference_url = _extract_reference_url_from_pr_body(body)
    if not reference_url:
        result["status"] = "fidelity_skipped_no_reference"
        result["error"] = "no reference URL extracted from PR body"
        log.info(f"  ✗ no reference URL in PR #{pr_number} body; skipping")
        return result
    result["arxiv_id"] = arxiv_id
    result["reference_url"] = reference_url

    # Workdir layout: ./reference/ holds the cloned ref impl; Claude's
    # cwd will be the workdir root so it can Read/Glob/Grep into it.
    workdir = Path(tempfile.mkdtemp(prefix="outrider-fidelity-"))
    log.info(f"  → workdir: {workdir}")

    ok, ref_dir, err = _clone_reference_repo(reference_url, workdir)
    if not ok:
        result["status"] = "fidelity_failed_clone"
        result["error"] = err
        log.error(f"  ✗ {err}")
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

    coverage_section = _render_coverage_matrix(matrix)
    new_body = _append_or_replace_coverage(body, coverage_section)

    try:
        _update_pr_body(target, pr_number, new_body)
    except RuntimeError as e:
        result["status"] = "fidelity_failed_claude"
        result["error"] = f"PR body update failed: {e}"
        return result

    needs_judgment = bool(matrix.get("needs_judgment", False))
    if needs_judgment:
        _add_pr_label(target, pr_number, FIDELITY_LABEL_NEEDS_JUDGMENT)
        result["status"] = "fidelity_audited_needs_judgment"
    else:
        result["status"] = "fidelity_audited"

    # Transition the trigger label → fidelity-done so the convention pass
    # workflow picks it up.
    _add_pr_label(target, pr_number, FIDELITY_LABEL_DONE)
    _remove_pr_label(target, pr_number, FIDELITY_LABEL_DRAFT)

    result["items_count"] = len(matrix.get("items", []))
    result["needs_judgment"] = needs_judgment
    result["coverage_summary"] = matrix.get("summary", "")
    result["pr_url"] = pr.get("html_url", "")
    log.info(
        f"  ✓ {result['status']}: {result['items_count']} items, "
        f"needs_judgment={needs_judgment}"
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
    lines.append("\n**Cost & tokens this run**\n")
    lines.append(f"- **Cost**: `${cost:.4f}`")
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
        "claude_calls": result.get("claude_calls"),
        "error": result.get("error"),
    }
    try:
        _remyx_post("/api/v1.0/outrider/runs", payload)
        log.info(f"  run telemetry posted (run_id={run_id}, "
                 f"status={payload['status']})")
    except Exception as e:
        log.warning(f"  run telemetry POST failed (non-fatal): {str(e)[:200]}")


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
    if mode not in ("recommend", "weekly-summary", "fidelity"):
        log.error(f"Unknown mode {mode!r}; must be 'recommend', "
                  f"'weekly-summary', or 'fidelity'.")
        sys.exit(2)

    target = build_target_from_env()
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

    # Token/cost totals across every Claude pass this run, captured even when
    # process_target raised.
    result["cost_usd"] = round(_RUN_COST["cost_usd"], 4)
    result["input_tokens"] = _RUN_COST["input_tokens"]
    result["output_tokens"] = _RUN_COST["output_tokens"]
    result["cache_read_input_tokens"] = _RUN_COST["cache_read_input_tokens"]
    result["claude_calls"] = _RUN_COST["claude_calls"]
    log.info(f"  cost: ${result['cost_usd']} "
             f"({result['input_tokens']} in / {result['output_tokens']} out "
             f"tokens, {result['claude_calls']} claude calls)")

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
