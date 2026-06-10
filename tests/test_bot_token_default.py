"""Tests for default remyx[bot] identity via the self-minted token:

  - `_github_token` preference: explicit `github-token` input > the
    self-minted remyx[bot] installation token > GITHUB_TOKEN
  - `_mint_bot_token` mints with REMYX_API_KEY + TARGET_REPO, caches a
    single attempt per run (success or failure), normalizes URL-shaped
    TARGET_REPO, and degrades to "" without the API key
  - `_post_discussion_comment` retries with GITHUB_TOKEN when the active
    (bot) token can't post Discussions, and re-raises everything else

Run with: pytest tests/ -q
"""
import json
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


def _clean_env(monkeypatch, **env):
    for var in ("INPUT_GITHUB_TOKEN", "GITHUB_TOKEN", "REMYX_API_KEY",
                "REMYXAI_API_KEY", "TARGET_REPO"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    run._BOT_TOKEN.update(attempted=False, token="", permissions={})


class _FakeMintResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ─── _github_token preference order ───────────────────────────────────────


def test_explicit_input_token_wins(monkeypatch):
    _clean_env(
        monkeypatch,
        INPUT_GITHUB_TOKEN="pat-explicit",
        GITHUB_TOKEN="builtin",
        REMYX_API_KEY="rmxa_x",
        TARGET_REPO="owner/name",
    )
    monkeypatch.setattr(run, "_mint_bot_token", lambda: pytest.fail(
        "explicit input token must short-circuit the self-mint"
    ))
    assert run._github_token() == "pat-explicit"


def test_self_minted_token_preferred_over_builtin(monkeypatch):
    _clean_env(
        monkeypatch,
        GITHUB_TOKEN="builtin",
        REMYX_API_KEY="rmxa_x",
        TARGET_REPO="owner/name",
    )
    seen = {}

    def fake_urlopen(req, timeout=15):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        seen["auth"] = req.headers.get("Authorization")
        return _FakeMintResponse({
            "token": "ghs_bot", "expires_at": "x",
            "permissions": {"contents": "write"},
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert run._github_token() == "ghs_bot"
    assert seen["url"].endswith("/api/v1.0/github/installation-token")
    assert seen["body"] == {"repo": "owner/name"}
    assert seen["auth"] == "Bearer rmxa_x"
    assert run._BOT_TOKEN["permissions"] == {"contents": "write"}


def test_falls_back_to_builtin_when_mint_fails(monkeypatch):
    _clean_env(
        monkeypatch,
        GITHUB_TOKEN="builtin",
        REMYX_API_KEY="rmxa_x",
        TARGET_REPO="owner/name",
    )
    calls = {"n": 0}

    def boom(req, timeout=15):
        calls["n"] += 1
        raise urllib.error.URLError("engine down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert run._github_token() == "builtin"
    # One attempt per run — the failure is cached, not retried per call.
    assert run._github_token() == "builtin"
    assert calls["n"] == 1


def test_mint_skipped_without_api_key(monkeypatch):
    _clean_env(monkeypatch, GITHUB_TOKEN="builtin", TARGET_REPO="owner/name")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail(
        "must not call the engine without REMYX_API_KEY"
    ))
    assert run._github_token() == "builtin"


def test_mint_normalizes_url_shaped_target_repo(monkeypatch):
    _clean_env(
        monkeypatch,
        GITHUB_TOKEN="builtin",
        REMYX_API_KEY="rmxa_x",
        TARGET_REPO="https://github.com/owner/name/",
    )
    seen = {}

    def fake_urlopen(req, timeout=15):
        seen["body"] = json.loads(req.data.decode())
        return _FakeMintResponse({"token": "ghs_bot", "permissions": {}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert run._github_token() == "ghs_bot"
    assert seen["body"] == {"repo": "owner/name"}


# ─── Discussion-post permission fallback ──────────────────────────────────


def test_discussion_post_falls_back_when_bot_lacks_discussions(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "builtin")
    monkeypatch.setattr(run, "_github_token", lambda: "ghs_bot")
    calls = []

    def fake_graphql(query, variables=None, token=None):
        calls.append(token)
        if token is None:
            raise RuntimeError(
                "GitHub GraphQL errors: [{'type': 'FORBIDDEN', 'message': "
                "'Resource not accessible by integration'}]"
            )
        return {"addDiscussionComment": {"comment": {"url": "https://x/#c1"}}}

    monkeypatch.setattr(run, "gh_graphql", fake_graphql)
    url = run._post_discussion_comment("D_node", "body")
    assert url == "https://x/#c1"
    assert calls == [None, "builtin"]


def test_discussion_post_reraises_non_permission_errors(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "builtin")
    monkeypatch.setattr(run, "_github_token", lambda: "ghs_bot")

    def fake_graphql(query, variables=None, token=None):
        raise RuntimeError("GitHub GraphQL → HTTP 502: bad gateway")

    monkeypatch.setattr(run, "gh_graphql", fake_graphql)
    with pytest.raises(RuntimeError, match="502"):
        run._post_discussion_comment("D_node", "body")


def test_discussion_post_no_fallback_when_already_on_builtin(monkeypatch):
    """When the active token IS GITHUB_TOKEN (no bot token minted), a
    permission error is terminal — retrying with the same token would
    just fail again."""
    monkeypatch.setenv("GITHUB_TOKEN", "builtin")
    monkeypatch.setattr(run, "_github_token", lambda: "builtin")

    def fake_graphql(query, variables=None, token=None):
        raise RuntimeError(
            "GitHub GraphQL errors: 'Resource not accessible by integration'"
        )

    monkeypatch.setattr(run, "gh_graphql", fake_graphql)
    with pytest.raises(RuntimeError, match="Resource not accessible"):
        run._post_discussion_comment("D_node", "body")
