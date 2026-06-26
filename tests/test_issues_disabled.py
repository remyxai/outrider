"""Tests for the IssuesDisabledError graceful-skip path.

GitHub disables the Issues tab on forks (and some repos) by default.
Outrider's ``open_issue`` tries to auto-enable it via PATCH /repos, but
the scoped App installation token deliberately lacks
``administration: write`` — the PATCH returns 403, and the run errored
out generically before this change.

The fix:

* ``open_issue`` catches the PATCH 403 and raises ``IssuesDisabledError``
  (the class was defined at ``src/run.py:947`` for this case but never
  raised or caught).
* ``process_target`` pre-flights ``has_issues`` before doing any LLM
  work, so the common case (forks with Issues disabled from creation)
  short-circuits with status ``skipped_issues_disabled`` before any
  selection or scaffold cost is incurred.
* ``process_target`` also catches ``IssuesDisabledError`` defensively in
  case Issues get disabled mid-run after the pre-flight passed.

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

import run  # noqa: E402
from run import IssuesDisabledError, Target  # noqa: E402


def _target() -> Target:
    return Target(
        repo="owner/repo",
        interest_id="iid",
        min_confidence="moderate",
        draft_mode="always",
        rate_limit_days=7,
    )


# ─── open_issue: happy path + 410-recovery + 403-graceful ──────────────────


def test_open_issue_succeeds_when_post_works(monkeypatch):
    """Normal case: POST /issues returns a usable issue dict; no retry."""
    calls = []

    def fake_gh_api(method, path, body=None):
        calls.append((method, path))
        if method == "POST" and "/issues" in path:
            return {"html_url": "https://github.com/owner/repo/issues/42", "number": 42}
        raise AssertionError(f"unexpected call: {method} {path}")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    url, number = run.open_issue(_target(), "title", "body")
    assert url == "https://github.com/owner/repo/issues/42"
    assert number == 42
    assert calls == [("POST", "/repos/owner/repo/issues")]


def test_open_issue_retries_after_410_when_patch_succeeds(monkeypatch):
    """410 on POST → PATCH to enable → retry POST. The pre-existing
    recovery path; assertion is that the PATCH-then-retry still works
    when the bot's token DOES have admin scope (e.g. customer ran with
    a PAT instead of the default App token)."""
    call_log = []

    def fake_gh_api(method, path, body=None):
        call_log.append((method, path))
        if method == "POST" and "/issues" in path and len(call_log) == 1:
            raise RuntimeError("HTTP 410: Issues has been disabled in this repository")
        if method == "PATCH" and path == "/repos/owner/repo":
            return {"has_issues": True}
        if method == "POST" and "/issues" in path and len(call_log) >= 3:
            return {"html_url": "https://github.com/owner/repo/issues/7", "number": 7}
        raise AssertionError(f"unexpected call sequence: {call_log}")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    url, number = run.open_issue(_target(), "title", "body")
    assert url == "https://github.com/owner/repo/issues/7"
    assert number == 7
    # Three calls: POST (410) → PATCH (success) → POST (success)
    assert len(call_log) == 3
    assert call_log[1] == ("PATCH", "/repos/owner/repo")


def test_open_issue_raises_issues_disabled_error_on_patch_403(monkeypatch):
    """The fork-default case: POST 410 → PATCH 403 → IssuesDisabledError.
    Replaces the previous generic-RuntimeError path that produced
    status=error and a wasted selection + scaffold run."""

    def fake_gh_api(method, path, body=None):
        if method == "POST" and "/issues" in path:
            raise RuntimeError("HTTP 410: Issues has been disabled in this repository")
        if method == "PATCH" and path == "/repos/owner/repo":
            raise RuntimeError(
                "HTTP 403: Resource not accessible by integration"
            )
        raise AssertionError(f"unexpected call: {method} {path}")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    with pytest.raises(IssuesDisabledError) as excinfo:
        run.open_issue(_target(), "title", "body")
    # Surface the actionable hint so the operator can self-resolve.
    msg = str(excinfo.value)
    assert "owner/repo" in msg
    assert "gh repo edit owner/repo --enable-issues" in msg


def test_open_issue_propagates_non_403_patch_errors(monkeypatch):
    """If the PATCH fails for a NON-403 reason (e.g. 500), the generic
    RuntimeError still propagates — we only graceful-skip the 403
    permission case, not arbitrary API failures."""

    def fake_gh_api(method, path, body=None):
        if method == "POST" and "/issues" in path:
            raise RuntimeError("HTTP 410: Issues has been disabled")
        if method == "PATCH":
            raise RuntimeError("HTTP 500: Internal Server Error")
        raise AssertionError(f"unexpected call: {method} {path}")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    # NOT IssuesDisabledError — should propagate as plain RuntimeError.
    with pytest.raises(RuntimeError) as excinfo:
        run.open_issue(_target(), "title", "body")
    assert not isinstance(excinfo.value, IssuesDisabledError)
    assert "HTTP 500" in str(excinfo.value)


def test_open_issue_propagates_non_410_post_errors(monkeypatch):
    """If the initial POST fails for a NON-410 reason (e.g. 422
    validation), the generic RuntimeError propagates without attempting
    the PATCH-and-retry recovery path."""

    def fake_gh_api(method, path, body=None):
        if method == "POST":
            raise RuntimeError("HTTP 422: Validation failed")
        raise AssertionError("PATCH shouldn't fire on non-410 errors")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    with pytest.raises(RuntimeError) as excinfo:
        run.open_issue(_target(), "title", "body")
    assert "HTTP 422" in str(excinfo.value)


# ─── IssuesDisabledError type contract ─────────────────────────────────────


def test_issues_disabled_error_inherits_from_runtime_error():
    """Callers that broadly catch RuntimeError still see the exception
    (back-compat for any catch-all error handler), but the new typed
    name lets process_target route it to skipped_issues_disabled
    specifically."""
    err = IssuesDisabledError("test")
    assert isinstance(err, RuntimeError)
    assert isinstance(err, IssuesDisabledError)


def test_issues_disabled_error_carries_chained_patch_error(monkeypatch):
    """`raise ... from patch_err` preserves the underlying PATCH error
    for debugging; the __cause__ chain shouldn't be lost."""

    def fake_gh_api(method, path, body=None):
        if method == "POST":
            raise RuntimeError("HTTP 410: Issues has been disabled")
        if method == "PATCH":
            raise RuntimeError("HTTP 403: Resource not accessible by integration")
        raise AssertionError("unreachable")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    try:
        run.open_issue(_target(), "title", "body")
    except IssuesDisabledError as e:
        assert e.__cause__ is not None
        assert "HTTP 403" in str(e.__cause__)
        return
    raise AssertionError("expected IssuesDisabledError")
