"""Orchestrator phases must work on a non-Claude backend.

Everything else in this branch tests the port at or below the `_run_agent`
boundary. That leaves a real gap: every existing phase test monkeypatches the
runner, so the phases *above* the boundary — selection, coverage gating,
verdict parsing — had never seen a backend that emits a different transcript
shape.

These tests stub `subprocess.run` rather than the runner, so the whole chain
executes for real: finalize_cmd → subprocess → the backend's own parser →
normalized events → coverage → gate → verdict. The transcripts are the
captured R-CLI and Codex sessions, so the shapes are the vendors' own.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

import run
from agents.backboard import BackboardBackend
from agents.codex import CodexBackend

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agents"


def transcript(agent: str, name: str) -> str:
    return (FIXTURES / agent / f"{name}.jsonl").read_text()


class _Proc:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _make_candidates():
    geo = run._paper_to_recommendation(
        {
            "title": "GeoWeaver",
            "resource_id": "2605.22558v1",
            "relevance_score": 0.98,
            "reasoning": "geometric grounding — a VLM architecture",
            "interest_name": "VQASynth",
            "resource": {"abstract": "Spatio-temporal reasoning in VLMs..."},
        },
        fallback_interest_name="fallback",
        interest_context="team focus body",
        experiment_history="",
    )
    count = run._paper_to_recommendation(
        {
            "title": "HieraCount open-world counting",
            "resource_id": "2605.10887v1",
            "relevance_score": 0.87,
            "reasoning": "explicit counting granularity",
            "resource": {"abstract": "Open-world counting remains brittle..."},
        },
        fallback_interest_name="VQASynth",
        interest_context="team focus body",
        experiment_history="",
    )
    return geo, count


VERDICT = json.dumps({
    "chosen_index": 1,
    "integration_shape": "addition",
    "reasoning": "verified the call site at src/calc.py:1",
    "rejected": [],
})


def _rcli_selection_stream() -> str:
    """A real R-CLI tool session with a selection verdict as the answer."""
    lines = []
    for line in transcript("backboard", "tool_session").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        # Replace the captured answer with a selection verdict; every other
        # event — the tool calls, the usage, the ordering — stays verbatim.
        if event.get("type") == "assistant:message":
            event["payload"]["text"] = VERDICT
        lines.append(json.dumps(event))
    return "\n".join(lines)


# ─── selection on Backboard R-CLI ───────────────────────────────────────────

@pytest.fixture
def rcli(monkeypatch):
    monkeypatch.setattr(run, "_BACKEND", BackboardBackend())
    monkeypatch.setenv("BACKBOARD_API_KEY", "bk-test")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _Proc(_rcli_selection_stream()),
    )


def test_selection_pass_works_on_rcli(tmp_path, rcli):
    """The whole phase, with R-CLI's own transcript shape underneath."""
    geo, count = _make_candidates()
    selection = run.select_recommendation(tmp_path, "pkg", [geo, count])
    assert selection is not None
    assert selection["chosen_index"] == 1


def test_selection_coverage_is_populated_from_the_rcli_transcript(tmp_path, rcli):
    """The verdict alone is not enough — the telemetry that rides with it has
    to survive a different vendor's event shape, including R-CLI emitting
    tool:result *before* the tool:requested that describes it."""
    geo, count = _make_candidates()
    selection = run.select_recommendation(tmp_path, "pkg", [geo, count])
    coverage = selection["selection_coverage"]
    assert coverage["file_reads"] >= 1
    assert coverage["visible_lines"] >= 1, "reads must be paired with results"


def test_rcli_cost_lands_in_run_cost(tmp_path, rcli):
    before = run._RUN_COST["cost_usd"]
    geo, count = _make_candidates()
    run.select_recommendation(tmp_path, "pkg", [geo, count])
    assert run._RUN_COST["cost_usd"] > before, "usage must be accounted"
    assert run._RUN_COST["cost_basis"] == "agent_envelope"


# ─── the coverage gate, cross-agent ─────────────────────────────────────────

def test_enforce_gate_does_not_reject_an_rcli_pick(tmp_path, rcli, monkeypatch):
    """R-CLI reports a transcript, so the gate applies normally — but it must
    apply on the *normalized* numbers, not score zero and downgrade."""
    monkeypatch.setenv("REMYX_SELECTION_COVERAGE_GATE", "enforce")
    monkeypatch.setenv("REMYX_SELECTION_MIN_VISIBLE_LINES", "1")
    geo, count = _make_candidates()
    selection = run.select_recommendation(tmp_path, "pkg", [geo, count])
    assert selection["chosen_index"] == 1


# ─── implementation invocation on Codex ─────────────────────────────────────

def test_invoke_claude_code_runs_on_codex(tmp_path, monkeypatch):
    """The implementation entry point is agent-neutral despite its name.

    Driven with the captured Codex success transcript, so the JSONL-only
    shape and the `-o`-less final-message path both execute.
    """
    monkeypatch.setattr(run, "_BACKEND", CodexBackend())
    monkeypatch.setenv("CODEX_API_KEY", "ck-test")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _Proc(transcript("codex", "turn_completed_live")),
    )
    bundle = tmp_path / run.BUNDLE_DIR_NAME
    bundle.mkdir(parents=True)
    (bundle / "INVOCATION.md").write_text("Implement the thing.")

    ok, log_tail = run.invoke_claude_code(tmp_path, timeout_s=60)
    assert ok
    assert "DONE" in log_tail


def test_codex_prompt_is_delivered_on_stdin_through_the_orchestrator(
    tmp_path, monkeypatch
):
    """Codex takes the prompt on stdin; the orchestrator must actually pass
    it as `input=`, not drop it."""
    monkeypatch.setattr(run, "_BACKEND", CodexBackend())
    monkeypatch.setenv("CODEX_API_KEY", "ck-test")
    seen = {}

    def capture(*args, **kwargs):
        seen.update(kwargs)
        seen["argv"] = args[0] if args else kwargs.get("args")
        return _Proc(transcript("codex", "turn_completed_live"))

    monkeypatch.setattr(subprocess, "run", capture)
    bundle = tmp_path / run.BUNDLE_DIR_NAME
    bundle.mkdir(parents=True)
    (bundle / "INVOCATION.md").write_text("UNIQUE-PROMPT-BODY")

    run.invoke_claude_code(tmp_path, timeout_s=60)
    assert "UNIQUE-PROMPT-BODY" in (seen.get("input") or "")
    assert seen["argv"][-1] == "-", "codex reads the prompt from stdin"
    assert seen.get("cwd") == tmp_path


def test_rcli_prompt_is_also_delivered_on_stdin(tmp_path, monkeypatch):
    """R-CLI moved to stdin because argv caps a single arg at 128 KB."""
    monkeypatch.setattr(run, "_BACKEND", BackboardBackend())
    monkeypatch.setenv("BACKBOARD_API_KEY", "bk-test")
    seen = {}

    def capture(*args, **kwargs):
        seen.update(kwargs)
        seen["argv"] = args[0] if args else kwargs.get("args")
        return _Proc(transcript("backboard", "simple_turn"))

    monkeypatch.setattr(subprocess, "run", capture)
    bundle = tmp_path / run.BUNDLE_DIR_NAME
    bundle.mkdir(parents=True)
    (bundle / "INVOCATION.md").write_text("UNIQUE-PROMPT-BODY")

    run.invoke_claude_code(tmp_path, timeout_s=60)
    assert "UNIQUE-PROMPT-BODY" in (seen.get("input") or "")
    assert "--print" not in seen["argv"]


# ─── a refused / empty run still routes through the normal failure path ─────

def test_rcli_permission_denial_fails_the_implementation_call(
    tmp_path, monkeypatch
):
    """An un-bypassed R-CLI run exits 0 with the turn "completed", so without
    the adapter's detection this would look like a successful no-op."""
    monkeypatch.setattr(run, "_BACKEND", BackboardBackend())
    monkeypatch.setenv("BACKBOARD_API_KEY", "bk-test")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _Proc(
            transcript("backboard", "permission_denied"), returncode=0
        ),
    )
    bundle = tmp_path / run.BUNDLE_DIR_NAME
    bundle.mkdir(parents=True)
    (bundle / "INVOCATION.md").write_text("Implement the thing.")

    ok, log_tail = run.invoke_claude_code(tmp_path, timeout_s=60)
    assert not ok
    assert "permission" in log_tail.lower()


# ─── operator-facing logs must name the agent that actually ran ─────────────

def test_implementation_log_names_the_configured_agent(tmp_path, monkeypatch, caplog):
    """A real Backboard run logged "invoking Claude Code".

    Harmless to the run, but an operator reading the job log would conclude
    the wrong agent executed — and cost, latency and failure modes all differ
    per agent, so that is a genuinely misleading breadcrumb.
    """
    import logging

    monkeypatch.setattr(run, "_BACKEND", BackboardBackend())
    monkeypatch.setenv("BACKBOARD_API_KEY", "bk-test")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _Proc(transcript("backboard", "simple_turn")),
    )
    bundle = tmp_path / run.BUNDLE_DIR_NAME
    bundle.mkdir(parents=True)
    (bundle / "INVOCATION.md").write_text("do the thing")

    with caplog.at_level(logging.INFO):
        run.invoke_claude_code(tmp_path, timeout_s=60)

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "Backboard R-CLI" in logged
    assert "invoking Claude Code" not in logged


def test_no_operator_log_hardcodes_claude():
    """Guard the whole class, not just the one line that was found."""
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "src" / "run.py").read_text()
    offenders = [
        line.strip() for line in src.splitlines()
        if re.search(r'log\.(info|warning|error)\(f?"[^"]*Claude', line)
    ]
    assert not offenders, f"operator logs must name the active agent: {offenders}"


# ─── failure remedies must point at the right vendor ────────────────────────

AUTH_TAIL = "HTTP 401 authentication_error invalid api key"


def test_failure_remedy_names_the_agents_own_console(monkeypatch):
    """A Backboard failure used to tell the operator to top up at Anthropic.

    The recognizable causes are common across vendors, but the remedy — which
    console, which secret — is not.
    """
    monkeypatch.setattr(run, "_BACKEND", BackboardBackend())
    monkeypatch.delenv("INPUT_PROVIDER", raising=False)
    blob = " ".join(run._agent_failure_blocks("backboard", AUTH_TAIL, 3))
    assert "BACKBOARD_API_KEY" in blob
    assert "app.backboard.io" in blob
    assert "anthropic" not in blob.lower()


def test_codex_failure_remedy_points_at_openai(monkeypatch):
    monkeypatch.setattr(run, "_BACKEND", CodexBackend())
    monkeypatch.delenv("INPUT_PROVIDER", raising=False)
    blob = " ".join(run._agent_failure_blocks("codex", AUTH_TAIL, 1))
    assert "CODEX_API_KEY" in blob
    assert "platform.openai.com" in blob


def test_remedy_names_the_providers_secret_not_the_agents(monkeypatch):
    """A z.ai-routed Claude Code run authenticates with ZAI_API_KEY, so
    telling the operator to reset ANTHROPIC_API_KEY sends them to the wrong
    secret."""
    from agents.claude import ClaudeCodeBackend

    monkeypatch.setattr(run, "_BACKEND", ClaudeCodeBackend())
    monkeypatch.setenv("INPUT_PROVIDER", "zai")
    blob = " ".join(run._agent_failure_blocks("claude", AUTH_TAIL, 2))
    assert "ZAI_API_KEY" in blob
    assert "gh secret set ZAI_API_KEY" in blob


def test_claude_default_remedy_is_unchanged(monkeypatch):
    from agents.claude import ClaudeCodeBackend

    monkeypatch.setattr(run, "_BACKEND", ClaudeCodeBackend())
    monkeypatch.delenv("INPUT_PROVIDER", raising=False)
    blob = " ".join(run._agent_failure_blocks("claude", AUTH_TAIL, 2))
    assert "ANTHROPIC_API_KEY" in blob
    assert "console.anthropic.com" in blob


def test_credit_exhaustion_recognizes_both_vendors_wording(monkeypatch):
    """Anthropic says "credit balance is too low"; OpenAI says "no credits
    remaining" — both are the same actionable state."""
    monkeypatch.setattr(run, "_BACKEND", CodexBackend())
    monkeypatch.delenv("INPUT_PROVIDER", raising=False)
    blob = " ".join(
        run._agent_failure_blocks("codex", "You have no credits remaining.", 1)
    )
    assert "credit balance exhausted" in blob
    assert "platform.openai.com" in blob


def test_no_user_facing_string_hardcodes_the_agent_name():
    """Guard the whole class, including multi-line calls and step-summary
    lines, not just the one call that was found on a real run.

    Contract names are exempt: telemetry fields (`claude_calls`,
    `claude_log_tail`), the `claude-timeout` input, status values the engine
    stores (`claude_failed`, `claude_code_envelope`), the back-compat
    `_CLAUDE_ENV_WHITELIST` alias, and the Claude-Code-specific auth
    validator, which is only ever called for that agent.
    """
    import re
    from pathlib import Path

    CONTRACTS = (
        "_CLAUDE_ENV", "claude_calls", "claude_log_tail", "claude-timeout",
        "claude_timeout", "claude_failed", "claude_code_envelope",
        # The auth-header matrix inside _validate_claude_auth_env, which is
        # only ever called for Claude Code. The sentence spans two source
        # lines, so both fragments are exempt.
        "Claude Code prefers",
        "non-default backend is configured",
        "pre_pr_fidelity_failed_claude",
    )
    src = (Path(__file__).resolve().parent.parent / "src" / "run.py").read_text()
    offenders = []
    for lineno, line in enumerate(src.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for literal in re.findall(r'"([^"]*Claude[^"]*)"', line):
            if any(c in line for c in CONTRACTS):
                continue
            offenders.append(f"{lineno}: {literal[:60]}")
    assert not offenders, (
        "user-facing text must name the active agent: " + "; ".join(offenders)
    )


# ─── stored status vs displayed status ──────────────────────────────────────

def test_displayed_status_is_agent_neutral():
    """A Backboard failure rendered its step-summary header as
    `claude_failed`, which is the same misleading breadcrumb the log sweep
    removed everywhere else."""
    assert run._display_status("claude_failed") == "agent_failed"
    assert run._display_status("fidelity_failed_claude") == "fidelity_failed_agent"


def test_unmapped_statuses_pass_through_unchanged():
    for status in ("pr_opened", "issue_opened_preflight", "skipped_by_cadence"):
        assert run._display_status(status) == status


def test_the_stored_status_value_is_not_changed():
    """The engine persists these and existing queries group on them, so the
    posted value must stay until a server-side normalize-on-read lands."""
    src = (
        Path(__file__).resolve().parent.parent / "src" / "run.py"
    ).read_text()
    assert 'result["status"] = "claude_failed"' in src, (
        "the stored value must remain claude_failed until the server maps it"
    )


def test_the_header_renders_the_display_form(tmp_path, monkeypatch):
    lines = run._agent_failure_blocks("backboard", "some tail", 1)
    assert lines  # sanity
    src = (
        Path(__file__).resolve().parent.parent / "src" / "run.py"
    ).read_text()
    assert "_display_status(status)" in src
