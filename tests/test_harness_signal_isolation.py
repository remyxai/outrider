"""The harness must not die of signals raised inside the agent session.

Three openpi dispatches ended with ``Process completed with exit code
143`` 15–27 minutes into the implementation phase, with nothing logged
after "→ invoking Claude Code". The step's ``python run.py`` received a
SIGTERM. Runner resources, the harness's own timeout path and Claude
Code's Bash-timeout teardown were each ruled out on the runner; the only
consistent source was a signal sent from inside the agent session.

Three layers close that:

1. ``agent_bash_gate.sh`` denies ``kill``/``pkill``/``killall`` (see
   ``test_agent_bash_gate.py``).
2. ``_run_agent`` starts the agent in its own session, so a group-wide
   signal from the session cannot reach the harness.
3. ``main()`` installs a SIGTERM handler that logs the cause before
   exiting 143, so the next such run explains itself.

Run with: pytest tests/test_harness_signal_isolation.py -q
"""
import logging
import signal
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import run  # noqa: E402


def _source_between(start: str, end: str) -> str:
    src = (ROOT / "src" / "run.py").read_text()
    body = src[src.index(start):]
    return body[: body.index(end)]


def test_the_agent_is_spawned_in_its_own_session():
    body = _source_between("def _run_agent(", "except subprocess.TimeoutExpired")
    assert "start_new_session=True" in body


def test_run_agent_passes_start_new_session_to_subprocess(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, '{"result": "ok"}', "")

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    run._run_agent(["claude"], "prompt", tmp_path, 60)
    assert seen.get("start_new_session") is True


def test_main_installs_the_sigterm_handler_first():
    body = _source_between("def main():", "mode = (")
    assert "_install_harness_signal_handler()" in body


def test_sigterm_handler_logs_the_cause_and_exits_143(caplog):
    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert run._install_harness_signal_handler() is True
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler) and handler is not previous
        with caplog.at_level(logging.ERROR, logger=run.log.name):
            with pytest.raises(SystemExit) as exc:
                handler(signal.SIGTERM, None)
        assert exc.value.code == 143
        assert "received SIGTERM" in caplog.text
        assert "pkill" in caplog.text
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_sigterm_handler_install_failure_is_not_fatal(monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(run.signal, "signal", boom)
    assert run._install_harness_signal_handler() is False
