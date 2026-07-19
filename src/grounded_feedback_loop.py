"""Grounded feedback loop for iterative code refinement.

Implements interaction scaling as described in arXiv:2607.11598v1.
Uses real observations (lint errors, test failures) to drive iterative
refinement: the agent proposes, external instruments observe, and the
agent revises based on grounded feedback. Each cycle imports a real
observation, breaking through the ceiling that reasoning-only and
sampling-only approaches hit.
"""
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _build_feedback_prompt(
    lint_issues: str, test_failures: str, iteration: int
) -> str:
    """Construct a prompt asking Claude to fix lint/test failures.

    Args:
        lint_issues: Formatted lint output describing problems found
        test_failures: Formatted test output describing failures
        iteration: Iteration number (for safety limiting)

    Returns:
        Markdown-formatted prompt for Claude Code invocation
    """
    prompt = f"""# Refinement iteration {iteration} — fix grounded feedback

The code you wrote has issues found by automated instruments:

## Lint issues

```
{lint_issues}
```

## Test failures

```
{test_failures}
```

## Your task

Fix the issues above. Use the real observations to guide your revisions.
Each change should address a specific failure or lint warning.
Re-run your mental test suite on the fixed code.

Minimal changes are better than rewrites.
"""
    return prompt


def should_attempt_interaction_refinement(
    lint_status: str, test_status: str, touched_py: list[str]
) -> bool:
    """Decide whether to attempt interaction-based refinement.

    Interaction refinement is worth attempting when:
    - There are Python files to fix
    - Lint or tests failed (real observations to ground on)
    - The failure is not a catastrophic setup failure

    Args:
        lint_status: Result of lint pass ("passed", "failed", "error")
        test_status: Result of test pass ("passed", "failed", "unvalidated")
        touched_py: List of .py files the PR modified

    Returns:
        True if conditions warrant attempting refinement
    """
    has_py = bool(touched_py)
    has_observation = (lint_status == "failed") or (test_status == "failed")
    not_catastrophic = lint_status != "error"
    return has_py and has_observation and not_catastrophic


def _format_feedback_for_claude(
    lint_output: str, test_output: str, max_chars: int = 8000
) -> tuple[str, str]:
    """Trim and format feedback to fit within Claude's context.

    Args:
        lint_output: Full lint output
        test_output: Full test output
        max_chars: Total character budget for both

    Returns:
        (lint_feedback, test_feedback) trimmed to budget
    """
    lint_lines = lint_output.split('\n')[:50]
    test_lines = test_output.split('\n')[:50]

    lint_trimmed = '\n'.join(lint_lines)[:max_chars // 2]
    test_trimmed = '\n'.join(test_lines)[:max_chars // 2]

    return lint_trimmed, test_trimmed


def attempt_interaction_refinement(
    clone_workdir: Path,
    touched_py: list[str],
    lint_output: str,
    test_output: str,
    claude_timeout_s: int = 600,
    max_iterations: int = 2,
) -> Optional[dict]:
    """Attempt to fix lint/test failures via interaction loop.

    Runs Claude with grounded feedback on observed failures, then re-runs
    lint/tests to verify the fixes. Limited to max_iterations to avoid
    unbounded loops.

    Args:
        clone_workdir: Path to repo clone
        touched_py: List of .py files modified by the original implementation
        lint_output: Output from the lint pass
        test_output: Output from the test pass
        claude_timeout_s: Timeout for Claude invocation
        max_iterations: Maximum refinement iterations to attempt

    Returns:
        Dict with keys:
          - status: "interaction_succeeded", "interaction_failed", or None if not attempted
          - iterations_run: Number of iterations attempted
          - final_lint_status: Status after final iteration
          - final_test_status: Status after final iteration
        Or None if interaction was not attempted or skipped.
    """
    # Import here to avoid circular dependency on run.py
    from run import invoke_claude_code, _run_test_lint, _run_test_pytest

    if not should_attempt_interaction_refinement("failed", "failed", touched_py):
        return None

    result = {
        "status": None,
        "iterations_run": 0,
        "final_lint_status": None,
        "final_test_status": None,
    }

    lint_feedback, test_feedback = _format_feedback_for_claude(lint_output, test_output)

    for iteration in range(1, max_iterations + 1):
        log.info(f"  → interaction refinement iteration {iteration}/{max_iterations}")

        bundle_dir = clone_workdir / ".outrider-interaction-refine"
        bundle_dir.mkdir(parents=True, exist_ok=True)

        invocation = _build_feedback_prompt(lint_feedback, test_feedback, iteration)
        (bundle_dir / "INVOCATION.md").write_text(invocation)

        ok, patch_output = invoke_claude_code(clone_workdir, timeout_s=claude_timeout_s)
        if not ok:
            log.warning(f"  ! interaction iteration {iteration}: Claude invocation failed")
            result["status"] = "interaction_failed"
            result["iterations_run"] = iteration
            break

        lint_status, _, lint_issues = _run_test_lint(clone_workdir, touched_py)
        test_status, test_output_new = _run_test_pytest(clone_workdir, [])

        result["iterations_run"] = iteration
        result["final_lint_status"] = lint_status
        result["final_test_status"] = test_status

        if lint_status == "passed" and test_status in ("passed", "unvalidated"):
            log.info(f"  ✓ interaction loop converged in {iteration} iterations")
            result["status"] = "interaction_succeeded"
            return result

        lint_feedback, test_feedback = _format_feedback_for_claude(
            lint_output, test_output_new
        )
        log.info(
            f"    iteration {iteration}: lint={lint_status}, "
            f"test={test_status}; continuing..."
        )

    if result["status"] != "interaction_succeeded":
        log.info(
            f"  → interaction loop did not converge "
            f"(final: lint={result['final_lint_status']}, "
            f"test={result['final_test_status']})"
        )
        result["status"] = "interaction_plateaued"

    return result
