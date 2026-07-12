"""Tests for the tool-plane response envelope.

Covers shape stability + audit-fields presence. Mirrors what RepoRanger's
own envelope tests assert, so the two codebases can't silently drift.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from tool_plane.envelope import (
    TOOL_PLANE_VERSION,
    ToolResponse,
    tool_response_error,
    tool_response_ok,
)


def test_tool_response_ok_populates_audit_fields():
    r = tool_response_ok(
        connector="linear",
        latency_ms=42.0,
        inline_snippet="hi",
        data={"key": "value"},
    )
    assert r.status == "ok"
    assert r.connector == "linear"
    assert r.latency_ms == 42.0
    assert r.inline_snippet == "hi"
    assert r.data == {"key": "value"}
    assert r.connector_version == TOOL_PLANE_VERSION
    assert r.source_id.startswith("linear_")
    assert r.retrieved_at  # non-empty ISO timestamp


def test_tool_response_error_captures_error_code():
    r = tool_response_error(
        connector="linear",
        latency_ms=5.0,
        error_code="http_500",
        message="upstream boom",
    )
    assert r.status == "error"
    assert r.error_code == "http_500"
    assert r.inline_snippet == "upstream boom"
    assert r.data is None


def test_tool_response_error_custom_status():
    """The `status` argument override lets callers emit rate-limited / timeout
    / not-configured shapes without duplicating the constructor."""
    r = tool_response_error(
        connector="linear",
        latency_ms=0.0,
        error_code="linear_api_key_missing",
        status="not_configured",
    )
    assert r.status == "not_configured"
    assert r.error_code == "linear_api_key_missing"


def test_to_dict_drops_none_fields():
    """Audit block should render compactly — None-valued fields
    shouldn't clutter the step-summary table."""
    r = tool_response_ok(connector="linear", latency_ms=1.0, inline_snippet="hi")
    d = r.to_dict()
    assert "error_code" not in d  # None → dropped
    assert "data" not in d  # None → dropped
    assert "inline_snippet" in d  # present → kept
    assert d["inline_snippet"] == "hi"


def test_source_id_uniqueness():
    ids = {ToolResponse.new_source_id("foo") for _ in range(100)}
    assert len(ids) == 100
    assert all(i.startswith("foo_") for i in ids)
