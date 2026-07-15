"""Tests for agent trajectory analysis in failure diagnostics.

Validates that the trajectory-based failure detection (adapted from
"Failure as a Process") properly integrates with Outrider's existing
_agent_failure_blocks() function.

Run with: pytest tests/test_trajectory_analysis.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from agent_trajectory_analysis import analyze_agent_trajectory  # noqa: E402


# ─── Trajectory analyzer unit tests ─────────────────────────────────────────


def test_early_epistemic_file_not_found():
    """Detects early epistemic errors when agent tries non-existent files."""
    log = "Error: File not found: /tmp/missing.txt"
    diag = analyze_agent_trajectory(log)
    assert diag is not None
    assert diag.onset_timing == "early"
    assert diag.failure_class == "epistemic"
    assert diag.confidence > 0.8


def test_early_epistemic_import_error():
    """Detects early epistemic errors with import misunderstandings."""
    log = "ModuleNotFoundError: No module named 'unknown_lib'"
    diag = analyze_agent_trajectory(log)
    assert diag is not None
    assert diag.onset_timing == "early"
    assert diag.failure_class == "epistemic"


def test_early_epistemic_undefined_variable():
    """Detects early epistemic errors with undefined variable references."""
    log = "NameError: name 'my_function' is not defined"
    diag = analyze_agent_trajectory(log)
    assert diag is not None
    assert diag.onset_timing == "early"
    assert diag.failure_class == "epistemic"


def test_early_epistemic_syntax_error():
    """Detects early epistemic errors with invalid syntax generation."""
    log = "SyntaxError: invalid syntax, expected ':' at line 5"
    diag = analyze_agent_trajectory(log)
    assert diag is not None
    assert diag.onset_timing == "early"
    assert diag.failure_class == "epistemic"


def test_mid_epistemic_type_error():
    """Detects mid-stage epistemic errors with type mismatches."""
    log = "TypeError: unsupported operand type(s) for +: 'str' and 'int'"
    diag = analyze_agent_trajectory(log)
    assert diag is not None
    assert diag.onset_timing == "mid"
    assert diag.failure_class == "epistemic"


def test_mid_epistemic_command_not_found():
    """Detects mid-stage epistemic errors with CLI misunderstandings."""
    log = "bash: unknown_command: command not found"
    diag = analyze_agent_trajectory(log)
    assert diag is not None
    assert diag.onset_timing == "mid"
    assert diag.failure_class == "epistemic"


def test_resource_timeout():
    """Detects resource exhaustion (timeout)."""
    log = "Error: execution timed out after 900 seconds"
    diag = analyze_agent_trajectory(log)
    assert diag is not None
    assert diag.failure_class == "resource"
    assert diag.onset_timing == "late"


def test_resource_out_of_memory():
    """Detects resource exhaustion (OOM)."""
    log = "MemoryError: out of memory"
    diag = analyze_agent_trajectory(log)
    assert diag is not None
    assert diag.failure_class == "resource"


def test_no_signal_on_empty_log():
    """Returns None when log is empty."""
    diag = analyze_agent_trajectory("")
    assert diag is None


def test_no_signal_on_none_log():
    """Returns None when log is None."""
    diag = analyze_agent_trajectory(None)
    assert diag is None


def test_signal_not_found_returns_none():
    """Returns None when no known patterns match."""
    log = "Everything went fine and the tests passed"
    diag = analyze_agent_trajectory(log)
    assert diag is None


# ─── Integration with _agent_failure_blocks ────────────────────────────────


def test_agent_failure_blocks_surfaces_early_epistemic():
    """_agent_failure_blocks() surfaces early epistemic errors with clear signal."""
    log_tail = "Traceback: File not found: /src/nonexistent.py"
    blocks = run._agent_failure_blocks("claude", log_tail, 1)
    block_text = "\n".join(blocks)
    assert "Early-stage" in block_text or "epistemic error" in block_text.lower()


def test_agent_failure_blocks_surfaces_mid_epistemic():
    """_agent_failure_blocks() surfaces mid-stage epistemic errors."""
    log_tail = "TypeError: unsupported operand types"
    blocks = run._agent_failure_blocks("claude", log_tail, 1)
    block_text = "\n".join(blocks)
    assert "epistemic" in block_text.lower() or "assumption" in block_text.lower()


def test_agent_failure_blocks_handles_unknown_errors():
    """_agent_failure_blocks() falls back to log tail for unknown errors."""
    log_tail = "Some mysterious error that we don't recognize"
    blocks = run._agent_failure_blocks("claude", log_tail, 1)
    block_text = "\n".join(blocks)
    # Should contain a details section with the log tail
    assert "Claude agent failure tail" in block_text or log_tail[:100] in block_text


def test_agent_failure_blocks_ignores_known_errors():
    """_agent_failure_blocks() prioritizes known patterns over trajectory."""
    # Credit exhaustion is a known pattern that should NOT be overridden
    log_tail = "Credit balance is too low and also a file not found"
    blocks = run._agent_failure_blocks("claude", log_tail, 2)
    block_text = "\n".join(blocks)
    assert "credit balance" in block_text.lower()
    # Should NOT surface epistemic error when credit balance is the real issue
    assert "epistemic" not in block_text.lower()


def test_agent_failure_blocks_handles_empty_log():
    """_agent_failure_blocks() handles empty log gracefully."""
    blocks = run._agent_failure_blocks("claude", "", 0)
    assert isinstance(blocks, list)
    assert len(blocks) == 0
