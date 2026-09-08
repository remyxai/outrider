"""Generate the agent-axis section of ``docs/backends.md`` from the registries.

The prose around it is hand-written; only the block between the markers is
generated, so the tables cannot drift from the code while the explanation
stays editable. ``--check`` verifies freshness and is run by
``tests/test_agent_matrix_artifact.py``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agents import available, resolve  # noqa: E402
from agents.providers import PROVIDERS  # noqa: E402

DOC = ROOT / "docs" / "backends.md"
BEGIN = "<!-- BEGIN GENERATED: agent-axis (scripts/gen_backends_doc.py) -->"
END = "<!-- END GENERATED: agent-axis -->"


def cell(text: str) -> str:
    """Escape a table cell — a literal pipe splits the column."""
    return (text or "").replace("|", "\\|")


def build() -> str:
    matrix = json.loads((ROOT / "docs" / "agent-matrix.json").read_text())
    names = sorted(available())
    out: list[str] = []
    w = out.append

    w("## Coding agents (the `agent` input)")
    w("")
    w("`provider` selects a **model** backend; `agent` selects the "
      "**coding-agent CLI** that drives the implementation. They are separate "
      "axes, and each agent speaks exactly one model-API family — so which "
      "providers an agent can reach follows from that, rather than from a "
      "list someone maintains by hand.")
    w("")
    w("```")
    w("agent --speaks--> API family <--serves-- provider")
    w("```")
    w("")
    w("| `agent` | Speaks | Install |")
    w("|---|---|---|")
    for n in names:
        b = resolve(n)
        w(f"| `{n}` | {b.api_family.value} | `{cell(b.install_hint)}` |")
    w("")
    w("Leave `agent` empty for Claude Code — every existing workflow keeps "
      "its exact behavior.")
    w("")
    w("### Which pairs work")
    w("")
    w("| `agent` | `provider` | Secret you set | Endpoint | Default model | Verified |")
    w("|---|---|---|---|---|---|")
    for r in matrix["pairs"]:
        pid = r["provider"]
        provider = PROVIDERS.get(pid)
        if provider is not None and provider.caller_supplied_endpoint:
            endpoint = "_you supply `model-base-url`_"
        else:
            endpoint = r["endpoint"] or "—"
        w(
            f"| `{r['agent']}` | `{pid}` | `{cell(r['secret'])}` | "
            f"{cell(endpoint)} | {r['default_model'] or '_(agent default)_'} | "
            f"{'yes' if r['verified'] else '**not verified**'} |"
        )
    w("")
    w('"Verified" means a real run reached that vendor\'s endpoint '
      "end-to-end. An unverified pair still runs, but the action logs a "
      "warning naming the `provider: custom` + gateway workaround rather "
      "than claiming support it hasn't demonstrated.")
    w("")
    w("Two rejections are deliberate rather than missing: `agent: codex` "
      "with `provider: anthropic` fails because Anthropic serves the Messages "
      "API, not OpenAI Responses — and the reverse for `agent: claude` with "
      "`provider: openai`. Both errors name the agent that *does* serve the "
      "provider.")
    w("")
    w("This table and [`agent-matrix.json`](agent-matrix.json) are generated "
      "from the same registry, so they cannot drift from the code. The JSON "
      "is the machine-readable contract the `remyxai` CLI and the engine read.")
    w("")
    w("### Capabilities, and what happens when one is missing")
    w("")
    w("A backend may be partially capable and still usable — the affected "
      "telemetry degrades, the run does not fail.")
    w("")
    w("| Capability | " + " | ".join(f"`{n}`" for n in names) + " |")
    w("|---|" + "---|" * len(names))
    caps = sorted({c.value for n in names for c in resolve(n).capabilities})
    for c in caps:
        cells = " | ".join(
            "yes" if c in {x.value for x in resolve(n).capabilities} else "—"
            for n in names
        )
        w(f"| `{c}` | {cells} |")
    w("")
    w("| Missing | Effect on the run |")
    w("|---|---|")
    w("| `turn_cap` | `claude-timeout` becomes the only spend bound. Neither "
      "Codex nor R-CLI has a round-limit flag, so keep the timeout tight on "
      "cron-driven installs. |")
    w("| `cost_usd` | Cost resolves from the per-host rate table instead of "
      "the CLI's own figure (`cost_basis: backend_rate_table`); with no rate "
      "row it reports `cost_basis: unavailable` rather than a fabricated "
      "`$0.00`. Token counts stay exact either way. |")
    w("| `stream_transcript` | Selection coverage reports "
      "`basis: unavailable` and the coverage gate runs in `observe` mode, so "
      "a quiet agent is not punished for being quiet. |")
    w("| `web_research` | The staged research phase is skipped; the coding "
      "session runs without web context. |")
    w("| `guardrail_policy` | The injection-hardening tool gate that Claude "
      "Code runs get is **not** in effect, and the run logs a warning saying "
      "so. The post-hoc diff validators still apply. |")
    w("| `output_schema` | Verdict passes fall back to extracting JSON from "
      "the model's prose. |")
    w("")
    w("### Model selection per agent")
    w("")
    w("All three agents take the same `model` input. R-CLI addresses models "
      "as `<provider>/<model>`, so the action composes `provider` and `model` "
      "into that form for you — `provider: openai` + `model: gpt-5.5` becomes "
      "`--model openai/gpt-5.5`. An already-qualified `model` is left alone.")
    w("")
    w("### Codex and Chat-Completions providers")
    w("")
    w("`codex exec` 0.151.0 removed Chat-Completions support: a provider must "
      "serve an OpenAI **Responses** endpoint. A Chat-only provider (or a "
      "local ollama) needs a translating gateway in front of it, reached via "
      "`provider: custom` plus `model-base-url`.")
    return "\n".join(out)


def render(existing: str) -> str:
    block = f"{BEGIN}\n\n{build()}\n\n{END}"
    if BEGIN in existing and END in existing:
        head = existing[: existing.index(BEGIN)]
        tail = existing[existing.index(END) + len(END):]
        return head + block + tail
    # First run: insert before the Related section, else append.
    marker = "\n## Related\n"
    if marker in existing:
        i = existing.index(marker)
        return existing[:i] + "\n\n" + block + "\n" + existing[i:]
    return existing.rstrip() + "\n\n" + block + "\n"


def main(argv: list[str]) -> int:
    existing = DOC.read_text()
    fresh = render(existing)
    if "--check" in argv:
        if existing != fresh:
            print("docs/backends.md agent section is stale — run "
                  "`python scripts/gen_backends_doc.py`", file=sys.stderr)
            return 1
        print("docs/backends.md is up to date")
        return 0
    DOC.write_text(fresh)
    print(f"updated {DOC.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
