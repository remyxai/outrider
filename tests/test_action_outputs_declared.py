"""The action exposes what run.py writes, and prices what it defaults to.

A composite action only surfaces the outputs its ``outputs:`` block declares.
run.py wrote chain_* / pr_number / candidates_considered to $GITHUB_OUTPUT
while action.yml declared none of them, so a documented
``steps.<id>.outputs.chain_test_status`` gate read an empty string. These
tests pin the two files together, and pin the z.ai default model to a rate
row so the default install's cost figure is priced off its own card.
"""
import os
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import run  # noqa: E402

ACTION = yaml.safe_load((ROOT / "action.yml").read_text())
RUN_SRC = (ROOT / "src" / "run.py").read_text()


def _names_written_to_github_output() -> set[str]:
    """Every ``<name>=`` that main()'s $GITHUB_OUTPUT block writes."""
    start = RUN_SRC.index('github_output = os.environ.get("GITHUB_OUTPUT")')
    end = RUN_SRC.index("Could not write to $GITHUB_OUTPUT", start)
    block = RUN_SRC[start:end]
    return set(re.findall(r'f\.write\(\s*f?"([a-z_]+)=', block))


def test_every_output_run_py_writes_is_declared():
    written = _names_written_to_github_output()
    assert written, "could not find the $GITHUB_OUTPUT block in run.py"
    declared = set(ACTION["outputs"])
    missing = sorted(written - declared)
    assert not missing, (
        "written to $GITHUB_OUTPUT but not declared in action.yml outputs "
        f"(callers read an empty string): {missing}"
    )


def test_every_declared_output_is_wired_to_the_recommend_step():
    for name, spec in ACTION["outputs"].items():
        assert spec.get("value") == "${{ steps.recommend.outputs.%s }}" % name, name
        assert spec.get("description"), f"{name} has no description"
    step_ids = [s.get("id") for s in ACTION["runs"]["steps"]]
    assert "recommend" in step_ids


@pytest.mark.parametrize("name", [
    "chain_fidelity_status", "chain_convention_status", "chain_test_status",
    "chain_draft_dropped", "pr_number", "issue_number",
    "candidates_considered", "selection_rejected",
])
def test_the_outputs_the_docs_promise_exist(name):
    assert name in ACTION["outputs"]


def test_issue_number_reaches_github_output():
    """issue_number is on every issue_opened* result; it reaches the
    runner alongside issue_url instead of only the JSON log line."""
    assert "issue_number" in _names_written_to_github_output()


def test_mode_description_names_every_dispatchable_mode():
    desc = ACTION["inputs"]["mode"]["description"]
    for mode in run._MODES:
        assert f'"{mode}"' in desc, f"mode input description omits {mode!r}"


def test_cost_output_is_not_described_as_anthropic_only():
    for name in ("cost_usd", "input_tokens", "output_tokens"):
        desc = ACTION["outputs"][name]["description"]
        assert "Anthropic" not in desc and "Claude" not in desc, name


def test_pin_arxiv_description_does_not_require_the_pool():
    assert "present in the candidate pool" not in ACTION["inputs"]["pin-arxiv"]["description"]


# ─── z.ai default model has a rate row ──────────────────────────────────────

def test_zai_default_model_has_a_rate_row():
    from agents import providers
    default = providers.PROVIDERS["zai"].default_model[providers.ApiFamily.ANTHROPIC_MESSAGES]
    assert default in run._BACKEND_RATES["api.z.ai"], (
        f"z.ai default {default!r} is priced off another model's card"
    )
    assert run._BACKEND_DEFAULT_MODEL["api.z.ai"] == default


def test_glm_5_3_rate_row_matches_the_published_card():
    assert run._BACKEND_RATES["api.z.ai"]["glm-5.3"] == (1.40, 4.40, 0.26)


def test_rate_basis_is_authoritative_for_the_default_zai_model():
    assert run._rate_basis("https://api.z.ai/api/anthropic", "glm-5.3") == "backend_rate_table"


# ─── brief mode does not need an interest ───────────────────────────────────

def _base_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("INPUT_") or k in ("REMYX_MODE",):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TARGET_REPO", "owner/name")


def test_interest_id_is_optional_in_brief_mode(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("INPUT_MODE", "brief")
    target = run.build_target_from_env()
    assert target.interest_id == ""


def test_interest_id_is_still_required_in_recommend_mode(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("INPUT_MODE", "recommend")
    with pytest.raises(SystemExit):
        run.build_target_from_env()


def test_interest_id_input_is_declared_optional():
    assert ACTION["inputs"]["interest-id"]["required"] is False
