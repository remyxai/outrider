"""Terminal failure statuses must exit the workflow step non-zero.

Brief mode ("lead-content is the spec") and issue-convention mode set
``brief_failed`` / ``issue_convention_failed_claude`` as their
``failure_status`` when the runner throws — e.g. a rejected ``git push``.
If those aren't in ``FAILURE_EXIT_STATUSES`` the run exits 0 and looks green
with no PR/branch, masking a real failure as a graceful "declined to publish".

Run with: pytest tests/test_failure_exit_statuses.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


def test_brief_failed_is_a_failure_status():
    assert "brief_failed" in run.FAILURE_EXIT_STATUSES


def test_issue_convention_failed_is_a_failure_status():
    assert "issue_convention_failed_claude" in run.FAILURE_EXIT_STATUSES


def test_graceful_brief_outcomes_stay_green():
    # Legitimate green outcomes must NOT be treated as failures.
    for ok in ("branch_pushed_no_pr", "pr_opened_brief"):
        assert ok not in run.FAILURE_EXIT_STATUSES
