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
    run._BOT_TOKEN.update(attempted=False, token="", permissions={}, minted_at=0.0)


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


def test_mint_re_mints_when_cached_token_is_stale(monkeypatch):
    """Installation tokens have a 60-min TTL. Long-running Kimi/GLM
    sessions can spend 30-40 min per phase — a cached token from clone
    time would be expired at push time. `_mint_bot_token` must re-mint
    when the cached token age exceeds `_BOT_TOKEN_MAX_AGE_S`.

    Regression test for the git push 401 seen on the atropos smoke
    (run 29552199454): first mint at 03:26:34, push attempt at 04:40:24
    — 74 min later, 14 min past the 60-min TTL, cached-token bug bit.
    """
    _clean_env(
        monkeypatch,
        GITHUB_TOKEN="builtin",
        REMYX_API_KEY="rmxa_x",
        TARGET_REPO="owner/name",
    )
    mint_calls = []
    counter = {"n": 0}

    def fake_urlopen(req, timeout=15):
        counter["n"] += 1
        mint_calls.append(counter["n"])
        return _FakeMintResponse({
            "token": f"ghs_bot_v{counter['n']}",
            "permissions": {"contents": "write"},
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    # First call at t=1000 — mints for the first time.
    now = [1000.0]
    monkeypatch.setattr(run.time, "time", lambda: now[0])
    assert run._github_token() == "ghs_bot_v1"
    assert mint_calls == [1]

    # Second call at t=1000 + 30min — cached token still fresh, no re-mint.
    now[0] = 1000.0 + 30 * 60
    assert run._github_token() == "ghs_bot_v1"
    assert mint_calls == [1]

    # Third call at t=1000 + 74min — past the 55-min freshness window;
    # must re-mint (this is exactly the push-time scenario from the smoke).
    now[0] = 1000.0 + 74 * 60
    assert run._github_token() == "ghs_bot_v2"
    assert mint_calls == [1, 2]

    # Fourth call at t=1000 + 74min + 5min — cached refresh still fresh.
    now[0] = 1000.0 + 79 * 60
    assert run._github_token() == "ghs_bot_v2"
    assert mint_calls == [1, 2]


def test_mint_does_not_retry_after_cached_failure_even_when_stale(monkeypatch):
    """A cached-empty result (App not installed, engine unreachable) must
    NOT re-attempt just because time passed — the failure was structural,
    not TTL-related. Retrying on every call would flood the engine."""
    _clean_env(
        monkeypatch,
        GITHUB_TOKEN="builtin",
        REMYX_API_KEY="rmxa_x",
        TARGET_REPO="owner/name",
    )
    counter = {"n": 0}

    def boom(req, timeout=15):
        counter["n"] += 1
        raise urllib.error.URLError("engine down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    now = [1000.0]
    monkeypatch.setattr(run.time, "time", lambda: now[0])

    # First call: engine unreachable, cached-empty.
    assert run._github_token() == "builtin"
    assert counter["n"] == 1

    # Fast-forward well past _BOT_TOKEN_MAX_AGE_S — still no retry.
    now[0] = 1000.0 + 6 * 3600
    assert run._github_token() == "builtin"
    assert counter["n"] == 1


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
