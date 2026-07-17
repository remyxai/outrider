---
description: Editing action.yml, the composite-action definition.
applyTo: "action.yml"
---

# action.yml conventions

- **Additive changes only on the `provider` input's accepted values.** Never remove or rename existing values (`anthropic`, `zai`, `moonshot`, `custom`). Adding new values is safe.
- **Every new input needs threading.** `inputs.<name>` in the top-level `inputs:` block AND `INPUT_<NAME>: ${{ inputs.<name> }}` in the recommend step's `env:` block. Missing the second half means run.py never sees it.
- **Validate YAML after every edit.** `python3 -c "import yaml; yaml.safe_load(open('action.yml'))"`.
- **Secrets are read from the caller's env block.** Composite actions cannot access `secrets.*` directly. The caller passes them via `env:` on the `- uses:` step; the action's Configure step then chooses the right one per `provider`.
- **Mutual exclusion between `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`** — Claude Code prefers `x-api-key` when both are set, and non-Anthropic backends 401 on that. The Configure step explicitly clears the non-selected auth var; do not remove that clearing.
