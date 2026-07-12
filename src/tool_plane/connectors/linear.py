"""Linear GraphQL connector for the tool-plane.

Fetches an issue's body + relevant metadata given a ``linear.app/*/issue/<ID>``
URL. Returns a ``ToolResponse`` envelope so callers get uniform audit-shaped
output regardless of connector.

Auth: reads the Linear API key from ``INPUT_LINEAR_API_KEY`` (set by the
action.yml ``linear-api-key`` input) with a fallback to a bare
``LINEAR_API_KEY`` env var (for local invocations or workflows that
export the secret at the job-level ``env:`` block instead of passing it
through the action input). Missing key returns ``status: not_configured``
(not an error) so callers can fall through to plain HTTP GET when
Linear-specific auth isn't wired.

Scope: read-only ``issue`` fetch by identifier. Comments, attachments,
related-issue graph — deferred to follow-up connector work once
traversal-shape design decisions settle. This connector returns just the
primary issue body + top-level metadata, enough for ``lead-content`` to
substitute a Linear issue's content in place of a gist.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Optional

from ..envelope import (
    ToolResponse,
    tool_response_error,
    tool_response_ok,
)


CONNECTOR = "linear"
CONNECTOR_VERSION = "0.1.0"
LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
LINEAR_URL_PATTERN = re.compile(
    r"^https?://linear\.app/[^/]+/issue/([A-Za-z0-9][A-Za-z0-9_-]*-[0-9]+)"
)


def is_linear_url(url: str) -> bool:
    """True iff the URL matches ``linear.app/<workspace>/issue/<IDENT>``."""
    return LINEAR_URL_PATTERN.match(url) is not None


def _extract_identifier(url: str) -> Optional[str]:
    m = LINEAR_URL_PATTERN.match(url)
    return m.group(1) if m else None


_QUERY = """
query IssueLookup($id: String!) {
  issue(id: $id) {
    identifier
    title
    description
    url
    state { name type }
    priority
    priorityLabel
    project { name }
    labels { nodes { name } }
    updatedAt
    createdAt
  }
}
"""


def fetch_issue(url: str, *, timeout_s: float = 15.0) -> ToolResponse:
    """Fetch a Linear issue by URL, return a ``ToolResponse``.

    Args:
        url: ``linear.app/<workspace>/issue/<IDENT>`` URL (query params +
            fragments are ignored).
        timeout_s: hard timeout on the GraphQL POST. Distinguishes a genuinely
            slow Linear response from a hung network.

    Returns ``ToolResponse`` with ``status`` set to:
        - ``ok`` with ``data`` populated on success
        - ``not_configured`` when ``LINEAR_API_KEY`` is missing
        - ``not_found`` when Linear returns null for the issue
        - ``rate_limited`` on HTTP 429
        - ``timeout`` on network timeout
        - ``error`` for all other failures
    """
    identifier = _extract_identifier(url)
    if identifier is None:
        return tool_response_error(
            connector=CONNECTOR,
            latency_ms=0.0,
            error_code="malformed_url",
            message=f"URL does not match Linear issue shape: {url}",
            connector_version=CONNECTOR_VERSION,
        )

    # Prefer the action-input passthrough (INPUT_LINEAR_API_KEY) over the
    # bare env var, matching the same precedence as INPUT_GITHUB_TOKEN vs
    # GITHUB_TOKEN. Either wiring path works; explicit-input takes precedence
    # so a customer who passes both gets the input-declared value.
    api_key = (
        os.environ.get("INPUT_LINEAR_API_KEY", "").strip()
        or os.environ.get("LINEAR_API_KEY", "").strip()
    )
    if not api_key:
        return tool_response_error(
            connector=CONNECTOR,
            latency_ms=0.0,
            error_code="linear_api_key_missing",
            message="LINEAR_API_KEY not set; connector cannot authenticate.",
            status="not_configured",
            connector_version=CONNECTOR_VERSION,
        )

    payload = json.dumps({"query": _QUERY, "variables": {"id": identifier}}).encode()
    req = urllib.request.Request(
        LINEAR_GRAPHQL_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": api_key,  # Linear takes the raw key; no "Bearer" prefix
        },
        method="POST",
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        latency_ms = (time.perf_counter() - started) * 1000
        if e.code == 429:
            return tool_response_error(
                connector=CONNECTOR,
                latency_ms=latency_ms,
                error_code="rate_limited",
                message=f"Linear API rate-limited (HTTP 429) for {identifier}",
                status="rate_limited",
                connector_version=CONNECTOR_VERSION,
            )
        return tool_response_error(
            connector=CONNECTOR,
            latency_ms=latency_ms,
            error_code=f"http_{e.code}",
            message=f"Linear API returned HTTP {e.code} for {identifier}",
            connector_version=CONNECTOR_VERSION,
        )
    except urllib.error.URLError as e:
        latency_ms = (time.perf_counter() - started) * 1000
        reason = str(e.reason) if e.reason else "network_error"
        # Timeout surfaces as URLError(reason=timeout(...)) or socket.timeout
        is_timeout = "timeout" in reason.lower() or "timed out" in reason.lower()
        return tool_response_error(
            connector=CONNECTOR,
            latency_ms=latency_ms,
            error_code="timeout" if is_timeout else "network_error",
            message=f"Linear API {reason} for {identifier}",
            status="timeout" if is_timeout else "error",
            connector_version=CONNECTOR_VERSION,
        )
    latency_ms = (time.perf_counter() - started) * 1000

    if "errors" in body:
        return tool_response_error(
            connector=CONNECTOR,
            latency_ms=latency_ms,
            error_code="graphql_error",
            message=json.dumps(body["errors"])[:400],
            connector_version=CONNECTOR_VERSION,
        )

    issue = body.get("data", {}).get("issue")
    if issue is None:
        return tool_response_error(
            connector=CONNECTOR,
            latency_ms=latency_ms,
            error_code="issue_not_found",
            message=f"Linear returned null for {identifier} — issue may not exist or may not be visible to this key",
            status="not_found",
            connector_version=CONNECTOR_VERSION,
        )

    # Build the inline snippet the way lead-content substitution expects:
    # a compact rendered view that reads well when embedded into SPEC.md.
    description = issue.get("description") or "(no description)"
    labels = [l["name"] for l in issue.get("labels", {}).get("nodes", [])]
    snippet_parts = [
        f"# {issue['identifier']}: {issue['title']}",
        "",
        f"**State**: {issue['state']['name']}   "
        f"**Priority**: {issue.get('priorityLabel') or 'None'}",
    ]
    if issue.get("project"):
        snippet_parts.append(f"**Project**: {issue['project']['name']}")
    if labels:
        snippet_parts.append(f"**Labels**: {', '.join(labels)}")
    snippet_parts.append(f"**URL**: {issue['url']}")
    snippet_parts.append("")
    snippet_parts.append(description)
    inline_snippet = "\n".join(snippet_parts)

    return tool_response_ok(
        connector=CONNECTOR,
        latency_ms=latency_ms,
        inline_snippet=inline_snippet,
        data={
            "identifier": issue["identifier"],
            "title": issue["title"],
            "url": issue["url"],
            "state": issue["state"]["name"],
            "priority": issue.get("priorityLabel"),
            "project": (issue.get("project") or {}).get("name"),
            "labels": labels,
            "updated_at": issue.get("updatedAt"),
            "created_at": issue.get("createdAt"),
            "description_length": len(description),
        },
        connector_version=CONNECTOR_VERSION,
    )
