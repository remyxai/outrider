"""OpenAI Codex adapter.

Verified against codex-cli 0.151.0 on 2026-09-07, including reading the
shipped binary's event-type and config strings.

Invocation::

    codex exec --json --dangerously-bypass-approvals-and-sandbox
               -C <workdir> [--output-schema <file>] <prompt>

Codex is the backend that stresses the port hardest, and each difference is
absorbed here rather than leaking into the orchestrator:

* **JSONL only** — there is no single-envelope mode. Events are
  ``thread.started``, ``turn.started``, ``turn.completed``, ``turn.failed``
  and ``item.started|updated|completed``.
* **No dollar figure.** ``turn.completed`` carries ``input_tokens``,
  ``cached_input_tokens``, ``output_tokens`` and ``reasoning_output_tokens``,
  so cost resolves through the rate table rather than the envelope.
* **No round cap of any kind** — no ``--max-turns`` equivalent exists, so the
  wall-clock timeout is the only bound. This is the one capability gap that
  costs real money, and it is why TURN_CAP is not declared.
* **Network is off inside the sandbox.** ``workspace-write`` blocks it, and
  the agent needs ``gh`` and ``remyxai``. The runner is already an isolated
  throwaway VM, so the bypass flag is the right trade there; the alternative
  is ``-c sandbox_workspace_write.network_access=true``.
* **``--output-schema`` constrains the final response to a JSON Schema**,
  which is strictly better than the fence-and-brace heuristics the verdict
  passes fall back to elsewhere.
"""
from __future__ import annotations

import json
import os

from agents.base import AgentBackend, AgentResult, Capability, Event

# Codex item types → the normalized tool vocabulary.
_ITEM_TOOL_MAP = {
    "command_execution": "execute",
    "file_change": "write",
    "web_search": "web",
    "mcp_tool_call": "execute",
}


class CodexBackend(AgentBackend):
    name = "codex"
    tool = "codex"
    display_name = "Codex"
    install_hint = "npm install -g @openai/codex"
    billing_url = "https://platform.openai.com/settings/organization/billing"
    keys_url = "https://platform.openai.com/api-keys"

    #: CODEX_API_KEY is the CI credential; CODEX_HOME relocates the config and
    #: auth directory, which matters on a runner with a scratch HOME.
    auth_env = ("CODEX_API_KEY", "CODEX_HOME")

    capabilities = frozenset({
        Capability.ONESHOT_JSON,
        Capability.STREAM_TRANSCRIPT,
        Capability.TOKEN_USAGE,
        Capability.WEB_RESEARCH,
        Capability.OUTPUT_SCHEMA,
        # No COST_USD: tokens only, so cost comes from the rate table.
        # No TURN_CAP: `codex exec` has no round-limit flag at 0.151.0.
        # No GUARDRAIL_POLICY yet: Codex has an execpolicy `.rules` mechanism
        # (`--ignore-rules` implies it), but Outrider does not yet emit one,
        # so the honest declaration is that the launch-time gate is absent.
    })

    def preflight(self) -> tuple[bool, list[str]]:
        if not (os.environ.get("CODEX_API_KEY") or "").strip():
            return False, [
                "agent=codex requires CODEX_API_KEY in the caller's env block"
            ]
        return True, []

    def base_cmd(self) -> list[str]:
        cmd = [
            "codex", "exec", "--json",
            # The runner is already an isolated throwaway VM, and the agent
            # needs network for `gh` / `remyxai`, which workspace-write blocks.
            "--dangerously-bypass-approvals-and-sandbox",
            # The workdir is a fresh clone, but keep this so a detached or
            # shallow checkout can't dead-end the run.
            "--skip-git-repo-check",
        ]
        model = (os.environ.get("CODEX_MODEL") or "").strip()
        if model:
            cmd += ["-m", model]
        return cmd

    def finalize_cmd(
        self, cmd_prefix: list[str], prompt: str, *, stream: bool = False
    ) -> tuple[list[str], str | None]:
        # The prompt goes on stdin rather than argv: `codex exec` reads stdin
        # as the full prompt when the positional arg is `-`, which sidesteps
        # ARG_MAX on the large spec-bundle invocations.
        return [*cmd_prefix, "-"], prompt

    def parse(
        self, returncode: int, stdout: str, stderr: str, *, stream: bool = False
    ) -> AgentResult | None:
        raw = _parse_jsonl(stdout)
        if not raw:
            return None

        text = ""
        failed = False
        failure_cause = ""
        usage_total = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        turns = 0
        turn_index = 0
        saw_usage = False
        norm: list[Event] = []

        for ev in raw:
            etype = ev.get("type") or ""

            if etype == "turn.started":
                turn_index += 1
            elif etype == "turn.completed":
                turns += 1
                usage = ev.get("usage") or {}
                if usage:
                    saw_usage = True
                usage_total["input_tokens"] += int(usage.get("input_tokens") or 0)
                usage_total["output_tokens"] += int(
                    usage.get("output_tokens") or 0
                )
                # reasoning tokens are billed as output; fold them in so the
                # rate table doesn't under-count a reasoning-heavy run.
                usage_total["output_tokens"] += int(
                    usage.get("reasoning_output_tokens") or 0
                )
                usage_total["cache_read_input_tokens"] += int(
                    usage.get("cached_input_tokens") or 0
                )
            elif etype == "turn.failed":
                failed = True
                err = ev.get("error") or {}
                failure_cause = str(
                    err.get("message") if isinstance(err, dict) else err or ""
                ).strip()
            elif etype.startswith("item."):
                item = ev.get("item") or {}
                item_type = str(item.get("item_type") or item.get("type") or "")
                if item_type == "agent_message" and etype == "item.completed":
                    text = item.get("text") or text
                elif etype == "item.completed":
                    norm.extend(
                        _normalize_item(item_type, item, turn_index)
                    )

        ok = (not failed) and returncode == 0
        if not ok:
            cause = failure_cause or (stderr or "").strip()
            if cause and cause not in text:
                text = (text + "\n--- ERROR ---\n" + cause).strip()

        envelopes: list[dict] = []
        if saw_usage:
            envelopes.append({
                "usage": usage_total,
                # No total_cost_usd — Codex reports tokens only, so the rate
                # table is the sole source of dollars for this backend.
                "num_turns": turns,
                "model": (os.environ.get("CODEX_MODEL") or "").strip(),
                "is_error": not ok,
            })

        return AgentResult(
            ok=ok, text=text, usage_envelopes=envelopes,
            events=norm if stream else [],
        )


def _parse_jsonl(stdout: str) -> list[dict]:
    out: list[dict] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def _normalize_item(
    item_type: str, item: dict, turn: int = 0
) -> list[Event]:
    """One completed Codex item → normalized events.

    A ``file_change`` names every path it touched; a ``command_execution``
    carries the command and its aggregated output.
    """
    tool = _ITEM_TOOL_MAP.get(item_type)
    if tool is None:
        return []

    paths: tuple[str, ...] = ()
    command = None
    lines = 0

    if item_type == "file_change":
        changes = item.get("changes") or []
        found = []
        for change in changes:
            if isinstance(change, dict):
                path = change.get("path")
                if path:
                    found.append(str(path))
            elif isinstance(change, str):
                found.append(change)
        paths = tuple(found)
    elif item_type == "command_execution":
        command = str(item.get("command") or "")
        output = item.get("aggregated_output") or item.get("output") or ""
        if isinstance(output, str) and output:
            lines = output.count("\n") + 1

    return [
        Event(
            kind="tool_use",
            turn=turn,
            id=str(item.get("id") or ""),
            tool=tool,
            paths=paths,
            command=command,
        ),
        Event(
            kind="tool_result", turn=turn,
            id=str(item.get("id") or ""), lines=lines,
        ),
    ]
