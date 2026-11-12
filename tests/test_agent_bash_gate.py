"""The spawned agent's PreToolUse Bash gate (``src/agent_bash_gate.sh``).

Runs the real script the way Claude Code does — hook JSON on stdin, exit 2
to deny — so the regexes are tested as shipped, not re-implemented here.

The process-signal rules exist because the harness (``python run.py``),
the step shell and the runner worker share one process group on hosted
runners. A by-name kill from the session (``pkill python``) takes the
harness down with ``exit code 143`` and no log line; a group kill
(``kill 0``) would take the runner too.

Run with: pytest tests/test_agent_bash_gate.py -q
"""
import json
import subprocess
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "src" / "agent_bash_gate.sh"


def _gate(command: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(GATE)], input=payload, capture_output=True, text=True,
        timeout=30, check=False,
    )


def _denied(command: str) -> str:
    r = _gate(command)
    assert r.returncode == 2, f"expected deny for {command!r}, got {r.returncode}"
    assert "Denied by agent-bash-gate" in r.stderr
    return r.stderr


def _allowed(command: str) -> None:
    r = _gate(command)
    assert r.returncode == 0, f"expected allow for {command!r}: {r.stderr}"


# ─── process signals ──────────────────────────────────────────────────────


@pytest.mark.parametrize("command", [
    "pkill -f python",
    "pkill python",
    "killall python3",
    "kill -9 1234",
    "kill 0",
    "kill -- -4242",
    "kill -TERM $(pgrep -f jax)",
    "cd /tmp/work && pkill -f pytest",
    "ps aux | grep jax | awk '{print $2}' | xargs kill -9",
    "pgrep -f 'python -m pytest' | xargs kill",
    "fuser -k 8080/tcp",
    "fuser -vk /dev/nvidia0",
    "sleep 5; killall -9 python",
])
def test_process_signal_commands_are_denied(command):
    err = _denied(command)
    # The remedy is named so the model stops trying variants and bounds
    # the command instead.
    assert "timeout" in err


@pytest.mark.parametrize("command", [
    "timeout 120 python -m pytest -q tests/",
    "python -m pytest tests/test_kill_switch.py -q",
    "grep -rn 'kill' src/ | head",
    "echo killer-feature",
    "git log --oneline -5",
    "ls skills/",
    "fuser 8080/tcp",
    "python -c 'import signal; print(signal.SIGTERM)'",
])
def test_ordinary_commands_mentioning_kill_are_allowed(command):
    _allowed(command)


# ─── the pre-existing rules still hold ────────────────────────────────────


@pytest.mark.parametrize("command", [
    "pip install pynvml",
    "uv add pytest",
    "python -m pip install -e .",
    "curl -s https://example.com",
    "gh pr create --title x",
    "git push origin HEAD",
    "rm -rf /home",
])
def test_existing_denials_unchanged(command):
    _denied(command)


def test_non_bash_payload_is_allowed():
    r = subprocess.run(
        ["bash", str(GATE)], input=json.dumps({"tool_input": {"file_path": "x"}}),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert r.returncode == 0
