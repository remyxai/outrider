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

def test_routing_lives_in_one_step_that_calls_the_resolver():
    """Routing used to be per-agent `case` arms in this file.

    It is now a single step delegating to src/configure_backend.py, so adding
    a provider or an agent never requires editing the action's YAML. If that
    regresses into per-agent shell branches, this fails.
    """
    steps = _action_yaml()["runs"]["steps"]
    configure = [
        s for s in steps if s.get("name") == "Configure the model backend"
    ]
    assert len(configure) == 1, "exactly one routing step"
    assert "configure_backend.py" in configure[0]["run"]

    # No other step may re-implement provider routing.
    others = [
        s for s in steps
        if s.get("name") != "Configure the model backend"
        and "provider" in s.get("run", "").lower()
    ]
    assert not others, f"provider logic leaked into {[s['name'] for s in others]}"


def test_all_four_routing_inputs_reach_the_resolver():
    steps = _action_yaml()["runs"]["steps"]
    configure = next(
        s for s in steps if s.get("name") == "Configure the model backend"
    )
    for var in ("INPUT_AGENT", "INPUT_PROVIDER", "INPUT_MODEL",
                "INPUT_MODEL_BASE_URL"):
        assert var in configure["env"], f"{var} not threaded"


def test_install_step_is_the_only_remaining_per_agent_branch():
    """Installing a CLI genuinely differs per agent (npm vs a native binary),
    so that one stays a case. Everything else is data."""
    steps = _action_yaml()["runs"]["steps"]
    branchy = [
        s.get("name") for s in steps
        if "case \"${INPUT_AGENT" in s.get("run", "")
    ]
    assert branchy == ["Install the coding-agent CLI"]


# ─── out-of-band tooling is agent-aware ─────────────────────────────────────

def test_only_agents_with_a_skills_mechanism_declare_one():
    """Installing a skill into ~/.claude/skills for a Codex run wastes a
    clone and leaves the tool unreachable by the advertised route."""
    assert resolve("claude").skills_home == ".claude/skills"
    assert resolve("codex").skills_home is None
    assert resolve("backboard").skills_home is None


def test_invocation_hint_never_promises_a_route_the_agent_lacks():
    """ENVIRONMENTS.md told every agent ccc was "invocable via /ccc slash
    command". For Codex and R-CLI that is a command they cannot run, and the
    failure looks like the model ignoring an instruction."""
    claude_hint = resolve("claude").tool_invocation_hint("ccc")
    assert "slash command" in claude_hint

    for name in ("codex", "backboard"):
        hint = resolve(name).tool_invocation_hint("ccc")
        assert "slash command" not in hint
        assert "skill" not in hint.lower()
        # The portable surface: every agent has a shell tool.
        assert "shell" in hint.lower()


def test_tool_steps_ask_the_backend_rather_than_hardcoding_claude():
    steps = _action_yaml()["runs"]["steps"]
    bodies = {s.get("name", ""): s.get("run", "") for s in steps}
    ccc = next(v for k, v in bodies.items() if "cocoindex" in k)
    assert "agent_tooling.py" in ccc
    assert ".claude/skills" not in ccc, "skills path must come from the backend"

    envmd = next(v for k, v in bodies.items() if "ENVIRONMENTS.md" in k)
    assert "agent_tooling.py" in envmd
    assert "Claude Code skill" not in envmd
