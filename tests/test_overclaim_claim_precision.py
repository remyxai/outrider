"""Precision refinements for overclaiming coverage detection.

Pins two refinements over the baseline overclaiming port
(arXiv:2609.20812v1):

  1. Negation-aware completeness-claim detection: a disclaimed phrase
     ("did not review all files") must not be counted as an assertion of
     complete coverage, so it no longer produces a false coverage overclaim.
  2. Grounding the coverage signal on the selection coverage gate's own
     ``under_explored`` verdict, wired through ``run._overclaim_check_from_events``,
     so a completeness claim is flagged when it contradicts the repo's
     measured coverage rather than a fixed read-count heuristic.

Run with: pytest tests/test_overclaim_claim_precision.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run
from agents.base import Event
from claim_language import asserts_complete_coverage, find_completeness_claims
from overclaim_detection import detect_overclaiming

# ─── Negation-aware claim language ─────────────────────────────────────────


def test_plain_completeness_claim_is_detected():
    assert asserts_complete_coverage("I reviewed all files in the module.")
    assert find_completeness_claims("I performed a comprehensive review.")


def test_negated_completeness_claim_is_not_a_claim():
    """A disclaimer is not a completeness assertion."""
    assert not asserts_complete_coverage("I did not review all files.")
    assert not asserts_complete_coverage("I could not read every module.")
    assert not asserts_complete_coverage("I only reviewed all of the top-level files")


def test_negation_scoped_to_preceding_clause():
    """A negation in an earlier sentence does not suppress a later claim."""
    assert asserts_complete_coverage("No luck at first. I reviewed all files.")


# ─── Detector no longer over-fires on disclaimers ──────────────────────────


def test_disclaimer_produces_no_coverage_overclaim():
    """Baseline flagged this (substring 'review all'); refinement must not."""
    signals = detect_overclaiming(
        reasoning_text="I did not review all files due to time limits.",
        file_reads=1,
        searches=5,
        visible_lines=50,
        paths_read={"foo.py"},
        tools_executed=[],
    )
    assert not any(s.claim_type == "coverage" for s in signals)


# ─── Grounding on the coverage gate verdict (wired through run.py) ─────────


def test_under_explored_verdict_flags_completeness_claim():
    """Gate says under-explored + a completeness claim => coverage overclaim,
    even though the raw read count would not have tripped the heuristic."""
    events = [
        Event(kind="tool_use", tool="read", id="ev1", paths=("a.py",)),
        Event(kind="tool_use", tool="read", id="ev2", paths=("b.py",)),
        Event(kind="tool_use", tool="read", id="ev3", paths=("c.py",)),
        Event(kind="tool_use", tool="read", id="ev4", paths=("d.py",)),
    ]
    reasoning = "I reviewed all files and confirmed the call site."
    coverage = {
        "searches": 2,
        "file_reads": 4,  # above the raw < 3 heuristic
        "visible_lines": 40,
        "under_explored": True,
    }

    result = run._overclaim_check_from_events(events, reasoning, coverage)
    assert any(s.claim_type == "coverage" for s in result["signals"])
    assert result["risk"]["high_severity"] >= 1


def test_gate_says_covered_suppresses_low_read_heuristic():
    """When the gate measured adequate coverage, a completeness claim with a
    low read count is not a contradiction and must not be flagged."""
    signals = detect_overclaiming(
        reasoning_text="I reviewed all files in the target module.",
        file_reads=1,
        searches=0,
        visible_lines=800,
        paths_read={"a.py"},
        tools_executed=[],
        under_explored=False,
    )
    assert not any(s.claim_type == "coverage" for s in signals)
