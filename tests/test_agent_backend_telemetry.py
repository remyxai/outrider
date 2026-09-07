"""Cost attribution and transcript paths across agent backends.

Both behaviors pinned here were found by running a real agent end-to-end
rather than by parsing a fixture, and neither is visible to an adapter unit
test:

* A Backboard run was being attributed to **Anthropic** with
  ``cost_basis="claude_code_envelope"``, because cost resolution sniffed
  ``ANTHROPIC_BASE_URL`` — a variable no non-Claude agent sets. The fleet
  report would have filed those dollars under the wrong backend.
* Tool paths arrive **absolute**, so the exploration parser derived the
  subsystem from ``/tmp`` (or ``/home``) instead of ``src``, collapsing the
  domain-coverage signal to a single bucket.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

import run
from agents import Capability, Event
from agents.backboard import BackboardBackend
from agents.claude import ClaudeCodeBackend
from agents.codex import CodexBackend
from exploration_structure import _domain_of


@pytest.fixture(autouse=True)
def reset_cost():
    before = dict(run._RUN_COST)
    yield
    run._RUN_COST.clear()
    run._RUN_COST.update(before)


def envelope(**over):
    base = {
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "cache_read_input_tokens": 0,
        },
        "total_cost_usd": 0.05,
        "num_turns": 2,
        "model": "gpt-5.5",
        "is_error": False,
    }
    base.update(over)
    return base


# ─── cost attribution on the agent axis ─────────────────────────────────────

def test_backboard_cost_is_attributed_to_backboard(monkeypatch):
    """R-CLI reports authoritative dollars, so the envelope is trusted."""
    monkeypatch.setattr(run, "_BACKEND", BackboardBackend())
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    run._RUN_COST.update(cost_usd=0.0, cost_basis=None, model_backend=None)

    run._record_claude_usage(envelope())

    assert run._RUN_COST["cost_basis"] == "agent_envelope"
    assert run._RUN_COST["cost_usd"] == pytest.approx(0.05)
    assert "Backboard" in run._RUN_COST["model_backend"]
    assert "Anthropic" not in run._RUN_COST["model_backend"]


def test_backboard_label_names_the_model_it_routed_at(monkeypatch):
    monkeypatch.setattr(run, "_BACKEND", BackboardBackend())
    run._RUN_COST.update(cost_usd=0.0)
    run._record_claude_usage(envelope(model="glm-5.2"))
    assert run._RUN_COST["model_backend"] == "Backboard R-CLI (glm-5.2)"


def test_codex_without_rates_reports_unavailable_not_fake_dollars(monkeypatch):
    """Codex emits tokens only.

    Trusting an absent total_cost_usd would silently report $0.00 spend; the
    honest answer is that dollars aren't knowable for this pair yet, while
    token counts stay exact.
    """
    monkeypatch.setattr(run, "_BACKEND", CodexBackend())
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    run._RUN_COST.update(cost_usd=0.0, cost_basis=None)

    run._record_claude_usage(envelope(total_cost_usd=None))

    assert run._RUN_COST["cost_basis"] == "unavailable"
    assert run._RUN_COST["cost_usd"] == 0.0
    # Tokens are still accurate — only the dollar figure is withheld.
    assert run._RUN_COST["input_tokens"] >= 1000
    assert not CodexBackend().can(Capability.COST_USD)


def test_claude_attribution_is_unchanged(monkeypatch):
    """The default path must keep its historical cost_basis exactly."""
    monkeypatch.setattr(run, "_BACKEND", ClaudeCodeBackend())
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    run._RUN_COST.update(cost_usd=0.0, cost_basis=None)

    run._record_claude_usage(envelope())

    assert run._RUN_COST["cost_basis"] == "claude_code_envelope"
    assert run._RUN_COST["model_backend"] == "Anthropic"
    assert run._RUN_COST["cost_usd"] == pytest.approx(0.05)


def test_claude_rate_table_path_is_unchanged(monkeypatch):
    """A z.ai-routed Claude run still resolves through the rate table."""
    monkeypatch.setattr(run, "_BACKEND", ClaudeCodeBackend())
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic")
    run._RUN_COST.update(cost_usd=0.0, cost_basis=None)

    run._record_claude_usage(envelope(model="glm-5.2"))

    assert run._RUN_COST["cost_basis"] == "backend_rate_table"
    assert run._RUN_COST["model_backend"] == "z.ai (GLM)"


# ─── transcript path relativization ─────────────────────────────────────────

def test_absolute_paths_become_repo_relative(tmp_path):
    events = [Event(kind="tool_use", tool="read",
                    paths=(str(tmp_path / "src" / "calc.py"),))]
    out = run._relativize_events(events, tmp_path)
    assert out[0].paths == ("src/calc.py",)


def test_relativized_paths_restore_the_domain_signal(tmp_path):
    """This is why it matters: the domain is the first path segment."""
    absolute = str(tmp_path / "src" / "calc.py")
    assert _domain_of(absolute) != "src", "absolute paths mis-bucket (the bug)"

    out = run._relativize_events(
        [Event(kind="tool_use", tool="read", paths=(absolute,))], tmp_path
    )
    assert _domain_of(out[0].paths[0]) == "src"


def test_workdir_root_maps_to_the_repo_root_marker(tmp_path):
    """A directory glob targets the workdir itself; left absolute it files
    the whole run under `tmp`."""
    out = run._relativize_events(
        [Event(kind="tool_use", tool="glob", paths=(str(tmp_path),))], tmp_path
    )
    assert out[0].paths == (".",)


def test_already_relative_paths_are_untouched(tmp_path):
    events = [Event(kind="tool_use", tool="read", paths=("src/calc.py",))]
    out = run._relativize_events(events, tmp_path)
    assert out[0].paths == ("src/calc.py",)
    assert out[0] is events[0], "no needless copying"


def test_paths_outside_the_workdir_are_left_alone(tmp_path):
    """Not ours to rewrite — and a path escaping the workdir is a signal."""
    out = run._relativize_events(
        [Event(kind="tool_use", tool="read", paths=("/etc/passwd",))], tmp_path
    )
    assert out[0].paths == ("/etc/passwd",)


def test_events_without_paths_pass_through(tmp_path):
    events = [Event(kind="tool_result", id="x", lines=12)]
    assert run._relativize_events(events, tmp_path) == events


def test_empty_transcript_is_fine(tmp_path):
    assert run._relativize_events([], tmp_path) == []


# ─── the coverage gate must not punish a backend for being quiet ────────────

class _NoTranscript(BackboardBackend):
    """A backend that reports no tool transcript."""
    capabilities = frozenset({Capability.ONESHOT_JSON, Capability.TOKEN_USAGE})


def test_enforce_does_not_zero_out_a_transcript_less_backend(monkeypatch):
    """The trap: visible_lines is 0 without a transcript, below every floor.

    Enforcing would downgrade every pick to skipped_by_selection_verification,
    so the run would look like the model found nothing worth doing rather than
    like the agent can't report coverage.
    """
    monkeypatch.setattr(run, "_BACKEND", _NoTranscript())
    monkeypatch.setenv("REMYX_SELECTION_COVERAGE_GATE", "enforce")

    data = {"chosen_index": 2}
    coverage = {"visible_lines": 0}
    out = run._apply_coverage_gate(data, coverage, higher_floor=False)

    assert out["chosen_index"] == 2, "the pick must survive"
    assert "under_explored" not in out
    assert coverage["basis"] == "unavailable"


def test_enforce_still_gates_a_backend_that_does_report(monkeypatch):
    """Capability-awareness must not become a blanket exemption."""
    monkeypatch.setattr(run, "_BACKEND", ClaudeCodeBackend())
    monkeypatch.setenv("REMYX_SELECTION_COVERAGE_GATE", "enforce")

    data = {"chosen_index": 2}
    out = run._apply_coverage_gate(data, {"visible_lines": 3}, higher_floor=False)

    assert out["chosen_index"] == -1
    assert out["under_explored"] is True
