"""Backboard R-CLI adapter, parsed against real captured output.

The fixtures in ``tests/fixtures/agents/backboard/`` are verbatim stdout from
R-CLI 3.0.5 driven with a live key — not hand-written approximations of what
the vendor's docs say it emits. That distinction matters here because the docs
understate the CLI: they describe ``--format json`` as an output format and say
nothing about usage, cost or tool events, all three of which are present.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from agents import Capability, resolve
from agents.backboard import BackboardBackend

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agents" / "backboard"


def load(name: str) -> str:
    return (FIXTURES / f"{name}.jsonl").read_text()


def usage_events(name: str) -> list[dict]:
    """The raw camelCase usage payloads in a capture.

    Real events nest as {type, payload: {type, usage: {...}}}.
    """
    out = []
    for line in load(name).splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") != "usage":
            continue
        out.append((event.get("payload") or {}).get("usage") or {})
    return out


@pytest.fixture
def backend():
    return BackboardBackend()


# ─── invocation ─────────────────────────────────────────────────────────────

def test_registered_and_resolvable():
    assert resolve("backboard").name == "backboard"


def test_base_cmd_forces_bypass_permission_mode(backend):
    """Under --print, any tool call that would prompt is denied *silently*.

    Without bypass the run produces no diff and looks like a weak model rather
    than a misconfiguration, so this flag is not optional.
    """
    cmd = backend.base_cmd()
    assert cmd[0] == "backboard"
    assert "--format" in cmd and "json" in cmd
    idx = cmd.index("--permission-mode")
    assert cmd[idx + 1] == "bypass"


def test_model_override_is_threaded(monkeypatch):
    monkeypatch.setenv("BACKBOARD_MODEL", "openai/gpt-5.5")
    cmd = BackboardBackend().base_cmd()
    assert cmd[cmd.index("--model") + 1] == "openai/gpt-5.5"


def test_prompt_is_delivered_on_stdin_not_argv(backend):
    """Linux caps one argv string at 128 KB; a spec bundle can exceed that.

    `--print <prompt>` is the documented one-shot form but would raise E2BIG
    before the agent starts. Piped stdin is equally one-shot and unbounded —
    verified live: the process still exits after one turn and tools still
    execute.
    """
    argv, stdin_text = backend.finalize_cmd(["backboard"], "PROMPT")
    assert argv == ["backboard"]
    assert stdin_text == "PROMPT"
    assert "--print" not in argv


def test_oversized_prompt_would_not_fit_in_argv():
    """Pins the constraint that forced stdin delivery, so nobody 'simplifies'
    it back to --print without hitting this."""
    import subprocess

    oversized = "x" * 131_072
    with pytest.raises(OSError):
        subprocess.run(["/bin/true", oversized], capture_output=True, timeout=10)


def test_preflight_requires_the_api_key(monkeypatch, backend):
    monkeypatch.delenv("BACKBOARD_API_KEY", raising=False)
    ok, messages = backend.preflight()
    assert not ok
    assert any("BACKBOARD_API_KEY" in m for m in messages)

    monkeypatch.setenv("BACKBOARD_API_KEY", "bb-key")
    assert backend.preflight()[0]


def test_auth_env_is_whitelisted(backend):
    wl = backend.env_whitelist()
    assert "BACKBOARD_API_KEY" in wl
    assert "BACKBOARD_API_URL" in wl
    # The orchestrator's own secrets must never reach the agent.
    assert "REMYX_API_KEY" not in wl
    assert "INPUT_GITHUB_TOKEN" not in wl


# ─── usage: emitted per round, camelCase, with dollars ──────────────────────

def test_usage_events_are_summed_into_one_envelope(backend):
    """R-CLI emits `usage` once per agentic round; Claude emits once per call.

    Counting each round as a separate call would inflate claude_calls and
    break every per-call average, so the adapter folds them into one.
    """
    result = backend.parse(0, load("tool_session"), "")
    assert result is not None
    assert len(result.usage_envelopes) == 1, "must fold to a single call"

    env = result.usage_envelopes[0]
    raw = usage_events("tool_session")
    assert len(raw) > 1, "fixture should contain multiple usage events"
    assert env["usage"]["input_tokens"] == sum(u["inputTokens"] for u in raw)
    assert env["usage"]["output_tokens"] == sum(u["outputTokens"] for u in raw)
    assert env["usage"]["cache_read_input_tokens"] == sum(
        u["cachedTokens"] for u in raw
    )


def test_cost_is_authoritative_and_summed(backend):
    """costUsd is on the usage event, so no rate-table row is needed."""
    result = backend.parse(0, load("tool_session"), "")
    raw = usage_events("tool_session")
    expected = sum(u["costUsd"] for u in raw)
    assert result.usage_envelopes[0]["total_cost_usd"] == pytest.approx(expected)
    assert result.usage_envelopes[0]["total_cost_usd"] > 0


def test_camelcase_fields_are_mapped(backend):
    """Every other backend is snake_case; the mapping happens in the adapter."""
    result = backend.parse(0, load("simple_turn"), "")
    usage = result.usage_envelopes[0]["usage"]
    assert set(usage) == {
        "input_tokens", "output_tokens", "cache_read_input_tokens",
    }
    assert usage["input_tokens"] == 5485
    assert usage["output_tokens"] == 5


def test_model_is_carried_through(backend):
    result = backend.parse(0, load("simple_turn"), "")
    assert result.usage_envelopes[0]["model"] == "gpt-5.5"


def test_declares_cost_capability(backend):
    assert backend.can(Capability.COST_USD)
    assert backend.can(Capability.TOKEN_USAGE)


# ─── the final message ──────────────────────────────────────────────────────

def test_final_message_comes_from_assistant_message(backend):
    result = backend.parse(0, load("simple_turn"), "")
    assert result.ok
    assert result.text.strip() == "ok"


# ─── transcript normalization ───────────────────────────────────────────────

def test_tool_events_are_read_from_requested_not_start(backend):
    """`tool:start` carries only `inputSummary`, a human display string.

    The structured input lives on `tool:requested`, so that is what the
    adapter walks.
    """
    result = backend.parse(0, load("tool_session"), "", stream=True)
    uses = [e for e in result.events if e.kind == "tool_use"]
    assert uses, "expected tool_use events from the capture"

    read = next(e for e in uses if e.tool == "read")
    assert read.paths == ("./src/calc.py",)


def test_tool_names_are_case_normalized(backend):
    """Names are lowercase on tool:requested, TitleCase on start/result."""
    result = backend.parse(0, load("tool_session"), "", stream=True)
    tools = {e.tool for e in result.events if e.kind == "tool_use"}
    assert "read" in tools
    assert "execute" in tools
    assert "other" not in tools, "every captured tool should map to a known verb"


def test_apply_patch_paths_come_from_the_patch_body(backend):
    """apply_patch has no file_path key — paths live in the patch headers."""
    result = backend.parse(0, load("tool_session"), "", stream=True)
    writes = [e for e in result.events if e.tool == "write"]
    assert writes, "the capture creates a file via apply_patch"
    assert "src/mul.py" in writes[0].paths


def test_execute_events_carry_the_command(backend):
    result = backend.parse(0, load("tool_session"), "", stream=True)
    commands = {
        e.command for e in result.events
        if e.kind == "tool_use" and e.tool == "execute"
    }
    assert "ls src" in commands


def test_visible_lines_extracted_from_result_titles(backend):
    """"Read 3 lines" carries the payload size in prose, not a numeric field."""
    result = backend.parse(0, load("tool_session"), "", stream=True)
    results = [e for e in result.events if e.kind == "tool_result"]
    assert results
    assert any(e.lines == 3 for e in results), "expected the 'Read 3 lines' title"
    assert sum(e.lines for e in results) > 0


def test_events_suppressed_when_not_streaming(backend):
    """Non-stream callers get no transcript, matching the Claude contract."""
    result = backend.parse(0, load("tool_session"), "", stream=False)
    assert result.events == []


# ─── failure surfacing ──────────────────────────────────────────────────────

AUTH_FAILURE = "\n".join([
    json.dumps({"type": "session:created",
                "payload": {"sessionId": "sess_x", "threadId": None}}),
    json.dumps({"type": "turn:start", "payload": {"turnId": "turn_x"}}),
    json.dumps({"type": "run:error", "payload": {
        "error": 'Backboard request failed: HTTP 401: {"detail":"Invalid API Key format"}'}}),
    json.dumps({"type": "turn:end",
                "payload": {"turnId": "turn_x", "status": "failed"}}),
])


def test_run_error_on_stdout_is_lifted_into_the_text(backend):
    """The cause is a stdout event and stderr is EMPTY — verified live.

    `_format_agent_cli_failure` puts stderr last so the cause survives the
    caller's tail-slice; with nothing on stderr the adapter has to lift the
    run:error payload itself or the operator sees an empty diagnostic.
    """
    result = backend.parse(1, AUTH_FAILURE, "")
    assert result is not None
    assert not result.ok
    assert "HTTP 401" in result.text
    assert "Invalid API Key format" in result.text


def test_failed_turn_status_marks_not_ok(backend):
    result = backend.parse(0, AUTH_FAILURE, "")
    assert not result.ok


def test_unparseable_stdout_returns_none(backend):
    """None tells the orchestrator to fall back to its generic diagnostic."""
    assert backend.parse(1, "not json at all", "boom") is None


# ─── capability honesty ─────────────────────────────────────────────────────

def test_no_turn_cap_declared(backend):
    """R-CLI 3.0.5 has no --max-turns / --max-tokens knob."""
    assert not backend.can(Capability.TURN_CAP)
    assert backend.turn_cap_args(20) == []


def test_unguarded_run_is_surfaced(backend):
    """No launch-time tool gate is wired, so the run must say so."""
    assert not backend.can(Capability.GUARDRAIL_POLICY)
    note = backend.guardrail_note()
    assert note and "backboard" in note


# ─── event ordering is not a contract ───────────────────────────────────────

def test_results_precede_requests_in_real_output(backend):
    """R-CLI emits tool:result BEFORE the tool:requested that describes it.

    Captured live. Claude Code emits the pair the other way round, so any
    parser that assumes tool_use-then-tool_result scores every R-CLI run at
    visible_lines=0 — which is the signal the coverage gate blocks on.
    """
    import json as _json
    order = []
    for line in load("reads_out_of_order").splitlines():
        if not line.strip():
            continue
        etype = _json.loads(line).get("type")
        if etype in ("tool:requested", "tool:result"):
            order.append(etype)
    assert order.index("tool:result") < order.index("tool:requested")


def test_visible_lines_survive_that_ordering(backend):
    import run as _run

    result = backend.parse(0, load("reads_out_of_order"), "", stream=True)
    coverage = _run._selection_coverage_from_events(result.events)
    assert coverage["file_reads"] == 2
    # "Read 3 lines" + "Read 4 lines"
    assert coverage["visible_lines"] == 7


# ─── a refused run must not read as a weak model ────────────────────────────

def test_permission_denial_fails_the_run_loudly(backend):
    """Captured live with the default (manual) permission mode.

    R-CLI reports the refusal as tool:error events but still ends the turn
    `completed` and exits 0 — so an un-bypassed run produces no diff while
    looking like a success. This adapter always passes bypass, so a permission
    error is a configuration problem and has to surface as one.
    """
    result = backend.parse(0, load("permission_denied"), "")
    assert result is not None
    assert not result.ok, "an un-actionable run must not report success"
    assert "permission" in result.text.lower()
    assert "bypass" in result.text, "the message must name the fix"


def test_permission_denial_names_the_refused_tools(backend):
    result = backend.parse(0, load("permission_denied"), "")
    assert "ApplyPatch" in result.text or "Execute" in result.text


def test_ordinary_run_is_unaffected(backend):
    """The detector must not fire on a healthy transcript."""
    assert backend.parse(0, load("tool_session"), "").ok


def test_web_search_is_available(backend):
    """Verified live: the agent reaches a web_search tool, so the staged
    research phase can run on this backend."""
    assert backend.can(Capability.WEB_RESEARCH)
