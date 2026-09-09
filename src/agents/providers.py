"""Model-provider registry and (agent, provider) routing resolution.

This is the single place that knows how to point an agent at a model backend.
Before it existed, that knowledge lived in `case` arms in ``action.yml`` —
duplicated per agent, so adding one provider meant editing several shell
branches and adding one agent meant writing another.

The shape of the problem
------------------------
An agent CLI speaks exactly one API family; a provider may serve several. So
compatibility is not a hand-maintained matrix, it is a join:

    agent --speaks--> ApiFamily <--serves-- provider

That makes the extension points small and obvious:

* **New provider** — add one :class:`Provider` row naming the families it
  serves and the endpoint for each. Every agent that speaks one of those
  families can use it immediately, with no other edits.
* **New agent** — add one adapter declaring ``api_family``. Every provider
  serving that family works immediately.
* **New endpoint for an existing provider** — add one entry to its
  ``endpoints`` map.

Nothing else in the codebase enumerates agent/provider pairs, and the
documentation table is generated from this registry rather than written by
hand, so the two cannot drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ApiFamily(str, Enum):
    """The wire protocol an agent CLI speaks to its model backend."""

    #: Anthropic Messages (`/v1/messages`). Claude Code.
    ANTHROPIC_MESSAGES = "anthropic-messages"
    #: OpenAI Responses (`/v1/responses`). Codex — note codex-cli 0.151.0
    #: removed Chat-Completions support, so a Chat-only provider needs a
    #: translating gateway rather than a different wire_api setting.
    OPENAI_RESPONSES = "openai-responses"
    #: The agent resolves models itself against its own catalogue, so there
    #: is no endpoint for Outrider to set. Backboard R-CLI.
    NATIVE_ROUTER = "native-router"


class AuthStyle(str, Enum):
    """How the provider expects the credential to be presented."""

    #: `x-api-key: <key>`
    API_KEY = "api-key"
    #: `Authorization: Bearer <key>`
    BEARER = "bearer"


@dataclass(frozen=True)
class Provider:
    """One model vendor, and how to reach it per API family."""

    id: str
    display_name: str
    #: Env var the *caller* puts the key in. Convention: `<VENDOR>_API_KEY`.
    secret_env: str
    #: family -> base URL. An empty string means "the vendor's default host",
    #: i.e. the agent needs no base-URL override.
    endpoints: dict[ApiFamily, str]
    #: family -> auth style. Defaults to BEARER for anything unlisted.
    auth: dict[ApiFamily, AuthStyle] = field(default_factory=dict)
    #: family -> the model to use when the caller names none.
    default_model: dict[ApiFamily, str] = field(default_factory=dict)
    #: Families we have actually confirmed end-to-end. An unverified pair
    #: still runs, but warns — see :func:`resolve`.
    verified: frozenset[ApiFamily] = frozenset()
    #: True when the caller supplies the endpoint themselves.
    caller_supplied_endpoint: bool = False

    def serves(self, family: ApiFamily) -> bool:
        return family in self.endpoints

    def auth_style(self, family: ApiFamily) -> AuthStyle:
        return self.auth.get(family, AuthStyle.BEARER)


PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider(
        id="anthropic",
        display_name="Anthropic",
        secret_env="ANTHROPIC_API_KEY",
        endpoints={ApiFamily.ANTHROPIC_MESSAGES: ""},
        auth={ApiFamily.ANTHROPIC_MESSAGES: AuthStyle.API_KEY},
        verified=frozenset({ApiFamily.ANTHROPIC_MESSAGES}),
    ),
    "openai": Provider(
        id="openai",
        display_name="OpenAI",
        secret_env="OPENAI_API_KEY",
        endpoints={ApiFamily.OPENAI_RESPONSES: ""},
        verified=frozenset({ApiFamily.OPENAI_RESPONSES}),
    ),
    "zai": Provider(
        id="zai",
        display_name="z.ai (GLM)",
        secret_env="ZAI_API_KEY",
        # ANTHROPIC_MESSAGES only. z.ai does NOT serve the OpenAI Responses
        # API that Codex requires — verified with a real key:
        #   POST /api/paas/v4/responses        -> 404 {"path":"/v4/responses"}
        #   POST /api/paas/v4/chat/completions -> 200-class (endpoint exists)
        # It is Chat-Completions-only, and codex-cli 0.151.0 removed Chat
        # support, so Codex cannot reach z.ai directly at all. This row
        # previously advertised the paas/v4 base for OPENAI_RESPONSES, which
        # would have 404'd mid-run; omitting the family makes the pair fail
        # fast with a message naming the agent that does work.
        #
        # A dev who wants GLM under Codex needs a translating gateway
        # (provider=custom + model-base-url) or a router that exposes
        # Responses.
        endpoints={
            ApiFamily.ANTHROPIC_MESSAGES: "https://api.z.ai/api/anthropic",
        },
        default_model={
            ApiFamily.ANTHROPIC_MESSAGES: "glm-5.3",
        },
        verified=frozenset({ApiFamily.ANTHROPIC_MESSAGES}),
    ),
    "moonshot": Provider(
        id="moonshot",
        display_name="Moonshot (Kimi)",
        secret_env="MOONSHOT_API_KEY",
        endpoints={
            ApiFamily.ANTHROPIC_MESSAGES: "https://api.moonshot.ai/anthropic",
            # Verified: /v1/responses exists on this host — a bogus path on
            # the same host 404s while /v1/responses does not.
            ApiFamily.OPENAI_RESPONSES: "https://api.moonshot.ai/v1",
        },
        default_model={
            ApiFamily.ANTHROPIC_MESSAGES: "kimi-k3",
            ApiFamily.OPENAI_RESPONSES: "kimi-k3",
        },
        verified=frozenset({
            ApiFamily.ANTHROPIC_MESSAGES, ApiFamily.OPENAI_RESPONSES,
        }),
    ),
    "openrouter": Provider(
        id="openrouter",
        display_name="OpenRouter",
        secret_env="OPENROUTER_API_KEY",
        # Serves BOTH families, which makes it the one provider every agent
        # can reach: one key, one id, any agent. Probed — /api/v1/responses
        # and /api/v1/messages both answer 401 (the latter in Anthropic's own
        # error shape) while a bogus path on the same host 404s.
        #
        # Base URLs differ by family because the clients append different
        # suffixes: Claude Code appends /v1/messages to its base, Codex
        # appends /responses.
        endpoints={
            ApiFamily.ANTHROPIC_MESSAGES: "https://openrouter.ai/api",
            ApiFamily.OPENAI_RESPONSES: "https://openrouter.ai/api/v1",
        },
        # No default: OpenRouter ids are namespaced (`z-ai/glm-5.3`,
        # `anthropic/claude-...`) and picking one here would rot as models
        # move. Callers name a model; see the no-default warning in resolve().
        #
        # Both families verified with real completions against z-ai/glm-5.3:
        # /v1/messages returned an Anthropic-shaped message with content
        # blocks and usage; /v1/responses returned status="completed" with a
        # usage block. So the protocol claim is backed, not inferred.
        #
        # Caveat for operators rather than for this registry: both agent CLIs
        # request a very large max_tokens (Codex asks for 131,072), which a
        # free-tier OpenRouter account rejects with HTTP 402 before the model
        # is ever called. A paid account is required for a real run — that is
        # a billing state, not a compatibility problem.
        verified=frozenset({
            ApiFamily.ANTHROPIC_MESSAGES, ApiFamily.OPENAI_RESPONSES,
        }),
    ),
    "custom": Provider(
        id="custom",
        display_name="Custom endpoint",
        secret_env="",  # resolved per agent — see Routing
        endpoints={
            ApiFamily.ANTHROPIC_MESSAGES: "",
            ApiFamily.OPENAI_RESPONSES: "",
        },
        caller_supplied_endpoint=True,
    ),
}


class RoutingError(Exception):
    """A configuration that cannot work, with an actionable message."""


@dataclass
class Routing:
    """The environment an agent needs to reach the selected provider.

    ``env`` values are written verbatim; an empty string means *clear this
    variable*, which is how mutual-exclusion between two auth vars is
    expressed.
    """

    env: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: Resolved for the step summary / telemetry label.
    provider_display: str = ""
    model: str = ""


def providers_for(family: ApiFamily) -> list[str]:
    """Provider ids that serve ``family``, in registry order."""
    return [p.id for p in PROVIDERS.values() if p.serves(family)]


def compatibility_matrix(agents: dict) -> list[dict]:
    """Every valid (agent, provider) pair, derived — not hand-written.

    ``agents`` maps agent name -> its backend. Used by the docs generator and
    by the tests that keep the published matrix honest.
    """
    rows: list[dict] = []
    for agent, backend in sorted(agents.items()):
        family = backend.api_family
        if family is ApiFamily.NATIVE_ROUTER:
            rows.append({
                "agent": agent, "provider": "(any — agent-resolved)",
                "family": family.value, "endpoint": "",
                # A consumer still has to tell the user which secret to set,
                # and for a native router that is the agent's own key.
                "secret": backend.key_env,
                "default_model": "", "verified": True,
            })
            continue
        for pid in providers_for(family):
            provider = PROVIDERS[pid]
            rows.append({
                "agent": agent,
                "provider": pid,
                "family": family.value,
                "endpoint": provider.endpoints[family] or "(vendor default)",
                "secret": provider.secret_env or "(agent's own)",
                "default_model": provider.default_model.get(family, ""),
                "verified": family in provider.verified,
            })
    return rows


def resolve(
    backend,
    provider_id: str,
    model: str,
    base_url_override: str,
    env: dict[str, str],
) -> Routing:
    """Resolve ``(agent, provider, model)`` into environment for the run.

    Raises :class:`RoutingError` with an actionable message for any pair that
    cannot work. An empty ``provider_id`` is a passthrough: it changes
    nothing, which is what keeps every pre-existing workflow byte-for-byte on
    its current path.
    """
    provider_id = (provider_id or "").strip().lower()
    model = (model or "").strip()
    base_url_override = (base_url_override or "").strip()
    family = backend.api_family

    if not provider_id:
        return _passthrough(backend, model, base_url_override, env)

    if family is ApiFamily.NATIVE_ROUTER:
        # A native router resolves provider ids against its own catalogue —
        # Backboard's includes openrouter, cerebras, featherless and more that
        # this registry has no reason to know, because Outrider never picks an
        # endpoint for them. Validating against PROVIDERS here would reject
        # valid combinations and go stale, so unknown ids pass through and the
        # vendor rejects what it does not recognize.
        provider = PROVIDERS.get(provider_id) or Provider(
            id=provider_id,
            display_name=provider_id,
            secret_env="",
            endpoints={ApiFamily.NATIVE_ROUTER: ""},
        )
        return backend.routing(provider, family, model, "", env)

    provider = PROVIDERS.get(provider_id)
    if provider is None:
        raise RoutingError(
            f"unknown provider '{provider_id}'; must be one of: "
            + ", ".join(sorted(PROVIDERS))
        )

    if not provider.serves(family):
        alternatives = [
            name for name, fam in _AGENT_FAMILIES.items()
            if provider.serves(fam)
        ]
        hint = (
            f" Use agent={' or '.join(sorted(alternatives))} for this provider."
            if alternatives else ""
        )
        raise RoutingError(
            f"agent={backend.name} speaks {family.value}, which "
            f"{provider.display_name} does not serve.{hint}"
        )

    base_url = provider.endpoints[family]
    if provider.caller_supplied_endpoint:
        if not base_url_override:
            raise RoutingError(
                f"provider=custom requires model-base-url (a "
                f"{family.value} endpoint)"
            )
        base_url = base_url_override

    routing = backend.routing(provider, family, model, base_url, env)
    if not model and not provider.default_model.get(family):
        routing.warnings.append(
            f"no model named and {provider.display_name} has no default for "
            f"{family.value}, so {backend.display_name} will send its own "
            f"default model id — which this provider may not recognise. Set "
            f"the `model` input to a model id {provider.display_name} lists."
        )
    if family not in provider.verified and not provider.caller_supplied_endpoint:
        routing.warnings.append(
            f"agent={backend.name} + provider={provider_id} is unverified: "
            f"{provider.display_name}'s {family.value} support has not been "
            f"confirmed end-to-end. If the run fails with an unexpected 4xx, "
            f"the endpoint likely speaks a different protocol — use "
            f"provider=custom with a translating gateway."
        )
    return routing


def _passthrough(backend, model, base_url_override, env) -> Routing:
    """No provider selected: honor only an explicit model / base URL."""
    routing = Routing(provider_display="", model=model)
    routing.env.update(
        backend.passthrough_env(model=model, base_url=base_url_override)
    )
    return routing


# Populated by agents/__init__ once the registry is built, so error messages
# can name a working alternative agent. Kept as a module global rather than an
# import to avoid a cycle.
_AGENT_FAMILIES: dict[str, ApiFamily] = {}


def register_agent_family(name: str, family: ApiFamily) -> None:
    _AGENT_FAMILIES[name] = family
