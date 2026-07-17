"""Tests for the pluggable ANTHROPIC_BASE_URL.

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


def test_anthropic_auth_token_in_subprocess_whitelist():
    """Regression guard: ANTHROPIC_AUTH_TOKEN must stay in the whitelist
    so Claude Code can authenticate against non-default Anthropic-compat
    backends (z.ai's GLM Coding Plan requires Bearer auth — sending
    x-api-key returns HTTP 401). Without this, every glm-routed run
    fails at auth."""
    assert "ANTHROPIC_AUTH_TOKEN" in run._CLAUDE_ENV_WHITELIST


def test_claude_subprocess_env_forwards_auth_token(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-zai-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-default")
    env = run._claude_subprocess_env()
    assert env.get("ANTHROPIC_AUTH_TOKEN") == "test-zai-token"
    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-default"


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
    # Startup auth check requires either ANTHROPIC_API_KEY (default
    # backend) or ANTHROPIC_AUTH_TOKEN (non-default). Default path is
    # what these tests exercise unless they override ANTHROPIC_BASE_URL.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fakebutlongenough")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "zai-test-fakebutlongenough")


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


# ─── _detect_backend + cost-override behavior ─────────────────────────────


def test_detect_backend_returns_anthropic_for_empty_url():
    name, rates = run._detect_backend("")
    assert name == "Anthropic"
    assert rates is None  # caller trusts the CLI's envelope cost


def test_detect_backend_recognizes_zai():
    name, rates = run._detect_backend("https://api.z.ai/api/anthropic")
    assert name == "z.ai (GLM)"
    assert rates is not None and rates[0] > 0 and rates[1] > 0


def test_detect_backend_recognizes_moonshot():
    name, rates = run._detect_backend("https://api.moonshot.ai/anthropic")
    assert name == "Moonshot (Kimi)"
    assert rates is not None and rates[0] > 0 and rates[1] > 0


def test_detect_backend_unknown_returns_host_with_no_rates():
    name, rates = run._detect_backend("https://api.example.com/anthropic")
    assert name == "api.example.com"
    assert rates is None  # caller falls back to CLI's envelope cost


def test_detect_backend_zai_explicit_model_selects_row():
    _, glm46 = run._detect_backend("https://api.z.ai/api/anthropic", "glm-4.6")
    _, glm52 = run._detect_backend("https://api.z.ai/api/anthropic", "glm-5.2")
    assert glm46 == (0.60, 2.20, 0.11)
    assert glm52 == (1.40, 4.40, 0.26)


def test_detect_backend_moonshot_explicit_model_selects_row():
    _, k3 = run._detect_backend("https://api.moonshot.ai/anthropic", "kimi-k3")
    _, k27 = run._detect_backend("https://api.moonshot.ai/anthropic", "kimi-k2.7-code")
    _, k27hs = run._detect_backend("https://api.moonshot.ai/anthropic", "kimi-k2.7-code-highspeed")
    assert k3 == (3.00, 15.00, 0.30)
    assert k27 == (0.95, 4.00, 0.19)
    assert k27hs == (1.90, 8.00, 0.38)


def test_detect_backend_unknown_model_on_known_host_falls_back_to_default():
    """A model name we don't have rates for still returns the host's default
    rates — closer than nothing, and matches how a fork without ANTHROPIC_MODEL
    set would behave."""
    _, moonshot_unknown = run._detect_backend("https://api.moonshot.ai/anthropic", "kimi-unreleased")
    _, moonshot_default = run._detect_backend("https://api.moonshot.ai/anthropic", "")
    assert moonshot_unknown == moonshot_default  # both fall back to kimi-k3
    _, zai_unknown = run._detect_backend("https://api.z.ai/api/anthropic", "glm-6")
    _, zai_default = run._detect_backend("https://api.z.ai/api/anthropic", "")
    assert zai_unknown == zai_default  # both fall back to glm-5.2


def test_cost_override_for_zai_uses_default_glm52_rates(monkeypatch):
    """When ANTHROPIC_BASE_URL routes to z.ai with no ANTHROPIC_MODEL set,
    _record_claude_usage falls back to the per-host default (glm-5.2) — the
    tier-flagship default matches Anthropic (Opus) and Moonshot (kimi-k3)."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic")
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    run._reset_run_cost()
    # Pretend Claude Code reported a (wrong, Anthropic-rate) cost of $1.00
    # for 1M input + 1M output tokens. With glm-5.2 rates (1.40 + 4.40),
    # the real cost is $5.80, not $1.00.
    envelope = {
        "total_cost_usd": 1.00,
        "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        "num_turns": 1,
    }
    run._record_claude_usage(envelope)
    assert run._RUN_COST["cost_usd"] == pytest.approx(1.40 + 4.40, abs=0.01)
    assert run._RUN_COST["cost_basis"] == "backend_rate_table"
    assert run._RUN_COST["model_backend"] == "z.ai (GLM)"
    # Token counts come straight from the envelope — accurate regardless.
    assert run._RUN_COST["input_tokens"] == 1_000_000
    assert run._RUN_COST["output_tokens"] == 1_000_000


def test_cost_override_reads_model_from_env(monkeypatch):
    """ANTHROPIC_MODEL selects the specific rate row — verifies the
    drafter-tier accuracy case that the default-fallback path misses."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "glm-4.6")
    run._reset_run_cost()
    envelope = {
        "total_cost_usd": 1.00,
        "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        "num_turns": 1,
    }
    run._record_claude_usage(envelope)
    # glm-4.6 rates: 0.60 + 2.20 = 2.80 (not the 5.80 that glm-5.2 default would give)
    assert run._RUN_COST["cost_usd"] == pytest.approx(0.60 + 2.20, abs=0.01)
    assert run._RUN_COST["cost_basis"] == "backend_rate_table"


def test_cost_override_envelope_model_wins_over_env(monkeypatch):
    """When the envelope names a model, that wins over ANTHROPIC_MODEL —
    envelope = what was actually served, env = what we requested."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.moonshot.ai/anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "kimi-k3")  # we asked for k3
    run._reset_run_cost()
    envelope = {
        "model": "kimi-k2.7-code",  # but got k2.7-code back
        "total_cost_usd": 1.00,
        "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        "num_turns": 1,
    }
    run._record_claude_usage(envelope)
    # Cost priced at kimi-k2.7-code rates (0.95 + 4.00), not kimi-k3
    assert run._RUN_COST["cost_usd"] == pytest.approx(0.95 + 4.00, abs=0.01)
    assert run._RUN_COST["model_backend"] == "Moonshot (Kimi)"


def test_cost_trusts_envelope_for_default_anthropic(monkeypatch):
    """The default (no ANTHROPIC_BASE_URL) trusts the CLI's
    total_cost_usd — Claude Code's Anthropic-rate calc is correct
    when talking to Anthropic."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    run._reset_run_cost()
    envelope = {
        "total_cost_usd": 1.5,
        "usage": {"input_tokens": 100_000, "output_tokens": 50_000},
        "num_turns": 2,
    }
    run._record_claude_usage(envelope)
    assert run._RUN_COST["cost_usd"] == pytest.approx(1.5, abs=0.001)
    assert run._RUN_COST["cost_basis"] == "claude_code_envelope"
    assert run._RUN_COST["model_backend"] == "Anthropic"


def test_cost_trusts_envelope_for_unknown_backend(monkeypatch):
    """Unknown backends fall back to the envelope's value — accurate
    or not, that's the best signal available without a rate-table entry.
    The step summary will flag this case with a 'may be approximate' note."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.example.com/v1")
    run._reset_run_cost()
    envelope = {
        "total_cost_usd": 0.75,
        "usage": {"input_tokens": 50_000, "output_tokens": 25_000},
        "num_turns": 1,
    }
    run._record_claude_usage(envelope)
    assert run._RUN_COST["cost_usd"] == pytest.approx(0.75, abs=0.001)
    assert run._RUN_COST["cost_basis"] == "claude_code_envelope"
    assert run._RUN_COST["model_backend"] == "api.example.com"  # raw host


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
