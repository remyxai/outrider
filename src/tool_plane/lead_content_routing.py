"""URL routing for the ``lead-content`` input.

The classic path treats ``INPUT_LEAD_CONTENT`` as either verbatim text or
a URL for the coding session's WebFetch to resolve. The tool-plane
extension lets Outrider pre-resolve URLs owned by known connectors,
substituting the fetched content in place of the URL before the coding
session starts.

Two properties matter:

1. **Backward compat**: unknown URLs / raw text fall through unchanged.
   The URL still appears in SPEC.md; the coding session's WebFetch still
   resolves it at session-time if it wants.
2. **Auth-aware routing**: if a URL matches a connector's owned domain
   but that connector's auth isn't configured, the router leaves the URL
   verbatim (falling through) rather than crashing.
"""

from __future__ import annotations

from typing import Optional, Tuple

from .connectors import linear
from .envelope import ToolResponse


def resolve_lead_content(raw: str) -> Tuple[str, Optional[ToolResponse]]:
    """Resolve a ``lead-content`` value to the text that should be
    substituted into SPEC.md, plus the ``ToolResponse`` for audit.

    Args:
        raw: value of ``INPUT_LEAD_CONTENT`` — may be verbatim text, a URL
            with no matching connector, or a URL that a connector owns.

    Returns:
        ``(resolved_text, tool_response)`` where:

        - ``resolved_text`` is the substitution the caller should use.
          Falls through to ``raw`` when the URL doesn't match a connector
          OR when the connector fetch fails / is not configured.
        - ``tool_response`` is populated when a connector was consulted
          (regardless of success), ``None`` when the input didn't match
          any connector's URL pattern. Callers should log the response
          in the run's audit block when non-None.
    """
    value = (raw or "").strip()
    if not value:
        return value, None

    # Linear
    if linear.is_linear_url(value):
        response = linear.fetch_issue(value)
        if response.status == "ok" and response.inline_snippet:
            return response.inline_snippet, response
        # Any non-ok status: fall through to raw URL. Caller still logs
        # the response so the audit block records the attempt.
        return value, response

    # No connector owns this URL/text.
    return value, None
