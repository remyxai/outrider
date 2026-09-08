"""The agent port must not change what a Claude Code run actually does.

This is the load-bearing test of the agent-backend refactor: with the `agent`
input unset (or set to `claude`), the argv Outrider builds and the environment
it hands the subprocess must be byte-for-byte what shipped before the port.
Every other backend is additive; this file is what lets a large diff be
reviewed with confidence that the default path is untouched.

The expected values below are written as literals on purpose. Deriving them
from the code under test would make the test tautological — a drift in either
direction has to fail here.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

import run
from agents import Capability, available, resolve
from agents.claude import ClaudeCodeBackend


# The exact tuple run.py carried before the port, in order.
HISTORICAL_ENV_WHITELIST = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TERM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TMPDIR",
    "TMP",
    "TEMP",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "CI",
    "GITHUB_ACTIONS",
)


# ─── registry defaults ──────────────────────────────────────────────────────

def test_unset_agent_resolves_to_claude():
    """An unset `agent` input must keep every existing run on Claude Code."""
    for value in (None, "", "   "):
        assert resolve(value).name == "claude"


def test_explicit_claude_resolves_to_claude():
    assert resolve("claude").name == "claude"
    assert resolve("CLAUDE").name == "claude"


def test_unknown_agent_raises_rather_than_falling_back():
    """A typo must fail the run loudly, not silently route to another agent."""
    with pytest.raises(ValueError) as excinfo:
        resolve("claud")
    assert "unknown agent" in str(excinfo.value)


def test_run_module_default_backend_is_claude():
    assert run._BACKEND.name == "claude"


# ─── argv invariance ────────────────────────────────────────────────────────

def test_base_cmd_is_the_historical_prefix():
    """`_agent_base_cmd()` must reproduce the pre-port literal exactly."""
    cmd = run._agent_base_cmd()
    assert cmd[0] == "claude"
    assert cmd[1] == "--dangerously-skip-permissions"
    # The rest is the guardrail policy — asserted in detail below.
    assert cmd[2:] == ClaudeCodeBackend().hardening_settings_arg()


def test_json_argv_matches_pre_port_shape():
    backend = ClaudeCodeBackend()
    argv, stdin_text = backend.finalize_cmd(["claude"], "PROMPT")
    assert argv == ["claude", "--output-format", "json", "-p", "PROMPT"]
    assert stdin_text is None, "the prompt rides in argv for Claude Code"


def test_stream_argv_matches_pre_port_shape():
    """--verbose is required by the CLI when stream-json is paired with -p."""
    backend = ClaudeCodeBackend()
    argv, stdin_text = backend.finalize_cmd(["claude"], "PROMPT", stream=True)
    assert argv == [
        "claude", "--output-format", "stream-json", "--verbose", "-p", "PROMPT",
    ]
    assert stdin_text is None


def test_turn_cap_args_match_pre_port_flag():
    backend = ClaudeCodeBackend()
    assert backend.turn_cap_args(20) == ["--max-turns", "20"]
    assert backend.turn_cap_args("8") == ["--max-turns", "8"]
    assert backend.turn_cap_args(None) == []


# ─── environment invariance ─────────────────────────────────────────────────

def test_whitelist_is_byte_for_byte_historical():
    assert run._CLAUDE_ENV_WHITELIST == HISTORICAL_ENV_WHITELIST


def test_whitelist_composes_from_auth_plus_base():
    """The composed form must equal the historical literal, order included."""
    backend = ClaudeCodeBackend()
    assert backend.env_whitelist() == HISTORICAL_ENV_WHITELIST


def test_github_token_stays_out_of_the_agent_env():
    """A write-scoped token in the agent's context is an exfiltration risk.

    run.py's whitelist deliberately omits GITHUB_TOKEN; the port must not
    quietly reintroduce it. (Note: tests/test_claude_subprocess_env.py asserts
    the opposite — that divergence predates this branch and is a live
    maintainer decision, not something this refactor resolves.)
    """
    assert "GITHUB_TOKEN" not in ClaudeCodeBackend().env_whitelist()


def test_subprocess_env_strips_everything_unlisted(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("REMYX_API_KEY", "remyx-secret")
    monkeypatch.setenv("INPUT_GITHUB_TOKEN", "ghs-bot-token")
    env = run._claude_subprocess_env()
    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    assert "REMYX_API_KEY" not in env
    assert "INPUT_GITHUB_TOKEN" not in env
    assert set(env).issubset(set(HISTORICAL_ENV_WHITELIST))


# ─── the guardrail policy (previously untested) ─────────────────────────────

def test_claude_ships_the_injection_hardening_bash_gate():
    """The PreToolUse gate is a security control with no prior test.

    It removes tool *reach* (package installs, network egress, `gh` writes,
    `git push`) so an agent that complies with an injected instruction still
    can't act on it. If the hook file stops shipping, this fails.
    """
    from agents.claude import AGENT_BASH_GATE

    assert AGENT_BASH_GATE.exists(), f"gate script missing at {AGENT_BASH_GATE}"

    args = ClaudeCodeBackend().hardening_settings_arg()
    assert args[0] == "--settings"
    settings = json.loads(args[1])
    hook = settings["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "Bash"
    assert hook["hooks"][0]["command"] == str(AGENT_BASH_GATE)


def test_claude_declares_a_guardrail_policy():
    """Claude Code can defang tool reach, so it emits no unguarded warning."""
    assert ClaudeCodeBackend().can(Capability.GUARDRAIL_POLICY)
    assert ClaudeCodeBackend().guardrail_note() is None


# ─── capability declarations ────────────────────────────────────────────────

def test_claude_capability_set():
    caps = ClaudeCodeBackend().capabilities
    assert Capability.ONESHOT_JSON in caps
    assert Capability.STREAM_TRANSCRIPT in caps
    assert Capability.TURN_CAP in caps
    assert Capability.TOKEN_USAGE in caps
    assert Capability.COST_USD in caps
    # Claude Code has no JSON-Schema-constrained final response.
    assert Capability.OUTPUT_SCHEMA not in caps


def test_claude_is_registered():
    assert "claude" in available()


# ─── action.yml wiring ──────────────────────────────────────────────────────

def _action_yaml():
    import yaml

    path = Path(__file__).resolve().parent.parent / "action.yml"
    return yaml.safe_load(path.read_text())


def test_agent_input_exists_and_defaults_to_empty():
    """Empty default is what keeps every existing workflow on Claude Code."""
    spec = _action_yaml()["inputs"]["agent"]
    assert spec["default"] == ""
    assert spec["required"] is False


def test_agent_input_documents_every_registered_backend():
    description = _action_yaml()["inputs"]["agent"]["description"]
    for name in available():
        assert f"`{name}`" in description, f"{name} undocumented in action.yml"


def test_provider_input_values_are_not_removed():
    """Version discipline: `provider` values are additive-only forever."""
    description = _action_yaml()["inputs"]["provider"]["description"]
    for value in ("anthropic", "zai", "moonshot", "custom"):
        assert f"`{value}`" in description


def test_agent_is_threaded_into_the_recommend_step():
    steps = _action_yaml()["runs"]["steps"]
    env_blocks = [s.get("env") or {} for s in steps]
    assert any(
        block.get("INPUT_AGENT") for block in env_blocks
    ), "INPUT_AGENT must reach run.py"


def test_install_step_covers_every_registered_backend():
    """A backend the registry knows but the action can't install is a trap:
    the run would fail at `not found on PATH` deep into a dispatch."""
    steps = _action_yaml()["runs"]["steps"]
    install = next(
        s for s in steps if s.get("name") == "Install the coding-agent CLI"
    )
    for backend in (resolve(n) for n in available()):
        assert backend.name in install["run"], f"{backend.name} not installed"


def test_codex_provider_pairing_is_configured():
    """Moonshot serves both Messages and Responses, so provider=moonshot is
    meaningful for claude AND codex — but they are different endpoint
    families and must map to different base URLs."""
    steps = _action_yaml()["runs"]["steps"]
    codex_cfg = next(
        s for s in steps
        if s.get("name") == "Configure Codex backend from provider input"
    )
    assert "api.moonshot.ai/v1" in codex_cfg["run"]
    assert "CODEX_BASE_URL" in codex_cfg["run"]
    # Anthropic has no Responses API — the pair must be rejected, not routed.
    assert "cannot use provider=anthropic" in codex_cfg["run"]


def test_provider_plus_model_works_the_same_way_for_every_agent():
    """One mental model across the action: agent, provider, model.

    R-CLI addresses models as `<provider>/<model>`, so the action composes
    the two rather than making backboard the one agent with different rules.
    """
    steps = _action_yaml()["runs"]["steps"]
    bb = next(
        s for s in steps if s.get("name") == "Configure Backboard backend"
    )
    assert "INPUT_PROVIDER" in bb["run"]
    assert "$INPUT_PROVIDER/$MODEL" in bb["run"]
    # An already-qualified model must not be double-prefixed.
    assert "*/*)" in bb["run"]
    # No agent rejects `provider` outright any more.
    assert not any(
        s.get("name") == "Validate the agent / provider pair" for s in steps
    )


def test_model_input_reaches_every_agent():
    """One `model` input for all three agents.

    `model` was silently ignored for backboard, which needed a BACKBOARD_MODEL
    env var instead — the kind of per-agent special case that makes the action
    feel like a maze. Every agent must honor the same input.
    """
    steps = _action_yaml()["runs"]["steps"]
    configured = {
        s["name"]: s["run"]
        for s in steps
        if s.get("name", "").startswith("Configure ")
        and "backend" in s.get("name", "")
    }
    assert any("CODEX_MODEL" in r for r in configured.values())
    assert any("BACKBOARD_MODEL" in r for r in configured.values())
    for name, body in configured.items():
        assert "INPUT_MODEL" in body, f"{name} ignores the model input"


def test_every_agent_validates_its_credential_before_running():
    """A missing key must fail in the Configure step, not deep in a dispatch."""
    steps = _action_yaml()["runs"]["steps"]
    bodies = " ".join(
        s.get("run", "") for s in steps
        if s.get("name", "").startswith("Configure ")
        and "backend" in s.get("name", "")
    )
    for secret in ("CODEX_API_KEY", "BACKBOARD_API_KEY", "MOONSHOT_API_KEY"):
        assert secret in bodies, f"{secret} never validated"
