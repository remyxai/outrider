"""Agent-backend registry.

``resolve(name)`` returns the backend for an ``agent`` action-input value.
Empty / unset resolves to Claude Code, which is what every run used before
this port existed — that default is load-bearing for backward compatibility
and is pinned by test.
"""
from __future__ import annotations

from agents.base import (  # noqa: F401 — re-exported for callers
    BASE_ENV_WHITELIST,
    AgentBackend,
    AgentResult,
    Capability,
    Event,
    PromptDelivery,
)
from agents.backboard import BackboardBackend
from agents.claude import ClaudeCodeBackend
from agents.codex import CodexBackend

DEFAULT_AGENT = "claude"

_REGISTRY: dict[str, type[AgentBackend]] = {
    ClaudeCodeBackend.name: ClaudeCodeBackend,
    CodexBackend.name: CodexBackend,
    BackboardBackend.name: BackboardBackend,
}


def available() -> tuple[str, ...]:
    """Registered agent names, for error messages and input validation."""
    return tuple(sorted(_REGISTRY))


def resolve(name: str | None) -> AgentBackend:
    """Instantiate the backend for ``name``.

    Empty / None resolves to the default. An unknown name raises rather than
    silently falling back — a typo in the ``agent`` input should fail the run
    loudly at startup, not quietly route to a different agent than the caller
    asked for.
    """
    key = (name or "").strip().lower() or DEFAULT_AGENT
    try:
        return _REGISTRY[key]()
    except KeyError:
        raise ValueError(
            f"unknown agent '{key}'; must be one of: {', '.join(available())}"
        ) from None
