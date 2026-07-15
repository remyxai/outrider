"""Agent trajectory analysis for early failure detection.

Adapted from "Failure as a Process: An Anatomy of CLI Coding Agent Trajectories"
(arxiv 2607.09510v1), which demonstrates that agent failures are predominantly
driven by epistemic errors and typically emerge early — within the first few steps.

This module applies that insight to Outrider's Claude Code invocations by:
1. Detecting early epistemic failure patterns in the agent's log output
2. Classifying failures by onset timing (early, mid, late)
3. Surfacing these patterns in failure diagnostics for more actionable feedback

The paper's key finding — that failures "often remain hidden until recovery is
no longer possible" — informs our strategy to flag early signals so they can be
caught before reaching a terminal state.
"""

import re
from dataclasses import dataclass


@dataclass
class TrajectoryDiagnostic:
    """Represents a detected failure signal in an agent trajectory."""

    onset_timing: str  # "early", "mid", or "late"
    failure_class: str  # "epistemic", "resource", or "operational"
    signal: str  # Human-readable description of the signal
    confidence: float  # 0.0–1.0 confidence in the diagnosis


def analyze_agent_trajectory(log_tail: str, claude_calls: int = 0) -> TrajectoryDiagnostic | None:
    """Analyze an agent's log tail for early-stage failure signals.

    Detects patterns that indicate epistemic errors (wrong understanding of
    state, incorrect assumptions about the environment) in the first few
    execution steps, as described in the paper.

    Returns a TrajectoryDiagnostic if a signal is found, None otherwise.
    """
    if not log_tail:
        return None

    tail_lower = log_tail.lower()

    # Early epistemic errors: agent misunderstands the repository structure
    # or makes incorrect assumptions about available tools/APIs.
    early_epistemic_patterns = [
        (
            r"(file not found|no such file|directory not found)",
            "Early epistemic error: attempting to access non-existent paths",
            0.85,
        ),
        (
            r"(import error|modulenotfounderror|cannot import|no module named)",
            "Early epistemic error: misunderstanding of available modules",
            0.80,
        ),
        (
            r"(undefined variable|not defined|nameerror)",
            "Early epistemic error: referencing undefined variables or functions",
            0.75,
        ),
        (
            r"syntaxerror.*(?:invalid syntax|expected |unexpected token)",
            "Early epistemic error: generating syntactically invalid code",
            0.70,
        ),
    ]

    # Mid-stage epistemic errors: agent's initial approach is flawed but only
    # becomes evident after several steps.
    mid_epistemic_patterns = [
        (
            r"(type error|typeerror|attribute error|attributeerror)",
            "Mid-stage epistemic error: type mismatches or attribute assumptions",
            0.75,
        ),
        (
            r"(command not found|unknown command|unrecognized arguments?)",
            "Mid-stage epistemic error: misunderstanding of CLI tool interfaces",
            0.70,
        ),
    ]

    # Resource exhaustion typically happens later but is a sign the agent
    # wandered unproductively.
    resource_patterns = [
        (r"(timeout|timed out)", "Resource: execution timeout", 0.90),
        (r"(out of memory|oom|memory error)", "Resource: memory exhaustion", 0.95),
    ]

    # Check for early epistemic patterns (onset within first few steps).
    for pattern, signal, confidence in early_epistemic_patterns:
        if re.search(pattern, tail_lower):
            return TrajectoryDiagnostic(
                onset_timing="early",
                failure_class="epistemic",
                signal=signal,
                confidence=confidence,
            )

    # Check for mid-stage epistemic patterns.
    for pattern, signal, confidence in mid_epistemic_patterns:
        if re.search(pattern, tail_lower):
            return TrajectoryDiagnostic(
                onset_timing="mid",
                failure_class="epistemic",
                signal=signal,
                confidence=confidence,
            )

    # Check for resource exhaustion.
    for pattern, signal, confidence in resource_patterns:
        if re.search(pattern, tail_lower):
            return TrajectoryDiagnostic(
                onset_timing="late",
                failure_class="resource",
                signal=signal,
                confidence=confidence,
            )

    return None
