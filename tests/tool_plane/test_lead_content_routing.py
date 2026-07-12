"""Tests for the ``lead-content`` URL routing extension.

Verifies that:

- Verbatim text passes through unchanged (no connector consulted)
- Non-Linear URLs pass through unchanged (no connector consulted)
- Linear URLs get pre-resolved by the connector when it succeeds
- Linear URLs fall through to the URL string when the connector fails,
  but the connector's ToolResponse is still returned for audit logging
- Missing ``LINEAR_API_KEY`` returns ``status: not_configured`` and the
  URL falls through (backward compat with today's WebFetch flow)
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from tool_plane import lead_content_routing


class _FakeResponse:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_verbatim_text_passes_through_untouched():
    text = "This is a hand-crafted phase spec paragraph. No URL."
    resolved, response = lead_content_routing.resolve_lead_content(text)
    assert resolved == text
    assert response is None


def test_non_linear_url_passes_through_untouched():
    url = "https://gist.github.com/user/abc123"
    resolved, response = lead_content_routing.resolve_lead_content(url)
    assert resolved == url
    assert response is None


def test_empty_input_returns_empty_no_connector():
    resolved, response = lead_content_routing.resolve_lead_content("")
    assert resolved == ""
    assert response is None


def test_linear_url_resolved_to_issue_body_on_success(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")
    fake_body = {
        "data": {
            "issue": {
                "identifier": "TEAM-2",
                "title": "Another issue",
                "description": "A second description body.",
                "url": "https://linear.app/example/issue/TEAM-2",
                "state": {"name": "Backlog", "type": "backlog"},
                "priority": 0,
                "priorityLabel": None,
                "project": None,
                "labels": {"nodes": []},
                "updatedAt": "2026-07-12T00:00:00.000Z",
                "createdAt": "2026-07-12T00:00:00.000Z",
            }
        }
    }
    with patch(
        "tool_plane.connectors.linear.urllib.request.urlopen",
        return_value=_FakeResponse(fake_body),
    ):
        resolved, response = lead_content_routing.resolve_lead_content(
            "https://linear.app/example/issue/TEAM-2"
        )
    assert response is not None
    assert response.status == "ok"
    # Content was substituted, not the URL
    assert "TEAM-2" in resolved
    assert "A second description body." in resolved
    assert resolved != "https://linear.app/example/issue/TEAM-2"


def test_linear_url_falls_through_on_missing_api_key(monkeypatch):
    """Backward compat: without LINEAR_API_KEY, the URL passes through unchanged
    (the coding session's WebFetch will hit Linear's login page and the operator
    will see the auth gap in the run log). Response is still returned for audit."""
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    url = "https://linear.app/example/issue/TEAM-2"
    resolved, response = lead_content_routing.resolve_lead_content(url)
    assert resolved == url
    assert response is not None
    assert response.status == "not_configured"
    assert response.error_code == "linear_api_key_missing"


def test_linear_url_falls_through_on_fetch_failure(monkeypatch):
    """If the connector fails (rate limit, 500, etc.), the URL falls through
    unchanged so downstream WebFetch retains its chance to resolve it. The
    ToolResponse still surfaces for audit."""
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")
    with patch(
        "tool_plane.connectors.linear.urllib.request.urlopen",
        return_value=_FakeResponse({"data": {"issue": None}}),
    ):
        resolved, response = lead_content_routing.resolve_lead_content(
            "https://linear.app/example/issue/TEAM-99999"
        )
    assert resolved == "https://linear.app/example/issue/TEAM-99999"
    assert response is not None
    assert response.status == "not_found"
