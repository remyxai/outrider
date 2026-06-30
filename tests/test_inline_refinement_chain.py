"""Tests for the inline refinement chain.

After recommend mode files a draft PR, the same run.py invocation continues
sequentially into the chain — fidelity audit → convention pass → test gate —
on the just-opened PR, so the chain runs by default without the customer
deploying the standalone outrider-fidelity/convention/test workflows.

Coverage:
  - `build_target_from_env` parses `INPUT_CHAIN` into `Target.chain_enabled`
    (default on; the usual falsey spellings opt out).
  - `run_refinement_chain` runs all three phases when fidelity audits, and
    short-circuits when fidelity skips/fails. It sets INPUT_PR_NUMBER so the
    phase runners (which read it from the env) target the right PR.
  - `main()`'s recommend path invokes the chain only when `chain_enabled` is
    set AND a PR actually opened.

Run with: pytest tests/test_inline_refinement_chain.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


def _base_env(monkeypatch, **overrides):
    """Minimal env for build_target_from_env / main(); clears chain-related
    vars so each test controls them explicitly."""
    for var in (
        "INPUT_CHAIN", "INPUT_PR_NUMBER", "REMYX_MODE", "INPUT_MODE",
        "GITHUB_OUTPUT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TARGET_REPO", "owner/repo")
    monkeypatch.setenv("INPUT_INTEREST_ID", "11111111-1111-1111-1111-111111111111")
    # Real customer workflows always set an API key; the startup auth
    # check would hard-fail otherwise. Tests that want to exercise the
    # auth-check failure path do so explicitly elsewhere.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fakebutlongenough")
    for var, value in overrides.items():
        monkeypatch.setenv(var, value)


# ─── build_target_from_env: INPUT_CHAIN → chain_enabled ─────────────────────


def test_chain_enabled_defaults_true(monkeypatch):
    _base_env(monkeypatch)
    target = run.build_target_from_env()
    assert target.chain_enabled is True


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off", "OFF"])
def test_chain_disabled_falsey_spellings(monkeypatch, value):
    _base_env(monkeypatch, INPUT_CHAIN=value)
    target = run.build_target_from_env()
    assert target.chain_enabled is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "anything"])
def test_chain_enabled_truthy_spellings(monkeypatch, value):
    _base_env(monkeypatch, INPUT_CHAIN=value)
    target = run.build_target_from_env()
    assert target.chain_enabled is True


# ─── run_refinement_chain: phase sequencing + gating ────────────────────────


def _record_phases(monkeypatch, fidelity_status):
    """Stub the three phase runners; return the call-order list. Convention
    and test report fixed terminal statuses."""
    calls = []

    def fake_fidelity(target):
        calls.append(("fidelity", run.os.environ.get("INPUT_PR_NUMBER")))
        return {"status": fidelity_status}

    def fake_convention(target):
        calls.append(("convention", run.os.environ.get("INPUT_PR_NUMBER")))
        return {"status": "convention_aligned"}

    def fake_test(target):
        calls.append(("test", run.os.environ.get("INPUT_PR_NUMBER")))
        return {"status": "test_passed", "draft_dropped": True}

    monkeypatch.setattr(run, "run_fidelity_audit", fake_fidelity)
    monkeypatch.setattr(run, "run_convention_pass", fake_convention)
    monkeypatch.setattr(run, "run_test_gate", fake_test)
    return calls


def test_chain_runs_all_phases_when_fidelity_audits(monkeypatch):
    _base_env(monkeypatch)
    calls = _record_phases(monkeypatch, "fidelity_audited")
    chain = run.run_refinement_chain(run.Target(repo="owner/repo"), 42)

    assert [c[0] for c in calls] == ["fidelity", "convention", "test"]
    # Every phase saw the PR number via INPUT_PR_NUMBER.
    assert all(prn == "42" for _, prn in calls)
    assert chain == {
        "pr_number": 42,
        "fidelity_status": "fidelity_audited",
        "convention_status": "convention_aligned",
        "test_status": "test_passed",
        "draft_dropped": True,
    }


def test_chain_runs_all_phases_on_needs_judgment(monkeypatch):
    # `fidelity_audited_needs_judgment` is still an audited state — chain
    # continues (prefix match).
    _base_env(monkeypatch)
    calls = _record_phases(monkeypatch, "fidelity_audited_needs_judgment")
    run.run_refinement_chain(run.Target(repo="owner/repo"), 7)
    assert [c[0] for c in calls] == ["fidelity", "convention", "test"]


@pytest.mark.parametrize("audited_status", [
    "fidelity_audited_paper_anchored",
    "fidelity_audited_paper_anchored_needs_judgment",
])
def test_chain_runs_all_phases_for_paper_anchored_audit(monkeypatch, audited_status):
    # Phase A's paper-anchored degraded mode still produces an audited
    # state — B + C must run. Without this the chain bails whenever a
    # paper lacks a public reference impl (observed on NeMo-Curator
    # PR #4 during v1.6.15 validation).
    _base_env(monkeypatch)
    calls = _record_phases(monkeypatch, audited_status)
    run.run_refinement_chain(run.Target(repo="owner/repo"), 9)
    assert [c[0] for c in calls] == ["fidelity", "convention", "test"]


@pytest.mark.parametrize("skip_status", [
    "fidelity_skipped_no_reference",
    "fidelity_skipped_not_bot",
    "fidelity_failed_clone",
])
def test_chain_short_circuits_when_fidelity_does_not_audit(monkeypatch, skip_status):
    _base_env(monkeypatch)
    calls = _record_phases(monkeypatch, skip_status)
    chain = run.run_refinement_chain(run.Target(repo="owner/repo"), 42)

    assert [c[0] for c in calls] == ["fidelity"]  # convention/test never ran
    assert chain == {"pr_number": 42, "fidelity_status": skip_status}


# ─── main(): recommend-mode continuation gated by chain_enabled ─────────────


def _stub_main_tail(monkeypatch):
    """No-op the heavy side-effects so main() can run headless."""
    monkeypatch.setattr(run, "_write_step_summary", lambda result: None)
    monkeypatch.setattr(run, "_post_run_telemetry", lambda result, target: None)


def _make_recommend_main(monkeypatch, *, chain_enabled, process_result):
    _stub_main_tail(monkeypatch)
    target = run.Target(repo="owner/repo", chain_enabled=chain_enabled)
    monkeypatch.setattr(run, "build_target_from_env", lambda: target)
    monkeypatch.setattr(run, "process_target", lambda t: dict(process_result))

    invoked = []
    monkeypatch.setattr(
        run, "run_refinement_chain",
        lambda t, pr: invoked.append(pr) or {"pr_number": pr},
    )
    return invoked


def test_main_invokes_chain_when_enabled_and_pr_opened(monkeypatch):
    _base_env(monkeypatch)
    invoked = _make_recommend_main(
        monkeypatch, chain_enabled=True,
        process_result={"repo": "owner/repo", "status": "pr_opened", "pr_number": 99},
    )
    run.main()
    assert invoked == [99]


def test_main_invokes_chain_on_draft_pr(monkeypatch):
    # pr_opened_draft also starts with "pr_opened" — chain should run.
    _base_env(monkeypatch)
    invoked = _make_recommend_main(
        monkeypatch, chain_enabled=True,
        process_result={"repo": "owner/repo", "status": "pr_opened_draft", "pr_number": 5},
    )
    run.main()
    assert invoked == [5]


def test_main_skips_chain_when_disabled(monkeypatch):
    _base_env(monkeypatch)
    invoked = _make_recommend_main(
        monkeypatch, chain_enabled=False,
        process_result={"repo": "owner/repo", "status": "pr_opened", "pr_number": 99},
    )
    run.main()
    assert invoked == []


def test_main_skips_chain_when_no_pr_opened(monkeypatch):
    # An Issue-routed run without an issue_number must not trigger
    # either the PR-route or the Issue-route chain, since there's
    # nothing to operate on.
    _base_env(monkeypatch)
    invoked = _make_recommend_main(
        monkeypatch, chain_enabled=True,
        process_result={"repo": "owner/repo", "status": "issue_opened"},
    )
    issue_invoked = []
    monkeypatch.setattr(
        run, "run_issue_convention_pass",
        lambda t: issue_invoked.append(run.os.environ.get("INPUT_ISSUE_NUMBER")) or {"status": "issue_convention_aligned"},
    )
    run.main()
    assert invoked == []
    assert issue_invoked == []


def test_main_invokes_issue_convention_when_issue_opened(monkeypatch):
    # When recommend mode opens an Issue with a known issue_number,
    # the inline dispatcher invokes run_issue_convention_pass with
    # INPUT_ISSUE_NUMBER set to the just-opened Issue.
    _base_env(monkeypatch)
    invoked = _make_recommend_main(
        monkeypatch, chain_enabled=True,
        process_result={
            "repo": "owner/repo",
            "status": "issue_opened_preflight",
            "issue_number": 17,
        },
    )
    issue_invoked = []
    monkeypatch.setattr(
        run, "run_issue_convention_pass",
        lambda t: issue_invoked.append(run.os.environ.get("INPUT_ISSUE_NUMBER")) or {"status": "issue_convention_aligned"},
    )
    run.main()
    assert invoked == []  # PR-route chain not called for Issue-route artifact
    assert issue_invoked == ["17"]  # Issue-route convention pass called with the issue number


def test_main_skips_issue_convention_when_chain_disabled(monkeypatch):
    _base_env(monkeypatch)
    invoked = _make_recommend_main(
        monkeypatch, chain_enabled=False,
        process_result={
            "repo": "owner/repo",
            "status": "issue_opened_substitution",
            "issue_number": 8,
        },
    )
    issue_invoked = []
    monkeypatch.setattr(
        run, "run_issue_convention_pass",
        lambda t: issue_invoked.append(t.repo) or {"status": "issue_convention_aligned"},
    )
    run.main()
    assert invoked == []
    assert issue_invoked == []
