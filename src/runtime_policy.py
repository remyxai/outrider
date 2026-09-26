"""Runtime policies for agent reliability — FIRE (arxiv:2609.26048v1).

Language-model agents often reach a working solution then fail to deliver it
consistently. This module implements targeted natural-language instructions
applied at states that preceded observed failures, without changing model
weights or the user prompt. Policies are attached to the invocation context
after failures are detected, improving repeated success.

Policy structure:
  - trigger: failure pattern to match (e.g., "tool_not_found", "permission_denied")
  - instruction: natural-language correction, injected into the prompt
  - scope: optional constraint (e.g., "after_tool_use", "in_implementation")
"""

from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class Policy:
    """One runtime policy: a corrective instruction tied to a failure mode.

    Attributes:
        trigger: Failure pattern (e.g., "tool_not_found", "command_failed")
        instruction: Natural-language directive to inject into the prompt.
        rationale: Why this policy helps (for logs, not shown to agent).
    """

    trigger: str  #: Failure pattern name
    instruction: str  #: The correction directive
    rationale: str = ""  #: Optional context for log output


def policies_from_failure(failure_tail: str) -> list[Policy]:
    """Infer applicable policies from an observed failure log tail.

    Scans the failure text for recognizable patterns and returns policies
    that address them. The orchestrator calls this when a prior run failed,
    then injects the returned policies into the next invocation.

    Args:
        failure_tail: Agent log output (typically last ~1KB) from a failure.

    Returns:
        List of policies matched against this failure, or empty list.
    """
    policies: list[Policy] = []
    tail_lower = (failure_tail or "").lower()

    # Tool not found / command failed patterns
    if "command not found" in tail_lower or "no such file or directory" in tail_lower:
        policies.append(
            Policy(
                trigger="tool_not_found",
                instruction=(
                    "Before using any command-line tool, verify it is available "
                    "on PATH. If not found, check the error message and either "
                    "install the tool or use an alternative approach. Do not assume "
                    "tools that were available earlier are still available."
                ),
                rationale="Agent attempted to use a missing tool without checking availability.",
            )
        )

    # Permission denied
    if "permission denied" in tail_lower:
        policies.append(
            Policy(
                trigger="permission_denied",
                instruction=(
                    "Permission was denied on a file or directory operation. "
                    "This may be intentional (guardrails blocking a write) or an "
                    "infrastructure issue. Verify the file path is correct and "
                    "within the allowed scope. If blocked by design, find an "
                    "alternative approach."
                ),
                rationale="Agent hit permission boundary without recovery strategy.",
            )
        )

    # Timeout / timeout-like issues
    if "timeout" in tail_lower or "timed out" in tail_lower:
        policies.append(
            Policy(
                trigger="timeout",
                instruction=(
                    "The operation timed out. This suggests the task is taking too long. "
                    "Break the work into smaller steps, avoid long-running operations, and "
                    "focus on completing the core task within time limits."
                ),
                rationale="Agent exceeded time budget on a task.",
            )
        )

    # API / authentication failures
    if "401" in tail_lower or "unauthorized" in tail_lower or "invalid key" in tail_lower:
        policies.append(
            Policy(
                trigger="auth_failed",
                instruction=(
                    "Authentication failed. Verify that all required API keys and "
                    "credentials are correctly set in the environment. If using "
                    "external services, check that tokens are valid and not expired."
                ),
                rationale="Agent encountered authentication failure.",
            )
        )

    return policies


def inject_policies_into_prompt(prompt: str, policies: Sequence[Policy]) -> str:
    """Append policy instructions to the agent prompt.

    Policies are added as a new section before the final constraints,
    so the agent sees them as part of the extended guidance without
    disrupting the main task structure.

    Args:
        prompt: The base invocation prompt.
        policies: Policies to inject.

    Returns:
        The prompt with policy instructions appended.
    """
    if not policies:
        return prompt

    section = "\n# Runtime policies — addressing prior failure modes\n\n"
    section += "The following targeted instructions address failure patterns detected "
    section += "in prior attempts. Apply them during this run:\n\n"

    for i, policy in enumerate(policies, 1):
        section += f"**Policy {i} ({policy.trigger}):** {policy.instruction}\n\n"

    return prompt + section


def format_policies_for_log(policies: Sequence[Policy]) -> str:
    """Format policies as a human-readable log line.

    Args:
        policies: Policies to format.

    Returns:
        One-line summary of injected policies.
    """
    if not policies:
        return ""
    triggers = ", ".join(p.trigger for p in policies)
    return f"policies={len(policies)} [{triggers}]"
