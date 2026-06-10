# Outrider — GitHub Action

Scouts the arXiv frontier for your repo and picks the next paper most implementable against your codebase. Verifies each candidate's structural fit against your actual modules (not just keyword relevance), then either opens a draft PR wiring it into an existing call site, opens an Issue when a PR would be premature, or opens an RFC-shape Issue when the candidate proposes a new capability your team has signaled openness to. Won't re-recommend a paper that's already in front of your team — whether Outrider filed it or you did.

<p align="center">
  <img src="https://github.com/remyxai/outrider/releases/download/readme-assets/outrider-v1.gif" alt="Outrider demo" width="800">
</p>

```yaml
- uses: remyxai/outrider@v1
  with:
    interest-id: ${{ vars.REMYX_INTEREST_ID }}
```

## What you get

- **Draft PRs** that wire a paper's contribution into an existing module, with a self-review section in the body honestly noting what was implemented vs. left out
- **Issues** when a PR would be premature — pre-flight, validators, or self-review route the paper to discussion instead of scaffold-shaped PRs
- **RFC-shape Issues** when the team has signaled openness to a new capability (a README roadmap section, an open `[RFC]` Issue, a CONTEXT.md investment pattern) and a candidate fits as an extension — a clear proposal instead of speculation
- **No duplicate work** — the same paper isn't re-recommended once any Outrider or maintainer Issue references it; reopen the Issue to re-engage
- **A selection narrative** in the run's GitHub Actions step summary explaining why this paper (or why nothing actionable this run) — visible at a glance, not buried in logs
- **One artifact per `rate-limit-days`** by default — no Issue spam

## Setup

Two install paths — pick whichever fits.

### One-command install (CLI)

The [`remyxai` CLI](https://github.com/remyxai/remyxai-cli) installs Outrider on a target repo via the Remyx GitHub App: writes the workflow, sets the repo secrets, and opens a bot-authored setup PR. Your local git isn't touched.

```bash
pip install remyxai
remyxai outrider init --repo owner/name --auto-interest
```

Requires `REMYXAI_API_KEY` (from [engine.remyx.ai](https://engine.remyx.ai) Settings) and an Anthropic key (`--anthropic-key` or `ANTHROPIC_API_KEY`). The `--auto-interest` flag auto-creates a `ResearchInterest` from the repo if one doesn't exist; drop it if you already have an interest UUID to wire in. If the Remyx GitHub App isn't installed on the target repo yet, the command surfaces the install link.

### Manual install (5 minutes)

1. **Sign up at [engine.remyx.ai](https://engine.remyx.ai)** and connect your repo. Remyx ingests your commit history and creates a `ResearchInterest`. Edit its context body to sharpen the framing.

2. **Generate a `REMYX_API_KEY`** from the engine.remyx.ai Settings page.

3. **Add two secrets** in your repo's *Settings → Secrets and variables → Actions*:
   - `REMYX_API_KEY` — from step 2
   - `ANTHROPIC_API_KEY` — your key from [console.anthropic.com](https://console.anthropic.com)

4. **Allow Actions to open PRs**: *Settings → Actions → General → Workflow permissions* → ☑ *Allow GitHub Actions to create and approve pull requests*. (Without this, the action returns `HTTP 403` at PR creation.)

5. **Add the workflow** at `.github/workflows/outrider.yml`:

   ```yaml
   name: Outrider
   on:
     schedule:
       - cron: '0 14 * * 1'  # Mondays 14:00 UTC; pick any cadence
     workflow_dispatch:
   jobs:
     recommend:
       runs-on: ubuntu-latest
       permissions:
         contents: write
         pull-requests: write
         issues: write
       env:
         REMYX_API_KEY: ${{ secrets.REMYX_API_KEY }}
         ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
       steps:
         - uses: remyxai/outrider@v1
           with:
             interest-id: 'YOUR-INTEREST-UUID-HERE'
   ```

   (Tip: the engine.remyx.ai UI has a "copy workflow snippet" button that emits this pre-filled.)

6. **First run**: *Actions tab → Outrider → Run workflow*. Takes 4–6 minutes. A draft PR or Issue appears when complete.

## Inputs

| Input | Default | Description |
|---|---|---|
| `interest-id` | *(required)* | Remyx ResearchInterest UUID |
| `github-token` | `${{ github.token }}` | Override only for cross-repo controller patterns |
| `min-confidence` | `moderate` | Tier gate: `high` / `moderate` / `low` |
| `draft-mode` | `always` | `always` / `on_test_failure` / `never` |
| `rate-limit-days` | `7` | Cadence guard. Skip the run if any Remyx artifact (PR **or** Issue) was opened within this window. Set `0` to disable. |
| `guardrails-allowlist` | `''` | Extra path globs Claude Code may modify, **added on top of** the defaults (`*.py`, `.remyx-recommendation/**`, `**/*.md`). Most repos won't need this. |
| `test-integration-policy` | `strict` | `strict` (demote to Issue if new tests don't import an existing module) / `soft` (open draft PR with warning) / `off` (skip the gate). Use `soft` for layer/component repos where standalone modules are the contribution. |
| `lookback` | `week` | Candidate pool window: `today` / `week` / `month` |
| `candidate-pool` | `25` | How many candidates the selection pass picks from |
| `claude-timeout` | `900` | Wall-clock seconds for the Claude Code implementation step. Bump for very large repos; lower to cap cost. |
| `pin-arxiv` | `''` | Optional `arxiv_id`. When set and present in the candidate pool, the action implements that exact paper and skips the selection pass — use it for reproducible eval re-runs. Empty = normal selection. |
| `mode` | `recommend` | `recommend` (classic per-run flow) / `weekly-summary` (post a weekly digest to a Discussion — see [Weekly Discussion summary](#weekly-discussion-summary-opt-in)) |
| `weekly-discussion-id` | `''` | Discussion number (from its URL) or GraphQL node ID. Only read in `weekly-summary` mode. |

## Outputs

| Output | When | Description |
|---|---|---|
| `status` | always | Run outcome — see status codes below |
| `pr_url` | `pr_opened*` | URL of the opened PR |
| `issue_url` | `issue_opened*` | URL of the opened Issue |
| `arxiv` | when a paper was picked | arxiv_id |
| `tier` | when a paper was picked | `high` / `moderate` / `low` / `noise` |
| `cost_usd` | always | Claude spend for this run |
| `input_tokens` / `output_tokens` | always | Token usage |
| `discussion_comment_url` | `weekly_summary_posted` | URL of the posted weekly digest comment |

## Costs

- **Claude Code**: ~$2–3 per PR-track run (pre-flight + selection + implementation + self-review). Issue-track runs cost less since they skip the implementation pass. You bring `ANTHROPIC_API_KEY`.
- **Remyx API**: included in your engine.remyx.ai subscription.
- **GitHub Actions**: ~6–8 min on `ubuntu-latest` per run.

At weekly cadence (default `rate-limit-days: 7`), expect ~$2–4/mo Claude.

<details>
<summary><b>Status codes</b></summary>

| Status | Meaning |
|---|---|
| `pr_opened` | PR opened ready-for-review (tests passed, `draft-mode != always`) |
| `pr_opened_draft` | PR opened as draft |
| `issue_opened_preflight` | Pre-flight Claude pass routed to Issue before implementation |
| `issue_opened` | Claude elected Issue-mode (wrote `OPEN_AS_ISSUE.md` instead of code) |
| `issue_opened_no_integration` | Diff adds code that nothing invokes |
| `issue_opened_stub_density` | New module is ≥50% stubs (`pass` / `NotImplementedError` / empty bodies) |
| `issue_opened_no_test_integration` | New tests don't import from any pre-existing module |
| `issue_opened_self_review` | Self-review judged the new code an orphan, unreachable from production. Body preserves Claude's implementation diff so the maintainer can review or apply it manually |
| `issue_opened_substitution` | Selection identified a replacement / pipeline-simplification / extension candidate (vs. additive drop-in); routed to Issue because the swap needs dep changes the PR guardrails block, or there's no existing call site to anchor against |
| `skipped_low_confidence` | Recommendation below `min-confidence` |
| `skipped_rate_limit` | A Remyx PR or Issue was opened within `rate-limit-days` |
| `skipped_pr_exists` | Every candidate already has an open PR |
| `skipped_issue_exists` | Every candidate already has a prior Issue referencing the arxiv id — Outrider-opened OR maintainer-opened, open OR closed. Step summary differentiates "Already in flight" (open) vs "Already addressed" (closed). Reopen the Issue to re-engage |
| `skipped_external_issue_exists` | Selection pass surfaced an out-of-pool candidate but it's already in the team's attention — same Outrider/Maintainer × open/closed differentiation as above |
| `skipped_by_selection_verification` | Selection pass verified every candidate against the repo and rejected all. The `selection_reasoning` payload renders open in the step summary explaining why — the most useful signal for "no actionable paper this run" outcomes |
| `skipped_test_failure` | Tests failed AND `draft-mode: never` |
| `claude_failed` | Claude CLI exited non-zero |
| `rejected_path_violations` | Claude touched files outside the guardrails allowlist |
| `error` | Unhandled exception |
| `weekly_summary_posted` | Weekly digest comment posted to the configured Discussion |
| `weekly_summary_skipped_no_discussion_id` | `mode: weekly-summary` ran without a `weekly-discussion-id` |
| `weekly_summary_failed` | Weekly mode hit an unhandled error (nothing was posted) |

</details>

<details>
<summary><b>Guardrails</b> — what Claude can and can't modify</summary>

**Allowed paths** (defaults):
- `*.py` — any Python source, anywhere in the repo
- `.remyx-recommendation/**` — the spec bundle (scrubbed before commit)
- `**/*.md` — Markdown anywhere (README, CHANGELOG, docs/, ADR notes); the 50-line edit cap still applies to existing files

**Always blocked** by *role* (filename/type), not directory:
- `.github/**` — CI / workflow config
- `*Dockerfile`, `*Dockerfile.*`, `*.dockerfile`, `*.sh` — container builds and shell scripts
- `*requirements*.txt`, `setup.py`, `setup.cfg`, `pyproject.toml`, `MANIFEST.in`, `*.lock` — dependency / build manifests

The block list takes precedence over the allowlist. Non-`.py` config not on the block list (e.g. `pipelines/*.yaml`) simply isn't allowed either.

**Edit-size caps** (enforced after the Claude session):
- Each edit to a pre-existing file: ≤50 net lines (additions + deletions)
- At most 3 new `.py` files per run
- At least one newly-added function/method/class must be invoked from another changed file (an import alone doesn't count)

Extend the allowlist for your repo via the `guardrails-allowlist` input.

</details>

<details>
<summary><b>How selection works</b> — four integration shapes + discharge model</summary>

Outrider's selection pass classifies every candidate against your repo using a four-shape taxonomy. A candidate that doesn't fit one of these shapes is a structural mismatch and gets rejected:

- **addition** — paper adds a new module wired into existing code. Most common. Verification: the call site exists and the new module's I/O contract fits.
- **replacement** — strict drop-in for an existing component with the same I/O contract but better internals (smaller / faster / newer foundation). Verification: I/O contracts are functionally equivalent, not just thematically related.
- **simplification** — merges two or more existing components into one with the same end-to-end contract. Pipeline collapses. Verification: merged contribution spans the existing boundary contract cleanly.
- **extension** — proposes a new capability your repo lacks AND that you've signaled openness to (README roadmap, an open `[RFC]` Issue, CONTEXT.md investment pattern). Stricter bar than addition — four gates: pipeline-compatible I/O, explicit team-direction signal, no existing implementation, tier=high + relevance ≥ 0.90. Without ≥1 explicit direction signal in your repo, extension picks are RFC-fishing and get rejected.

Tie-break preference: `simplification > replacement > addition > extension`. Extension is last-resort — picked only when the other three shapes fail AND the four gates pass.

**Discharge model** — the same paper isn't re-recommended once it's already in front of your team. The dedup gate counts:

- **Outrider-opened Issues** (any state, open OR closed) — closing an Issue means "the team has decided," still a discharge signal
- **Maintainer-opened Issues** (RFCs, discussions) whose body links the paper's arxiv id — a stronger signal than Outrider's own, since you authored the thread

Re-engagement lever: **reopen the Issue** to drop the paper from the discharge set so Outrider can re-recommend it.

</details>

<details>
<summary><b>How it works</b> — full pipeline</summary>

```
GitHub cron fires the workflow
       ↓
Query engine.remyx.ai for the candidate pool + interest context
       ↓
Rate-limit + per-candidate viability gates:
  - confidence (tier above min-confidence)
  - PR exists for arxiv?
  - any prior Issue references arxiv? (Outrider OR maintainer; open OR closed)
       ↓
Clone the target repo + detect package / default branch
       ↓
Selection pass (Claude agentic, ~5 min budget):
  Prompt threads in:
    - candidate brief (with inline "✗ already filed [Outrider/Maintainer]" tags)
    - "Already in the team's attention" discharge section
    - 4 integration shapes + tie-break ordering
    - verification tools: gh code-search, gh api, remyxai search query/info
  Outputs: chosen_index + integration_shape + selection_reasoning
       ↓
External-pick dedup (if out-of-pool / extension candidate) against the same set
       ↓
Write the .remyx-recommendation/ spec bundle
       ↓
Pre-flight Claude pass: PR or Issue?
       ↓                              ↓
     ISSUE                            PR
       ↓                              ↓
   open Issue        Invoke Claude Code (implement integration)
   (impl. diff                        ↓
    preserved in     Path-allowlist + integration validator
    body when         (new module must be imported by a modified file)
    self-review                       ↓
    routed here)     Stub-density + pytest + test-integration check
                                      ↓
                     Self-review pass (downgrade to Issue if orphan;
                     diff preserved in Issue body for manual review)
                                      ↓
                     Commit (bundle scrubbed) + push + open draft PR
```

The Remyx engine (commit-history extraction, candidate pool, embedding pre-filter, ranking) runs server-side. This action is a pure consumer.

</details>

## Weekly Discussion summary (opt-in)

A rolling weekly digest of Outrider's work on your repo, posted as a comment
on a Discussion you designate: run outcomes, the selection pass's verdicts
(with its rejection reasoning quoted verbatim), refine-query themes, the
license gate's class distribution, open Outrider Issues with a next-action
column, and a short "patterns worth attention" section. Makes the action's
work auditable at a glance — including the runs that deliberately produced
no PR or Issue.

Setup:

1. **Enable Discussions** on your repo if it isn't already on:
   *Settings → General → Features → ☑ Discussions*. (Repos — forks
   especially — have Discussions off by default.)
2. **Create (or pick) a Discussion** on your repo to host the digests, and
   note its number from the URL.
3. **Add a second scheduled job** (weekly cron) that calls the action in
   `weekly-summary` mode. Note the extra `discussions: write` permission:

   ```yaml
   name: Outrider weekly summary
   on:
     schedule:
       - cron: '0 15 * * 1'  # Mondays 15:00 UTC
     workflow_dispatch:
   jobs:
     weekly-summary:
       runs-on: ubuntu-latest
       permissions:
         contents: read
         actions: read        # read the week's run logs
         issues: read         # list open Outrider Issues
         discussions: write   # post the digest comment
       env:
         REMYX_API_KEY: ${{ secrets.REMYX_API_KEY }}
         ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
       steps:
         - uses: remyxai/outrider@v1
           with:
             interest-id: 'YOUR-INTEREST-UUID-HERE'
             mode: weekly-summary
             weekly-discussion-id: '123'  # your Discussion number
   ```

The digest posts as `remyx-ai[bot]` when the Remyx GitHub App is installed
on the repo with Discussions access (if a permission prompt is pending,
accept it under *Settings → GitHub Apps → remyx-ai*); otherwise it falls
back to the workflow's `GITHUB_TOKEN` and posts as `github-actions[bot]`.

Cost: one Claude call per week (~$0.10–0.20) to draft the interpretive
sections; the rest is GitHub API reads. If that call fails, the digest still
posts with the data tables only. Runs whose logs have aged out of GitHub's
retention window are listed as "details unavailable" rather than silently
dropped.

## Diff Risk Score — adapted from *Automating Low-Risk Code Review at Meta: RADAR, Risk Calibration, and Review Efficiency*

The integration funnel includes a **calibrated Diff Risk Score** gate
(`src/diff_risk_score.py`, wired into `process_target`). RADAR stratifies
every diff with a machine-learned risk score over static-diff features and
lets low-risk diffs auto-land while routing higher-risk ones to deeper
review. Outrider ports the *result*, not the trained model: a transparent
logistic over the same static features the other gates already extract
(files touched, lines changed, new public callables, critical-path edits,
test-coverage impact) yields a score in `[0, 1]` and a band:

- **low** → flows straight through the funnel to a PR;
- **elevated** → still a PR, but forced to draft so a human reviews before
  it lands;
- **high** → routed to a human-review Issue (`issue_opened_high_risk`)
  instead of an auto-PR, with the implementation diff attached.

The two band cut points are the single tunable knob RADAR exposes — a
percentile that trades automation yield against safety — expressed as fixed
score thresholds. Scoping note: this delivers the risk-aware *routing
decision*, not RADAR's trained model, telemetry pipeline, or revert/incident
measurement, none of which the action hosts.

## License

Apache 2.0. See [LICENSE](./LICENSE).
