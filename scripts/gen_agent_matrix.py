"""Generate ``docs/agent-matrix.json`` from the agent + provider registries.

Three consumers need to know which (agent, provider) pairs exist: this
action, the `remyxai` CLI, and the engine. Hand-maintaining that in three
places guarantees drift, so it is generated from one source and committed as
a machine-readable artifact the others can vendor or fetch.

Run: ``python scripts/gen_agent_matrix.py`` (``--check`` to verify freshness).
``tests/test_agent_matrix_artifact.py`` runs the check, so a registry change
without a regenerate fails the suite rather than shipping a stale table.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agents import agent_matrix, available, resolve  # noqa: E402
from agents.providers import PROVIDERS  # noqa: E402

ARTIFACT = ROOT / "docs" / "agent-matrix.json"


def build() -> dict:
    return {
        "_generated_by": "scripts/gen_agent_matrix.py — do not hand-edit",
        "agents": {
            name: {
                "display_name": backend.display_name,
                "api_family": backend.api_family.value,
                "install": backend.install_hint,
                "key_env": backend.key_env,
                "base_url_env": backend.base_url_env,
                "model_env": backend.model_env,
                "skills_home": backend.skills_home,
                "capabilities": sorted(c.value for c in backend.capabilities),
            }
            for name, backend in ((n, resolve(n)) for n in available())
        },
        "providers": {
            pid: {
                "display_name": p.display_name,
                "secret_env": p.secret_env,
                "families": {f.value: url for f, url in p.endpoints.items()},
                "default_model": {
                    f.value: m for f, m in p.default_model.items()
                },
                "verified": sorted(f.value for f in p.verified),
                "caller_supplied_endpoint": p.caller_supplied_endpoint,
            }
            for pid, p in PROVIDERS.items()
        },
        "pairs": agent_matrix(),
    }


def render() -> str:
    return json.dumps(build(), indent=2, sort_keys=False) + "\n"


def main(argv: list[str]) -> int:
    fresh = render()
    if "--check" in argv:
        current = ARTIFACT.read_text() if ARTIFACT.exists() else ""
        if current != fresh:
            print(
                "docs/agent-matrix.json is stale — run "
                "`python scripts/gen_agent_matrix.py`",
                file=sys.stderr,
            )
            return 1
        print("docs/agent-matrix.json is up to date")
        return 0
    ARTIFACT.write_text(fresh)
    print(f"wrote {ARTIFACT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
