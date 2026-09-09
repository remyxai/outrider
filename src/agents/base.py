"""Agent-backend port: the contract every coding-agent CLI must satisfy.

Outrider drives a coding agent as a headless subprocess: it hands the agent a
prompt and a working directory, and the git diff the agent leaves behind is the
product. Everything downstream of that diff — the path allowlist, the stub
density check, pytest, the integration validator, diff-risk scoring, the
fidelity/convention/test chain — is already agent-neutral. This module isolates
the part that is not.

Design notes
------------
**Capability sets, not feature parity.** A backend declares what it can do; the
orchestrator degrades the affected telemetry rather than refusing to run. This
mirrors how Outrider already behaves elsewhere (a PR downgrades to an Issue; an
unknown model annotates ``cost_basis`` instead of inventing a dollar figure).
Degradation is decided centrally from :class:`Capability` — never by
``if backend.name == ...`` at a call site.

**Backends normalize into the shapes run.py already speaks.** Usage is emitted
as a list of Anthropic-style envelopes so ``_record_claude_usage`` needs no
per-backend branching, and transcripts are emitted as :class:`Event` so the
selection-coverage and exploration-structure parsers stop reading Anthropic
content blocks. Where a vendor differs (camelCase fields, usage emitted per
round rather than per call, paths hidden inside a patch body) the adapter
absorbs it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum


class Capability(str, Enum):
    """What a backend can do. Absence drives degradation, not failure."""

    #: Returns a parseable final assistant message from a one-shot run.
    ONESHOT_JSON = "oneshot_json"
    #: Emits a structured tool-call transcript (powers coverage telemetry).
    STREAM_TRANSCRIPT = "stream_transcript"
    #: The CLI itself enforces a round limit (e.g. ``--max-turns``).
    TURN_CAP = "turn_cap"
    #: Reports token counts.
    TOKEN_USAGE = "token_usage"
    #: Reports authoritative dollars (otherwise: rate table, or unavailable).
    COST_USD = "cost_usd"
    #: Has a web search/fetch tool (gates the staged research phase).
    WEB_RESEARCH = "web_research"
    #: Can constrain the final response to a JSON Schema.
    OUTPUT_SCHEMA = "output_schema"
    #: Can be given a launch-time guardrail policy that removes tool reach
    #: from the agent (Claude Code's PreToolUse hook, Codex's execpolicy
    #: rules, R-CLI's deny rules / --excluded-tools). Absence means an
    #: injected instruction the agent complies with is not defanged at the
    #: tool boundary — see ``guardrail_note``.
    GUARDRAIL_POLICY = "guardrail_policy"


# Environment every agent CLI legitimately needs. Per-backend auth vars are
# appended by the adapter; nothing else is inherited.
#
# The parent process holds secrets the agent must never see — REMYX_API_KEY,
# INPUT_GITHUB_TOKEN (the bot's installation token), every INPUT_* action
# input. Stripping at the launch boundary stops those from entering the
# agent's context at all, pairing with the outbound-body scrubber that
# catches secrets at egress.
#
# Add a var only with a comment naming the case. Never widen to a prefix
# wildcard — a future vendor env var may carry a telemetry token the agent
# shouldn't see verbatim.
BASE_ENV_WHITELIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TERM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TMPDIR",
    "TMP",
    "TEMP",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "CI",
    "GITHUB_ACTIONS",
)


@dataclass(frozen=True)
class Event:
    """One normalized transcript event.

    The single shape the selection-coverage and exploration-structure parsers
    consume. ``tool`` is a closed vocabulary — each adapter maps its vendor
    names into it — so those parsers never learn a vendor's tool taxonomy.
    """

    #: "tool_use" | "tool_result" | "text"
    kind: str
    #: Correlation id pairing a tool_use with its tool_result.
    id: str = ""
    #: Normalized tool name: read|write|edit|search|glob|execute|web|other
    tool: str = ""
    #: Repo paths this call touched, already extracted from the vendor payload.
    paths: tuple[str, ...] = ()
    #: Shell command, when ``tool == "execute"``.
    command: str | None = None
    #: Payload size in lines — the ``visible_lines`` coverage signal.
    lines: int = 0
    #: Index of the agent turn this call belongs to. Turn grouping is what
    #: makes `linearity` / `parallel_turns` meaningful: a turn issuing several
    #: reads at once is branching exploration, several turns issuing one read
    #: each is linear. A flat event list cannot express that difference.
    turn: int = 0


@dataclass
class AgentResult:
    """Outcome of one agent invocation, normalized across backends."""

    ok: bool
    #: The final assistant message.
    text: str = ""
    #: Anthropic-style usage envelopes, ready for ``_record_claude_usage``.
    #: Backends that report usage per round MUST fold them into a single
    #: envelope so per-call accounting stays comparable across agents.
    usage_envelopes: list[dict] = field(default_factory=list)
    #: Normalized transcript; empty when STREAM_TRANSCRIPT is absent or the
    #: caller didn't ask for it.
    events: list[Event] = field(default_factory=list)
    #: Operator-facing notes about the run that are NOT part of the agent's
    #: answer — kept out of ``text`` because several passes parse that as a
    #: JSON verdict. The orchestrator logs these.
    diagnostics: list[str] = field(default_factory=list)


class AgentBackend:
    """Base class for coding-agent CLI adapters.

    Subclasses override the small surface below. Everything else — subprocess
    launch, timeout, env stripping — stays in the orchestrator so that policy
    lives in one place.
    """

    #: Registry key and the value of the ``agent`` action input.
    name: str = ""
    #: Program name on PATH, used for the not-found message and failure labels.
    tool: str = ""
    #: How to install it, quoted verbatim in the not-found message.
    install_hint: str = ""
    #: Vendor console URLs surfaced when a run fails with a billing/auth cause.
    billing_url: str | None = None
    keys_url: str | None = None
    #: Auth env vars appended to :data:`BASE_ENV_WHITELIST`.
    auth_env: tuple[str, ...] = ()
    capabilities: frozenset[Capability] = frozenset()

    # ── capability helpers ────────────────────────────────────────────────

    def can(self, cap: Capability) -> bool:
        return cap in self.capabilities

    def guardrail_note(self) -> str | None:
        """Warning to surface when this backend cannot defang tool reach.

        Returns None when GUARDRAIL_POLICY is available. Callers log this
        loudly rather than silently running an unguarded agent.
        """
        if self.can(Capability.GUARDRAIL_POLICY):
            return None
        return (
            f"{self.name}: no launch-time guardrail policy is wired for this "
            f"backend, so the injection-hardening tool gate that Claude Code "
            f"runs get is NOT in effect. The post-hoc diff validators still "
            f"apply."
        )

    # ── tool surface ──────────────────────────────────────────────────────

    #: Directory this CLI loads packaged "skills" from, relative to $HOME.
    #: None when the agent has no such mechanism — the tool is then reachable
    #: only as a plain executable through the agent's shell tool.
    skills_home: str | None = None

    def tool_invocation_hint(self, executable: str) -> str:
        """How to tell *this* agent to use an out-of-band tool.

        Every backend has a shell/execute tool, so a binary on PATH is the
        portable surface. Agent-native wrappers (a Claude Code skill, a slash
        command) are not, and describing one to an agent that has no such
        mechanism sends it looking for a command it cannot run.
        """
        return f"Run `{executable}` from the shell."

    # ── model routing ─────────────────────────────────────────────────────

    #: The wire protocol this CLI speaks to its model backend. Set by every
    #: adapter; it is the whole basis of provider compatibility.
    api_family = None  # type: ignore[assignment]

    def routing(self, provider, family, model: str, base_url: str, env: dict):
        """Environment that points this agent at ``provider``.

        Default implementation covers any agent that takes a base URL, a
        bearer-style key and a model name in three env vars — which is most
        of them. Override only for a genuine idiosyncrasy (Claude Code's
        two mutually-exclusive auth vars; a router that takes no endpoint).
        """
        from agents.providers import Routing, RoutingError

        secret_env = provider.secret_env or self.key_env
        key = (env.get(secret_env) or "").strip()
        if not key:
            raise RoutingError(
                f"provider={provider.id} requires {secret_env} in the "
                f"caller's env block"
            )
        chosen = model or provider.default_model.get(family, "")
        routing = Routing(provider_display=provider.display_name, model=chosen)
        routing.env[self.key_env] = key
        if base_url:
            routing.env[self.base_url_env] = base_url
        elif self.base_url_env:
            # This provider uses its vendor default, so the agent must NOT
            # inherit an endpoint from anywhere else. Writing an empty value
            # clears it: without this, a base URL left in the caller's env
            # (or by an earlier step) silently wins and the run talks to the
            # wrong vendor with this vendor's key — observed as a 401 whose
            # telemetry named the wrong backend entirely. Same reasoning as
            # Claude Code's mutually-exclusive auth vars.
            routing.env[self.base_url_env] = ""
        if chosen:
            routing.env[self.model_env] = chosen
        return routing

    def passthrough_env(self, *, model: str, base_url: str) -> dict[str, str]:
        """Env for an unset `provider` — honor only what the caller gave.

        This is the backward-compatibility path: with no provider selected,
        nothing about the environment may change beyond an explicit model or
        base URL, so every pre-existing workflow behaves exactly as before.
        """
        out: dict[str, str] = {}
        if model and self.model_env:
            out[self.model_env] = model
        if base_url and self.base_url_env:
            out[self.base_url_env] = base_url
        return out

    #: Env var names this agent reads for its credential / endpoint / model.
    key_env: str = ""
    base_url_env: str = ""
    model_env: str = ""

    # ── cost attribution ──────────────────────────────────────────────────

    #: Human-readable name for this agent in the step summary and telemetry.
    display_name: str = ""

    def cost_label(self, model: str = "") -> str:
        """What served this run, for the ``model_backend`` telemetry field.

        Kept distinct from :attr:`name` (the *agent*) because the two are
        separate axes: the same agent can route at several model backends, and
        collapsing them makes a Codex-on-OpenAI run indistinguishable from a
        Claude-on-Anthropic one in the fleet report.
        """
        label = self.display_name or self.name
        return f"{label} ({model})" if model else label

    # ── environment ───────────────────────────────────────────────────────

    def env_whitelist(self) -> tuple[str, ...]:
        """Env vars this backend's subprocess may inherit."""
        return tuple(self.auth_env) + BASE_ENV_WHITELIST

    def subprocess_env(self) -> dict[str, str]:
        """Minimal env dict built from :meth:`env_whitelist`."""
        env: dict[str, str] = {}
        for key in self.env_whitelist():
            val = os.environ.get(key)
            if val is not None:
                env[key] = val
        return env

    def preflight(self) -> tuple[bool, list[str]]:
        """Check auth env before any clone or prompt build.

        Returns ``(ok, messages)``; messages are surfaced verbatim. Default is
        permissive — a backend with no checkable precondition passes.
        """
        return True, []

    # ── invocation ────────────────────────────────────────────────────────

    def base_cmd(self) -> list[str]:
        """argv prefix common to every invocation of this backend."""
        raise NotImplementedError

    def finalize_cmd(
        self,
        cmd_prefix: list[str],
        prompt: str,
        *,
        stream: bool = False,
    ) -> tuple[list[str], str | None]:
        """Complete the argv and decide prompt delivery.

        Returns ``(argv, stdin_text)``. ``stdin_text`` is None when the prompt
        rides in argv.
        """
        raise NotImplementedError

    def turn_cap_args(self, max_turns: int | None) -> list[str]:
        """Round-limit flags, or ``[]`` when the CLI has no such knob.

        A backend without TURN_CAP silently drops the cap here; the
        orchestrator's wall-clock timeout remains the outer bound.
        """
        return []

    def not_found_message(self) -> str:
        hint = f" (install: {self.install_hint})" if self.install_hint else ""
        return f"{self.tool} CLI not found on PATH{hint}"

    def timeout_message(self, timeout_s: int) -> str:
        return f"{self.tool} CLI timed out after {timeout_s}s"

    # ── parsing ───────────────────────────────────────────────────────────

    def parse(
        self, returncode: int, stdout: str, stderr: str, *, stream: bool = False
    ) -> AgentResult | None:
        """Turn raw process output into an :class:`AgentResult`.

        Returns None when the output could not be parsed at all, which tells
        the orchestrator to fall back to its generic CLI-failure diagnostic
        (exit code + stderr, formatted so the cause survives tail-slicing).
        """
        raise NotImplementedError
