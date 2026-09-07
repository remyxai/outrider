"""Backboard R-CLI adapter.

Verified against R-CLI 3.0.5 with a live key on 2026-09-07. The published docs
understate the CLI considerably — everything below was read off the binary and
a real run, not the documentation.

Invocation::

    backboard --print <prompt> --format json --cwd <workdir>
              --permission-mode bypass [--model <provider/model>]

``--format json`` is a JSONL **event stream**, not a flat envelope. The event
vocabulary is: ``session:created``, ``session:thread``, ``turn:start|end|
cancelled``, ``user:message``, ``assistant:message|delta|footer``,
``tool:pending|requested|start|result|error|retracted``, ``usage``,
``run:error``, ``checkpoint:restored``.

Three things differ from every other backend and are absorbed here:

1. **Usage arrives once per agentic round, not once per call**, and its fields
   are camelCase. They are summed into a single envelope so per-call
   accounting stays comparable across agents.
2. **The structured tool input is on ``tool:requested``, not ``tool:start``** —
   the latter carries only ``inputSummary``, a human display string. Tool names
   are lowercase on ``tool:requested`` and TitleCase on ``tool:start`` /
   ``tool:result``, so everything is lowercased before mapping.
3. **The failure cause is a ``run:error`` event on stdout, with stderr empty.**
   ``_format_agent_cli_failure`` expects the cause on stderr, so it is lifted
   into the result text here.
"""
from __future__ import annotations

import json
import os
import re

from agents.base import AgentBackend, AgentResult, Capability, Event

# R-CLI tool names (lowercased) → the normalized vocabulary.
# Verified against live runs: read, execute, apply_patch, glob. The write /
# edit / find_* entries come from the CLI's own docs and are mapped
# defensively — an unmapped name is NOT a path loss, because _paths_of works
# off the input shape rather than the name (see below).
_TOOL_MAP = {
    "read": "read",
    "write": "write",
    "edit": "edit",
    "apply_patch": "write",
    "execute": "execute",
    "glob": "glob",
    "find_skill": "other",
    "find_mcp": "other",
}

# apply_patch carries no file_path — the touched paths live in the patch body's
# own headers.
_PATCH_PATH_RE = re.compile(
    r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(.+?)\s*$", re.MULTILINE
)

# tool:result titles like "Read 3 lines" carry the payload size in prose.
_READ_LINES_RE = re.compile(r"\b(\d+)\s+lines?\b", re.IGNORECASE)


class BackboardBackend(AgentBackend):
    name = "backboard"
    tool = "backboard"
    display_name = "Backboard R-CLI"
    install_hint = (
        "BACKBOARD_INSTALL=<dir> curl -fsSL https://app.backboard.io/api/cli | sh"
    )
    billing_url = "https://app.backboard.io"
    keys_url = "https://app.backboard.io"

    #: BACKBOARD_API_KEY is the credential; BACKBOARD_API_URL overrides the
    #: control-plane host. Both are read by the binary directly.
    auth_env = ("BACKBOARD_API_KEY", "BACKBOARD_API_URL")

    capabilities = frozenset({
        Capability.ONESHOT_JSON,
        Capability.STREAM_TRANSCRIPT,
        Capability.TOKEN_USAGE,
        # costUsd is on the usage event and is authoritative — no rate table.
        Capability.COST_USD,
        # No --max-turns / --max-tokens knob exists as of 3.0.5. The
        # orchestrator's wall-clock timeout is the only bound.
        # No JSON-Schema-constrained final response either.
        # WEB_RESEARCH is unconfirmed: the SDK has web search but the CLI's
        # tool list isn't fully enumerated, so it is deliberately not claimed.
    })

    def preflight(self) -> tuple[bool, list[str]]:
        if not (os.environ.get("BACKBOARD_API_KEY") or "").strip():
            return False, [
                "agent=backboard requires BACKBOARD_API_KEY in the caller's "
                "env block (browser `backboard login` cannot work on a runner)"
            ]
        return True, []

    def base_cmd(self) -> list[str]:
        # `--permission-mode bypass` is mandatory under --print: in
        # non-interactive mode any tool call that *would* prompt is denied
        # silently, which reads as a weak model rather than a misconfiguration.
        cmd = ["backboard", "--format", "json", "--permission-mode", "bypass"]
        model = (os.environ.get("BACKBOARD_MODEL") or "").strip()
        if model:
            cmd += ["--model", model]
        return cmd

    def finalize_cmd(
        self, cmd_prefix: list[str], prompt: str, *, stream: bool = False
    ) -> tuple[list[str], str | None]:
        # One shape for both modes — R-CLI always emits the same JSONL stream,
        # so `stream` only decides whether the caller gets the transcript back.
        return [*cmd_prefix, "--print", prompt], None

    def parse(
        self, returncode: int, stdout: str, stderr: str, *, stream: bool = False
    ) -> AgentResult | None:
        events_raw = _parse_jsonl(stdout)
        if not events_raw:
            return None

        text = ""
        run_error = ""
        failed = False
        usage_total = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        cost_total = 0.0
        rounds = 0
        model = ""
        saw_usage = False
        norm: list[Event] = []

        for ev in events_raw:
            etype = ev.get("type") or ""
            payload = ev.get("payload") or {}

            if etype == "assistant:message":
                # Last one wins — that is the agent's final answer.
                text = payload.get("text") or text
            elif etype == "usage":
                saw_usage = True
                usage = ev.get("usage") or payload.get("usage") or {}
                usage_total["input_tokens"] += int(usage.get("inputTokens") or 0)
                usage_total["output_tokens"] += int(usage.get("outputTokens") or 0)
                usage_total["cache_read_input_tokens"] += int(
                    usage.get("cachedTokens") or 0
                )
                cost_total += float(usage.get("costUsd") or 0.0)
                model = usage.get("model") or model
            elif etype == "tool:requested":
                rounds += 1
                norm.extend(_normalize_requested(payload))
            elif etype == "tool:result":
                norm.append(
                    Event(
                        kind="tool_result",
                        id=str(payload.get("toolCallId") or ""),
                        lines=_result_lines(payload),
                    )
                )
            elif etype == "run:error":
                run_error = str(payload.get("error") or "").strip()
                failed = True
            elif etype == "turn:end":
                if (payload.get("status") or "") != "completed":
                    failed = True

        ok = (not failed) and returncode == 0

        # The cause is on stdout as run:error and stderr is empty — lift it so
        # it lands where the caller's tail-slice will keep it.
        if not ok:
            cause = run_error or (stderr or "").strip()
            if cause and cause not in text:
                text = (text + "\n--- ERROR ---\n" + cause).strip()

        envelopes: list[dict] = []
        if saw_usage:
            # Folded into ONE envelope: R-CLI emits usage per round, and
            # counting each as a separate call would inflate claude_calls and
            # break every per-call average.
            envelopes.append({
                "usage": usage_total,
                "total_cost_usd": cost_total,
                "num_turns": rounds,
                "model": model,
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


def _normalize_requested(payload: dict) -> list[Event]:
    """One ``tool:requested`` payload → normalized tool_use events.

    This is the only event carrying structured input; ``tool:start`` has just
    a display string.
    """
    events: list[Event] = []
    for call in payload.get("calls") or []:
        if not isinstance(call, dict):
            continue
        raw_name = str(call.get("name") or "").lower()
        inp = call.get("input") or {}
        events.append(
            Event(
                kind="tool_use",
                id=str(call.get("id") or ""),
                tool=_TOOL_MAP.get(raw_name, "other"),
                paths=_paths_of(raw_name, inp),
                command=(
                    str(inp.get("command") or "")
                    if raw_name == "execute"
                    else None
                ),
            )
        )
    return events


def _paths_of(name: str, inp: dict) -> tuple[str, ...]:
    """Repo paths one tool call touched.

    Deliberately keyed on the *input shape*, not the tool name: R-CLI's tool
    vocabulary is not fully published, and a name this adapter hasn't seen
    should still contribute its path to the coverage signal rather than
    silently drop it. Only apply_patch needs special handling, because it
    carries no path key at all.
    """
    inp = inp or {}
    if name == "apply_patch":
        return tuple(_PATCH_PATH_RE.findall(str(inp.get("patch") or "")))
    val = inp.get("file_path") or inp.get("path")
    return (str(val),) if val else ()


def _result_lines(payload: dict) -> int:
    """Payload size for a ``tool:result``.

    R-CLI reports size three different ways depending on the tool: a prose
    ``title`` ("Read 3 lines"), raw stdout in ``detail`` (execute), or
    structured diff rows in ``detailLines`` (apply_patch).
    """
    title = str(payload.get("title") or "")
    match = _READ_LINES_RE.search(title)
    if match:
        return int(match.group(1))
    detail = payload.get("detail")
    if isinstance(detail, str) and detail:
        return detail.count("\n") + 1
    lines = payload.get("detailLines")
    if isinstance(lines, list):
        return len(lines)
    return 0
