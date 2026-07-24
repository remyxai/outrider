# REVIEW.md — starter template

Copy this file to your repo root as `REVIEW.md` (or `.github/REVIEW.md`) and
adapt to your review conventions. Outrider reads it on dispatch and threads
it into the drafter, fidelity audit, and convention pass so the generated PRs
respect your norms without you having to infer them from PR history.

Sections below are suggestions — drop any that don't fit, add repo-specific
ones as needed. Aim for terse and decision-forcing over exhaustive; a review
convention that produces false negatives is worse than one that stays silent.

---

# REVIEW.md — <your-repo>

Review conventions for this repo. Loaded as first-class context by Outrider
on dispatch. Complements `CLAUDE.md` (coordination-before-coding) and
`CONTRIBUTING.md` (broad contribution guide).

## Verdicts

- **approve** — <what triggers approve on your repo: scoped diff, tests
  updated, CI green, PR body names the ticket/decision it lands, …>
- **request-changes** — <what triggers a blocking review: missing tests
  on new code paths, cross-cutting refactor bundled with a feature, an
  anti-pattern below, …>
- **comment** — advisory only (style, alt approaches, docs). Non-blocking

## Test bar

- <the minimum tests you expect for a merge — unit for logic, integration
  for X, benchmarks on eval harness Y if the PR touches Z>
- CI green on the PR branch
- <if the repo has an eval harness or benchmark set that PRs should run>

## Scope stance

- <how you feel about PR size / splitting: e.g. one mechanism per PR;
  refactor separate from feature; formatting separate>
- <what unrelated changes get rejected on sight>

## Formatter / style

- <your linter/formatter and whether it's enforced at CI or review>

## Author-preference policy

- <do you prefer paper-authors or upstream maintainers submit PRs when
  possible? escalation window if they don't respond?>
- <if this is a target repo Outrider drafts against, note whether the
  maintainer wants Outrider PRs at all vs. Issues surfacing findings>

## Response norms

- <how reviewers respond: suggested-changes vs comment threads, expected
  turnaround, stale-review handling>

## Benchmark expectations

- <if your repo has an eval harness: when should PRs run it, and how
  should results be reported in the PR body>

## Anti-patterns

- <patterns that get PRs auto-rejected — e.g. wholesale reformat bundled
  with feature, brittle regex rules, silent truncation without a log line,
  cross-cutting refactor in a feature PR, …>

## Meta

- Maintainer-authored; Outrider reads, does not write
- To propose an update to these conventions: PR against this file with
  rationale in the body
