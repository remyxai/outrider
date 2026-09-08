"""Existing installations must not break.

`remyxai/outrider@v1` is a moving tag: customer forks pin it and get new
action code without changing a line of their own workflow YAML. Their
workflows were rendered by the engine's provisioner at various past versions,
and older `remyxai` CLIs still dispatch with the input set they knew about.

So the compatibility contract is specifically:

* every input an older workflow or CLI sends must still be accepted,
* the resulting environment must be what it was before the agent port,
* no input may become required,
* nothing may start failing that used to succeed.

This file encodes that as tests rather than as a claim. Each case is written
from the caller's side — the inputs a real installation actually sends.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
import yaml

import run
from agents import RoutingError, available, resolve
from agents.providers import resolve as route

ACTION = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "action.yml").read_text()
)


# ─── the inputs older callers actually send ─────────────────────────────────
#
# From the engine provisioner's dispatch payload and `remyxai outrider
# trigger` as of CLI 0.4.12: pin-arxiv, interest-id, provider, model,
# test-integration-policy, fidelity-policy, start-from-ref, lead-content.

LEGACY_DISPATCH_INPUTS = [
    "pin-arxiv", "interest-id", "provider", "model",
    "test-integration-policy", "fidelity-policy",
    "start-from-ref", "lead-content",
]


def test_every_legacy_dispatch_input_still_exists():
    """A removed input makes GitHub reject the dispatch with 422."""
    declared = set(ACTION["inputs"])
    for name in LEGACY_DISPATCH_INPUTS:
        assert name in declared, f"{name} was removed — old dispatches 422"


def test_claude_timeout_input_survives():
    """The CLI passes --claude-timeout through to this input by name."""
    assert "claude-timeout" in ACTION["inputs"]


BASELINE = json.loads(
    (Path(__file__).resolve().parent / "fixtures"
     / "action_input_surface_baseline.json").read_text()
)


def test_no_input_was_removed():
    """A removed input makes an older workflow's dispatch 422.

    The baseline is the input surface of @v1 before the agent port, snapshotted
    from origin/main — so this compares against what installations actually
    pinned, not against a hand-kept list.
    """
    missing = sorted(set(BASELINE["inputs"]) - set(ACTION["inputs"]))
    assert not missing, f"inputs removed from the public contract: {missing}"


def test_no_input_became_required():
    """An older workflow cannot supply an input it has never heard of.

    `interest-id` was already required before this branch; the set must not
    grow.
    """
    required = sorted(
        name for name, spec in ACTION["inputs"].items()
        if spec.get("required") is True
    )
    assert required == BASELINE["required"], (
        f"required inputs changed from {BASELINE['required']} to {required}"
    )


def test_the_port_only_added_inputs():
    added = sorted(set(ACTION["inputs"]) - set(BASELINE["inputs"]))
    assert added == ["agent"], f"unexpected input changes: {added}"


def test_the_new_agent_input_defaults_to_empty():
    assert ACTION["inputs"]["agent"]["default"] == ""


def test_every_provider_value_the_docs_promise_is_still_accepted():
    """Version discipline: provider values are additive-only, forever."""
    for value in ("anthropic", "zai", "moonshot", "custom"):
        routing = route(
            resolve("claude"), value, "",
            "https://proxy.internal/v1" if value == "custom" else "",
            {"ANTHROPIC_API_KEY": "k", "ZAI_API_KEY": "k",
             "MOONSHOT_API_KEY": "k"},
        )
        assert routing is not None


# ─── the environment an old workflow ends up with ───────────────────────────

LEGACY_ENV_EXPECTATIONS = [
    # (provider, caller env, resulting env) — transcribed from the shell that
    # shipped before the agent port.
    ("anthropic", {"ANTHROPIC_API_KEY": "ak"},
     {"ANTHROPIC_API_KEY": "ak", "ANTHROPIC_AUTH_TOKEN": ""}),
    ("zai", {"ZAI_API_KEY": "zk"},
     {"ANTHROPIC_AUTH_TOKEN": "zk", "ANTHROPIC_API_KEY": "",
      "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"}),
    ("moonshot", {"MOONSHOT_API_KEY": "mk"},
     {"ANTHROPIC_AUTH_TOKEN": "mk", "ANTHROPIC_API_KEY": "",
      "ANTHROPIC_BASE_URL": "https://api.moonshot.ai/anthropic"}),
]


@pytest.mark.parametrize("provider,caller,expected", LEGACY_ENV_EXPECTATIONS)
def test_legacy_workflow_gets_the_same_environment(provider, caller, expected):
    """An old workflow sends no `agent`, so it must resolve identically."""
    routing = route(resolve(None), provider, "", "", caller)
    assert routing.env == expected


def test_a_workflow_with_no_provider_at_all_is_untouched():
    """The oldest installs predate the `provider` input entirely: they set
    ANTHROPIC_API_KEY in `env:` and nothing else. Routing must be a no-op."""
    routing = route(resolve(None), "", "", "", {"ANTHROPIC_API_KEY": "ak"})
    assert routing.env == {}
    assert routing.warnings == []


def test_model_with_no_provider_stays_ignored_for_claude():
    """Deliberately preserving a documented wart.

    `model` is documented as always setting ANTHROPIC_MODEL, but the shipped
    step wrote it only inside the provider-gated branch. Honoring it now
    would change what an existing install does: a `model` naming something
    the default Anthropic backend does not serve currently works precisely
    because it is ignored, and would begin 404ing.
    """
    routing = route(resolve(None), "", "glm-5.2", "", {"ANTHROPIC_API_KEY": "k"})
    assert routing.env == {}


def test_unset_agent_is_claude_code():
    backend = resolve(None)
    assert backend.name == "claude"
    assert backend.display_name == "Claude Code"


# ─── the CLI and the engine ─────────────────────────────────────────────────

def test_a_dispatch_without_the_agent_input_is_valid():
    """Installed CLIs (0.4.11/0.4.12) and the engine's provisioner both
    dispatch without `agent`. That path must need no server change to keep
    working."""
    import os

    saved = os.environ.pop("INPUT_AGENT", None)
    try:
        assert resolve(os.environ.get("INPUT_AGENT")).name == "claude"
    finally:
        if saved is not None:
            os.environ["INPUT_AGENT"] = saved


def test_telemetry_field_values_are_unchanged_for_claude():
    """The engine stores `agent` / `model_backend` / `cost_basis` as free
    text, but existing dashboards group on the values. Claude runs must keep
    reporting exactly what they did."""
    assert run._initial_cost_attribution(resolve("claude")) == (
        "Anthropic", "claude_code_envelope",
    )
    assert resolve("claude").display_name == "Claude Code"
    assert resolve("claude").name == "claude"


def test_new_cost_basis_values_only_occur_for_new_agents():
    """`agent_envelope` and `unavailable` are new values. A Claude run must
    never emit them, so existing spend queries keep matching."""
    for name in available():
        label, basis = run._initial_cost_attribution(resolve(name))
        if name == "claude":
            assert basis == "claude_code_envelope"
        else:
            assert basis == "unavailable"


# ─── the action's own step graph ────────────────────────────────────────────

def test_routing_step_runs_even_with_no_provider():
    """It used to be gated on `provider != ''`. It is now unconditional, so
    it must be a no-op in the unset case — covered above — and must not
    depend on anything installed later in the graph."""
    steps = ACTION["runs"]["steps"]
    names = [s.get("name") for s in steps]
    configure = names.index("Configure the model backend")
    python_setup = names.index("Set up Python")
    assert python_setup < configure, "the resolver needs Python available"
    # It must not need Node, pip packages, or the agent CLI itself.
    for later in ("Install the coding-agent CLI",
                  "Set up Node (for the agent CLI)"):
        assert names.index(later) > configure


def test_resolver_imports_only_stdlib():
    """The routing step runs before `pip install`, so a third-party import
    would fail on a cold runner."""
    source = (
        Path(__file__).resolve().parent.parent / "src" / "configure_backend.py"
    ).read_text()
    for banned in ("import yaml", "import requests", "import httpx"):
        assert banned not in source


def test_environments_md_is_still_only_written_when_absent():
    """A target repo that ships its own ENVIRONMENTS.md must keep it."""
    steps = ACTION["runs"]["steps"]
    envmd = next(
        s for s in steps if "ENVIRONMENTS.md" in (s.get("name") or "")
    )
    assert "! -f ENVIRONMENTS.md" in envmd["run"]


def test_unknown_agent_fails_loudly_rather_than_defaulting():
    """A typo must not silently route to Claude Code and bill the wrong
    vendor — but note this can only happen on a workflow that opted in."""
    with pytest.raises(ValueError):
        resolve("claude-code")
