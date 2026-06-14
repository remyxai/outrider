"""Unit tests for selection-pass exploration telemetry.

Covers the segment-aware shell classifier, the transcript→coverage parser,
the context-efficiency proxy, and the coverage gate (observe / enforce / off).

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# ── segment-aware shell classifier ─────────────────────────────────────────

def test_classify_piped_search_excludes_pager():
    # `gh search code … | head` — search counts, the stdin pager doesn't.
    assert run._classify_shell_command('gh search code "x" --repo o/r | head') \
        == ["search"]


def test_classify_compound_command_each_stage():
    # grep over a path → search; sed/cat over a file → file_read.
    cmd = "grep -rn pat src; sed -n '1,40p' src/run.py; cat README.md"
    assert run._classify_shell_command(cmd) == ["search", "file_read", "file_read"]


def test_classify_stdin_filter_is_neither():
    # `cat file | grep foo`: the cat is a real read, the piped grep is stdin.
    assert run._classify_shell_command("cat foo.py | grep foo") == ["file_read"]


def test_classify_gh_api_contents_is_read():
    assert run._classify_shell_command(
        "gh api repos/o/r/contents/src/x.py") == ["file_read"]


def test_classify_gh_issue_is_neither():
    assert run._classify_shell_command("gh issue list --repo o/r") == []


def test_classify_gh_graph_is_neither():
    # gh-graph is structural navigation, not a content read.
    assert run._classify_shell_command("gh-graph src/x.py") == []


def test_classify_head_with_file_vs_stdin():
    assert run._classify_shell_command("head -n 50 src/x.py") == ["file_read"]
    assert run._classify_shell_command("head -n 50") == []   # reads stdin


# ── tool_use classification (native tools) ──────────────────────────────────

def test_classify_native_read_and_grep():
    assert run._classify_tool_use("Read", {"file_path": "x.py"}) == ["file_read"]
    assert run._classify_tool_use("Grep", {"pattern": "x"}) == ["search"]
    assert run._classify_tool_use("WebFetch", {"url": "http://x"}) == ["file_read"]


# ── transcript → coverage ───────────────────────────────────────────────────

def _events():
    return [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "s1", "name": "Bash",
             "input": {"command": 'gh search code "foo" --repo o/r'}},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "r1", "name": "Bash",
             "input": {"command": "gh api repos/o/r/contents/a.py"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "r1",
             "content": "l1\nl2\nl3\nl4\nl5"},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "r2", "name": "Read",
             "input": {"file_path": "b.py"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "r2",
             "content": [{"type": "text", "text": "x\ny\nz"}]},
        ]}},
    ]


def test_coverage_counts_and_ratio():
    cov = run._selection_coverage_from_events(_events())
    assert cov["searches"] == 1
    assert cov["file_reads"] == 2
    assert cov["visible_lines"] == 8                 # 5 + 3
    assert cov["search_to_read_ratio"] == round(1 / 2, 2)


def test_coverage_empty_transcript():
    cov = run._selection_coverage_from_events([])
    assert cov == {"searches": 0, "file_reads": 0, "visible_lines": 0,
                   "search_to_read_ratio": 0.0}


def test_coverage_result_lines_only_for_reads():
    # A search's tool_result must not contribute to visible_lines.
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "s1", "name": "Grep",
             "input": {"pattern": "x"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "s1",
             "content": "hit1\nhit2\nhit3"},
        ]}},
    ]
    cov = run._selection_coverage_from_events(events)
    assert cov["searches"] == 1
    assert cov["visible_lines"] == 0


# ── context-efficiency proxy ────────────────────────────────────────────────

def test_context_efficiency_distinct_citations():
    text = "verified src/run.py:120 and src/run.py:120 and pkg/a.py:9"
    # distinct pairs = {(src/run.py,120),(pkg/a.py,9)} = 2
    assert run._selection_context_efficiency(text, 100) == round(2 / 100, 4)


def test_context_efficiency_zero_visible_guard():
    assert run._selection_context_efficiency("src/x.py:1", 0) == 1.0


def test_context_efficiency_no_citations():
    assert run._selection_context_efficiency("no file refs here", 50) == 0.0


# ── coverage gate ───────────────────────────────────────────────────────────

def test_gate_observe_flags_without_blocking(monkeypatch):
    monkeypatch.delenv("REMYX_SELECTION_COVERAGE_GATE", raising=False)  # default observe
    data = {"chosen_index": 1}
    cov = {"visible_lines": 50}
    run._apply_coverage_gate(data, cov, higher_floor=False)
    assert cov["under_explored"] is True             # 50 < 150 floor
    assert data["chosen_index"] == 1                 # observe never blocks
    assert "under_explored" not in data


def test_gate_observe_above_floor(monkeypatch):
    monkeypatch.delenv("REMYX_SELECTION_COVERAGE_GATE", raising=False)
    cov = {"visible_lines": 200}
    run._apply_coverage_gate({"chosen_index": 1}, cov, higher_floor=False)
    assert cov["under_explored"] is False


def test_gate_enforce_downgrades(monkeypatch):
    monkeypatch.setenv("REMYX_SELECTION_COVERAGE_GATE", "enforce")
    data = {"chosen_index": 1}
    cov = {"visible_lines": 50}
    run._apply_coverage_gate(data, cov, higher_floor=False)
    assert data["chosen_index"] == -1
    assert data["under_explored"] is True


def test_gate_off_is_noop(monkeypatch):
    monkeypatch.setenv("REMYX_SELECTION_COVERAGE_GATE", "off")
    data = {"chosen_index": 1}
    cov = {"visible_lines": 1}
    run._apply_coverage_gate(data, cov, higher_floor=False)
    assert "under_explored" not in cov
    assert data["chosen_index"] == 1


def test_gate_higher_floor_for_extension(monkeypatch):
    monkeypatch.setenv("REMYX_SELECTION_COVERAGE_GATE", "enforce")
    # 200 lines clears the in-pool floor (150) but not the extension floor (300).
    data = {"chosen_index": 2}
    cov = {"visible_lines": 200}
    run._apply_coverage_gate(data, cov, higher_floor=True)
    assert cov["under_explored"] is True
    assert data["chosen_index"] == -1


def test_gate_env_override_floor(monkeypatch):
    monkeypatch.setenv("REMYX_SELECTION_COVERAGE_GATE", "enforce")
    monkeypatch.setenv("REMYX_SELECTION_MIN_VISIBLE_LINES", "10")
    data = {"chosen_index": 1}
    cov = {"visible_lines": 50}
    run._apply_coverage_gate(data, cov, higher_floor=False)
    assert cov["under_explored"] is False            # 50 ≥ overridden floor 10
    assert data["chosen_index"] == 1
