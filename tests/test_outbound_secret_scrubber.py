"""Tests for the outbound secret-scrubber at the GitHub API boundary.

The scrubber refuses to send any GitHub API body whose string fields
match known credential shapes (Anthropic, GitHub, Remyx, JWTs, Bearer
headers, env-var-style leaks). It's the one-place catch-all that
covers leaks from every upstream body-assembly path — agent
self-review, test stdout, pre-flight reasoning, file-fallback content
— since each of those can in principle propagate untrusted content
into a public-repo PR/Issue/Discussion body.

The fix exists because PR bodies are assembled from many sources, and
any individual upstream getting a regression could leak a credential
into a public repo. Defense in depth at the boundary catches it
regardless of which upstream path leaks.

The exception message includes only the JSON path and a pattern
identifier — never the actual matched secret, so log lines built from
the exception don't propagate the credential further.

Run with: pytest tests/test_outbound_secret_scrubber.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from run import (  # noqa: E402
    OutboundSecretError,
    _scan_for_secrets,
    _scrub_outbound_payload,
)


# ─── Synthetic, non-credential strings shaped like real tokens ─────
# Each value below matches the production token format precisely
# enough for the regex to fire, but the payload bytes are uniform
# letters so the strings themselves are obviously not real secrets
# and never grant access anywhere.
_SYNTH_SK_ANT = "sk-ant-api03-" + ("A" * 95)
_SYNTH_GHP = "ghp_" + ("A" * 36)
_SYNTH_GHS = "ghs_" + ("A" * 36)
_SYNTH_GITHUB_PAT = "github_pat_" + ("A" * 30) + "_" + ("B" * 30)
_SYNTH_RMXU = "rmxu_" + ("A" * 32)
_SYNTH_JWT = "eyJ" + ("A" * 20) + "." + ("B" * 20) + "." + ("C" * 20)
_SYNTH_BEARER = "Authorization: Bearer " + ("A" * 64)
_SYNTH_GENERIC_BEARER = "Bearer " + ("A" * 64)
_SYNTH_ENV_LEAK = "ANTHROPIC_API_KEY=" + ("X" * 64)


# ─── _scan_for_secrets: per-pattern detection ─────────────────────


def test_scan_detects_anthropic_key():
    assert "anthropic_api_key" in _scan_for_secrets(f"key={_SYNTH_SK_ANT}")


def test_scan_detects_github_personal_token():
    assert "github_token" in _scan_for_secrets(_SYNTH_GHP)


def test_scan_detects_github_app_token():
    assert "github_token" in _scan_for_secrets(_SYNTH_GHS)


def test_scan_detects_github_fine_grained_pat():
    assert "github_pat" in _scan_for_secrets(_SYNTH_GITHUB_PAT)


def test_scan_detects_remyx_api_key():
    assert "remyx_api_key" in _scan_for_secrets(_SYNTH_RMXU)


def test_scan_detects_jwt():
    assert "jwt" in _scan_for_secrets(_SYNTH_JWT)


def test_scan_detects_authorization_header():
    assert "authorization_header" in _scan_for_secrets(_SYNTH_BEARER)


def test_scan_detects_generic_bearer_token():
    assert "bearer_token" in _scan_for_secrets(_SYNTH_GENERIC_BEARER)


def test_scan_detects_env_var_leak():
    assert "env_var_leak" in _scan_for_secrets(_SYNTH_ENV_LEAK)


# ─── _scan_for_secrets: clean text + false-positive guards ────────


def test_scan_clean_text_returns_empty():
    assert _scan_for_secrets(
        "This is a normal PR body about exploration structure in agents."
    ) == []


def test_scan_commit_sha_no_false_positive():
    # Short hex SHAs in commit messages shouldn't trip the patterns.
    assert _scan_for_secrets("commit abc1234def567890") == []


def test_scan_arxiv_id_no_false_positive():
    assert _scan_for_secrets("see arxiv 2606.11976v1") == []


def test_scan_empty_string_returns_empty():
    assert _scan_for_secrets("") == []


# ─── Return value never propagates the actual secret ──────────────


def test_scan_return_value_does_not_include_actual_secret():
    """If a caller logs the result of _scan_for_secrets, the log line
    must not contain the matched secret — only the pattern name."""
    hits = _scan_for_secrets(_SYNTH_SK_ANT)
    for h in hits:
        # The synthetic payload was all 'A's; no hit identifier should
        # contain that payload signature.
        assert "AAAA" not in h
        # The hit identifier should be a short pattern name, not the
        # matched substring.
        assert len(h) < 40


# ─── _scrub_outbound_payload: refuses payloads with secrets ───────


def test_scrub_clean_payload_no_raise():
    _scrub_outbound_payload({"title": "Fix typo", "body": "Clean."})


def test_scrub_raises_on_top_level_body_secret():
    body = {"title": "PR", "body": f"see {_SYNTH_GHS} for context"}
    with pytest.raises(OutboundSecretError) as excinfo:
        _scrub_outbound_payload(body)
    msg = str(excinfo.value)
    # Path identifies which field tripped the gate.
    assert "body" in msg
    # The actual secret value must NOT appear in the exception message.
    assert "AAA" not in msg
    # The pattern name SHOULD appear so the operator can find the leak.
    assert "github_token" in msg


def test_scrub_raises_on_nested_list_secret():
    body = {
        "title": "Recommended paper",
        "body": "Clean prose.",
        "labels": ["docs", _SYNTH_RMXU],
    }
    with pytest.raises(OutboundSecretError) as excinfo:
        _scrub_outbound_payload(body)
    # Nested-path notation pinpoints the offending element.
    assert "labels[1]" in str(excinfo.value)


def test_scrub_raises_on_deeply_nested_secret():
    body = {
        "input": {
            "discussion": {
                "comment": {
                    "body": f"context: {_SYNTH_BEARER}",
                },
            },
        },
    }
    with pytest.raises(OutboundSecretError) as excinfo:
        _scrub_outbound_payload(body)
    assert "input.discussion.comment.body" in str(excinfo.value)


def test_scrub_handles_none_payload():
    # GET requests pass body=None; the scrubber must be a no-op.
    _scrub_outbound_payload(None)


def test_scrub_handles_non_string_leaf_values():
    # bool / int / None values should pass through silently.
    _scrub_outbound_payload(
        {"draft": True, "number": 42, "labels": None, "body": "ok"}
    )


def test_outbound_secret_error_inherits_from_runtime_error():
    """Callers that broadly catch RuntimeError still see the error
    (back-compat for any catch-all handler), but the typed name lets
    higher-level handlers route it to a security-incident pathway
    specifically."""
    err = OutboundSecretError("test")
    assert isinstance(err, RuntimeError)
    assert isinstance(err, OutboundSecretError)


# ─── End-to-end: gh_api refuses to send a body containing a token ─


def test_gh_api_refuses_to_send_payload_with_anthropic_key(monkeypatch):
    """If a body with a leaked token reaches gh_api, the request must
    be aborted before any network call. urlopen would have to NOT be
    invoked — we monkeypatch it to a sentinel-raising stub so any
    accidental network call surfaces as a different exception."""
    import run

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError(
            "urlopen was reached despite OutboundSecretError being raised "
            "upstream — defense bypassed"
        )

    monkeypatch.setattr(run.urllib.request, "urlopen", _should_not_be_called)
    # Make _github_token return a valid-looking value so we know the
    # rejection is due to the scrubber, not the missing-token guard.
    monkeypatch.setattr(run, "_github_token", lambda: "test-token-value")

    body_with_leak = {
        "title": "Recommended paper",
        "body": f"agent output: {_SYNTH_SK_ANT}",
    }
    with pytest.raises(OutboundSecretError):
        run.gh_api("POST", "/repos/owner/repo/pulls", body_with_leak)
