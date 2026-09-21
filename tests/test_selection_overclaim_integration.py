"""Tests for overclaiming detection in the selection pass.

Verifies that overclaiming detection is wired into the agentic selection
pass and that detected signals are properly stored for downstream use.

Run with: pytest tests/test_selection_overclaim_integration.py -q
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run
from agents.base import Event
from overclaim_detection import OverclaimSignal


# ─── _overclaim_check_from_events ──────────────────────────────────────────


def test_overclaim_check_extracts_paths_from_events():
    """_overclaim_check_from_events properly extracts tool use data."""
    # Create mock events simulating file reads and shell commands.
    events = [
        Event(kind="tool_use", tool="read", id="ev1", paths=("src/run.py",)),
        Event(kind="tool_use", tool="read", id="ev2", paths=("src/selection.py",)),
        Event(kind="tool_use", tool="execute", command="grep search_recommendation src/run.py"),
    ]
    reasoning = "I reviewed the key files to locate the call site."
    coverage = {
        "searches": 1,
        "file_reads": 2,
        "visible_lines": 200,
    }

    result = run._overclaim_check_from_events(events, reasoning, coverage)

    assert "signals" in result
    assert "risk" in result
    assert isinstance(result["signals"], list)
    assert isinstance(result["risk"], dict)


def test_overclaim_check_detects_coverage_mismatch():
    """Detects when agent claims comprehensive review but reads few files."""
    events = [
        Event(kind="tool_use", tool="read", id="ev1", paths=("a.py",)),
    ]
    reasoning = "I comprehensively reviewed all files in the repository."
    coverage = {
        "searches": 0,
        "file_reads": 1,
        "visible_lines": 50,
    }

    result = run._overclaim_check_from_events(events, reasoning, coverage)
    signals = result["signals"]

    # Should detect the coverage mismatch.
    assert len(signals) > 0
    assert any(s.claim_type == "coverage" for s in signals)


def test_overclaim_check_returns_risk_assessment():
    """Returns structured risk assessment for routing decisions."""
    events = [
        Event(kind="tool_use", tool="read", id="ev1", paths=("src/run.py",)),
    ]
    reasoning = "I reviewed all files completely."
    coverage = {
        "searches": 0,
        "file_reads": 1,
        "visible_lines": 30,
    }

    result = run._overclaim_check_from_events(events, reasoning, coverage)
    risk = result["risk"]

    assert "is_risky" in risk
    assert "high_severity" in risk
    assert "medium_severity" in risk
    assert "low_severity" in risk
    assert "summary" in risk
    assert isinstance(risk["is_risky"], bool)


def test_overclaim_check_with_empty_events():
    """Handles empty event list gracefully."""
    events = []
    reasoning = "I verified the call site."
    coverage = {
        "searches": 0,
        "file_reads": 0,
        "visible_lines": 0,
    }

    result = run._overclaim_check_from_events(events, reasoning, coverage)

    assert "signals" in result
    assert "risk" in result
    # No file reads means no coverage claim should be flagged.
    assert result["risk"]["is_risky"] is False


def test_overclaim_signals_structure_matches_expected_schema():
    """Overclaim signals in result have the expected structure."""
    events = [
        Event(kind="tool_use", tool="read", id="ev1", paths=("a.py",)),
    ]
    reasoning = "I reviewed all files comprehensively."
    coverage = {
        "searches": 0,
        "file_reads": 1,
        "visible_lines": 20,
    }

    result = run._overclaim_check_from_events(events, reasoning, coverage)
    signals = result["signals"]

    if signals:
        signal = signals[0]
        assert hasattr(signal, "claim_type")
        assert hasattr(signal, "summary")
        assert hasattr(signal, "severity")
        assert signal.severity in ("high", "medium", "low")
