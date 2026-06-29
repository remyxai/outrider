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


# ─── expanded payload fields ─────────────────────────────────────────


def test_backend_and_cost_basis_round_trip(monkeypatch):
    """model_backend / cost_basis / cache_read_input_tokens / num_turns
    are essential for splitting telemetry by backend; verify they reach
    the payload exactly as the result-dict carries them."""
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = _capture(monkeypatch)
    res = _result(
        agent="Claude Code",
        model_backend="z.ai (GLM)",
        cost_basis="backend_rate_table",
        cache_read_input_tokens=10368,
        num_turns=14,
        envelopes_without_usage=2,
    )
    run._post_run_telemetry(res, _target())
    body = calls[0][1]
    assert body["agent"] == "Claude Code"
    assert body["model_backend"] == "z.ai (GLM)"
    assert body["cost_basis"] == "backend_rate_table"
    assert body["cache_read_input_tokens"] == 10368
    assert body["num_turns"] == 14
    assert body["envelopes_without_usage"] == 2


def test_preflight_and_routing_fields(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = _capture(monkeypatch)
    res = _result(
        preflight_decision="ISSUE",
        audit_anchor="reference",
        pin_method="2606.27369v1",
        pin_method_resolution="2606.27369v1",
        selection_proposed_call_site="src/foo.py:12",
        selection_team_direction_signal="rfc_issue",
        selection_contract_match=True,
        selection_migration_cost="low",
        selection_external_arxiv_id=None,
        selection_external_query_used=None,
        selection_code_override_justification=None,
    )
    run._post_run_telemetry(res, _target())
    body = calls[0][1]
    assert body["preflight_decision"] == "ISSUE"
    assert body["audit_anchor"] == "reference"
    assert body["pin_method"] == "2606.27369v1"
    assert body["pin_method_resolution"] == "2606.27369v1"
    assert body["selection_proposed_call_site"] == "src/foo.py:12"
    assert body["selection_team_direction_signal"] == "rfc_issue"
    assert body["selection_contract_match"] is True
    assert body["selection_migration_cost"] == "low"
    # None-valued strings stay None in the payload (not "")
    assert body["selection_external_arxiv_id"] is None
    assert body["selection_external_query_used"] is None
    assert body["selection_code_override_justification"] is None


def test_long_text_fields_are_capped(monkeypatch):
    """Defense-in-depth: a misbehaving run can't bloat the row with
    multi-MB strings for any of the new text fields."""
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = _capture(monkeypatch)
    res = _result(
        selection_proposed_call_site="x" * 5000,
        selection_external_query_used="y" * 5000,
        selection_code_override_justification="z" * 5000,
        pr_body_rationale="r" * 5000,
        coverage_summary="c" * 5000,
    )
    run._post_run_telemetry(res, _target())
    body = calls[0][1]
    assert len(body["selection_proposed_call_site"]) == 512
    assert len(body["selection_external_query_used"]) == 512
    assert len(body["selection_code_override_justification"]) == 1024
    assert len(body["pr_body_rationale"]) == 2048
    assert len(body["coverage_summary"]) == 2048


def test_chain_outcomes_round_trip(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = _capture(monkeypatch)
    res = _result(
        chain=True,
        self_review="keep",
        needs_judgment=False,
        pr_body_updated=True,
        pr_body_rationale="convention pass rewrote the body to match repo style",
        draft_dropped=True,
        test_integration_gate="pass",
        tests_status="pass",
        test_status="pass",
        tests_touch_existing=True,
        stub_density=0.12,
        lint_status="clean",
        lint_issues=0,
        diff_risk_band="low",
        diff_risk_score=0.18,
        diff_risk_factors={"critical_files_touched": 0.0, "loc_delta": 0.18},
        coverage_summary="3/3 contract elements covered",
    )
    run._post_run_telemetry(res, _target())
    body = calls[0][1]
    assert body["chain"] is True
    assert body["self_review"] == "keep"
    assert body["needs_judgment"] is False
    assert body["pr_body_updated"] is True
    assert body["pr_body_rationale"].startswith("convention pass")
    assert body["draft_dropped"] is True
    assert body["test_integration_gate"] == "pass"
    assert body["tests_status"] == "pass"
    assert body["tests_touch_existing"] is True
    assert body["stub_density"] == 0.12
    assert body["lint_status"] == "clean"
    assert body["lint_issues"] == 0
    assert body["diff_risk_band"] == "low"
    assert body["diff_risk_score"] == 0.18
    assert body["diff_risk_factors"]["critical_files_touched"] == 0.0
    assert "3/3 contract elements" in body["coverage_summary"]


def test_integration_violations_are_compacted(monkeypatch):
    """Verbose violation lists are bounded so a scaffolding-heavy run
    can't bloat the row."""
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = _capture(monkeypatch)
    long_violation = "v" * 500
    res = _result(integration_violations=[long_violation] * 100)
    run._post_run_telemetry(res, _target())
    body = calls[0][1]
    # Capped at 50 entries, each truncated to 300 chars
    assert len(body["integration_violations"]) == 50
    assert all(len(v) == 300 for v in body["integration_violations"])


def test_integration_violations_absent_is_null(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = _capture(monkeypatch)
    run._post_run_telemetry(_result(), _target())
    assert calls[0][1]["integration_violations"] is None


def test_issue_convention_fields_round_trip(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = _capture(monkeypatch)
    res = _result(
        picked_template="feature-request.md",
        templates_eligible=1,
        templates_filtered_kinds=["bug", "question"],
        templates_found=4,
        existing_issue_state="open",
        existing_issue_url="https://github.com/remyxai/example/issues/3",
    )
    run._post_run_telemetry(res, _target())
    body = calls[0][1]
    assert body["picked_template"] == "feature-request.md"
    assert body["templates_eligible"] == 1
    assert body["templates_filtered_kinds"] == ["bug", "question"]
    assert body["templates_found"] == 4
    assert body["existing_issue_state"] == "open"
    assert body["existing_issue_url"].endswith("/issues/3")


def test_file_touch_telemetry_round_trip(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = _capture(monkeypatch)
    res = _result(
        files_touched=5,
        touched_py_files=3,
        touched_test_files=2,
        files_dropped_out_of_scope=1,
        package_manager="uv",
        deps_installed=True,
        deps_install_summary="14 packages installed in 8s",
    )
    run._post_run_telemetry(res, _target())
    body = calls[0][1]
    assert body["files_touched"] == 5
    assert body["touched_py_files"] == 3
    assert body["touched_test_files"] == 2
    assert body["files_dropped_out_of_scope"] == 1
    assert body["package_manager"] == "uv"
    assert body["deps_installed"] is True
    assert "14 packages" in body["deps_install_summary"]


def test_paper_metadata_round_trip(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = _capture(monkeypatch)
    res = _result(
        arxiv_id="2402.00025v2",
        upstream_repo="https://github.com/foo/SplitK-kernel",
        paper_license="Apache-2.0",
        license_class="permissive",
        license_compat=1.0,
        reference_url="https://arxiv.org/abs/2402.00025v2",
    )
    run._post_run_telemetry(res, _target())
    body = calls[0][1]
    assert body["arxiv_id"] == "2402.00025v2"
    assert body["upstream_repo"].endswith("SplitK-kernel")
    assert body["paper_license"] == "Apache-2.0"
    assert body["license_class"] == "permissive"
    assert body["license_compat"] == 1.0
    assert body["reference_url"].endswith("2402.00025v2")


def test_new_fields_default_to_null_on_skipped_run(monkeypatch):
    """A bare skip (only status) doesn't populate any of the new fields;
    they must all serialize as None so the engine sees clean nulls."""
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = _capture(monkeypatch)
    run._post_run_telemetry({"status": "skipped_rate_limit"}, _target())
    body = calls[0][1]
    null_fields = [
        "agent", "model_backend", "cost_basis",
        "envelopes_without_usage", "num_turns", "cache_read_input_tokens",
        "preflight_decision", "audit_anchor", "pin_method",
        "selection_proposed_call_site", "selection_external_arxiv_id",
        "chain", "self_review", "draft_dropped", "test_integration_gate",
        "stub_density", "integration_violations", "diff_risk_band",
        "diff_risk_factors", "coverage_summary",
        "picked_template", "templates_filtered_kinds", "existing_issue_state",
        "files_touched", "package_manager", "deps_installed",
        "arxiv_id", "upstream_repo", "paper_license", "license_compat",
    ]
    for field in null_fields:
        assert body[field] is None, f"expected {field} to be None on a skip run"


def test_compact_string_list_helper_caps_entries_and_chars():
    out = run._compact_string_list_for_telemetry(["x" * 1000] * 100)
    assert len(out) == 50
    assert all(len(s) == 300 for s in out)


def test_compact_string_list_helper_returns_none_for_none():
    assert run._compact_string_list_for_telemetry(None) is None


def test_compact_string_list_helper_returns_none_for_non_list():
    # Defensive: a misbehaving caller passing a dict / string should not
    # produce a malformed payload — engine sees null instead.
    assert run._compact_string_list_for_telemetry({"x": 1}) is None  # type: ignore[arg-type]
    assert run._compact_string_list_for_telemetry("abc") is None  # type: ignore[arg-type]


def test_compact_string_list_helper_passes_short_lists_through():
    out = run._compact_string_list_for_telemetry(["foo", "bar"])
    assert out == ["foo", "bar"]
