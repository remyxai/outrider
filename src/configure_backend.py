"""Resolve `(agent, provider, model)` into the run's environment.

Invoked once by ``action.yml``, replacing the per-agent shell `case` arms that
previously encoded routing. All of the knowledge lives in
``agents/providers.py``; this file is only the GitHub Actions adapter — it
reads the inputs, writes ``$GITHUB_ENV``, and turns a RoutingError into a
workflow annotation.

Keeping it this thin is the point: adding a provider or an agent never
requires touching the action's YAML again.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents import RoutingError, available, resolve, resolve_routing  # noqa: E402


def main() -> int:
    agent_name = os.environ.get("INPUT_AGENT", "")
    provider = os.environ.get("INPUT_PROVIDER", "")
    model = os.environ.get("INPUT_MODEL", "")
    base_url = os.environ.get("INPUT_MODEL_BASE_URL", "")

    try:
        backend = resolve(agent_name)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    try:
        routing = resolve_routing(backend, provider, model, base_url, os.environ)
    except RoutingError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    for warning in routing.warnings:
        print(f"::warning::{warning}", file=sys.stderr)

    github_env = os.environ.get("GITHUB_ENV")
    lines = [f"{k}={v}" for k, v in routing.env.items()]
    if github_env:
        with open(github_env, "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")

    # Log what was selected, with secret VALUES never echoed — only names.
    target = routing.provider_display or "(caller-supplied environment)"
    print(f"agent={backend.name} ({backend.display_name}) → {target}")
    if routing.model:
        print(f"model={routing.model}")
    for key in sorted(routing.env):
        shown = "(cleared)" if routing.env[key] == "" else "(set)"
        if key.endswith(("_BASE_URL", "_MODEL")):
            shown = routing.env[key] or "(cleared)"
        print(f"  {key}={shown}")
    return 0


if __name__ == "__main__":
    if "--matrix" in sys.argv:
        # Documentation generator: the compatibility table is derived from the
        # registry so the docs cannot drift from the code.
        from agents import agent_matrix

        for row in agent_matrix():
            print("\t".join(str(row[k]) for k in (
                "agent", "provider", "family", "endpoint", "secret",
                "default_model", "verified",
            )))
        raise SystemExit(0)
    raise SystemExit(main())
