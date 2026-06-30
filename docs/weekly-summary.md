---
type: Weekly Summary Guide
title: Weekly Discussion summary (opt-in)
description: Opt-in weekly-summary mode setup — post a rolling digest of Outrider's work as a Discussion comment.
resource: https://github.com/remyxai/outrider/blob/main/docs/weekly-summary.md
tags: [outrider, weekly-summary, opt-in]
timestamp: 2026-06-30T03:57:23Z
---

# Weekly Discussion summary (opt-in)

A rolling weekly digest of Outrider's work on your repo, posted as a comment on a Discussion you designate.

## What goes in the digest

- Run outcomes for the past 7 days
- The selection pass's verdicts, with its rejection reasoning quoted verbatim
- Refine-query themes (what topics Outrider broaden-searched on)
- The license gate's class distribution
- Open Outrider Issues with a next-action column
- A short "patterns worth attention" section

Makes the action's work auditable at a glance — including the runs that deliberately produced no PR or Issue.


## Setup

### 1. Enable Discussions on your repo

*Settings → General → Features → ☑ Discussions*. Forks especially have Discussions off by default. The same is true for the Issues tab, which Outrider needs for Issue-route recommendations — when Issues are disabled the run exits cleanly with `skipped_issues_disabled` and a hint to run `gh repo edit <repo> --enable-issues`.

### 2. Create (or pick) a Discussion to host the digests

Note its number from the URL.

### 3. Add a second scheduled job (weekly cron)

The weekly job calls the action in `weekly-summary` mode. Note the extra `discussions: write` permission:

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


## Authoring identity

The digest posts as `remyx-ai[bot]` when the Remyx GitHub App is installed on the repo with Discussions access — if a permission prompt is pending, accept it under *Settings → GitHub Apps → remyx-ai*. Otherwise it falls back to the workflow's `GITHUB_TOKEN` and posts as `github-actions[bot]`.


## Cost

One Claude call per week (~$0.10–0.20) to draft the interpretive sections; the rest is GitHub API reads. If that call fails, the digest still posts with the data tables only.

Runs whose logs have aged out of GitHub's retention window are listed as "details unavailable" rather than silently dropped.
