"""Tests for the pluggable ANTHROPIC_BASE_URL — REMYX-151.

Covers:
- `Target.model_base_url` defaults empty and propagates from INPUT_MODEL_BASE_URL
- `main()` exports the base URL into ``os.environ["ANTHROPIC_BASE_URL"]`` so
  the existing ``_CLAUDE_ENV_WHITELIST`` passthrough picks it up for every
  Claude subprocess in the run
- When the input is empty, ``os.environ["ANTHROPIC_BASE_URL"]`` is left
  untouched (preserves the "I'll set it via workflow `env:` block myself"
  workaround)
- The Claude subprocess env builder includes ``ANTHROPIC_BASE_URL`` when it's
  present in the parent env (existing behavior — pinned here to prevent
  regressions)

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# ─── Target.model_base_url + env reader ───────────────────────────────────


def test_target_model_base_url_defaults_empty(monkeypatch):
    monkeypatch.setenv("TARGET_REPO", "owner/name")
    monkeypatch.setenv(
        "INPUT_INTEREST_ID", "00000000-0000-0000-0000-000000000000"
    )
    monkeypatch.delenv("INPUT_MODEL_BASE_URL", raising=False)
    target = run.build_target_from_env()
    assert target.model_base_url == ""


def test_target_picks_up_model_base_url_from_env(monkeypatch):
    monkeypatch.setenv("TARGET_REPO", "owner/name")
    monkeypatch.setenv(
        "INPUT_INTEREST_ID", "00000000-0000-0000-0000-000000000000"
    )
    monkeypatch.setenv("INPUT_MODEL_BASE_URL", "https://api.z.ai/anthropic")
    target = run.build_target_from_env()
    assert target.model_base_url == "https://api.z.ai/anthropic"


# ─── _CLAUDE_ENV_WHITELIST coverage ───────────────────────────────────────


def test_anthropic_base_url_in_subprocess_whitelist():
    """Regression guard: ANTHROPIC_BASE_URL must stay in the Claude
    subprocess env whitelist or the model-base-url input is silently a
    no-op (the parent env's value would get stripped before the CLI
    sees it).
    """
    assert "ANTHROPIC_BASE_URL" in run._CLAUDE_ENV_WHITELIST


def test_claude_subprocess_env_forwards_anthropic_base_url(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.z.ai/anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    env = run._claude_subprocess_env()
    assert env.get("ANTHROPIC_BASE_URL") == "https://api.z.ai/anthropic"
    assert env.get("ANTHROPIC_API_KEY") == "test-key"


def test_claude_subprocess_env_omits_base_url_when_unset(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    env = run._claude_subprocess_env()
    assert "ANTHROPIC_BASE_URL" not in env


# ─── main() exports the URL into os.environ ────────────────────────────────


def _set_minimum_main_env(monkeypatch):
    """Set the minimum env so build_target_from_env succeeds in main()."""
    monkeypatch.setenv("TARGET_REPO", "owner/name")
    monkeypatch.setenv(
        "INPUT_INTEREST_ID", "00000000-0000-0000-0000-000000000000"
    )


def test_main_exports_anthropic_base_url_when_set(monkeypatch):
    """When `model-base-url` is set, main() must export it into os.environ
    BEFORE the first Claude subprocess fires (the whitelist forwarder reads
    from os.environ). We monkeypatch process_target so we can intercept the
    side effect without running the full pipeline."""
    _set_minimum_main_env(monkeypatch)
    monkeypatch.setenv("INPUT_MODEL_BASE_URL", "https://api.z.ai/anthropic")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    seen = {}

    def fake_process_target(target):
        seen["env_base_url"] = run.os.environ.get("ANTHROPIC_BASE_URL")
        seen["target_base_url"] = target.model_base_url
        return {"status": "skipped_low_confidence", "repo": target.repo}

    monkeypatch.setattr(run, "process_target", fake_process_target)
    # The runner main() catches some exceptions and exits; tolerate that.
    try:
        run.main()
    except SystemExit:
        pass

    assert seen["target_base_url"] == "https://api.z.ai/anthropic"
    assert seen["env_base_url"] == "https://api.z.ai/anthropic"


def test_main_does_not_set_base_url_when_input_empty(monkeypatch):
    """Empty model-base-url input must not touch os.environ — preserves the
    workaround where customers set ANTHROPIC_BASE_URL directly in their
    workflow `env:` block."""
    _set_minimum_main_env(monkeypatch)
    monkeypatch.delenv("INPUT_MODEL_BASE_URL", raising=False)
    # User's pre-existing workflow-level setting:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://customer-proxy.example/")

    seen = {}

    def fake_process_target(target):
        seen["env_base_url"] = run.os.environ.get("ANTHROPIC_BASE_URL")
        return {"status": "skipped_low_confidence", "repo": target.repo}

    monkeypatch.setattr(run, "process_target", fake_process_target)
    try:
        run.main()
    except SystemExit:
        pass

    # The workflow-level value survives — Outrider did not overwrite it.
    assert seen["env_base_url"] == "https://customer-proxy.example/"
