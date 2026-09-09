"""OpenAI Codex adapter.

**Fixture provenance: all captured, none inferred.** Every ``*_live.jsonl``
in ``tests/fixtures/agents/codex/`` is verbatim stdout from codex-cli 0.151.0
against the real API:

* ``turn_failed_no_credits`` — the failure envelope (``turn.failed`` nesting
  ``error.message``, plus the top-level ``{"type":"error"}`` retry events).
* ``turn_completed_live`` — the success path: ``command_execution`` with
  ``command`` / ``aggregated_output`` / ``exit_code`` / ``status``, two
  ``agent_message`` items (a preamble and the answer), and ``turn.completed``
  usage.
* ``file_change_live`` — a patch-tool write, which the model only emits when
  told not to use bash; left to itself it writes files with a shell heredoc,
  so this shape needed forcing to observe.

Runs were kept deliberately cheap — ``gpt-5-nano`` at ``model_reasoning_effort
= "low"`` with web search off, on a two-file repo. The point was to verify
message passing and event shapes, not model quality; the whole exercise cost
under a cent. Note ``minimal`` effort is rejected outright because Codex
enables a ``web_search`` tool that is incompatible with it.

The synthesized ``SESSION`` constant is retained below only as a compact
fixture for the mapping unit tests; every shape in it is now corroborated by
a captured transcript.
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


# ─── captured success path ──────────────────────────────────────────────────

def test_live_success_transcript_parses(backend):
    result = backend.parse(0, real("turn_completed_live"), "")
    assert result is not None and result.ok


def test_final_message_is_the_last_agent_message_not_the_preamble(backend):
    """The model narrates before acting ("I'm going to list the src
    directory..."), so taking the first agent_message would return the plan
    instead of the answer."""
    result = backend.parse(0, real("turn_completed_live"), "")
    assert result.text == "DONE"


def test_live_usage_matches_the_captured_numbers(backend):
    result = backend.parse(0, real("turn_completed_live"), "")
    usage = result.usage_envelopes[0]["usage"]
    assert usage["input_tokens"] == 39075
    assert usage["cache_read_input_tokens"] == 25984
    # 502 output + 320 reasoning, folded because reasoning bills as output.
    assert usage["output_tokens"] == 822


def test_started_and_completed_items_are_not_double_counted(backend):
    """command_execution fires item.started then item.completed with the same
    id; counting both would double every tool call in the coverage stats."""
    import json as _json

    starts = sum(
        1 for line in real("turn_completed_live").splitlines()
        if line.strip() and _json.loads(line).get("type") == "item.started"
    )
    assert starts >= 1, "fixture must contain an item.started to be meaningful"

    result = backend.parse(0, real("turn_completed_live"), "", stream=True)
    uses = [e for e in result.events if e.kind == "tool_use"]
    assert len(uses) == 1


def test_live_shell_command_is_captured(backend):
    result = backend.parse(0, real("turn_completed_live"), "", stream=True)
    execs = [e for e in result.events if e.tool == "execute"]
    assert execs and "ls src" in execs[0].command


# ─── captured file_change ───────────────────────────────────────────────────

def test_live_file_change_paths_are_extracted(backend):
    result = backend.parse(0, real("file_change_live"), "", stream=True)
    writes = [e for e in result.events if e.tool == "write"]
    assert writes, "the capture creates a file via the patch tool"
    assert writes[0].paths and writes[0].paths[0].endswith("src/cube.py")


def test_live_file_change_paths_are_absolute(backend):
    """Codex reports absolute paths, which is why _run_agent relativizes.

    Left as-is the exploration parser buckets the whole run under `tmp`
    instead of `src`; this pins the input side of that fix.
    """
    result = backend.parse(0, real("file_change_live"), "", stream=True)
    writes = [e for e in result.events if e.tool == "write"]
    assert writes[0].paths[0].startswith("/")


# ─── a run that answers "done" having done nothing ──────────────────────────

def test_failed_commands_are_surfaced_as_diagnostics(backend):
    """Captured live: every shell command exited 1 and the model still
    replied DONE, leaving no diff.

    The cause was a weak model mangling its own tool-call escaping, but the
    shape of the failure is what matters — Codex's turn.completed does not
    reflect tool failure, so without this the operator sees an unexplained
    empty changeset.
    """
    result = backend.parse(0, real("all_commands_failed_live"), "", stream=True)
    assert result.text == "DONE"
    assert result.diagnostics, "a silently-empty run must state its cause"
    note = result.diagnostics[0]
    assert "exited non-zero" in note
    assert "empty" in note


def test_failed_commands_do_not_fail_the_run(backend):
    """A non-zero exit is often legitimate — a grep with no match, a test the
    agent is diagnosing. Failing the run on it would be wrong."""
    result = backend.parse(0, real("all_commands_failed_live"), "")
    assert result.ok


def test_diagnostics_stay_out_of_the_answer_text(backend):
    """Several passes parse `text` as a JSON verdict, so notes must never be
    mixed into it."""
    result = backend.parse(0, real("all_commands_failed_live"), "")
    assert "exited non-zero" not in result.text


def test_healthy_run_has_no_diagnostics(backend):
    assert backend.parse(0, real("turn_completed_live"), "").diagnostics == []


def test_inaccessible_model_surfaces_an_actionable_404(backend):
    """Verified live against two models this org cannot reach: the message
    names the model and the fix, rather than a bare non-zero exit."""
    transcript = jsonl(
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "turn.failed", "error": {"message": (
            "unexpected status 404 Not Found: The model `gpt-5.1-codex-mini` "
            "does not exist or you do not have access to it.")}},
    )
    result = backend.parse(1, transcript, "")
    assert not result.ok
    assert "do not have access" in result.text


# ─── server-side tools don't exist off OpenAI ───────────────────────────────

def test_web_search_is_disabled_when_routed_off_openai(monkeypatch):
    """OpenRouter rejects the whole request when Codex offers `web_search`
    ("Server tool request failed", HTTP 400) before the model is reached.

    It is an OpenAI *server-side* tool, not part of the Responses protocol
    third parties implement. Moonshot happens to tolerate it being offered;
    disabling it off-OpenAI makes behavior uniform instead of dependent on
    how forgiving each gateway is.
    """
    monkeypatch.setenv("CODEX_BASE_URL", "https://openrouter.ai/api/v1")
    cmd = CodexBackend().base_cmd()
    assert 'web_search="disabled"' in cmd


def test_web_search_is_left_alone_on_openai(monkeypatch):
    monkeypatch.delenv("CODEX_BASE_URL", raising=False)
    assert "web_search" not in " ".join(CodexBackend().base_cmd())


def test_web_search_uses_the_string_enum_not_a_boolean(monkeypatch):
    """Regression guard on the exact spelling.

    codex 0.151.0 takes `web_search` as a *top-level* key whose value is a
    string enum (`disabled`, `cached`, `indexed`, `live`). The two ways to
    get this wrong fail differently and one of them fails silently:

    * `web_search=false` — rejected at config load,
      "invalid type: unit variant, expected string only in `web_search`",
      which takes the whole run down.
    * `tools.web_search=false` — an unknown key, so Codex ignores it and
      still offers the tool. Captured off a local Responses mock, the
      request body carries `web_search` in `tools` regardless, so the
      HTTP 400 this is meant to prevent still happens.
    """
    monkeypatch.setenv("CODEX_BASE_URL", "https://openrouter.ai/api/v1")
    joined = " ".join(CodexBackend().base_cmd())
    assert "tools.web_search" not in joined
    assert "web_search=false" not in joined


def test_reasoning_effort_is_named_when_routed_off_openai(monkeypatch):
    """Codex only fills in `reasoning.effort` for catalog models.

    For an unrecognized model it sends `reasoning: {"summary": "auto"}` with
    no `effort`, and OpenRouter rejects that with "Reasoning is mandatory for
    this endpoint". Naming one completes the field for every model.
    """
    monkeypatch.setenv("CODEX_BASE_URL", "https://openrouter.ai/api/v1")
    assert 'model_reasoning_effort="medium"' in CodexBackend().base_cmd()


def test_reasoning_effort_is_left_to_codex_on_openai(monkeypatch):
    """On OpenAI's own endpoint Codex knows its catalog, so its per-model
    default is better than anything we would hardcode."""
    monkeypatch.delenv("CODEX_BASE_URL", raising=False)
    assert "model_reasoning_effort" not in " ".join(CodexBackend().base_cmd())


def test_web_research_capability_follows_the_routing(monkeypatch):
    """Reporting it statically would make the orchestrator stage a research
    phase the agent cannot perform."""
    monkeypatch.delenv("CODEX_BASE_URL", raising=False)
    assert CodexBackend().can(Capability.WEB_RESEARCH)

    monkeypatch.setenv("CODEX_BASE_URL", "https://openrouter.ai/api/v1")
    assert not CodexBackend().can(Capability.WEB_RESEARCH)


def test_other_capabilities_are_unaffected_by_routing(monkeypatch):
    monkeypatch.setenv("CODEX_BASE_URL", "https://openrouter.ai/api/v1")
    backend = CodexBackend()
    assert backend.can(Capability.STREAM_TRANSCRIPT)
    assert backend.can(Capability.OUTPUT_SCHEMA)
    assert not backend.can(Capability.TURN_CAP)
