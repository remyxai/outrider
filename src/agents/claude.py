"""Claude Code adapter — the reference implementation.

Behavior here is a straight lift of what ``run.py`` did inline before the port
existed. It is the backend every other adapter is compared against, and the
invariant pinned by ``tests/test_agent_backend_invariant.py`` is that with
``agent`` unset the argv and env this produces are byte-for-byte what shipped
previously.
"""
from __future__ import annotations

import json
from pathlib import Path

from agents.base import (
    BASE_ENV_WHITELIST,
    AgentBackend,
    AgentResult,
    Capability,
    Event,
)

# Injection-hardening Bash gate for the SPAWNED agent. NOTE: the repo's own
# .claude/hooks/pre-bash-gate.sh does NOT reach this agent (it governs only
# Claude Code sessions working on this repo). The agent runs with cwd set to
# the target checkout, so its hooks must be delivered explicitly via
# `--settings`. Anchored to the `src/` directory, not this package.
AGENT_BASH_GATE = Path(__file__).resolve().parent.parent / "agent_bash_gate.sh"

# Claude Code's native tool names → the normalized vocabulary. Anything not
# listed rides through as "other" and contributes no paths.
_TOOL_MAP = {
    "read": "read",
    "edit": "edit",
    "write": "write",
    "notebookedit": "edit",
    "grep": "search",
    "glob": "glob",
    "bash": "execute",
    "websearch": "web_search",
    "webfetch": "web_fetch",
}

# Where each path-bearing tool keeps its target path.
_PATH_KEYS = {
    "read": "file_path",
    "edit": "file_path",
    "write": "file_path",
    "notebookedit": "notebook_path",
    "grep": "path",
    "glob": "path",
}


class ClaudeCodeBackend(AgentBackend):
    name = "claude"
    tool = "claude"
    display_name = "Claude Code"
    install_hint = "npm install -g @anthropic-ai/claude-code"
    billing_url = "https://console.anthropic.com/settings/billing"
    keys_url = "https://console.anthropic.com/settings/keys"

    # ANTHROPIC_AUTH_TOKEN — used by Claude Code as a Bearer credential for
    # non-default backends (z.ai's GLM Coding Plan requires this:
    # https://docs.z.ai/devpack/tool/claude). When set, Claude Code sends
    # "Authorization: Bearer <token>" instead of "x-api-key: <key>". z.ai's
    # gateway rejects x-api-key with HTTP 401, so without this entry any
    # glm-routed run fails at auth.
    auth_env = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
    )
    # GITHUB_TOKEN is intentionally NOT exposed to the coding agent — a
    # write-scoped token in the agent's context is an exfiltration risk. The
    # orchestrator holds its own token separately (clone/push unaffected); the
    # agent's `gh` reads use unauthenticated access (fine for public repos).

    capabilities = frozenset({
        Capability.ONESHOT_JSON,
        Capability.STREAM_TRANSCRIPT,
        Capability.TURN_CAP,
        Capability.TOKEN_USAGE,
        Capability.COST_USD,
        Capability.WEB_RESEARCH,
        Capability.GUARDRAIL_POLICY,
    })

    # ── invocation ────────────────────────────────────────────────────────

    def hardening_settings_arg(self) -> list[str]:
        """``--settings`` arg loading the injection-hardening PreToolUse gate.

        The gate strips high-leverage Bash capabilities (package installs,
        network egress, ``gh`` writes, ``git push``) so an agent that
        *complies* with an instruction injected via untrusted issue/PR text
        still can't reach them — detecting the intent doesn't work
        (arXiv:2607.20759), so we remove the reach instead. PreToolUse hooks
        fire in headless ``-p`` mode even under
        ``--dangerously-skip-permissions`` (verified).

        Returns ``[]`` if the hook file is missing, so a packaging error
        degrades to the prior open behavior rather than crashing every
        dispatch. Callers log that case loudly.
        """
        if not AGENT_BASH_GATE.exists():
            return []
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": str(AGENT_BASH_GATE)}
                        ],
                    }
                ]
            }
        }
        return ["--settings", json.dumps(settings)]

    def base_cmd(self) -> list[str]:
        return [
            "claude",
            "--dangerously-skip-permissions",
            *self.hardening_settings_arg(),
        ]

    def turn_cap_args(self, max_turns: int | None) -> list[str]:
        # NOTE: `--max-turns` is accepted by the CLI but is absent from
        # `claude --help` (verified against 2.1.263: `--max-turns 3` parses,
        # a bogus flag errors "unknown option"). It is load-bearing for spend
        # control, so it stays — but see `preflight` for the probe that drops
        # TURN_CAP if a future release removes it.
        if max_turns is None:
            return []
        return ["--max-turns", str(max_turns)]

    def finalize_cmd(
        self, cmd_prefix: list[str], prompt: str, *, stream: bool = False
    ) -> tuple[list[str], str | None]:
        if stream:
            # --verbose is required by the CLI when stream-json is paired
            # with -p.
            return (
                [*cmd_prefix, "--output-format", "stream-json", "--verbose",
                 "-p", prompt],
                None,
            )
        return [*cmd_prefix, "--output-format", "json", "-p", prompt], None

    # ── parsing ───────────────────────────────────────────────────────────

    def parse(
        self, returncode: int, stdout: str, stderr: str, *, stream: bool = False
    ) -> AgentResult | None:
        return (
            self._parse_stream(returncode, stdout, stderr)
            if stream
            else self._parse_json(returncode, stdout, stderr)
        )

    def _parse_json(
        self, returncode: int, stdout: str, stderr: str
    ) -> AgentResult | None:
        """Single ``--output-format json`` envelope.

        The envelope carries {result, total_cost_usd, usage, num_turns,
        is_error, model}; the model's actual answer is in ``result``, so
        callers parsing a JSON decision out of the answer get the inner text,
        not the envelope.
        """
        try:
            envelope = json.loads((stdout or "").strip())
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(envelope, dict):
            return None
        text = envelope.get("result") or ""
        is_error = bool(envelope.get("is_error")) or returncode != 0
        text = _append_stderr(text, stderr, is_error)
        return AgentResult(
            ok=not is_error, text=text, usage_envelopes=[envelope]
        )

    def _parse_stream(
        self, returncode: int, stdout: str, stderr: str
    ) -> AgentResult | None:
        """JSONL event stream from ``--output-format stream-json``.

        Token/cost usage is recorded exactly once, off the terminal
        ``{"type": "result", ...}`` event (same shape as the json envelope),
        so accounting matches the non-streaming path.
        """
        raw_events: list[dict] = []
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict):
                raw_events.append(parsed)
        final = next(
            (e for e in reversed(raw_events) if e.get("type") == "result"), None
        )
        if final is None:
            return None
        text = final.get("result") or ""
        is_error = bool(final.get("is_error")) or returncode != 0
        text = _append_stderr(text, stderr, is_error)
        return AgentResult(
            ok=not is_error,
            text=text,
            usage_envelopes=[final],
            events=normalize_events(raw_events),
        )


def _append_stderr(text: str, stderr: str, is_error: bool) -> str:
    """On error, append the CLI stderr.

    The envelope's ``result`` often omits the operational cause (e.g. a usage
    limit) that stderr carries. Skipped when stderr is already echoed inside
    the result text.
    """
    if is_error and stderr and stderr.strip() not in text:
        return (text + "\n--- STDERR ---\n" + stderr.strip()).strip()
    return text


def normalize_events(raw_events: list[dict]) -> list[Event]:
    """Anthropic content blocks → normalized :class:`Event` list.

    Walks the ordered transcript pulling ``tool_use`` / ``tool_result`` blocks
    out of assistant and user messages, preserving order so the exploration
    parser can still see how the agent moved between subsystems.
    """
    events: list[Event] = []
    for turn, raw in enumerate(raw_events):
        message = raw.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                name = str(block.get("name") or "")
                inp = block.get("input") or {}
                events.append(
                    Event(
                        kind="tool_use",
                        turn=turn,
                        id=str(block.get("id") or ""),
                        tool=_TOOL_MAP.get(name.lower(), "other"),
                        paths=_paths_of(name, inp),
                        command=(
                            str(inp.get("command") or "")
                            if name.lower() == "bash"
                            else None
                        ),
                    )
                )
            elif btype == "tool_result":
                events.append(
                    Event(
                        kind="tool_result",
                        turn=turn,
                        id=str(block.get("tool_use_id") or ""),
                        lines=_result_lines(block.get("content")),
                    )
                )
    return events


def _paths_of(name: str, inp: dict) -> tuple[str, ...]:
    """Repo paths referenced by one tool_use block."""
    key = _PATH_KEYS.get(name.lower())
    if not key:
        return ()
    val = (inp or {}).get(key)
    return (str(val),) if val else ()


def _result_lines(content) -> int:
    """Line count of a ``tool_result`` payload (string or text-block list)."""
    if isinstance(content, str):
        return content.count("\n") + 1 if content else 0
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text") or ""
                total += text.count("\n") + 1 if text else 0
        return total
    return 0


# Back-compat alias: run.py exported this tuple and tests assert on it.
CLAUDE_ENV_WHITELIST: tuple[str, ...] = (
    ClaudeCodeBackend.auth_env + BASE_ENV_WHITELIST
)
