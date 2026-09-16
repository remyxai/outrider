"""Defects found reviewing the agent-axis port, pinned so they stay fixed.

Each test here corresponds to a path where the agent axis was in place but
something downstream still assumed Claude Code, or where a guard was lost in
the rewrite. They are cheap, and every one of them describes a run that looked
healthy while reporting or doing the wrong thing.
"""
import os
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(autouse=True)
def _restore_run_module():
    """Put the session's original `run` module back afterwards.

    These tests re-import `run` under a chosen agent, which is the only way
    to exercise backend selection (it is resolved at import). Two things make
    cleanup load-bearing: the module caches environment-derived state, and
    other test modules bind names from it at collection time — a test doing
    `pytest.raises(OutboundSecretError)` against a *re-imported* module's
    class does not catch it, because they are different objects. So restore
    the exact module object rather than just dropping ours.
    """
    original = sys.modules.get("run")
    yield
    if original is not None:
        sys.modules["run"] = original
    else:
        sys.modules.pop("run", None)


def _run_module(monkeypatch, **env):
    """Import `run` with a given agent selected, isolated per test."""
    for key in ("INPUT_AGENT", "INPUT_PROVIDER", "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                "OUTRIDER_CLAUDE_AUTH_VAR", "CODEX_API_KEY",
                "BACKBOARD_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Re-import `run` only. Dropping the `agents` package too would hand
    # this session a second set of backend classes, and every other test
    # holding a reference to the first would start failing isinstance checks
    # — which is exactly what happened the first time this fixture was
    # written.
    sys.modules.pop("run", None)
    import run  # noqa: WPS433 — deliberate re-import under a new environment
    return run


# ─── cost attribution survives the per-run reset ───────────────────────────


def test_the_run_reset_keeps_the_active_backends_attribution(monkeypatch):
    """`main()` resets run cost right before dispatching. Resetting to
    Anthropic literals threw away the backend-aware default, so a codex run
    that never recorded a usage envelope — a missing binary, a first-call
    timeout — reported Claude Code spend for an agent it never launched."""
    run = _run_module(monkeypatch, INPUT_AGENT="codex", CODEX_API_KEY="k")
    run._reset_run_cost()
    assert run._RUN_COST["model_backend"] == "Codex → OpenAI"
    assert run._RUN_COST["cost_basis"] == "unavailable"


def test_the_reset_still_zeroes_the_counters(monkeypatch):
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    run._RUN_COST.update(cost_usd=1.25, input_tokens=99, claude_calls=3)
    run._reset_run_cost()
    assert run._RUN_COST["cost_usd"] == 0.0
    assert run._RUN_COST["input_tokens"] == 0
    assert run._RUN_COST["claude_calls"] == 0


# ─── the auth validator and the launcher agree ─────────────────────────────


def test_the_validator_follows_the_marker_not_the_base_url(monkeypatch):
    """Routing records which credential it selected. Re-deriving that from
    the base URL let the validator hard-exit over ANTHROPIC_AUTH_TOKEN while
    the launcher was about to hand the agent ANTHROPIC_API_KEY — the exact
    credential the run had."""
    run = _run_module(
        monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="sk-ant-x",
        ANTHROPIC_BASE_URL="https://proxy.internal/v1",
        OUTRIDER_CLAUDE_AUTH_VAR="ANTHROPIC_API_KEY",
    )
    ok, _messages = run._validate_claude_auth_env()
    assert ok is True


def test_a_missing_selected_credential_still_fails_and_says_why(monkeypatch):
    """And the reason travels with the result: `mode: smoke` joins these
    into its error, which used to read "preflight failed: " with no cause."""
    run = _run_module(
        monkeypatch, INPUT_AGENT="claude",
        ANTHROPIC_BASE_URL="https://api.z.ai/api/anthropic",
        OUTRIDER_CLAUDE_AUTH_VAR="ANTHROPIC_AUTH_TOKEN",
    )
    ok, messages = run._validate_claude_auth_env()
    assert ok is False
    assert any("ANTHROPIC_AUTH_TOKEN" in m for m in messages)


def test_an_unrouted_run_still_reads_the_base_url(monkeypatch):
    """No marker means no `provider` input: the caller wired the vars
    themselves and the base URL is the only signal there is."""
    run = _run_module(
        monkeypatch, INPUT_AGENT="claude", ANTHROPIC_AUTH_TOKEN="sk-not-a-truncated-token-value",
        ANTHROPIC_BASE_URL="https://api.moonshot.ai/anthropic",
    )
    ok, _messages = run._validate_claude_auth_env()
    assert ok is True


# ─── naming: statuses, vendors, and who wrote the PR ───────────────────────


def test_every_claude_suffixed_status_has_an_agent_facing_form(monkeypatch):
    """A Backboard run rendering "issue_convention_failed_claude" in its own
    step summary is the misleading breadcrumb this mapping exists to stop."""
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    stored = [s for s in run._STATUS_EMOJI] if hasattr(run, "_STATUS_EMOJI") else []
    for status in stored or run._DISPLAY_STATUS:
        if status.endswith("_claude") or status == "claude_failed":
            assert "claude" not in run._display_status(status), status


def test_a_gateway_without_a_rate_row_keeps_its_name(monkeypatch):
    """OpenRouter prices per underlying model, so it has no rate row — and
    fell out of backend detection as a bare hostname, grouping its runs under
    a different series id than the same vendor reached any other way."""
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    assert run._detect_backend("https://openrouter.ai/api") == ("OpenRouter", None)


def test_the_pr_body_credits_the_agent_that_did_the_work(monkeypatch):
    """These PRs are published on someone else's repo. Attributing the work
    to an agent that never ran is a claim, not a cosmetic label."""
    run = _run_module(monkeypatch, INPUT_AGENT="backboard", BACKBOARD_API_KEY="k")
    assert "{agent_name}" in run._PR_BODY_TEMPLATE
    assert "{agent_name}" in run._PR_BODY_TEMPLATE_BRIEF
    assert "Claude Code as autonomous agent" not in run._PR_BODY_TEMPLATE
    assert "Claude Code as autonomous agent" not in run._PR_BODY_TEMPLATE_BRIEF


# ─── action.yml wiring the steps depend on ─────────────────────────────────


def _action_steps():
    return yaml.safe_load((ROOT / "action.yml").read_text())["runs"]["steps"]


def test_the_cocoindex_input_still_gates_both_steps_it_names():
    """Without the guard the input is a lie: the caller pays ~1 GB of deps
    and 60-90s on every run with no way to opt out."""
    gated = [
        s for s in _action_steps()
        if "cocoindex" in (s.get("name") or "").lower()
        or "ENVIRONMENTS.md" in (s.get("name") or "")
    ]
    assert len(gated) == 2
    for step in gated:
        assert step.get("if") == "${{ inputs.enable-cocoindex == 'true' }}", step["name"]


def test_the_run_step_receives_both_axes():
    """Composite-action inputs are not auto-exposed as INPUT_* to a `run:`
    step. Without these the "set this secret" remedy named the agent's own
    credential instead of the provider's on every routed run."""
    step = next(s for s in _action_steps()
                if "Recommend + implement" in (s.get("name") or ""))
    assert step["env"]["INPUT_PROVIDER"] == "${{ inputs.provider }}"
    assert step["env"]["INPUT_AGENT"] == "${{ inputs.agent }}"


# ─── degradation that has to actually happen ───────────────────────────────


def test_research_is_staged_only_where_the_agent_can_search(monkeypatch):
    """The capability was declared and dynamically degraded but never
    consulted, so a Codex run routed off OpenAI — where the action pins
    `web_search="disabled"` — staged a web-research phase anyway, burned a
    full timeout on a prompt it cannot satisfy, and soft-failed on the missing
    findings file."""
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    assert run.should_stage_research() is True

    run = _run_module(monkeypatch, INPUT_AGENT="codex", CODEX_API_KEY="k")
    assert run.should_stage_research() is True, "Codex on OpenAI can search"

    # Routed off OpenAI, the action disables the tool — so there is nothing
    # for a research phase to do.
    run = _run_module(monkeypatch, INPUT_AGENT="codex", CODEX_API_KEY="k",
                      CODEX_BASE_URL="https://api.moonshot.ai/v1")
    assert run.should_stage_research() is False


# ─── an endpoint override only applies where routing accepts one ───────────


def test_an_override_reaches_an_endpoint_the_caller_owns(monkeypatch):
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    applied, warning = run.endpoint_override("https://proxy.internal/v1",
                                             provider_id="custom")
    assert applied == "https://proxy.internal/v1" and warning == ""
    # No provider at all is the oldest supported shape: the caller wired it.
    applied, warning = run.endpoint_override("https://proxy.internal/v1")
    assert applied == "https://proxy.internal/v1" and warning == ""


def test_a_named_providers_endpoint_is_not_overridable(monkeypatch):
    """Routing discards the override for a named provider, but this is applied
    after routing, so it won. A leftover `model-base-url` from the
    pre-provider era sent the provider's key to a different vendor's host."""
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    applied, warning = run.endpoint_override("https://api.z.ai/api/anthropic",
                                             provider_id="moonshot")
    assert applied == ""
    assert "provider=moonshot" in warning


def test_a_native_routers_control_plane_is_not_a_model_endpoint(monkeypatch):
    """Its base-URL env var addresses the router itself. Repointing it aims
    the agent at the wrong service entirely."""
    run = _run_module(monkeypatch, INPUT_AGENT="backboard", BACKBOARD_API_KEY="k")
    applied, warning = run.endpoint_override("https://proxy.internal/v1",
                                             provider_id="custom")
    assert applied == ""
    assert "control plane" in warning
