"""Tests for `_record_claude_usage` envelope parsing.

Covers the normal Anthropic-shape envelope, the case where the CLI
returns a successful response without a `usage` block (the counter
increments silently — observable in telemetry, never surfaced in
customer-visible logs or step summary), and the error-envelope case
where missing usage is expected and must NOT inflate the counter.

Run with: pytest tests/test_record_claude_usage.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


def _envelope_anthropic(in_tok: int = 100, out_tok: int = 50) -> dict:
    """The shape `_run_claude_json` parses on a successful Anthropic call."""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "result": "OK",
        "total_cost_usd": 0.001,
        "usage": {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "server_tool_use": {"web_search_requests": 0},
            "service_tier": "standard",
        },
    }


def _envelope_no_usage() -> dict:
    """A clean success envelope that omits `usage` entirely. Observed
    against non-Anthropic backends where the CLI's terminal envelope
    occasionally drops the `usage` block on otherwise-successful calls.
    """
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "result": '{"decision":"ISSUE","reasoning":"…"}',
        "total_cost_usd": 0.0,
    }


def _envelope_error() -> dict:
    """A failed-call envelope. Missing `usage` here is expected — error
    envelopes legitimately carry no usage and must NOT trigger the
    diagnostic warning."""
    return {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "num_turns": 0,
        "result": "API Error: Request rejected (429) ...",
    }


def setup_function(_func):
    """Reset run-cost state before each test."""
    run._reset_run_cost()


# ─── normal Anthropic envelope ─────────────────────────────────────────


def test_anthropic_envelope_accumulates_tokens():
    run._record_claude_usage(_envelope_anthropic(in_tok=100, out_tok=50))
    run._record_claude_usage(_envelope_anthropic(in_tok=200, out_tok=80))

    assert run._RUN_COST["claude_calls"] == 2
    assert run._RUN_COST["input_tokens"] == 300
    assert run._RUN_COST["output_tokens"] == 130
    assert run._RUN_COST["envelopes_without_usage"] == 0


# ─── envelope without `usage` — silent counter increment ──────────────


def test_envelope_without_usage_increments_counter():
    run._record_claude_usage(_envelope_no_usage())

    assert run._RUN_COST["claude_calls"] == 1
    assert run._RUN_COST["input_tokens"] == 0
    assert run._RUN_COST["output_tokens"] == 0
    assert run._RUN_COST["envelopes_without_usage"] == 1


def test_envelope_without_usage_does_not_emit_log_records(caplog):
    """The counter is a silent telemetry signal; it must NOT produce a
    customer-visible log line. The bug is Outrider-internal and shouldn't
    appear in the customer's GitHub Actions log."""
    with caplog.at_level("DEBUG", logger=run.log.name):
        run._record_claude_usage(_envelope_no_usage())

    # No record should reference the missing-usage condition.
    assert not any(
        ("zero tokens" in r.getMessage() or "usage" in r.getMessage())
        for r in caplog.records
    )


def test_mixed_run_partial_under_count():
    """Two-call run where one envelope has usage and the other doesn't.
    The total isn't 0 but the counter still records the under-count."""
    run._record_claude_usage(_envelope_anthropic(in_tok=10_000, out_tok=10_000))
    run._record_claude_usage(_envelope_no_usage())

    assert run._RUN_COST["claude_calls"] == 2
    assert run._RUN_COST["input_tokens"] == 10_000
    assert run._RUN_COST["output_tokens"] == 10_000
    assert run._RUN_COST["envelopes_without_usage"] == 1


# ─── error envelope (missing usage is expected) ─────────────────────────


def test_error_envelope_does_not_count():
    """An error envelope legitimately carries no usage; we must NOT
    inflate the counter on it."""
    run._record_claude_usage(_envelope_error())

    assert run._RUN_COST["claude_calls"] == 1
    assert run._RUN_COST["input_tokens"] == 0
    assert run._RUN_COST["envelopes_without_usage"] == 0


# ─── _reset_run_cost zeroes the new counter ─────────────────────────────


def test_reset_zeroes_the_new_counter():
    run._RUN_COST["envelopes_without_usage"] = 42
    run._reset_run_cost()
    assert run._RUN_COST["envelopes_without_usage"] == 0
