"""Agent-aware tool provisioning helpers, for ``action.yml`` to shell out to.

Two things about out-of-band tooling differ per agent, and getting either
wrong is silent:

* **Where a packaged "skill" goes.** Only some CLIs have such a mechanism.
  Installing into ``~/.claude/skills`` for a Codex run wastes a clone and
  leaves the tool unreachable by the route the prompt advertises.
* **How the prompt should tell the agent to invoke it.** Every agent has a
  shell tool, so a binary on PATH is the portable surface; a slash command is
  not. Describing one to an agent that has none sends it looking for a
  command it cannot run, and the failure looks like the model ignoring an
  instruction.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents import resolve  # noqa: E402

CCC_REPO = "https://github.com/cocoindex-io/cocoindex-code"


def _backend():
    return resolve(os.environ.get("INPUT_AGENT"))


def environments_md(backend) -> str:
    """The ENVIRONMENTS.md surface, phrased for this agent's tool routes."""
    hint = backend.tool_invocation_hint("ccc")
    return f"""- title: cocoindex-code AST search available
  description: cocoindex-code AST-based semantic code search is pre-installed for the {backend.display_name} agent.
  resource: {CCC_REPO}
  tags: [outrider, environment, cocoindex-code, ast-search]
  surface: |
    # Environment: cocoindex-code AST search
    - {hint}
    - Use it to ground call-site claims on real AST paths rather than on a
      text match, during both selection and implementation.
    - `ccc --help` lists the available subcommands.
"""


def main(argv: list[str]) -> int:
    backend = _backend()
    if "--skills-home" in argv:
        # Empty output means "this agent has no skills mechanism", which the
        # caller tests with `[ -n "$..." ]`.
        print(backend.skills_home or "")
        return 0
    if "--environments-md" in argv:
        print(environments_md(backend), end="")
        return 0
    if "--invocation-hint" in argv:
        print(backend.tool_invocation_hint("ccc"))
        return 0
    print(
        "usage: agent_tooling.py "
        "[--skills-home|--environments-md|--invocation-hint]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
