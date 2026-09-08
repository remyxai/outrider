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
