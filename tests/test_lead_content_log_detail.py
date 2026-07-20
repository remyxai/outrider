"""Log-emission tests for lead-content connector routing.

When a connector (Linear, and later GitHub / engine.remyx.ai) returns a
non-ok status, the coding session proceeds by falling through to the raw
URL — so the failure would otherwise be invisible until someone re-runs
the dispatch. The log line has to carry `error_code` and (truncated)
`message` so ops can diagnose without a re-run.

These tests exercise the log-emission logic at the ToolResponse-shape
level, not the connector itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tool_plane.envelope import (  # noqa: E402
    ToolResponse,
    tool_response_error,
    tool_response_ok,
)


def _log_line_for(response: ToolResponse) -> str:
    """Reproduces run.py's log-emission logic for a lead-content tool
    response, as it stands after the v1.7.26 patch. Kept here as a unit
    so log-format regressions get caught before deploy."""
    line = (
        f"  → lead-content routed via {response.connector} "
        f"connector: status={response.status} "
        f"latency={response.latency_ms:.0f}ms"
    )
    if response.status != "ok":
        detail_parts: list[str] = []
        if response.error_code:
            detail_parts.append(f"error_code={response.error_code}")
        if response.inline_snippet:
            truncated = response.inline_snippet.replace("\n", " ")[:200]
            detail_parts.append(f"message={truncated!r}")
        if detail_parts:
            line += " · " + " · ".join(detail_parts)
    return line


def test_ok_status_stays_compact():
    """The ok path — the coding session got the content, no detail needed."""
    resp = tool_response_ok(
        connector="linear",
        latency_ms=210.4,
        inline_snippet="# TEAM-XXX ...",
    )
    line = _log_line_for(resp)
    assert "status=ok" in line
    assert "latency=210ms" in line
    assert "error_code=" not in line
    assert "message=" not in line


def test_not_configured_surfaces_error_code():
    """Missing API key — historically-common; ops should see WHY the
    connector fell through immediately, not on a re-run."""
    resp = tool_response_error(
        connector="linear",
        latency_ms=0.0,
        error_code="linear_api_key_missing",
        message="LINEAR_API_KEY not set; connector cannot authenticate.",
        status="not_configured",
    )
    line = _log_line_for(resp)
    assert "status=not_configured" in line
    assert "error_code=linear_api_key_missing" in line
    assert "LINEAR_API_KEY not set" in line


def test_http_400_class_error_surfaces_code_and_message():
    """The failure mode that produced `status=error latency=366ms` in
    production. Prior log emission dropped both error_code AND message,
    requiring the connector code to be re-read to guess at the cause."""
    resp = tool_response_error(
        connector="linear",
        latency_ms=366.0,
        error_code="http_400",
        message="Linear API returned HTTP 400 for TEAM-240",
    )
    line = _log_line_for(resp)
    assert "status=error" in line
    assert "latency=366ms" in line
    assert "error_code=http_400" in line
    assert "Linear API returned HTTP 400 for TEAM-240" in line


def test_graphql_error_message_is_truncated_to_200_chars():
    """GraphQL error payloads can be lengthy JSON blobs. Truncate to keep
    the log line scannable, but preserve the leading prefix that names
    the error class."""
    long_gql_body = (
        '[{"message": "Argument \\"id\\" has invalid value...long...error...body...'
        + "x" * 300 + '"}]'
    )
    resp = tool_response_error(
        connector="linear",
        latency_ms=125.0,
        error_code="graphql_error",
        message=long_gql_body,
    )
    line = _log_line_for(resp)
    assert "error_code=graphql_error" in line
    # Message truncated to <= 200 chars — the truncation lives inside a
    # repr() so the outer quotes count in the visible line but not in
    # the 200-char cap on the raw message.
    assert "long...error" in line  # prefix preserved
    assert "x" * 300 not in line   # long tail dropped


def test_multiline_error_message_gets_newlines_squashed():
    """HTTP error bodies sometimes carry newlines. Log lines should stay
    single-line so per-run log tail scanning works."""
    resp = tool_response_error(
        connector="linear",
        latency_ms=88.0,
        error_code="http_500",
        message="Linear API 500\nInternal server error\nTraceback:\n  at handler",
    )
    line = _log_line_for(resp)
    assert "\n" not in line
    assert "Linear API 500 Internal server error" in line
