"""Tests for runtime policy injection — FIRE (arxiv:2609.26048v1).

Runtime policies are targeted natural-language instructions applied at
states that preceded observed failures, improving reliability without
changing model weights or the user prompt.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runtime_policy import (
    Policy,
    policies_from_failure,
    inject_policies_into_prompt,
    format_policies_for_log,
)


class TestPolicyDetection:
    """Test failure-pattern matching and policy inference."""

    def test_empty_failure_returns_no_policies(self):
        """No failure text → no policies."""
        policies = policies_from_failure("")
        assert policies == []

    def test_tool_not_found_detection(self):
        """Detects 'command not found' pattern."""
        failure = "bash: ruff: command not found"
        policies = policies_from_failure(failure)
        assert len(policies) == 1
        assert policies[0].trigger == "tool_not_found"
        assert "verify it is available" in policies[0].instruction.lower()

    def test_permission_denied_detection(self):
        """Detects 'permission denied' pattern."""
        failure = "Permission denied: /root/.config/file"
        policies = policies_from_failure(failure)
        assert len(policies) == 1
        assert policies[0].trigger == "permission_denied"
        assert "permission" in policies[0].instruction.lower()

    def test_timeout_detection(self):
        """Detects timeout patterns."""
        failure = "Error: timeout after 30 seconds"
        policies = policies_from_failure(failure)
        assert len(policies) == 1
        assert policies[0].trigger == "timeout"
        assert "break" in policies[0].instruction.lower()

    def test_auth_failure_detection(self):
        """Detects authentication failures."""
        failure = "401 Unauthorized: invalid api key"
        policies = policies_from_failure(failure)
        assert len(policies) == 1
        assert policies[0].trigger == "auth_failed"
        assert "authentiation" in policies[0].instruction.lower() or \
               "credentials" in policies[0].instruction.lower()

    def test_multiple_policies_from_one_failure(self):
        """One failure can trigger multiple policies."""
        failure = (
            "ruff: command not found\n"
            "Permission denied on src/\n"
            "timeout after 60 seconds"
        )
        policies = policies_from_failure(failure)
        # All three patterns should trigger
        assert len(policies) >= 3
        triggers = {p.trigger for p in policies}
        assert "tool_not_found" in triggers
        assert "permission_denied" in triggers
        assert "timeout" in triggers

    def test_case_insensitive_matching(self):
        """Failure matching is case-insensitive."""
        failure = "COMMAND NOT FOUND: ruff"
        policies = policies_from_failure(failure)
        assert len(policies) == 1
        assert policies[0].trigger == "tool_not_found"


class TestPolicyInjection:
    """Test injecting policies into the prompt."""

    def test_empty_policies_no_change(self):
        """No policies → no changes to prompt."""
        prompt = "Original prompt text."
        result = inject_policies_into_prompt(prompt, [])
        assert result == prompt

    def test_policies_appended_to_prompt(self):
        """Policies are appended as a new section."""
        prompt = "Original prompt text."
        policies = [
            Policy(
                trigger="test_trigger",
                instruction="Do something different.",
            )
        ]
        result = inject_policies_into_prompt(prompt, policies)
        assert prompt in result
        assert "Runtime policies" in result or "runtime policies" in result.lower()
        assert "test_trigger" in result
        assert "Do something different" in result

    def test_multiple_policies_in_prompt(self):
        """Multiple policies are all included."""
        prompt = "Base prompt."
        policies = [
            Policy(trigger="trigger1", instruction="Instruction 1."),
            Policy(trigger="trigger2", instruction="Instruction 2."),
        ]
        result = inject_policies_into_prompt(prompt, policies)
        assert "trigger1" in result
        assert "trigger2" in result
        assert "Instruction 1" in result
        assert "Instruction 2" in result

    def test_policies_numbered_in_prompt(self):
        """Policies are numbered for clarity."""
        prompt = "Base."
        policies = [
            Policy(trigger="a", instruction="First."),
            Policy(trigger="b", instruction="Second."),
        ]
        result = inject_policies_into_prompt(prompt, policies)
        assert "Policy 1" in result
        assert "Policy 2" in result


class TestPolicyFormatting:
    """Test formatting policies for logging."""

    def test_empty_policies_empty_string(self):
        """No policies → empty log string."""
        result = format_policies_for_log([])
        assert result == ""

    def test_single_policy_formatted(self):
        """Single policy is formatted with count and trigger."""
        policies = [Policy(trigger="test", instruction="Do it.")]
        result = format_policies_for_log(policies)
        assert "policies=1" in result
        assert "test" in result

    def test_multiple_policies_formatted(self):
        """Multiple policies are comma-separated."""
        policies = [
            Policy(trigger="a", instruction="A."),
            Policy(trigger="b", instruction="B."),
        ]
        result = format_policies_for_log(policies)
        assert "policies=2" in result
        assert "a" in result and "b" in result


class TestPolicyDataclass:
    """Test the Policy dataclass."""

    def test_policy_creation(self):
        """Policy can be created with required fields."""
        p = Policy(
            trigger="test_failure",
            instruction="Try this instead.",
        )
        assert p.trigger == "test_failure"
        assert p.instruction == "Try this instead."
        assert p.rationale == ""

    def test_policy_with_rationale(self):
        """Policy can include rationale."""
        p = Policy(
            trigger="failure",
            instruction="Instruction.",
            rationale="Why this helps.",
        )
        assert p.rationale == "Why this helps."

    def test_policy_is_frozen(self):
        """Policy is immutable once created."""
        p = Policy(trigger="test", instruction="Do it.")
        with pytest.raises(AttributeError):
            p.trigger = "changed"
