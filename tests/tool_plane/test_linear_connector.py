"""Tests for the Linear connector.

Unit-tested with mocked HTTP so the suite runs deterministically without
a live Linear API key. A separate integration test (gated on
LINEAR_API_KEY being set) exercises the real API path.
"""
import json
import sys
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from tool_plane.connectors import linear


# ---------- URL pattern recognition ----------


def test_is_linear_url_positive():
    assert linear.is_linear_url("https://linear.app/example/issue/TEAM-1")
    assert linear.is_linear_url("http://linear.app/team-a/issue/AB-1")
    assert linear.is_linear_url("https://linear.app/w/issue/W-999/some-slug")


def test_is_linear_url_negative():
    assert not linear.is_linear_url("https://gist.github.com/user/abc")
    assert not linear.is_linear_url("https://linear.app/example/team/MEMBERS")
    assert not linear.is_linear_url("linear.app/example/issue/TEAM-1")  # no scheme
    assert not linear.is_linear_url("")


# ---------- fetch_issue: auth handling ----------


def test_fetch_issue_missing_api_key_returns_not_configured(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    r = linear.fetch_issue("https://linear.app/example/issue/TEAM-1")
    assert r.status == "not_configured"
    assert r.error_code == "linear_api_key_missing"
    assert r.latency_ms == 0.0  # no network happened


def test_fetch_issue_malformed_url(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")
    r = linear.fetch_issue("https://not-linear.com/issue/foo")
    assert r.status == "error"
    assert r.error_code == "malformed_url"


# ---------- fetch_issue: happy path ----------


class _FakeResponse:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_fetch_issue_happy_path(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")
    fake_body = {
        "data": {
            "issue": {
                "identifier": "TEAM-1",
                "title": "Example issue title",
                "description": "Some description body.",
                "url": "https://linear.app/example/issue/TEAM-1",
                "state": {"name": "Backlog", "type": "backlog"},
                "priority": 3,
                "priorityLabel": "Medium",
                "project": {"name": "Example Project"},
                "labels": {"nodes": [{"name": "backend"}, {"name": "phase-1"}]},
                "updatedAt": "2026-07-12T00:00:00.000Z",
                "createdAt": "2026-07-10T00:00:00.000Z",
            }
        }
    }
    with patch("tool_plane.connectors.linear.urllib.request.urlopen", return_value=_FakeResponse(fake_body)):
        r = linear.fetch_issue("https://linear.app/example/issue/TEAM-1")

    assert r.status == "ok"
    assert r.connector == "linear"
    assert "TEAM-1" in r.inline_snippet
    assert "Example issue title" in r.inline_snippet
    assert "Some description body." in r.inline_snippet
    assert "Backlog" in r.inline_snippet
    assert "Medium" in r.inline_snippet
    assert "backend" in r.inline_snippet  # label present
    assert r.data["identifier"] == "TEAM-1"
    assert r.data["labels"] == ["backend", "phase-1"]
    assert r.data["description_length"] == len("Some description body.")
    assert r.latency_ms > 0.0


# ---------- fetch_issue: failure modes ----------


def test_fetch_issue_not_found_returns_not_found(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")
    with patch(
        "tool_plane.connectors.linear.urllib.request.urlopen",
        return_value=_FakeResponse({"data": {"issue": None}}),
    ):
        r = linear.fetch_issue("https://linear.app/example/issue/TEAM-99999")
    assert r.status == "not_found"
    assert r.error_code == "issue_not_found"


def test_fetch_issue_rate_limited_returns_rate_limited(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")
    err = urllib.error.HTTPError(
        url=linear.LINEAR_GRAPHQL_URL,
        code=429,
        msg="Too Many Requests",
        hdrs=None,
        fp=BytesIO(b""),
    )
    with patch("tool_plane.connectors.linear.urllib.request.urlopen", side_effect=err):
        r = linear.fetch_issue("https://linear.app/example/issue/TEAM-1")
    assert r.status == "rate_limited"
    assert r.error_code == "rate_limited"


def test_fetch_issue_http_500_returns_error(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")
    err = urllib.error.HTTPError(
        url=linear.LINEAR_GRAPHQL_URL,
        code=500,
        msg="Boom",
        hdrs=None,
        fp=BytesIO(b""),
    )
    with patch("tool_plane.connectors.linear.urllib.request.urlopen", side_effect=err):
        r = linear.fetch_issue("https://linear.app/example/issue/TEAM-1")
    assert r.status == "error"
    assert r.error_code == "http_500"


def test_fetch_issue_graphql_errors_returns_error(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")
    fake_body = {"errors": [{"message": "Authentication failed"}]}
    with patch(
        "tool_plane.connectors.linear.urllib.request.urlopen",
        return_value=_FakeResponse(fake_body),
    ):
        r = linear.fetch_issue("https://linear.app/example/issue/TEAM-1")
    assert r.status == "error"
    assert r.error_code == "graphql_error"
    assert "Authentication failed" in r.inline_snippet
