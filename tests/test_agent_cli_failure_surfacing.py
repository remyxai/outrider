"""Tests for surfacing agent-CLI stderr on failure.

When the agent CLI hard-rejects (usage limit, credit balance, auth) it
exits fast with the cause on **stderr** and no parseable JSON envelope on
stdout. The orchestrator stores only the tail of the returned string into
``claude_log_tail``, so the real cause must reach that tail rather than
being crowded out by stdout noise. These tests pin that contract.

Run with: pytest tests/test_agent_cli_failure_surfacing.py -q
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


def _fake_proc(stdout="", stderr="", returncode=0):
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


# ─── _format_agent_cli_failure: provider-agnostic, stderr-last ─────────────


def test_failure_diagnostic_is_provider_agnostic():
    """The tool label is parameterized — not hard-coded to Claude."""
    out = run._format_agent_cli_failure("aider", 1, "", "boom")
    assert out.startswith("[aider exited 1, no JSON envelope parsed]")
    assert "claude" not in out.lower()


def test_failure_diagnostic_puts_stderr_last():
    """stderr must be the trailing content so the caller's tail-slice keeps
    it even when stdout is bulky."""
    out = run._format_agent_cli_failure(
        "claude", 1, "x" * 10_000, "Claude AI usage limit reached"
    )
    # stderr survives the orchestrator's last-1KB slice.
    assert "Claude AI usage limit reached" in out[-1000:]
    # stdout head is capped so it can't dominate the window.
    assert "…(truncated)" in out


def test_failure_diagnostic_marks_empty_stderr():
    out = run._format_agent_cli_failure("claude", 1, "", "")
    assert "--- STDERR ---\n(empty)" in out


# ─── _run_claude_json: no-envelope path surfaces stderr ────────────────────


def test_run_claude_json_no_envelope_surfaces_stderr(monkeypatch, tmp_path):
    monkeypatch.setattr(
        run.subprocess, "run",
        lambda *a, **k: _fake_proc(stdout="", stderr="credit balance is too low",
                                   returncode=1),
    )
    ok, text = run._run_claude_json(["claude"], "prompt", tmp_path, 60)
    assert ok is False
    assert "credit balance is too low" in text
    # No usage recorded — there was no envelope to account.
    assert run._RUN_COST["claude_calls"] == 0


def test_run_claude_json_error_envelope_appends_stderr(monkeypatch, tmp_path):
    """An is_error envelope with a populated `result` still gets stderr
    appended — the operational cause often lives only on stderr."""
    run._reset_run_cost()
    monkeypatch.setattr(
        run.subprocess, "run",
        lambda *a, **k: _fake_proc(
            stdout='{"result": "I could not proceed.", "is_error": true}',
            stderr="usage limit reached",
            returncode=0,
        ),
    )
    ok, text = run._run_claude_json(["claude"], "prompt", tmp_path, 60)
    assert ok is False
    assert "I could not proceed." in text
    assert "usage limit reached" in text


def test_run_claude_stream_no_result_event_surfaces_stderr(monkeypatch, tmp_path):
    run._reset_run_cost()
    monkeypatch.setattr(
        run.subprocess, "run",
        lambda *a, **k: _fake_proc(stdout="", stderr="invalid x-api-key",
                                   returncode=1),
    )
    ok, text, events = run._run_claude_stream(["claude"], "prompt", tmp_path, 60)
    assert ok is False
    assert "invalid x-api-key" in text
