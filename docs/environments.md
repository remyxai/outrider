---
type: Workflow Environment Convention
title: ENVIRONMENTS.md — workflow-provided tool hints
description: How to describe workflow-attached tooling (skills, MCP servers, custom search) so the Outrider agent knows to reach for it.
resource: https://github.com/remyxai/outrider/blob/main/docs/environments.md
tags: [outrider, environments, workflow, tool-hints, skills]
timestamp: 2026-07-01T02:30:00Z
---

# ENVIRONMENTS.md — workflow-provided tool hints

Attach a `ENVIRONMENTS.md` file at your workflow's workspace root (or repo root) to tell the Outrider agent about tooling you've installed for this run — Claude Code skills, MCP servers, custom code-search CLIs, private lint plugins, anything the agent would benefit from knowing exists.

Outrider reads it, strips the OKF/YAML frontmatter, and injects the body into the agent's brief alongside SPEC.md / PAPER.md / GUARDRAILS.md. If no file is present, behavior is unchanged.

## Recommended default: attach cocoindex-code

The reference pattern most Outrider deployments benefit from is attaching [`cocoindex-code`](https://github.com/cocoindex-io/cocoindex-code) — AST-based semantic code search that lets the selection agent ground-truth call-site claims on real paths instead of speculating from paper metadata. Marginal cost is minimal (~60-90s runner time for the one-time pipx install, ~500 tokens of prompt context per selection call). Copy-pasteable workflow: [`examples/workflows/with-cocoindex.yml`](../examples/workflows/with-cocoindex.yml).

Runs without an `ENVIRONMENTS.md` still work — the pattern is opt-in.

## Motivation

Outrider spawns Claude Code with a small set of default tools (Read, Grep, Glob, Bash, edit_file). Workflow authors often want to attach *more*:

- An AST-based semantic code search (e.g. [`cocoindex-code`](https://github.com/cocoindex-io/cocoindex-code)) for large modules where whole-file reads are wasteful
- MCP servers for private data sources (internal docs, org-specific search)
- Custom lint / type / security scanners that produce agent-usable output
- Any skill discoverable via `~/.claude/skills/`

Discoverability alone is not enough — even a correctly-installed skill won't get used if the agent's brief doesn't mention it exists. `ENVIRONMENTS.md` is that mention.

## File location

Outrider looks in this order (first hit wins):

1. `$GITHUB_WORKSPACE/ENVIRONMENTS.md`
2. `$GITHUB_WORKSPACE/ENVIRONMENT.md` (singular, if you prefer the `CONTRIBUTING.md` naming)
3. `<workdir>/ENVIRONMENTS.md`
4. `<workdir>/ENVIRONMENT.md`

Content over 4 KB is truncated with a marker; keep the description terse.

## File shape

`ENVIRONMENTS.md` follows the same [OKF frontmatter convention](https://github.com/openknowledge-network/openknowledge) as Outrider's other docs. The frontmatter is machine-readable metadata (dropped before the agent sees the body); the markdown body is what gets injected.

```markdown
---
type: Workflow Environment
title: <descriptive name of this workflow's environment>
description: <one-line summary of tools/context this workflow provides>
resource: <URL to the workflow file or docs>
tags: [outrider, environment, <tool-specific>]
timestamp: <ISO 8601 datetime>
---

# <Title>

<Markdown body — described tools, expected usage patterns, invocation notes>
```

## Example — attaching AST-based semantic code search

Workflow step (see [`cocoindex-code`](https://github.com/cocoindex-io/cocoindex-code) for install specifics):

```yaml
- name: Install cocoindex-code as Claude Code skill
  run: |
    git clone --depth 1 https://github.com/cocoindex-io/cocoindex-code /tmp/cocoindex-code
    pipx install 'cocoindex-code[full]'
    mkdir -p ~/.claude/skills/
    ln -sfn /tmp/cocoindex-code ~/.claude/skills/cocoindex-code

- name: Describe environment to Outrider
  run: |
    cat > ENVIRONMENTS.md <<'EOF'
    ---
    type: Workflow Environment
    title: cocoindex-code AST search available
    description: cocoindex-code AST-based semantic code search is pre-installed as a Claude Code skill.
    resource: https://github.com/remyxai/outrider/blob/main/docs/environments.md
    tags: [outrider, environment, cocoindex-code, ast-search]
    timestamp: 2026-07-01T00:00:00Z
    ---

    # Environment: cocoindex-code AST search

    ## Available tools

    - **`ccc` CLI** (AST-based semantic code search across the cloned repo). Prefer over reading entire large files when locating functions, classes, or specific code patterns. Multi-language (tree-sitter based).

    ## Suggested use during implementation

    - For "find the function that does X" queries, invoke the semantic-search skill rather than Read/Grep on speculation.
    - For files >500 LOC where you only need one function, prefer AST search + targeted Read over reading the whole file.
    EOF
```

The agent's brief will now include a section describing the skill, so it knows to reach for `ccc` when the task calls for it.

See [`examples/workflows/with-cocoindex.yml`](../examples/workflows/with-cocoindex.yml) for the full copy-pasteable workflow.

## What Outrider does with the file

1. Reads it from the first location found in the search order
2. Strips any YAML frontmatter (the agent doesn't need the metadata)
3. Caps size at 4 KB with a truncation marker
4. Writes the body to `.remyx-recommendation/ENVIRONMENT.md` in the bundle Claude Code reads
5. Updates the agent's file-reading order to include it after ORIENTATION.md

If the file is absent, missing frontmatter body, or empty, no bundle entry is written and the agent's brief is unchanged.

## When not to use ENVIRONMENTS.md

- **Auth secrets or connection strings** — those belong in Actions secrets, not a markdown file. The frontmatter and body are not confidential.
- **Runtime configuration** the workflow already sets via environment variables — Outrider doesn't parse `ENVIRONMENTS.md` for structured config.
- **Documentation that lives with your target repo's contributor guide** — use `ORIENTATION.md` (already auto-detected from `CONTRIBUTING.md` / repo docs), not `ENVIRONMENTS.md`.

## Naming

`ENVIRONMENTS.md` (plural) is the primary spelling, since a workflow may attach multiple tools' worth of environment. `ENVIRONMENT.md` (singular) is a supported fallback for consistency with `CONTRIBUTING.md` / `LICENSE.md` conventions. Both work; pick one.
