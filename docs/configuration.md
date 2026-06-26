---
type: configuration_reference
description: Inputs, outputs, and status codes for the Outrider action.
tags: [outrider, configuration, reference]
---

# Configuration reference

Inputs, outputs, and status codes for the Outrider action.

## Inputs

| Input | Default | Description |
|---|---|---|
| `interest-id` | *(required)* | Remyx ResearchInterest UUID |
| `github-token` | `${{ github.token }}` | Override only for cross-repo controller patterns |
| `min-confidence` | `moderate` | Tier gate: `high` / `moderate` / `low` |
| `draft-mode` | `always` | `always` / `on_test_failure` / `never` |
| `rate-limit-days` | `7` | Cadence guard. Any value > 0 enables: skip the run if any **open** Remyx PR or Issue exists on the target. Engagement (merge or close) releases the gate. Set `0` to disable. The numeric value is otherwise ignored — kept as an on/off bit for compatibility with workflow files written for the prior sliding-window semantics. |
| `guardrails-allowlist` | `''` | Extra path globs Claude Code may modify, **added on top of** the defaults (`*.py`, `.remyx-recommendation/**`, `**/*.md`). Most repos won't need this. |
| `test-integration-policy` | `strict` | `strict` (demote to Issue if new tests don't import an existing module) / `soft` (open draft PR with warning) / `off` (skip the gate). Use `soft` for layer/component repos where standalone modules are the contribution. |
| `lookback` | `week` | Candidate pool window: `today` / `week` / `month` |
| `candidate-pool` | `25` | How many candidates the selection pass picks from |
| `claude-timeout` | `900` | Wall-clock seconds for the Claude Code implementation step. Bump for very large repos; lower to cap cost. |
| `pin-arxiv` | `''` | Optional `arxiv_id`. When set and present in the candidate pool, the action implements that exact paper and skips the selection pass — use it for reproducible eval re-runs. Empty = normal selection. |
| `mode` | `recommend` | `recommend` (classic per-run flow) / `weekly-summary` (post a weekly digest — see [weekly-summary.md](weekly-summary.md)) / `fidelity` / `convention` / `test` / `issue-convention` (standalone chain phases, for re-running a single phase against an existing PR or Issue) |
| `chain` | `true` | When `true`, `recommend` mode continues into the refinement chain (fidelity audit → convention pass → test gate for PRs, or convention pass alone for Issues) within the same run. Set `false` for cost-sensitive runs. |
| `pr-number` | `''` | PR number for standalone chain phases (`fidelity` / `convention` / `test`). Empty in `recommend` mode — the inline chain reads it automatically. |
| `issue-number` | `''` | Issue number for `mode: issue-convention`. Empty in `recommend` mode — the inline chain reads it automatically. |
| `weekly-discussion-id` | `''` | Discussion number (from its URL) or GraphQL node ID. Only read in `weekly-summary` mode. |


## Outputs

| Output | When | Description |
|---|---|---|
| `status` | always | Run outcome — see status codes below |
| `pr_url` | `pr_opened*` | URL of the opened PR |
| `pr_number` | `pr_opened*` | Number of the opened PR (handed to the inline refinement chain) |
| `issue_url` | `issue_opened*` | URL of the opened Issue |
| `issue_number` | `issue_opened*` | Number of the opened Issue (handed to the inline Issue-route pass) |
| `chain_fidelity_status` / `chain_convention_status` / `chain_test_status` | chain ran (PR route) | Per-phase outcome of the inline refinement chain |
| `chain_issue_convention_status` | chain ran (Issue route) | Outcome of the Issue-route convention pass |
| `chain_draft_dropped` | chain ran | `true` if the test gate flipped the draft to ready-for-review |
| `arxiv` | when a paper was picked | arxiv_id |
| `tier` | when a paper was picked | `high` / `moderate` / `low` / `noise` |
| `cost_usd` | always | Claude spend for this run |
| `input_tokens` / `output_tokens` | always | Token usage |
| `discussion_comment_url` | `weekly_summary_posted` | URL of the posted weekly digest comment |


## Status codes

### Recommend-mode outcomes

| Status | Meaning |
|---|---|
| `pr_opened` | PR opened ready-for-review (tests passed, `draft-mode != always`) |
| `pr_opened_draft` | PR opened as draft |
| `issue_opened_preflight` | Pre-flight Claude pass routed to Issue before implementation |
| `issue_opened` | Claude elected Issue-mode (wrote `OPEN_AS_ISSUE.md` instead of code) |
| `issue_opened_no_integration` | Diff adds code that nothing invokes |
| `issue_opened_stub_density` | New module is ≥50% stubs (`pass` / `NotImplementedError` / empty bodies) |
| `issue_opened_no_test_integration` | New tests don't import from any pre-existing module |
| `issue_opened_self_review` | Self-review judged the new code an orphan, unreachable from production. Body preserves Claude's implementation diff for manual review |
| `issue_opened_substitution` | Selection identified a replacement / pipeline-simplification / extension candidate; routed to Issue because the swap needs dep changes the PR guardrails block, or there's no existing call site to anchor against |
| `issue_opened_high_risk` | Diff Risk Score gate routed to a human-review Issue instead of a PR (implementation diff preserved in the Issue body) |
| `skipped_low_confidence` | Recommendation below `min-confidence` |
| `skipped_open_artifact` | An open Remyx PR or Issue from a prior run still exists on the target — engagement (merge or close) releases the gate |
| `skipped_issues_disabled` | The target repo has its Issues tab disabled (default on forks) and the scoped App token can't re-enable it. Enable with `gh repo edit <repo> --enable-issues` and the next run proceeds |
| `skipped_pr_exists` | Every candidate already has an open PR |
| `skipped_issue_exists` | Every candidate already has a prior Issue referencing the arxiv id — Outrider-opened OR maintainer-opened, open OR closed. Step summary differentiates "Already in flight" (open) vs "Already addressed" (closed). Reopen the Issue to re-engage |
| `skipped_external_issue_exists` | Selection pass surfaced an out-of-pool candidate but it's already in the team's attention |
| `skipped_by_selection_verification` | Selection pass verified every candidate against the repo and rejected all. The `selection_reasoning` payload renders in the step summary explaining why |
| `skipped_test_failure` | Tests failed AND `draft-mode: never` |
| `claude_failed` | Claude CLI exited non-zero |
| `rejected_path_violations` | Claude touched files outside the guardrails allowlist |
| `error` | Unhandled exception |

### Chain phase statuses (in `chain.*_status` outputs)

| Phase | Status | Meaning |
|---|---|---|
| **Fidelity** (PR route) | `fidelity_audited` | Coverage matrix posted; reference-impl anchor |
|  | `fidelity_audited_needs_judgment` | Coverage matrix posted; ≥1 item flagged for human review |
|  | `fidelity_audited_paper_anchored` | Coverage matrix posted; paper-abstract anchor (no reference impl available — paper-anchored degraded mode from v1.6.16) |
|  | `fidelity_audited_paper_anchored_needs_judgment` | Paper-anchored + needs-judgment |
|  | `fidelity_skipped_no_reference` | No reference URL **and** no arxiv abstract available — chain bails |
|  | `fidelity_skipped_not_bot` | PR not authored by `remyx-ai[bot]` |
|  | `fidelity_failed_clone` | Reference clone failed |
|  | `fidelity_failed_claude` | Audit Claude call failed or returned unparseable JSON |
| **Convention** (PR route) | `convention_aligned` | Body rewritten + non-algorithmic alignment patches applied |
|  | `convention_skipped_*` / `convention_failed_*` | See action source for full list |
| **Test gate** (PR route) | `test_passed` | Lint + tests passed; draft dropped to ready-for-review |
|  | `test_failed` | Lint or tests failed; `outrider:test-failed` label applied; PR stays draft |
| **Issue convention** (Issue route, v1.6.17) | `issue_convention_aligned` | Issue body rewritten to picked ISSUE_TEMPLATE shape |
|  | `issue_convention_aligned_no_fitting_template` | Templates exist but all bug/question kinds (e.g. bug-only repo); body still folded with scaffolding collapsed |
|  | `issue_convention_skipped_no_templates` | No `.github/ISSUE_TEMPLATE/` directory; body folded with scaffolding collapsed |
|  | `issue_convention_failed_claude` / `issue_convention_failed_patch` | Claude call or body PATCH failed |

### Weekly summary mode

| Status | Meaning |
|---|---|
| `weekly_summary_posted` | Weekly digest comment posted to the configured Discussion |
| `weekly_summary_skipped_no_discussion_id` | `mode: weekly-summary` ran without a `weekly-discussion-id` |
| `weekly_summary_failed` | Weekly mode hit an unhandled error (nothing was posted) |
