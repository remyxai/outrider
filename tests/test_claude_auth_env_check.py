"""Tests for the startup auth-env validation.

Covers:
- Default-Anthropic backend requires ANTHROPIC_API_KEY.
- Non-default backend (e.g. z.ai) requires ANTHROPIC_AUTH_TOKEN.
- The literal '-' value (from `gh secret set --body -` stdin
  ambiguity) is a hard fail.
- Suspiciously short values are a hard fail (likely truncated).
- Whitespace warns and strips rather than failing.
- Both auth vars set under a non-default backend warns (Claude Code
  prefers x-api-key which non-Anthropic backends reject).
- Diagnostic logs carry length + sha8 only; the secret value itself
  is never echoed.

Run with: pytest tests/test_claude_auth_env_check.py -q
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


def setup_function(_func):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        run.os.environ.pop(k, None)


# ─── default Anthropic backend ─────────────────────────────────────


def test_default_backend_with_valid_api_key_passes(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fakebutlongenoughxxxx")
    ok, warnings = run._validate_claude_auth_env()
    assert ok is True
    assert warnings == []


def test_default_backend_missing_api_key_fails():
    ok, _ = run._validate_claude_auth_env()
    assert ok is False


def test_default_backend_literal_dash_fails(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "-")
    ok, _ = run._validate_claude_auth_env()
    assert ok is False


def test_default_backend_too_short_fails(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "short")
    ok, _ = run._validate_claude_auth_env()
    assert ok is False


# ─── non-default backend (z.ai-style) ──────────────────────────────


def test_non_default_backend_with_valid_auth_token_passes(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "zai-fakebutlongenoughxxxx")
    ok, warnings = run._validate_claude_auth_env()
    assert ok is True
    assert warnings == []


def test_non_default_backend_missing_auth_token_fails(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic")
    ok, _ = run._validate_claude_auth_env()
    assert ok is False


def test_non_default_backend_literal_dash_fails(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "-")
    ok, _ = run._validate_claude_auth_env()
    assert ok is False


def test_non_default_backend_both_vars_set_warns(monkeypatch):
    """Both auth vars set under a non-default backend — Claude Code
    prefers API_KEY which non-Anthropic backends reject. Warn (don't
    fail) since the AUTH_TOKEN value may still be the intended one,
    but flag the likely workflow bug."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fakebutlongenoughxxxx")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "zai-fakebutlongenoughxxxx")
    ok, warnings = run._validate_claude_auth_env()
    assert ok is True
    assert any("Both ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN" in w
               for w in warnings)


def test_default_backend_with_both_vars_does_not_warn(monkeypatch):
    """On default Anthropic, having AUTH_TOKEN set alongside API_KEY
    is harmless (the CLI uses API_KEY and ignores AUTH_TOKEN); don't
    warn about mutual exclusion."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fakebutlongenoughxxxx")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "unused-but-presentxxxxxxxx")
    ok, warnings = run._validate_claude_auth_env()
    assert ok is True
    assert warnings == []


# ─── whitespace handling ───────────────────────────────────────────


def test_leading_whitespace_warns_and_strips(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  sk-ant-fakebutlongenoughxxxx")
    ok, warnings = run._validate_claude_auth_env()
    assert ok is True
    assert any("whitespace" in w for w in warnings)
    # The env var was mutated to the stripped value.
    assert run.os.environ["ANTHROPIC_API_KEY"].startswith("sk-ant-")
    assert not run.os.environ["ANTHROPIC_API_KEY"].startswith(" ")


def test_trailing_newline_warns_and_strips(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fakebutlongenoughxxxx\n")
    ok, warnings = run._validate_claude_auth_env()
    assert ok is True
    assert any("whitespace" in w for w in warnings)
    assert not run.os.environ["ANTHROPIC_API_KEY"].endswith("\n")


# ─── value never echoed in logs ────────────────────────────────────


def test_secret_value_never_appears_in_log_messages(monkeypatch, caplog):
    """Invariant: the validator never echoes the secret value, only
    length + sha8. This protects the GitHub Actions log from leaking
    a misconfigured-but-real key into the customer's public log."""
    unique_marker = "uniquemarkerdeadbeefcafe1234567890"
    monkeypatch.setenv("ANTHROPIC_API_KEY", f"  {unique_marker}  ")
    with caplog.at_level(logging.DEBUG, logger=run.log.name):
        _, warnings = run._validate_claude_auth_env()
    full_log = " ".join(r.getMessage() for r in caplog.records)
    combined = full_log + " " + " ".join(warnings)
    assert unique_marker not in combined


def test_default_backend_hard_fail_logs_an_error(monkeypatch, caplog):
    """Hard-fail paths must produce an ERROR-level log entry so the
    customer sees what to fix, not silent exit."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "-")
    with caplog.at_level(logging.ERROR, logger=run.log.name):
        ok, _ = run._validate_claude_auth_env()
    assert ok is False
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    assert any("literal '-'" in r.getMessage() for r in caplog.records)
