---
description: Ship a new Outrider release — tag the merge commit, move the @v1 pointer, push both, and create the GitHub Release. Use after a PR merges to main that changes action behavior.
argument-hint: <vX.Y.Z> <merge-sha> [release-notes-body]
---

Ship the Outrider release described by the arguments.

Arguments:
- `$1` — new version tag, `vX.Y.Z` shape (required)
- `$2` — merge commit SHA on origin/main (required)
- `$3` — release notes body (optional; prompt for content if missing)

Run this sequence in order and report each step's outcome. Do not skip any step, especially `gh release create` — a bare `git push origin <tag>` does not surface in the GitHub Releases UI.

```bash
# 1. Sync main and confirm the merge SHA is on it
git fetch origin main --tags
git rev-parse "$2"

# 2. Tag the new version at the merge commit
git tag "$1" "$2"

# 3. Move the @v1 moving pointer to the same commit
git tag -f v1 "$2"

# 4. Push the new tag + force-push the moved v1 pointer
git push origin "$1"
git push origin --force refs/tags/v1

# 5. Create the GitHub Release (the step everyone forgets)
gh release create "$1" --repo remyxai/outrider \
  --target "$2" \
  --title "$1" \
  --notes "${3:-<release notes>}"

# 6. Sanity check
gh release view "$1" --repo remyxai/outrider --json name,tagName,url,createdAt
git rev-parse v1 "$1"  # Both SHAs should match "$2"
```

Release notes conventions:
- Sections: `## New`, `## Fixed`, `## Compatibility`. Skip sections that don't apply.
- Reference each landed PR by number: `**<name>** ([PR #NN](url)) — one-paragraph summary.`
- Public repo — terse. No Linear IDs, no dollar figures, no customer names.

Failure modes to catch:
- Tag pushed but no `gh release create` → GitHub UI shows no release.
- v1 didn't move → old customers keep pinning the previous release.
- Wrong SHA in tag → delete + re-tag: `git tag -d "$1" && git push origin ":$1"` then re-run from step 2.
