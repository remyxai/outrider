"""Tests for the Claude CLI subprocess env whitelist.

The Claude CLI subprocess inherits whatever env we pass it. If we pass
the parent runner's ``os.environ`` verbatim, the agent's Bash tool can
echo secrets the runner holds (`REMYX_API_KEY`, `GITHUB_TOKEN` /
`INPUT_GITHUB_TOKEN`, `INPUT_*` action inputs, etc.) via `printenv`,
`git config --list`, `curl -v`, or any command that prints request
headers in debug mode. Stripping the env at the launch boundary stops
secrets from entering the agent's context at all.

This pairs with the v1.6.4 outbound-body scrubber: that one catches
secrets at egress; this one prevents them from being available to echo
in the first place. Defense in depth — the v1.6.4 scrubber is the
load-bearing fix, this is belt-and-suspenders.

The whitelist contains only what the Claude CLI legitimately needs
(auth, system paths, locale, temp dirs, XDG paths, CI sentinels). The
forbidden set is what the agent must NOT be able to echo —
specifically the secret-bearing env vars the parent runner holds.

Run with: pytest tests/test_claude_subprocess_env.py -q
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

import run  # noqa: E402


# Vars the agent's subprocess MUST inherit for the CLI to work.
_REQUIRED_FOR_CLI = ("ANTHROPIC_API_KEY", "PATH", "HOME")

# Vars the agent's subprocess MUST NOT see (would let it echo a secret).
_FORBIDDEN = (
    "REMYX_API_KEY",
    "GITHUB_TOKEN",
    "INPUT_GITHUB_TOKEN",
    "INPUT_INTEREST_ID",
    "INPUT_DRAFT_MODE",
    "GITHUB_ACTOR",
    "GITHUB_REPOSITORY",
    "GITHUB_REF",
    "GITHUB_SHA",
)


# ─── Whitelist content regression checks ─────────────────────────


def test_whitelist_includes_anthropic_api_key():
    """Removing this breaks the CLI entirely — the Claude CLI requires
    ANTHROPIC_API_KEY to be in the subprocess env to authenticate.
    Regression-pin so a future cleanup doesn't trim it as 'unused'."""
    assert "ANTHROPIC_API_KEY" in run._CLAUDE_ENV_WHITELIST


def test_whitelist_includes_path_and_home():
    """Required for the CLI binary lookup + per-user config state."""
    assert "PATH" in run._CLAUDE_ENV_WHITELIST
    assert "HOME" in run._CLAUDE_ENV_WHITELIST


def test_whitelist_excludes_remyx_api_key():
    """The Remyx engine key authenticates the parent runner to
    engine.remyx.ai for the bot-token-minting step. The agent doesn't
    need it; including it would let the agent echo it via printenv."""
    assert "REMYX_API_KEY" not in run._CLAUDE_ENV_WHITELIST


def test_whitelist_includes_workflow_github_token():
    """The workflow's built-in GITHUB_TOKEN is allowed through so the
    selection-pass agent's `gh` CLI invocations can authenticate.
    Without it, the agent falls back to
    unauthenticated GitHub API at 60 req/hr per shared runner IP and
    can't view private-repo content. Trade-off justified by the
    egress defenses (v1.6.4 scrubber + v1.6.8 diagnostic + v1.6.10
    prompt redaction)."""
    assert "GITHUB_TOKEN" in run._CLAUDE_ENV_WHITELIST


def test_whitelist_excludes_bot_installation_token():
    """The bot's installation token arrives via INPUT_GITHUB_TOKEN
    (the orchestrator uses it for PR/Issue creation through gh_api).
    The agent must NOT see this token — it's strictly more
    privileged than the workflow built-in (cross-repo, write scopes
    the bot was granted). Keeping it stripped preserves the
    least-privilege principle even after restoring GITHUB_TOKEN."""
    assert "INPUT_GITHUB_TOKEN" not in run._CLAUDE_ENV_WHITELIST


def test_whitelist_excludes_action_inputs():
    """INPUT_* action-input vars are runner-internal — even though most
    aren't secret-bearing, they identify the customer/repo and shouldn't
    be enumerable by the agent. The agent gets repo context via the
    workdir and the SPEC.md / orientation block, not via env."""
    for name in run._CLAUDE_ENV_WHITELIST:
        assert not name.startswith("INPUT_"), (
            f"INPUT_* var {name!r} in whitelist — these should be stripped"
        )


def test_whitelist_excludes_github_metadata_vars():
    """Most GITHUB_* vars identify the runner, the actor, and ship
    history. They aren't secrets per se but they don't belong in the
    agent's env either; legitimate cases (repo identity) are passed
    through prompt context.

    Two exceptions are allowed: ``GITHUB_ACTIONS`` (informational CI
    sentinel) and ``GITHUB_TOKEN`` (workflow built-in token for the
    agent's ``gh`` CLI verification tooling — see the
    ``test_whitelist_includes_workflow_github_token`` test for the
    rationale)."""
    allowed_github_prefix = {"GITHUB_ACTIONS", "GITHUB_TOKEN"}
    for name in run._CLAUDE_ENV_WHITELIST:
        if name.startswith("GITHUB_"):
            assert name in allowed_github_prefix, (
                f"GITHUB_* var {name!r} in whitelist (not one of the "
                f"two intentional exceptions {allowed_github_prefix}) "
                f"— these should be stripped"
            )


# ─── _claude_subprocess_env: builds the minimal env dict ─────────


def test_subprocess_env_returns_only_whitelisted(monkeypatch):
    """The function should return ONLY the vars in the whitelist that
    are present in os.environ — never more."""
    monkeypatch.setattr(run, "os", run.os)  # ensure we patch the same module
    # Set a known-good mix: some whitelisted, some forbidden, some unknown.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/runner")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_workflow_token")
    monkeypatch.setenv("REMYX_API_KEY", "rmxu_secret")
    monkeypatch.setenv("INPUT_GITHUB_TOKEN", "ghs_bot_installation_token")
    monkeypatch.setenv("INPUT_INTEREST_ID", "uuid-here")
    monkeypatch.setenv("RANDOM_UNRELATED_VAR", "should-not-appear")

    env = run._claude_subprocess_env()

    # Required vars present.
    assert env["ANTHROPIC_API_KEY"] == "sk-test-key"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/runner"
    # Workflow GITHUB_TOKEN is whitelisted for the agent's `gh` CLI auth.
    # The bot's installation token (via INPUT_GITHUB_TOKEN) stays stripped.
    assert env["GITHUB_TOKEN"] == "ghs_workflow_token"

    # Forbidden vars absent.
    assert "REMYX_API_KEY" not in env
    assert "INPUT_GITHUB_TOKEN" not in env
    assert "INPUT_INTEREST_ID" not in env

    # Unknown vars absent (would-be-fine but shouldn't appear).
    assert "RANDOM_UNRELATED_VAR" not in env


def test_subprocess_env_skips_unset_whitelisted_vars(monkeypatch):
    """Whitelisted vars that aren't set in the parent env shouldn't
    appear as empty strings in the subprocess env."""
    # Clear everything first, then set only one var.
    for name in run._CLAUDE_ENV_WHITELIST:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")

    env = run._claude_subprocess_env()

    assert env == {"ANTHROPIC_API_KEY": "key"}


def test_subprocess_env_empty_string_value_preserved(monkeypatch):
    """An explicitly-set empty value is distinct from unset and should
    be preserved (Claude CLI might rely on the distinction)."""
    monkeypatch.setenv("LANG", "")
    env = run._claude_subprocess_env()
    assert env.get("LANG") == ""


# ─── subprocess.run is invoked with env=_claude_subprocess_env() ─


def test_run_claude_json_passes_stripped_env(monkeypatch, tmp_path):
    """_run_claude_json must pass env= to subprocess.run so the agent's
    subprocess starts with the stripped env."""
    captured = {}

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return MagicMock(
            stdout='{"result": "ok"}',
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.setenv("REMYX_API_KEY", "rmxu_should-not-leak")

    run._run_claude_json(["claude"], "prompt", tmp_path, 60)

    assert captured["env"] is not None, "env= must be passed; got None"
    assert "ANTHROPIC_API_KEY" in captured["env"]
    assert "REMYX_API_KEY" not in captured["env"], (
        "REMYX_API_KEY must be stripped from the subprocess env"
    )


def test_run_claude_stream_passes_stripped_env(monkeypatch, tmp_path):
    """_run_claude_stream must apply the same stripping as _run_claude_json
    — the selection pass uses this path and is the highest-risk surface
    (agentic, with tool access)."""
    captured = {}

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return MagicMock(
            stdout='{"type": "result", "result": "ok"}\n',
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_workflow_token")
    monkeypatch.setenv("INPUT_GITHUB_TOKEN", "ghs_bot_installation_token")
    monkeypatch.setenv("INPUT_INTEREST_ID", "uuid")

    run._run_claude_stream(["claude"], "prompt", tmp_path, 60)

    assert captured["env"] is not None, "env= must be passed; got None"
    assert "ANTHROPIC_API_KEY" in captured["env"]
    # Workflow GITHUB_TOKEN passes through for the agent's `gh` auth.
    assert captured["env"]["GITHUB_TOKEN"] == "ghs_workflow_token"
    # Bot installation token (via INPUT_GITHUB_TOKEN) and other INPUT_*
    # action inputs stay stripped — they're strictly higher-privilege
    # than the agent needs.
    assert "INPUT_GITHUB_TOKEN" not in captured["env"]
    assert "INPUT_INTEREST_ID" not in captured["env"]


# ─── Belt-and-suspenders contract with the v1.6.4 scrubber ───────


def test_env_strip_complements_outbound_scrubber():
    """The env strip and the outbound scrubber are independent layers
    of defense — neither replaces the other. Verify both helpers
    exist and the env strip's TOKEN-shaped whitelist entries are
    intentional (not accidental inclusions).

    Three TOKEN-shaped entries are intentional:
      - ANTHROPIC_API_KEY: the Claude CLI's default Anthropic auth (x-api-key)
      - ANTHROPIC_AUTH_TOKEN: the Claude CLI's Bearer auth for non-default
        backends (REMYX-154; required by z.ai's GLM Coding Plan)
      - GITHUB_TOKEN: the workflow built-in for the agent's `gh` CLI
        verification tooling
    Any other TOKEN-shaped entry should be reviewed before landing."""
    assert hasattr(run, "_scrub_outbound_payload")
    assert hasattr(run, "_claude_subprocess_env")
    intentional_token_entries = {
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GITHUB_TOKEN",
    }
    for name in run._CLAUDE_ENV_WHITELIST:
        if "TOKEN" in name.upper() or "KEY" in name.upper():
            assert name in intentional_token_entries, (
                f"Whitelist entry {name!r} looks token-shaped but isn't on "
                f"the intentional list {intentional_token_entries} — review"
            )
