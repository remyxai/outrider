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
        assert "inputs.enable-cocoindex == 'true'" in step.get("if", ""), step["name"]


def test_the_tool_is_advertised_only_when_it_actually_installed():
    """Observed on a real run: the install failed (non-fatal, by design) and
    the next step wrote the file telling the agent the tool was there. An
    agent sent after a binary that does not exist reads as the model ignoring
    instructions — the exact failure the tooling module exists to prevent."""
    steps = _action_steps()
    install = next(s for s in steps
                   if "cocoindex" in (s.get("name") or "").lower())
    advertise = next(s for s in steps
                     if "ENVIRONMENTS.md" in (s.get("name") or ""))
    assert install.get("id"), "the install step must be referenceable"
    assert f"steps.{install['id']}.outputs.installed == 'true'" in advertise["if"]
    # And the install has to publish that verdict either way.
    assert "installed=true" in install["run"]
    assert "installed=false" in install["run"]


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


def test_a_named_providers_endpoint_override_is_honored_and_warned(monkeypatch):
    """An explicit `model-base-url` is the caller naming an endpoint, and
    installs generated before the provider axis pass a provider AND a base URL
    together — a self-hosted gateway, a proxy in front of the vendor.
    Discarding it silently sent those runs to the vendor's public endpoint,
    which is a behavior change they never asked for. It is honored, with a
    warning, because the same pairing is how a leftover URL sends one vendor's
    key to another vendor's host."""
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    applied, warning = run.endpoint_override("https://gateway.internal/v1",
                                             provider_id="moonshot")
    assert applied == "https://gateway.internal/v1"
    assert "provider=moonshot" in warning and "credential will be sent" in warning


def test_a_native_routers_control_plane_is_not_a_model_endpoint(monkeypatch):
    """Its base-URL env var addresses the router itself. Repointing it aims
    the agent at the wrong service entirely."""
    run = _run_module(monkeypatch, INPUT_AGENT="backboard", BACKBOARD_API_KEY="k")
    applied, warning = run.endpoint_override("https://proxy.internal/v1",
                                             provider_id="custom")
    assert applied == ""
    assert "control plane" in warning


# ─── a malformed engine key says which secret to fix ───────────────────────


def test_a_key_with_a_line_break_names_the_secret_instead_of_crashing(monkeypatch):
    """A secret pasted with a stray newline is a legal repo secret and an
    illegal HTTP header. The run died inside urllib on "Invalid header value
    b'***'" — masked, with nothing pointing at the secret. Hit for real on a
    live runner when a restore command concatenated two values."""
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    monkeypatch.setenv("REMYX_API_KEY", "rmxu_first\nrmxu_second")
    with pytest.raises(RuntimeError) as excinfo:
        run._engine_api_key()
    assert "REMYX_API_KEY" in str(excinfo.value)
    assert "gh secret set" in str(excinfo.value)


def test_a_padded_key_is_used_rather_than_refused(monkeypatch):
    """Trailing whitespace is a paste artifact, not a different key."""
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    monkeypatch.setenv("REMYX_API_KEY", "  rmxu_padded\t")
    assert run._engine_api_key() == "rmxu_padded"


def test_no_key_at_all_is_still_the_empty_string(monkeypatch):
    """The callers raise their own "REMYX_API_KEY is required" message; this
    must not pre-empt it with a confusing one."""
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    monkeypatch.delenv("REMYX_API_KEY", raising=False)
    monkeypatch.delenv("REMYXAI_API_KEY", raising=False)
    assert run._engine_api_key() == ""


def test_the_bot_token_mint_degrades_instead_of_raising(monkeypatch):
    """That path falls back to GITHUB_TOKEN by design, so a malformed key
    must not become an exception there — but it should say which secret is
    wrong rather than degrading silently."""
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    monkeypatch.setenv("REMYX_API_KEY", "rmxu_first\nrmxu_second")
    monkeypatch.setenv("TARGET_REPO", "owner/name")
    run._BOT_TOKEN.update(attempted=False, token="", minted_at=0.0)
    assert run._mint_bot_token() == ""


# ─── a coverage metric calibrated to one agent must not judge another ──────


def _coverage(run, **over):
    base = {"searches": 0, "file_reads": 0, "visible_lines": 0,
            "exploration_structure": {"domains": 8, "structure": "branching"}}
    base.update(over)
    return base


def test_a_transcript_that_parsed_to_nothing_is_not_an_unexplored_pick(monkeypatch):
    """The shape a real Backboard run produced: every signal zero, including
    the exploration structure. An agent that genuinely explored nothing still
    leaves some trace — it ran, it edited files — so an all-zero parse means
    the transcript was absent or unreadable, not that the pick was baseless."""
    run = _run_module(monkeypatch, INPUT_AGENT="backboard", BACKBOARD_API_KEY="k")
    coverage = _coverage(run, exploration_structure={"domains": 0,
                                                     "domain_list": []})
    run._apply_coverage_gate({}, coverage, higher_floor=False)
    assert coverage["basis"] == "unavailable"
    assert "under_explored" not in coverage


def test_a_transcript_this_parser_cannot_read_is_not_an_unexplored_pick(monkeypatch):
    """Observed on a real Codex run: eight domains of tool activity, and
    searches, file reads and visible lines all zero — recorded as
    `under_explored: true` for a pick that was nothing of the sort. The floor
    is calibrated to one agent's transcript vocabulary, so a zero from another
    agent is a measurement gap until proven otherwise. Under `enforce` this
    would have downgraded every Codex pick to a skip."""
    run = _run_module(monkeypatch, INPUT_AGENT="codex", CODEX_API_KEY="k")
    coverage = _coverage(run)
    data = run._apply_coverage_gate({}, coverage, higher_floor=False)
    assert coverage["basis"] == "unavailable"
    assert "under_explored" not in coverage
    assert data.get("chosen_index") != -1


def test_real_coverage_is_still_judged(monkeypatch):
    """The gate must keep working where it can measure: a transcript with
    reads and lines is judged against the floor as before."""
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    thin = _coverage(run, searches=1, file_reads=1, visible_lines=10)
    run._apply_coverage_gate({}, thin, higher_floor=False)
    assert thin["under_explored"] is True
    assert thin.get("basis") != "unavailable"

    deep = _coverage(run, searches=4, file_reads=9, visible_lines=900)
    run._apply_coverage_gate({}, deep, higher_floor=False)
    assert deep["under_explored"] is False


def test_enforce_mode_never_downgrades_what_it_cannot_measure(monkeypatch):
    """The whole point: enforcing a floor against an unmeasurable transcript
    turns every pick into "the model found nothing worth doing"."""
    run = _run_module(monkeypatch, INPUT_AGENT="codex", CODEX_API_KEY="k")
    monkeypatch.setenv("REMYX_SELECTION_COVERAGE_GATE", "enforce")
    data = {"chosen_index": 3}
    out = run._apply_coverage_gate(data, _coverage(run), higher_floor=False)
    assert out["chosen_index"] == 3


# ─── every push has to re-authenticate, because origin is token-less ───────


def test_a_git_remote_that_needs_credentials_carries_them(monkeypatch):
    """`prepare_workdir` rewrites origin token-less after cloning, so the
    token never sits in .git/config where a coding agent with shell access
    could read it. Anything needing credentials must re-authenticate through a
    one-shot URL."""
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    monkeypatch.setattr(run, "_github_token", lambda: "ghs_testtoken")
    url = run._authenticated_remote("owner/name")
    assert url == "https://x-access-token:ghs_testtoken@github.com/owner/name.git"


def test_it_falls_back_to_origin_when_there_is_no_token(monkeypatch):
    """So the caller's own error handling stays in charge rather than this
    helper inventing a broken URL."""
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    monkeypatch.setattr(run, "_github_token", lambda: "")
    assert run._authenticated_remote("owner/name") == "origin"


def test_the_fidelity_remediation_push_does_not_reach_for_origin():
    """It did, and origin has no credentials — so the fetch prompted for a
    username and died. The remediation commit could never land, and every run
    that tripped the fidelity gate ended as a skip whatever the patch had
    fixed. Seen on a real run: "could not read Username for
    'https://github.com'" right after "patch attempt applied edits"."""
    src = (ROOT / "src" / "run.py").read_text()
    block = src[src.index("Fidelity remediation") - 2000:
                src.index("Fidelity remediation") + 1200]
    assert '"git", "fetch", authed' in block
    assert '"git", "push", authed' in block
    assert '"git", "push", "origin", branch' not in block


# ─── dollars computed from another model's card say so ─────────────────────


def test_an_exact_rate_card_is_reported_as_authoritative(monkeypatch):
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    assert run._rate_basis("https://api.z.ai/api/anthropic", "glm-5.2") == \
        "backend_rate_table"


def test_a_fallback_rate_card_is_not(monkeypatch):
    """`_detect_backend` falls back to the host's default model when the exact
    id is missing. Reporting that as `backend_rate_table` renders in the step
    summary as "computed from PAYG rates" — authoritative — for a number
    computed from a different model's prices. This branch makes `glm-5.3` the
    z.ai default, and the table has rows only for glm-5.2 and glm-4.6."""
    run = _run_module(monkeypatch, INPUT_AGENT="claude", ANTHROPIC_API_KEY="k")
    assert run._rate_basis("https://api.z.ai/api/anthropic", "glm-5.3") == \
        "backend_rate_table_approx"
    # Tokens stay exact either way; only the dollars are approximate.
    assert run._rate_basis("https://api.moonshot.ai/anthropic", "kimi-k3") == \
        "backend_rate_table"
    # And an install that never pinned a model keeps its historical basis:
    # the per-host default is the standing assumption about what the vendor
    # served, and relabelling that history needs a telemetry reason.
    assert run._rate_basis("https://api.z.ai/api/anthropic", "") == \
        "backend_rate_table"
