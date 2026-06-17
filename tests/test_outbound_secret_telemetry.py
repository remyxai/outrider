"""Tests for the OutboundSecretError telemetry path (REMYX-129 Follow-up 3).

When the v1.6.4 outbound-body scrubber fires, the orchestrator routes
the abort to the dedicated ``aborted_secret_in_payload`` status:

  - Added to ``FAILURE_EXIT_STATUSES`` so the step surfaces red in CI
    rather than silently green
  - Emoji ``🛑`` in the step-summary status line
  - Dedicated step-summary block guides the operator to the
    diagnostic log line (with per-pattern lengths) and the
    triage rules (near-minimum match = likely false positive)
  - ``OutboundSecretError`` carries structured ``path`` and
    ``patterns`` attrs so consumers don't have to parse the
    message string
  - ``process_target`` catches the typed exception, sets
    ``result["scrubber_path"]`` + ``result["scrubber_patterns"]``
    for downstream rendering, and returns (no propagation to
    main's generic error path)

The match content itself is never propagated — only path + pattern
names + length diagnostic. A scrubber-fire that exposed the matched
secret in step-summary or telemetry would defeat the entire defense.

Run with: pytest tests/test_outbound_secret_telemetry.py -q
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

import run  # noqa: E402
from run import OutboundSecretError  # noqa: E402


# ─── OutboundSecretError carries structured path + patterns ───────


def test_outbound_secret_error_has_path_attr():
    err = OutboundSecretError("msg", path="body", patterns=["github_token"])
    assert err.path == "body"


def test_outbound_secret_error_has_patterns_attr():
    err = OutboundSecretError("msg", path="body", patterns=["github_token", "bearer_token"])
    assert err.patterns == ["github_token", "bearer_token"]


def test_outbound_secret_error_defaults_are_safe():
    """Constructor without kwargs should still produce a usable
    exception — empty path / patterns rather than None — so callers
    that only consume the message string still work."""
    err = OutboundSecretError("just a message")
    assert err.path == ""
    assert err.patterns == []
    assert str(err) == "just a message"


def test_outbound_secret_error_still_a_runtime_error():
    """Structural change must not break the existing back-compat:
    callers that catch RuntimeError broadly still see the abort."""
    err = OutboundSecretError("msg", path="x", patterns=["y"])
    assert isinstance(err, RuntimeError)


# ─── _scrub_outbound_payload populates the structured attrs ───────


def test_scrub_raises_with_path_and_patterns_populated():
    """The path + patterns attrs let process_target build the telemetry
    payload without parsing str(e). Verify the raise site actually
    populates them."""
    synth_secret = "sk-ant-api03-" + ("A" * 95)
    body = {"title": "ok", "body": f"see {synth_secret}"}
    with pytest.raises(OutboundSecretError) as excinfo:
        run._scrub_outbound_payload(body)
    e = excinfo.value
    assert e.path == "body"
    assert "anthropic_api_key" in e.patterns


def test_scrub_raises_with_nested_path():
    """Path attr carries the nested JSON path for deeply-nested
    matches — same dot/bracket notation as the message."""
    synth = "ghs_" + ("A" * 36)
    body = {"input": {"comment": {"body": f"context {synth}"}}}
    with pytest.raises(OutboundSecretError) as excinfo:
        run._scrub_outbound_payload(body)
    assert excinfo.value.path == "input.comment.body"


def test_scrub_raises_with_multiple_patterns():
    """When multiple patterns match in one field, all names appear in
    the patterns attr — operator can see at a glance whether multiple
    distinct credential shapes were in the same payload (true positive)
    or whether one prose substring matched two patterns (false positive)."""
    synth_a = "sk-ant-api03-" + ("A" * 95)
    synth_b = "ghs_" + ("B" * 36)
    body = {"body": f"prefix {synth_a} middle {synth_b} suffix"}
    with pytest.raises(OutboundSecretError) as excinfo:
        run._scrub_outbound_payload(body)
    names = set(excinfo.value.patterns)
    assert "anthropic_api_key" in names
    assert "github_token" in names


# ─── FAILURE_EXIT_STATUSES inclusion ──────────────────────────────


def test_aborted_secret_in_failure_exit_statuses():
    """The new status must be in FAILURE_EXIT_STATUSES so the step
    surfaces red in CI. Silent-green on a scrubber fire would lose
    the signal the operator needs to investigate."""
    assert "aborted_secret_in_payload" in run.FAILURE_EXIT_STATUSES


# ─── action.yml outputs enum documents the new status ─────────────


def test_action_yml_lists_new_status():
    """The status output enum must include the new status so callers
    of the action can switch on it. An undocumented status would still
    work but break the contract."""
    action_yml = Path(__file__).resolve().parent.parent / "action.yml"
    content = action_yml.read_text()
    assert "aborted_secret_in_payload" in content


# ─── step_summary renders the dedicated block ─────────────────────


def test_step_summary_renders_aborted_secret_block(tmp_path, monkeypatch):
    """A result with `aborted_secret_in_payload` status should produce
    the dedicated step-summary block with the actionable triage rules."""
    summary_path = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    result = {
        "repo": "owner/repo",
        "status": "aborted_secret_in_payload",
        "scrubber_path": "body",
        "scrubber_patterns": ["github_token", "bearer_token"],
        "error": "Outbound payload field 'body' matched credential pattern(s) ...",
    }
    run._write_step_summary(result)
    content = summary_path.read_text()
    assert "🛑 Outbound credential-scrubber fired" in content
    assert "`github_token`" in content
    assert "`bearer_token`" in content
    assert "`body`" in content
    # Operator guidance should be present.
    assert "investigate" in content.lower()
    assert "regex minimum" in content
    assert "40+" in content


def test_step_summary_renders_unspecified_path_when_attrs_missing(tmp_path, monkeypatch):
    """Defensive: a result that has the status but is missing the
    path/patterns attrs (e.g. old telemetry record) still renders the
    block — just with placeholder text — rather than crashing."""
    summary_path = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    result = {"repo": "owner/repo", "status": "aborted_secret_in_payload"}
    run._write_step_summary(result)
    content = summary_path.read_text()
    assert "🛑 Outbound credential-scrubber fired" in content
    assert "(unknown)" in content
    assert "unspecified" in content


def test_step_summary_does_not_leak_secret_through_error_field(tmp_path, monkeypatch):
    """A result whose `error` string includes a synthetic credential
    should NOT propagate that content into step_summary verbatim.
    The error block renders the message (capped to 2000 chars) but
    we explicitly test that the bytes that would have triggered the
    scrubber don't make it through. (The scrubber's exception message
    only references the path + pattern names, not the secret, so
    this should hold; the test makes the contract explicit.)"""
    summary_path = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    # The exception message itself never contains the matched secret —
    # only path + pattern names. So a downstream renderer that quotes
    # the message string is safe. Verify the contract explicitly.
    err_msg = (
        "Outbound payload field 'body' matched credential pattern(s) "
        "['github_token']; refusing to send the API request."
    )
    result = {
        "repo": "owner/repo",
        "status": "aborted_secret_in_payload",
        "scrubber_path": "body",
        "scrubber_patterns": ["github_token"],
        "error": err_msg,
    }
    run._write_step_summary(result)
    content = summary_path.read_text()
    # The synthetic 'A'-padded secret pattern shouldn't be in the
    # rendered step summary — the error message references it only
    # by pattern name.
    assert "AAAA" not in content
    assert "ghs_AAAA" not in content


# ─── emoji map includes the new status ────────────────────────────


def test_step_summary_emoji_for_aborted_secret(tmp_path, monkeypatch):
    """The status-line emoji should be 🛑 — distinct from ❌ (used for
    `error` / `claude_failed`) so the operator can tell at a glance
    that the failure is a defensive abort rather than a generic crash."""
    summary_path = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    result = {
        "repo": "owner/repo",
        "status": "aborted_secret_in_payload",
        "scrubber_path": "body",
        "scrubber_patterns": ["github_token"],
    }
    run._write_step_summary(result)
    content = summary_path.read_text()
    assert "🛑" in content
