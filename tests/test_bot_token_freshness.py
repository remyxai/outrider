"""A long run must not lose its push token.

Found on a real 76-minute dispatch: the agent selected a paper, implemented a
2,289-byte diff, ran pytest and passed self-review — then `git push` exited
128 because the token had expired. Installation tokens live 60 minutes, and a
workflow's mint step runs once, before the action starts.

This predates the agent port, but the port is what makes it likely: slow
backends (GLM, Kimi thinking mode) are now first-class and routinely run past
the hour, where previously runs were on fast models and stayed inside the
window.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run


def test_a_fresh_run_uses_the_explicit_token_untouched(monkeypatch):
    """Fast runs must behave exactly as before — no extra mint call."""
    monkeypatch.setenv("INPUT_GITHUB_TOKEN", "ghs_workflow_minted")
    monkeypatch.setattr(run.time, "monotonic", lambda: run._PROCESS_STARTED_AT + 60)
    called = []
    monkeypatch.setattr(run, "_mint_bot_token", lambda: called.append(1) or "x")

    assert run._github_token() == "ghs_workflow_minted"
    assert not called, "a fresh token must not trigger a re-mint"


def test_past_the_ttl_window_a_fresh_token_is_minted(monkeypatch):
    monkeypatch.setenv("INPUT_GITHUB_TOKEN", "ghs_stale")
    monkeypatch.setattr(
        run.time, "monotonic",
        lambda: run._PROCESS_STARTED_AT + run._BOT_TOKEN_MAX_AGE_S + 1,
    )
    monkeypatch.setattr(run, "_mint_bot_token", lambda: "ghs_fresh")

    assert run._github_token() == "ghs_fresh"


def test_the_stale_token_is_still_used_when_minting_is_unavailable(monkeypatch):
    """Degrade, don't crash: an unmintable run should still try the push
    rather than fail before attempting it."""
    monkeypatch.setenv("INPUT_GITHUB_TOKEN", "ghs_stale")
    monkeypatch.setattr(
        run.time, "monotonic",
        lambda: run._PROCESS_STARTED_AT + run._BOT_TOKEN_MAX_AGE_S + 1,
    )
    monkeypatch.setattr(run, "_mint_bot_token", lambda: "")

    assert run._github_token() == "ghs_stale"


def test_the_stale_path_warns_with_an_actionable_message(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("INPUT_GITHUB_TOKEN", "ghs_stale")
    monkeypatch.setattr(
        run.time, "monotonic",
        lambda: run._PROCESS_STARTED_AT + run._BOT_TOKEN_MAX_AGE_S + 1,
    )
    monkeypatch.setattr(run, "_mint_bot_token", lambda: "")
    with caplog.at_level(logging.WARNING):
        run._github_token()
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert "exit 128" in blob, "name the failure the operator will see"
    assert "REMYX_API_KEY" in blob, "name the fix"


def test_no_explicit_token_falls_through_as_before(monkeypatch):
    monkeypatch.delenv("INPUT_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(run, "_mint_bot_token", lambda: "ghs_selfminted")
    assert run._github_token() == "ghs_selfminted"


def test_the_ttl_window_is_under_the_real_expiry():
    """Installation tokens expire at 60 minutes; the re-mint threshold has to
    leave room for the push itself."""
    assert run._BOT_TOKEN_MAX_AGE_S < 60 * 60
