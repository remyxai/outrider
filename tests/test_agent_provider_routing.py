"""(agent, provider) routing: parity with the shipped behavior, then reach.

Routing used to live in `case` arms in action.yml, one branch per agent. This
suite exists for two reasons:

1. **Parity.** The Claude Code paths are in production. The expected values
   below are transcribed from the shell they replaced, so a drift in the
   resolver fails here rather than in a customer's dispatch.
2. **Extensibility.** Compatibility is a join — an agent speaks one API
   family, a provider serves several — so the tests assert the *derivation*
   rather than a hand-written matrix. Adding a provider row should light up
   every agent that speaks its family, with no test edits.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from agents import PROVIDERS, agent_matrix, available, resolve
from agents.providers import ApiFamily, AuthStyle, RoutingError, resolve as route


def env(**kw):
    return dict(kw)


# ─── parity with the shell that shipped ─────────────────────────────────────
#
# Transcribed from action.yml's "Configure backend from provider input" step.
# `""` means the variable is actively cleared, which is how Claude Code's
# two mutually-exclusive auth vars were handled.

CLAUDE_PARITY = [
    (
        "anthropic",
        env(ANTHROPIC_API_KEY="ak-1"),
        {"ANTHROPIC_API_KEY": "ak-1", "ANTHROPIC_AUTH_TOKEN": ""},
    ),
    (
        "zai",
        env(ZAI_API_KEY="zk-1"),
        {
            "ANTHROPIC_AUTH_TOKEN": "zk-1",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
        },
    ),
    (
        "moonshot",
        env(MOONSHOT_API_KEY="mk-1"),
        {
            "ANTHROPIC_AUTH_TOKEN": "mk-1",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_BASE_URL": "https://api.moonshot.ai/anthropic",
        },
    ),
]


@pytest.mark.parametrize("provider,caller_env,expected", CLAUDE_PARITY)
def test_claude_routing_matches_the_shipped_shell(provider, caller_env, expected):
    routing = route(resolve("claude"), provider, "", "", caller_env)
    assert routing.env == expected


def test_claude_unset_model_stays_unset():
    """The shipped step wrote ANTHROPIC_MODEL only when `model` was given.

    Injecting the registry's default here would silently change which model
    existing z.ai / Moonshot workflows run.
    """
    routing = route(resolve("claude"), "zai", "", "", env(ZAI_API_KEY="zk"))
    assert "ANTHROPIC_MODEL" not in routing.env


def test_claude_explicit_model_is_honored():
    routing = route(
        resolve("claude"), "zai", "glm-4.6", "", env(ZAI_API_KEY="zk")
    )
    assert routing.env["ANTHROPIC_MODEL"] == "glm-4.6"


def test_claude_custom_leaves_the_callers_auth_alone():
    """The caller supplied the endpoint, so they own the auth choice —
    clearing either var could break a working on-prem setup."""
    routing = route(
        resolve("claude"), "custom", "", "https://proxy.internal/v1",
        env(ANTHROPIC_AUTH_TOKEN="tok"),
    )
    assert "ANTHROPIC_API_KEY" not in routing.env
    assert "ANTHROPIC_AUTH_TOKEN" not in routing.env


def test_claude_custom_requires_a_credential():
    with pytest.raises(RoutingError) as exc:
        route(resolve("claude"), "custom", "", "https://x/v1", env())
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_claude_custom_requires_a_base_url():
    with pytest.raises(RoutingError) as exc:
        route(resolve("claude"), "custom", "", "", env(ANTHROPIC_API_KEY="k"))
    assert "model-base-url" in str(exc.value)


# ─── the mutual exclusion that motivated all of it ──────────────────────────

@pytest.mark.parametrize("provider", ["zai", "moonshot"])
def test_bearer_providers_clear_the_x_api_key_var(provider):
    """Claude Code *prefers* ANTHROPIC_API_KEY when both are set, and these
    gateways reject that header with 401 — so the other var must be cleared,
    not merely left unset. Callers pass every vendor's secret at once."""
    secret = PROVIDERS[provider].secret_env
    routing = route(resolve("claude"), provider, "", "", env(**{secret: "k"}))
    assert routing.env["ANTHROPIC_API_KEY"] == ""
    assert routing.env["ANTHROPIC_AUTH_TOKEN"] == "k"


def test_anthropic_uses_the_x_api_key_var():
    routing = route(
        resolve("claude"), "anthropic", "", "", env(ANTHROPIC_API_KEY="k")
    )
    assert routing.env["ANTHROPIC_API_KEY"] == "k"
    assert routing.env["ANTHROPIC_AUTH_TOKEN"] == ""


# ─── passthrough: the backward-compatibility path ───────────────────────────

def test_empty_provider_changes_nothing():
    routing = route(resolve("claude"), "", "", "", env(ANTHROPIC_API_KEY="k"))
    assert routing.env == {}


def test_empty_provider_still_honors_an_explicit_model():
    """`model` is documented as always applying. The shell only wrote it
    inside the provider-gated branch, so provider='' + model set silently
    ignored the model — fixed here, deliberately."""
    routing = route(resolve("claude"), "", "claude-haiku-4-5", "", env())
    assert routing.env == {"ANTHROPIC_MODEL": "claude-haiku-4-5"}


# ─── codex: same registry, different API family ─────────────────────────────

def test_codex_routes_at_moonshot():
    routing = route(
        resolve("codex"), "moonshot", "", "", env(MOONSHOT_API_KEY="mk")
    )
    assert routing.env["CODEX_BASE_URL"] == "https://api.moonshot.ai/v1"
    assert routing.env["CODEX_API_KEY"] == "mk"
    # Codex has no default of its own, so the registry's applies.
    assert routing.env["CODEX_MODEL"] == "kimi-k3"


def test_the_same_provider_maps_to_different_endpoints_per_family():
    """One `provider` value, two protocols. This is the crux of the design:
    Moonshot serves both families, at different paths."""
    claude = route(resolve("claude"), "moonshot", "", "", env(MOONSHOT_API_KEY="k"))
    codex = route(resolve("codex"), "moonshot", "", "", env(MOONSHOT_API_KEY="k"))
    assert claude.env["ANTHROPIC_BASE_URL"].endswith("/anthropic")
    assert codex.env["CODEX_BASE_URL"].endswith("/v1")


def test_codex_rejects_a_provider_that_does_not_serve_its_family():
    """Anthropic serves Messages, not Responses. Routing it anyway would be
    worse than failing, and the message must name a working agent."""
    with pytest.raises(RoutingError) as exc:
        route(resolve("codex"), "anthropic", "", "", env(ANTHROPIC_API_KEY="k"))
    message = str(exc.value)
    assert "does not serve" in message
    assert "agent=claude" in message


def test_unverified_pair_warns_but_still_runs():
    """z.ai authenticates before routing, so its Responses support could not
    be confirmed. Warn rather than claim — or block."""
    routing = route(resolve("codex"), "zai", "", "", env(ZAI_API_KEY="zk"))
    assert routing.env["CODEX_BASE_URL"] == "https://api.z.ai/api/paas/v4"
    assert any("unverified" in w for w in routing.warnings)


def test_verified_pair_does_not_warn():
    routing = route(
        resolve("codex"), "moonshot", "", "", env(MOONSHOT_API_KEY="mk")
    )
    assert routing.warnings == []


# ─── backboard: a native router ─────────────────────────────────────────────

def test_backboard_composes_provider_and_model():
    routing = route(
        resolve("backboard"), "openai", "gpt-5.5", "",
        env(BACKBOARD_API_KEY="bk"),
    )
    assert routing.env["BACKBOARD_MODEL"] == "openai/gpt-5.5"


def test_backboard_leaves_an_already_qualified_model_alone():
    routing = route(
        resolve("backboard"), "openai", "anthropic/claude-opus-4-8", "",
        env(BACKBOARD_API_KEY="bk"),
    )
    assert routing.env["BACKBOARD_MODEL"] == "anthropic/claude-opus-4-8"


def test_backboard_sets_no_endpoint():
    """It resolves models itself; Outrider must not try to point it anywhere."""
    routing = route(
        resolve("backboard"), "openai", "gpt-5.5", "",
        env(BACKBOARD_API_KEY="bk"),
    )
    assert "BACKBOARD_API_URL" not in routing.env


def test_backboard_provider_without_a_model_is_an_error():
    with pytest.raises(RoutingError) as exc:
        route(resolve("backboard"), "openai", "", "", env(BACKBOARD_API_KEY="bk"))
    assert "<provider>/<model>" in str(exc.value)


# ─── missing credentials ────────────────────────────────────────────────────

@pytest.mark.parametrize("agent,provider,secret", [
    ("claude", "zai", "ZAI_API_KEY"),
    ("claude", "moonshot", "MOONSHOT_API_KEY"),
    ("codex", "moonshot", "MOONSHOT_API_KEY"),
    ("codex", "openai", "OPENAI_API_KEY"),
])
def test_missing_secret_names_the_variable(agent, provider, secret):
    with pytest.raises(RoutingError) as exc:
        route(resolve(agent), provider, "kimi-k3", "", env())
    assert secret in str(exc.value)


# ─── the matrix is derived, not maintained ──────────────────────────────────

def test_matrix_covers_every_agent():
    agents = {row["agent"] for row in agent_matrix()}
    assert agents == set(available())


def test_matrix_pairs_are_all_actually_routable():
    """Every pair the matrix advertises must resolve without error — the docs
    are generated from this, so an unroutable row would be a false promise."""
    for row in agent_matrix():
        if row["provider"].startswith("("):
            continue  # native router — no provider axis
        provider = PROVIDERS[row["provider"]]
        backend = resolve(row["agent"])
        # provider=custom carries no secret of its own — the credential comes
        # from the agent's own key var.
        secret = provider.secret_env or backend.key_env
        caller = env(**{secret: "test-key"})
        base = "https://gw.internal/v1" if provider.caller_supplied_endpoint else ""
        route(resolve(row["agent"]), row["provider"], "m", base, caller)


def test_adding_a_provider_needs_no_agent_changes():
    """Extensibility guard: a new Provider row serving an existing family
    becomes usable by that family's agents with no other edit."""
    from agents.providers import Provider

    fake = Provider(
        id="_fake", display_name="Fake", secret_env="FAKE_API_KEY",
        endpoints={ApiFamily.OPENAI_RESPONSES: "https://fake.test/v1"},
        default_model={ApiFamily.OPENAI_RESPONSES: "fake-1"},
        verified=frozenset({ApiFamily.OPENAI_RESPONSES}),
    )
    PROVIDERS["_fake"] = fake
    try:
        routing = route(
            resolve("codex"), "_fake", "", "", env(FAKE_API_KEY="fk")
        )
        assert routing.env["CODEX_BASE_URL"] == "https://fake.test/v1"
        assert routing.env["CODEX_MODEL"] == "fake-1"
        # And it must NOT become available to an agent speaking another family.
        with pytest.raises(RoutingError):
            route(resolve("claude"), "_fake", "", "", env(FAKE_API_KEY="fk"))
    finally:
        PROVIDERS.pop("_fake", None)


def test_every_agent_declares_an_api_family():
    for name in available():
        assert resolve(name).api_family is not None, name


def test_auth_style_defaults_to_bearer():
    """Most gateways are Bearer; only Anthropic's own is x-api-key."""
    assert PROVIDERS["moonshot"].auth_style(
        ApiFamily.ANTHROPIC_MESSAGES
    ) is AuthStyle.BEARER
    assert PROVIDERS["anthropic"].auth_style(
        ApiFamily.ANTHROPIC_MESSAGES
    ) is AuthStyle.API_KEY
