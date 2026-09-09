"""`mode: smoke` — verify a configuration without doing any work.

A full dispatch is the only way to know a run *works*, but it costs 10-80
minutes and real tokens, which makes it a poor way to answer "did I wire my
secret correctly?". Six of the defects on this branch were found by dispatches
that burned an hour before failing on a one-line misconfiguration.

This mode does the smallest thing that exercises the whole configuration path,
and reaches the vendor for real so it catches what a unit test cannot.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

import run
from agents.backboard import BackboardBackend
from agents.codex import CodexBackend

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agents"


class _Proc:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _target(tmp_path):
    return run.Target(
        repo="o/r", interest_id="i", claude_timeout_s=600,
    )


def test_smoke_reports_ok_when_the_agent_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "_BACKEND", BackboardBackend())
    monkeypatch.setenv("BACKBOARD_API_KEY", "bk")
    monkeypatch.setattr(
        BackboardBackend, "_fetch", staticmethod(lambda url, key: None)
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _Proc(
            (FIXTURES / "backboard" / "simple_turn.jsonl").read_text()
        ),
    )
    result = run.run_agent_smoke(_target(tmp_path))
    assert result["status"] == "smoke_ok"
    assert "ok" in result["reply"].lower()
    assert result["cost_basis"] == "agent_envelope"


def test_smoke_fails_before_calling_when_the_secret_is_missing(tmp_path, monkeypatch):
    """The cheapest possible failure: no vendor call at all."""
    monkeypatch.setattr(run, "_BACKEND", BackboardBackend())
    monkeypatch.delenv("BACKBOARD_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: called.append(1))

    result = run.run_agent_smoke(_target(tmp_path))
    assert result["status"] == "smoke_failed"
    assert "BACKBOARD_API_KEY" in result["error"]
    assert not called, "must not reach the vendor when preflight fails"


def test_smoke_surfaces_a_vendor_rejection(tmp_path, monkeypatch):
    """A wrong model id or an exhausted quota must come back as the error,
    which is the whole point — that is what a dispatch takes an hour to
    tell you."""
    monkeypatch.setattr(run, "_BACKEND", CodexBackend())
    monkeypatch.setenv("CODEX_API_KEY", "ck")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _Proc(
            (FIXTURES / "codex" / "turn_failed_no_credits.jsonl").read_text(),
            returncode=1,
        ),
    )
    result = run.run_agent_smoke(_target(tmp_path))
    assert result["status"] == "smoke_failed"
    assert "credits" in result["error"].lower()


def test_smoke_never_clones_or_publishes(tmp_path, monkeypatch):
    """It must be safe to run against a production repo at any time."""
    src = (Path(__file__).resolve().parent.parent / "src" / "run.py").read_text()
    start = src.index("def run_agent_smoke(")
    body = src[start:src.index("\ndef ", start + 10)]
    for forbidden in ("prepare_workdir", "commit_and_push", "open_pr",
                      "open_issue", "git clone"):
        assert forbidden not in body, f"smoke mode must not {forbidden}"


def test_smoke_caps_the_turn_count(tmp_path, monkeypatch):
    """One turn: enough to prove the vendor answers, not enough to work."""
    src = (Path(__file__).resolve().parent.parent / "src" / "run.py").read_text()
    start = src.index("def run_agent_smoke(")
    body = src[start:src.index("\ndef ", start + 10)]
    assert "max_turns=1" in body


def test_smoke_mode_is_dispatchable():
    import yaml

    action = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "action.yml").read_text()
    )
    assert '"smoke"' in action["inputs"]["mode"]["description"]
    src = (Path(__file__).resolve().parent.parent / "src" / "run.py").read_text()
    assert 'if mode == "smoke":' in src
    assert "runner = run_agent_smoke" in src
