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


def test_empty_provider_ignores_model_for_claude_exactly_as_before():
    """Backwards compatibility, deliberately preserving a documented wart.

    `model` is documented as always applying, but the shipped step wrote it
    only inside the provider-gated branch. Honoring it would change what
    existing installs do — a `model` the default backend does not serve
    currently works because it is ignored, and would start 404ing. Frozen.
    """
    routing = route(resolve("claude"), "", "claude-haiku-4-5", "", env())
    assert routing.env == {}


def test_empty_provider_honors_model_for_the_new_agents():
    """Codex and R-CLI have no shipped behavior to preserve, so they get the
    documented behavior."""
    routing = route(resolve("codex"), "", "gpt-5-nano", "", env())
    assert routing.env == {"CODEX_MODEL": "gpt-5-nano"}


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


def test_codex_cannot_reach_zai_and_says_so():
    """z.ai does not serve the OpenAI Responses API — verified with a real
    key: /api/paas/v4/responses returns 404 while /chat/completions exists.

    It is Chat-Completions-only and codex-cli 0.151.0 removed Chat support,
    so this pair is impossible rather than merely unproven. Failing fast with
    a message naming the working agent beats a mid-run 404.
    """
    with pytest.raises(RoutingError) as exc:
        route(resolve("codex"), "zai", "glm-5.3", "", env(ZAI_API_KEY="zk"))
    message = str(exc.value)
    assert "does not serve" in message
    assert "agent=claude" in message


def test_claude_still_reaches_zai_directly():
    """Direct provider access must not depend on any router: a dev with only
    a ZAI_API_KEY gets a working config."""
    routing = route(resolve("claude"), "zai", "", "", env(ZAI_API_KEY="zk"))
    assert routing.env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert routing.env["ANTHROPIC_AUTH_TOKEN"] == "zk"
    assert routing.warnings == [], "a verified direct pair must not warn"


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


def test_backboard_leaves_a_model_already_prefixed_with_this_provider():
    routing = route(
        resolve("backboard"), "openai", "openai/gpt-5.5", "",
        env(BACKBOARD_API_KEY="bk"),
    )
    assert routing.env["BACKBOARD_MODEL"] == "openai/gpt-5.5"


def test_backboard_prefixes_a_multi_segment_model_id():
    """Backboard ids are often three levels deep — `openrouter/~z-ai/glm-latest`.

    A bare `contains "/"` test read `~z-ai/glm-latest` as already qualified
    and dropped the provider segment, and the router rejected the id. Found
    on a real run against the live catalogue.
    """
    routing = route(
        resolve("backboard"), "openrouter", "~z-ai/glm-latest", "",
        env(BACKBOARD_API_KEY="bk"),
    )
    assert routing.env["BACKBOARD_MODEL"] == "openrouter/~z-ai/glm-latest"


def test_backboard_does_not_double_prefix():
    routing = route(
        resolve("backboard"), "openrouter", "openrouter/~z-ai/glm-latest", "",
        env(BACKBOARD_API_KEY="bk"),
    )
    assert routing.env["BACKBOARD_MODEL"] == "openrouter/~z-ai/glm-latest"


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


def test_native_router_accepts_provider_ids_this_registry_never_heard_of():
    """Backboard's catalogue includes openrouter, cerebras, featherless…

    Outrider never picks an endpoint for a native router, so validating its
    provider ids against this registry would reject valid combinations and go
    stale. Unknown ids pass through; the vendor rejects what it doesn't know.
    """
    for pid in ("openrouter", "cerebras", "featherless"):
        routing = route(
            resolve("backboard"), pid, "some-model", "",
            env(BACKBOARD_API_KEY="bk"),
        )
        assert routing.env["BACKBOARD_MODEL"] == f"{pid}/some-model"


def test_a_non_router_agent_still_rejects_an_unknown_provider():
    """The passthrough is specific to native routers — Claude Code and Codex
    need a real endpoint, so an id this registry has never heard of must
    still fail rather than route somewhere arbitrary."""
    with pytest.raises(RoutingError) as exc:
        route(resolve("claude"), "some-vendor-we-do-not-know", "x", "", env())
    assert "unknown provider" in str(exc.value)


def test_a_native_router_accepts_the_same_unknown_id():
    """Same input, opposite outcome — because Backboard resolves it and
    Outrider does not have to."""
    routing = route(
        resolve("backboard"), "some-vendor-we-do-not-know", "m", "",
        env(BACKBOARD_API_KEY="bk"),
    )
    assert routing.env["BACKBOARD_MODEL"] == "some-vendor-we-do-not-know/m"


# ─── OpenRouter: the provider every agent can reach ─────────────────────────

def test_openrouter_serves_both_api_families():
    """One key, one provider id, any agent — which is why it is worth having.

    Probed: /api/v1/responses and /api/v1/messages both answer 401 (the
    latter in Anthropic's own error shape) while a bogus path on the same
    host 404s.
    """
    provider = PROVIDERS["openrouter"]
    assert provider.serves(ApiFamily.ANTHROPIC_MESSAGES)
    assert provider.serves(ApiFamily.OPENAI_RESPONSES)


def test_openrouter_base_urls_differ_by_family():
    """The clients append different suffixes: Claude Code adds /v1/messages
    to its base, Codex adds /responses. One shared base URL would 404 one of
    them."""
    claude = route(resolve("claude"), "openrouter", "z-ai/glm-4.6", "",
                   env(OPENROUTER_API_KEY="or"))
    codex = route(resolve("codex"), "openrouter", "z-ai/glm-4.6", "",
                  env(OPENROUTER_API_KEY="or"))
    assert claude.env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    assert codex.env["CODEX_BASE_URL"] == "https://openrouter.ai/api/v1"


def test_openrouter_uses_bearer_for_claude():
    routing = route(resolve("claude"), "openrouter", "z-ai/glm-4.6", "",
                    env(OPENROUTER_API_KEY="or"))
    assert routing.env["ANTHROPIC_AUTH_TOKEN"] == "or"
    assert routing.env["ANTHROPIC_API_KEY"] == ""


def test_openrouter_is_reachable_by_every_agent():
    """The point of the join: one row lights up every agent whose family it
    serves, and backboard reaches it natively."""
    reachable = {
        row["agent"] for row in agent_matrix()
        if row["provider"] in ("openrouter", "(any — agent-resolved)")
    }
    assert reachable == set(available())


def test_openrouter_is_verified_on_both_families():
    """Backed by real completions against z-ai/glm-5.3: /v1/messages returned
    an Anthropic-shaped message, /v1/responses returned status="completed".
    So neither pair warns about protocol compatibility any more."""
    for agent in ("claude", "codex"):
        routing = route(resolve(agent), "openrouter", "z-ai/glm-5.3", "",
                        env(OPENROUTER_API_KEY="or"))
        assert not any("unverified" in w for w in routing.warnings), agent


def test_a_provider_with_no_default_model_warns_when_none_is_named():
    """OpenRouter ids are namespaced (`z-ai/glm-4.6`), so there is no sane
    default to invent. Without a model the agent sends its own default id,
    which the provider will not recognise — say so."""
    routing = route(resolve("claude"), "openrouter", "", "",
                    env(OPENROUTER_API_KEY="or"))
    assert any("no model named" in w for w in routing.warnings)


def test_a_provider_with_a_default_model_does_not_warn():
    routing = route(resolve("codex"), "moonshot", "", "",
                    env(MOONSHOT_API_KEY="mk"))
    assert not any("no model named" in w for w in routing.warnings)
