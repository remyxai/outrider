"""OpenAI Codex adapter.

**Fixture provenance.** Mixed, and the difference is called out per test.

``turn_failed_no_credits.jsonl`` is a *real* codex-cli 0.151.0 transcript:
the CLI accepts ``CODEX_API_KEY`` and reached the API, which then refused for
lack of org credits. It confirms the envelope shapes first-hand —
``thread.started`` carries ``thread_id``, ``turn.started`` is bare,
``turn.failed`` nests ``error.message``, and an item is keyed ``type`` (not
``item_type``).

The success-path transcripts below are still *synthesized*, because no run
has yet completed a turn. Their event names, item types and usage field names
were read out of the shipped binary's strings and their item key corrected
against the real capture — but the success-item nesting
(``command_execution``, ``file_change``, ``agent_message`` payloads) remains
inferred. Diff it against a first billed run before trusting Codex cost
numbers in production.
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
        "id": "it_1", "type": "command_execution",
        "command": "ls src",
        "aggregated_output": "calc.py\nmul.py\n"}},
    {"type": "item.completed", "item": {
        "id": "it_2", "type": "file_change",
        "changes": [{"path": "src/mul.py", "kind": "add"}]}},
    {"type": "item.completed", "item": {
        "id": "it_3", "type": "agent_message",
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


# ─── real transcript: auth works, the org had no credits ────────────────────

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agents" / "codex"


def real(name: str) -> str:
    return (FIXTURES / f"{name}.jsonl").read_text()


def test_real_turn_failed_is_parsed(backend):
    """Captured from codex-cli 0.151.0 against the live API."""
    result = backend.parse(1, real("turn_failed_no_credits"), "")
    assert result is not None
    assert not result.ok


def test_real_failure_surfaces_an_actionable_cause(backend):
    """The operator needs the billing cause, not a generic non-zero exit."""
    result = backend.parse(1, real("turn_failed_no_credits"), "")
    assert "no credits remaining" in result.text.lower()


def test_no_usage_is_claimed_for_a_failed_turn(backend):
    """A turn that never completed has no tokens to account."""
    result = backend.parse(1, real("turn_failed_no_credits"), "")
    assert result.usage_envelopes == []


def test_transient_error_events_do_not_crash_the_parser(backend):
    """The real stream carries top-level {"type":"error"} retry events and an
    item.completed whose item type is "error" — neither is modeled, and
    neither may break parsing."""
    import json as _json

    types = {
        _json.loads(line).get("type")
        for line in real("turn_failed_no_credits").splitlines() if line.strip()
    }
    assert "error" in types
    assert backend.parse(1, real("turn_failed_no_credits"), "") is not None


# ─── routing at a non-OpenAI backend ────────────────────────────────────────

def test_no_provider_args_by_default(backend, monkeypatch):
    """Unset CODEX_BASE_URL must leave the argv exactly as it was."""
    monkeypatch.delenv("CODEX_BASE_URL", raising=False)
    assert backend.provider_args() == []
    assert "-c" not in backend.base_cmd()


def test_base_url_routes_codex_at_another_vendor(monkeypatch):
    """The Codex analogue of ANTHROPIC_BASE_URL.

    Verified live against Moonshot: the run reached api.moonshot.ai (its
    gateway answered, with a Moonshot request id) rather than OpenAI.
    """
    monkeypatch.setenv("CODEX_BASE_URL", "https://api.moonshot.ai/v1")
    args = CodexBackend().provider_args()
    joined = " ".join(args)
    assert 'base_url="https://api.moonshot.ai/v1"' in joined
    assert 'model_provider="outrider"' in joined


def test_wire_api_is_pinned_to_responses(monkeypatch):
    """codex-cli 0.151.0 removed Chat Completions support outright:
    `wire_api = "chat"` is no longer supported. A Chat-only provider needs a
    translating gateway, so emitting "chat" would fail at config load."""
    monkeypatch.setenv("CODEX_BASE_URL", "https://api.moonshot.ai/v1")
    joined = " ".join(CodexBackend().provider_args())
    assert 'wire_api="responses"' in joined
    assert "chat" not in joined


def test_credential_is_always_read_from_one_env_name(monkeypatch):
    """Each provider's secret is mapped into CODEX_API_KEY by the action, so
    the adapter never grows a per-vendor branch."""
    monkeypatch.setenv("CODEX_BASE_URL", "https://api.moonshot.ai/v1")
    joined = " ".join(CodexBackend().provider_args())
    assert 'env_key="CODEX_API_KEY"' in joined


def test_cost_label_names_the_vendor_that_served_the_run(monkeypatch):
    """A Codex run against Kimi is not OpenAI spend, and the fleet report
    slices on this field."""
    monkeypatch.setenv("CODEX_BASE_URL", "https://api.moonshot.ai/v1")
    assert CodexBackend().cost_label("kimi-k3") == "Codex \u2192 Moonshot (Kimi) (kimi-k3)"

    monkeypatch.setenv("CODEX_BASE_URL", "https://api.z.ai/api/paas/v4")
    assert "z.ai (GLM)" in CodexBackend().cost_label()

    monkeypatch.delenv("CODEX_BASE_URL", raising=False)
    assert CodexBackend().cost_label() == "Codex \u2192 OpenAI"


def test_unknown_host_is_its_own_series(monkeypatch):
    """Never lump an unrecognized vendor in with a known one."""
    monkeypatch.setenv("CODEX_BASE_URL", "https://gateway.internal/v1")
    assert "gateway.internal" in CodexBackend().cost_label()


def test_base_url_is_whitelisted(backend):
    assert "CODEX_BASE_URL" in backend.env_whitelist()
