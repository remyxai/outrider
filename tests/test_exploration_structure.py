"""Tests for the exploration-structure dimension (arXiv:2606.11976).

The paper distinguishes *linear* sequential exploration (one file per step,
single subsystem) from *non-linear, domain-scoped* exploration (branching
across subsystems). These tests exercise the standalone classifier and,
crucially, the wiring into the existing selection-coverage call site in
``run._selection_coverage_from_events`` — confirming the new dimension
rides along on the same coverage dict that already flows through the gate
and run telemetry.

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402  (existing, non-new call-site module)
import exploration_structure as es  # noqa: E402


def _read(turn_paths):
    """An assistant turn issuing one Read tool_use per path in ``turn_paths``."""
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"r{i}", "name": "Read",
         "input": {"file_path": p}}
        for i, p in enumerate(turn_paths)
    ]}}


# ── path / domain extraction ────────────────────────────────────────────────

def test_domain_of_top_level_and_root():
    assert es._domain_of("src/run.py") == "src"
    assert es._domain_of("./tests/x.py") == "tests"
    assert es._domain_of("README.md") == "<root>"


def test_paths_from_native_and_bash_tools():
    assert es._paths_in_tool_use("Read", {"file_path": "src/run.py"}) == \
        ["src/run.py"]
    assert es._paths_in_tool_use("Grep", {"pattern": "x"}) == []  # no path key
    bash = es._paths_in_tool_use(
        "Bash", {"command": "sed -n '1,40p' src/run.py; cat tests/a.py"})
    assert "src/run.py" in bash and "tests/a.py" in bash


def test_bash_skips_urls_and_flags():
    paths = es._paths_in_tool_use(
        "Bash", {"command": "gh api https://x/contents/src/run.py --json name"})
    assert all("://" not in p for p in paths)


# ── structure classification ────────────────────────────────────────────────

def test_linear_single_subsystem():
    # One read per step, all under src/ → the paper's structural-mismatch shape.
    events = [_read(["src/a.py"]), _read(["src/b.py"]), _read(["src/c.py"])]
    out = es.exploration_structure_from_events(events)
    assert out["structure"] == "linear"
    assert out["domains"] == 1
    assert out["linearity"] == 1.0
    assert out["parallel_turns"] == 0
    assert es.is_single_subsystem(out) is True


def test_domain_scoped_parallel_branching():
    # Batched reads spanning several subsystems → non-linear domain-scoped.
    events = [
        _read(["src/run.py", "tests/test_run.py", "docs/guide.md"]),
        _read(["src/gh_graph.py", "tests/test_axis.py"]),
    ]
    out = es.exploration_structure_from_events(events)
    assert out["structure"] == "domain-scoped"
    assert out["domains"] == 3
    assert out["parallel_turns"] == 2
    assert out["max_turn_width"] == 3
    assert out["linearity"] == 0.0
    assert es.is_single_subsystem(out) is False


def test_branching_within_one_subsystem():
    # Batched, but every read stays in src/ → branching, not domain-scoped.
    events = [_read(["src/a.py", "src/b.py"])]
    out = es.exploration_structure_from_events(events)
    assert out["structure"] == "branching"
    assert out["domains"] == 1


def test_domain_switches_counted_in_order():
    events = [_read(["src/a.py"]), _read(["tests/b.py"]), _read(["src/c.py"])]
    out = es.exploration_structure_from_events(events)
    # src → tests → src = 2 switches; sequential single steps over 2 domains
    # with ≥2 switches still reads as domain-scoped.
    assert out["domain_switches"] == 2
    assert out["structure"] == "domain-scoped"


def test_empty_and_no_path_events():
    assert es.exploration_structure_from_events([])["structure"] == "none"
    no_path = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "s1", "name": "Grep",
         "input": {"pattern": "x"}}]}}]
    assert es.exploration_structure_from_events(no_path)["structure"] == "none"


def test_summary_string_is_human_readable():
    out = es.exploration_structure_from_events([_read(["src/a.py"])])
    s = es.structure_summary(out)
    assert "linear" in s and "domains" in s


# ── integration: rides along on the existing coverage call site ─────────────

def test_coverage_merges_structure_dimension():
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "r1", "name": "Read",
             "input": {"file_path": "src/run.py"}},
            {"type": "tool_use", "id": "r2", "name": "Read",
             "input": {"file_path": "tests/test_run.py"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "r1", "content": "a\nb"},
        ]}},
    ]
    cov = run._selection_coverage_from_events(events)
    # Original dimensions still present and unchanged in shape.
    assert cov["file_reads"] == 2
    assert "search_to_read_ratio" in cov
    # New dimension merged in by the call-site edit.
    struct = cov["exploration_structure"]
    assert struct["domains"] == 2                 # src + tests
    assert struct["parallel_turns"] == 1
    assert struct["structure"] == "domain-scoped"


def test_structure_disabled_via_env(monkeypatch):
    monkeypatch.setenv("REMYX_SELECTION_EXPLORATION_STRUCTURE", "off")
    cov = run._selection_coverage_from_events([_read(["src/a.py"])])
    assert "exploration_structure" not in cov
    # Core coverage dimensions remain intact when the dimension is disabled.
    assert cov["file_reads"] == 1
