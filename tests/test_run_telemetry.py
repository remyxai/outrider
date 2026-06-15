"""Unit tests for the best-effort run-telemetry POST.

Covers payload mapping from the `result` dict, the source/env handling,
artifact-url fallback, reasoning truncation, the local-run skip (no
GITHUB_RUN_ID), and the best-effort guarantee (a POST failure never raises).

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Target  # noqa: E402


def _result(**over):
    base = {
        "status": "pr_opened",
        "pr_url": "https://github.com/remyxai/example/pull/1",
        "broad_pool_size": 30,
        "refine_pool_size": 12,
        "candidates_considered": 24,
        "refine_queries": ["q1", "q2"],
        "license_class_counts": {"permissive": 3},
        "selection_reasoning": "clear call site at src/foo.py:12",
        "selection_integration_shape": "addition",
        "selection_coverage": {"searches": 1, "file_reads": 6, "visible_lines": 318},
        "selection_context_efficiency": 0.0063,
        "cost_usd": 0.45,
        "input_tokens": 4188,
        "output_tokens": 4635,
        "claude_calls": 1,
    }
    base.update(over)
    return base


def _capture(monkeypatch):
    calls = []
    monkeypatch.setattr(run, "_remyx_post", lambda path, body: calls.append((path, body)) or {})
    return calls


def _target():
    return Target(repo="remyxai/example", interest_id="iid")


def test_posts_full_payload(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.delenv("REMYX_RUN_SOURCE", raising=False)
    calls = _capture(monkeypatch)

    run._post_run_telemetry(_result(), _target())

    assert len(calls) == 1
    path, body = calls[0]
    assert path == "/api/v1.0/outrider/runs"
    assert body["run_id"] == 12345 and isinstance(body["run_id"], int)
    assert body["target_repo"] == "remyxai/example"
    assert body["status"] == "pr_opened"
    assert body["source"] == "outrider"                      # default
    assert body["recommendation_id"] is None
    assert body["artifact_url"].endswith("/pull/1")
    assert body["refine_queries"] == ["q1", "q2"]
    assert body["license_class_counts"] == {"permissive": 3}
    assert body["selection_coverage"]["visible_lines"] == 318
    assert body["selection_context_efficiency"] == 0.0063
    assert body["cost_usd"] == 0.45 and body["claude_calls"] == 1


def test_recommendation_id_threaded_from_result(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = _capture(monkeypatch)
    rid = "11111111-2222-3333-4444-555555555555"
    run._post_run_telemetry(_result(recommendation_id=rid), _target())
    assert calls[0][1]["recommendation_id"] == rid


def test_recommendation_id_null_when_absent(monkeypatch):
    # Skip / out-of-pool runs never set result["recommendation_id"].
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = _capture(monkeypatch)
    run._post_run_telemetry(_result(), _target())   # no recommendation_id key
    assert calls[0][1]["recommendation_id"] is None


def test_skips_without_run_id(monkeypatch):
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    calls = _capture(monkeypatch)
    run._post_run_telemetry(_result(), _target())
    assert calls == []           # local run → no POST attempted


def test_best_effort_swallows_post_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")

    def boom(path, body):
        raise RuntimeError("Remyx API POST → HTTP 503")

    monkeypatch.setattr(run, "_remyx_post", boom)
    # Must NOT raise — telemetry is best-effort.
    run._post_run_telemetry(_result(), _target())


def test_source_env_override(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("REMYX_RUN_SOURCE", "outrider_eval")
    calls = _capture(monkeypatch)
    run._post_run_telemetry(_result(), _target())
    assert calls[0][1]["source"] == "outrider_eval"


def test_artifact_url_falls_back_to_issue(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = _capture(monkeypatch)
    res = _result(status="issue_opened_preflight")
    res.pop("pr_url")
    res["issue_url"] = "https://github.com/remyxai/example/issues/6"
    run._post_run_telemetry(res, _target())
    assert calls[0][1]["artifact_url"].endswith("/issues/6")


def test_reasoning_truncated_to_2kb(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = _capture(monkeypatch)
    run._post_run_telemetry(_result(selection_reasoning="x" * 5000), _target())
    assert len(calls[0][1]["selection_reasoning_excerpt"]) == 2048


def test_skipped_run_sends_nulls(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = _capture(monkeypatch)
    # A skip that never reached the selection pass — only status present.
    run._post_run_telemetry({"status": "skipped_rate_limit"}, _target())
    body = calls[0][1]
    assert body["status"] == "skipped_rate_limit"
    assert body["selection_coverage"] is None
    assert body["artifact_url"] is None
    assert body["selection_reasoning_excerpt"] is None
