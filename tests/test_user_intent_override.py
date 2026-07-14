"""Tests for the user-intent override on the discharge throttle.

The throttle drops candidates that already have an open PR or a prior
Outrider Issue on the paper. That's the right default for the classic
selection flow — Outrider shouldn't keep re-recommending what it already
pitched. But the refinement flow (Opus dispatch on an existing GLM draft
with `start-from-ref` + `lead-content`) short-circuits on
`skipped_pr_exists` because the open PR IS the thing we're refining.

The `_has_user_intent_override` helper widens the bypass to cover the
signals that only appear when the caller has explicit intent — matching
the post-selection intel-yaml re-pick guard's bypass set so both checks
in `process_target` treat "is this work already tracked" the same way.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Target  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    # Both env vars start unset so each test names its own override.
    monkeypatch.delenv("INPUT_START_FROM_REF", raising=False)
    monkeypatch.delenv("INPUT_LEAD_CONTENT", raising=False)


def _target(**overrides):
    kwargs = {"repo": "o/r", "interest_id": "iid"}
    kwargs.update(overrides)
    return Target(**kwargs)


def test_bare_target_no_override():
    assert run._has_user_intent_override(_target()) is False


def test_pin_arxiv_triggers_override():
    assert run._has_user_intent_override(_target(pin_arxiv="2606.25800v1")) is True


def test_search_method_triggers_override():
    assert run._has_user_intent_override(_target(search_method="riemannian LoRA")) is True


def test_start_from_ref_triggers_override(monkeypatch):
    # The refinement-flow signal that the previous release missed. Without
    # this, `gh workflow run ... -f start-from-ref=<branch>` short-circuits
    # at `skipped_pr_exists` because the branch already has an open PR.
    monkeypatch.setenv("INPUT_START_FROM_REF", "road-vla-refined-branch")
    assert run._has_user_intent_override(_target()) is True


def test_lead_content_triggers_override(monkeypatch):
    # The other refinement-flow signal — orchestrator handed the caller a
    # scoped experiment, so the "avoid re-recommending" throttle doesn't
    # apply (this is the mention-triggered comment path in the plan).
    monkeypatch.setenv("INPUT_LEAD_CONTENT", "Fix gap 1: softmax invariance.")
    assert run._has_user_intent_override(_target()) is True


def test_lead_content_url_form_also_triggers_override(monkeypatch):
    # Both raw text and Linear URLs pass through the connector router —
    # here we care that either shape signals intent to the throttle.
    monkeypatch.setenv(
        "INPUT_LEAD_CONTENT",
        "https://linear.app/remyx/issue/REMYX-239/road-vla-opus-refinement",
    )
    assert run._has_user_intent_override(_target()) is True


def test_whitespace_only_start_from_ref_does_not_trigger(monkeypatch):
    # `.strip()` guard — pure-whitespace env values shouldn't count as
    # explicit intent, matching the coding-session's own start_from_ref
    # handling later in the file.
    monkeypatch.setenv("INPUT_START_FROM_REF", "   \n\t  ")
    assert run._has_user_intent_override(_target()) is False


def test_whitespace_only_lead_content_does_not_trigger(monkeypatch):
    monkeypatch.setenv("INPUT_LEAD_CONTENT", "\n\n")
    assert run._has_user_intent_override(_target()) is False
