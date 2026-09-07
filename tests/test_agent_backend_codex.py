"""OpenAI Codex adapter.

**Fixture provenance.** Unlike the Backboard tests, these transcripts are
*synthesized*, not captured: this machine has codex-cli 0.151.0 installed but
not authenticated, so no live session could be recorded. The event names,
item types and usage field names below were read out of the shipped binary's
strings (``turn.completed``, ``turn.failed``, ``item.completed``,
``command_execution``, ``file_change``, ``agent_message``, ``web_search``,
``input_tokens``, ``cached_input_tokens``, ``reasoning_output_tokens``), and
the flags from ``codex exec --help``. The *nesting* of those fields is the
part that is inferred rather than observed, so a first live run should be
diffed against these fixtures before the backend is trusted in production.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from agents import Capability, resolve
from agents.codex import CodexBackend


def jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events)


SESSION = jsonl(
    {"type": "thread.started", "thread_id": "0199a213-81c0-7800-8aa1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {
        "id": "it_1", "item_type": "command_execution",
        "command": "ls src",
        "aggregated_output": "calc.py\nmul.py\n"}},
    {"type": "item.completed", "item": {
        "id": "it_2", "item_type": "file_change",
        "changes": [{"path": "src/mul.py", "kind": "add"}]}},
    {"type": "item.completed", "item": {
        "id": "it_3", "item_type": "agent_message",
        "text": "Added src/mul.py."}},
    {"type": "turn.completed", "usage": {
        "input_tokens": 5473, "cached_input_tokens": 4608,
        "output_tokens": 99, "reasoning_output_tokens": 40}},
)

FAILED = jsonl(
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "turn.started"},
    {"type": "turn.failed", "error": {"message": "401 Unauthorized"}},
)


@pytest.fixture
def backend():
    return CodexBackend()


# ─── invocation ─────────────────────────────────────────────────────────────

def test_registered_and_resolvable():
    assert resolve("codex").name == "codex"


def test_base_cmd_shape(backend):
    cmd = backend.base_cmd()
    assert cmd[:3] == ["codex", "exec", "--json"]


def test_sandbox_is_bypassed_so_the_agent_keeps_network(backend):
    """workspace-write blocks network, and the agent needs `gh` / `remyxai`.

    The runner is an isolated throwaway VM, so bypassing is the right trade;
    the alternative is -c sandbox_workspace_write.network_access=true.
    """
    assert "--dangerously-bypass-approvals-and-sandbox" in backend.base_cmd()


def test_model_override_is_threaded(monkeypatch):
    monkeypatch.setenv("CODEX_MODEL", "o3")
    cmd = CodexBackend().base_cmd()
    assert cmd[cmd.index("-m") + 1] == "o3"


def test_prompt_is_delivered_on_stdin(backend):
    """`-` makes codex read the whole prompt from stdin.

    Outrider's spec-bundle invocations are large; argv delivery risks ARG_MAX.
    """
    argv, stdin_text = backend.finalize_cmd(["codex", "exec"], "PROMPT")
    assert argv[-1] == "-"
    assert stdin_text == "PROMPT"


def test_preflight_requires_the_api_key(monkeypatch, backend):
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    ok, messages = backend.preflight()
    assert not ok
    assert any("CODEX_API_KEY" in m for m in messages)


def test_auth_env_is_whitelisted(backend):
    wl = backend.env_whitelist()
    assert "CODEX_API_KEY" in wl
    assert "CODEX_HOME" in wl
    assert "REMYX_API_KEY" not in wl


# ─── usage ──────────────────────────────────────────────────────────────────

def test_usage_is_read_off_turn_completed(backend):
    result = backend.parse(0, SESSION, "")
    assert result is not None
    usage = result.usage_envelopes[0]["usage"]
    assert usage["input_tokens"] == 5473
    assert usage["cache_read_input_tokens"] == 4608


def test_reasoning_tokens_fold_into_output(backend):
    """Reasoning tokens bill as output; omitting them under-counts cost."""
    result = backend.parse(0, SESSION, "")
    usage = result.usage_envelopes[0]["usage"]
    assert usage["output_tokens"] == 99 + 40


def test_no_dollar_figure_is_claimed(backend):
    """Codex reports tokens only — cost must come from the rate table.

    Emitting total_cost_usd here would make _record_claude_usage prefer a
    number the CLI never produced.
    """
    assert "total_cost_usd" not in result_envelope(backend)
    assert not backend.can(Capability.COST_USD)


def result_envelope(backend) -> dict:
    return backend.parse(0, SESSION, "").usage_envelopes[0]


# ─── the final message ──────────────────────────────────────────────────────

def test_final_message_comes_from_agent_message_item(backend):
    result = backend.parse(0, SESSION, "")
    assert result.ok
    assert result.text == "Added src/mul.py."


# ─── transcript normalization ───────────────────────────────────────────────

def test_file_change_maps_to_write_with_paths(backend):
    result = backend.parse(0, SESSION, "", stream=True)
    writes = [e for e in result.events if e.tool == "write"]
    assert writes and writes[0].paths == ("src/mul.py",)


def test_command_execution_maps_to_execute(backend):
    result = backend.parse(0, SESSION, "", stream=True)
    execs = [
        e for e in result.events if e.kind == "tool_use" and e.tool == "execute"
    ]
    assert execs and execs[0].command == "ls src"


def test_command_output_contributes_visible_lines(backend):
    result = backend.parse(0, SESSION, "", stream=True)
    results = [e for e in result.events if e.kind == "tool_result"]
    assert sum(e.lines for e in results) > 0


def test_events_suppressed_when_not_streaming(backend):
    assert backend.parse(0, SESSION, "", stream=False).events == []


# ─── failure surfacing ──────────────────────────────────────────────────────

def test_turn_failed_surfaces_the_cause(backend):
    result = backend.parse(1, FAILED, "")
    assert not result.ok
    assert "401 Unauthorized" in result.text


def test_unparseable_stdout_returns_none(backend):
    assert backend.parse(1, "<not jsonl>", "boom") is None


# ─── capability honesty ─────────────────────────────────────────────────────

def test_no_turn_cap_declared(backend):
    """`codex exec` has no round-limit flag at 0.151.0.

    This is the capability gap that costs real money on a cron-driven action,
    so it must not be silently papered over.
    """
    assert not backend.can(Capability.TURN_CAP)
    assert backend.turn_cap_args(20) == []


def test_output_schema_is_available(backend):
    """The one thing Codex does that Claude Code cannot."""
    assert backend.can(Capability.OUTPUT_SCHEMA)


def test_unguarded_run_is_surfaced(backend):
    assert not backend.can(Capability.GUARDRAIL_POLICY)
    assert "codex" in (backend.guardrail_note() or "")
