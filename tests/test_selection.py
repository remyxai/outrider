"""Unit tests for the candidate selection pass (lookback pool → pick the
most implementable candidate).

Covers the pure logic added alongside the selection pass: envelope→
Recommendation mapping, candidate brief rendering, select_recommendation's
parse / range / failure handling (which all fall back to candidates[0]),
and the PR-body selection section.

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

# run.py lives in src/ and isn't an installable package; put it on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Target  # noqa: E402


def _make_candidates():
    geo = run._paper_to_recommendation(
        {
            "title": "GeoWeaver",
            "resource_id": "2605.22558v1",
            "relevance_score": 0.98,
            "reasoning": "geometric grounding — a VLM architecture",
            "interest_name": "VQASynth",
            "resource": {"abstract": "Spatio-temporal reasoning in VLMs..."},
        },
        fallback_interest_name="fallback",
        interest_context="team focus body",
        experiment_history="",
    )
    count = run._paper_to_recommendation(
        {
            "title": "HieraCount open-world counting",
            "resource_id": "2605.10887v1",
            "relevance_score": 0.87,
            "reasoning": "explicit counting granularity for prompt templates",
            "resource": {"abstract": "Open-world object counting remains brittle..."},
        },
        fallback_interest_name="VQASynth",
        interest_context="team focus body",
        experiment_history="",
    )
    return geo, count


def test_paper_to_recommendation_maps_fields():
    geo, _ = _make_candidates()
    assert geo.paper_title == "GeoWeaver"
    assert geo.arxiv_id == "2605.22558v1"
    assert geo.relevance_score == 0.98
    assert geo.tier == "high"
    assert geo.interest_name == "VQASynth"          # from the paper envelope
    assert geo.interest_context == "team focus body"


def test_paper_to_recommendation_fallbacks():
    rec = run._paper_to_recommendation({"title": "X"}, "FB", "", "")
    assert rec.interest_name == "FB"                # falls back when absent
    assert rec.arxiv_id == ""
    assert rec.paper_title == "X"
    assert rec.experiment_history == ""             # threaded through


def test_candidate_brief_is_indexed():
    geo, count = _make_candidates()
    brief = run._render_candidate_brief([geo, count])
    assert "[0] GeoWeaver" in brief
    assert "[1] HieraCount" in brief
    assert "relevance 0.98" in brief


def test_select_single_candidate_short_circuits(tmp_path):
    geo, _ = _make_candidates()
    # One candidate → no point spending a Claude call; returns None and the
    # caller uses candidates[0].
    assert run.select_recommendation(tmp_path, "pkg", [geo]) is None


# ─── _render_environment_hint: selection-time ENVIRONMENTS.md injection ──


def test_environment_hint_empty_when_no_env_body():
    """Runs without an ENVIRONMENTS.md attached — hint block is empty so
    the selection prompt is unchanged."""
    assert run._render_environment_hint("") == ""
    assert run._render_environment_hint("   \n\n  ") == ""


def test_environment_hint_wraps_env_body_when_present():
    """When ENVIRONMENTS.md is attached, the hint block introduces the
    workflow-attached tooling and includes the body."""
    body = "AST search via `ccc` is pre-installed. Prefer over Read on files >500 LOC."
    hint = run._render_environment_hint(body)
    assert "Workflow-attached tooling" in hint
    assert "ENVIRONMENTS.md" in hint
    assert "AST search via `ccc`" in hint


def test_select_prompt_substitutes_environment_hint(tmp_path, monkeypatch):
    """The selection prompt gets the env body substituted at the
    __ENVIRONMENT_HINT__ placeholder, so the selection agent sees
    workflow-attached tooling before it verifies candidates."""
    geo, count = _make_candidates()
    captured_prompt = {}

    def _fake_stream(wd, prompt, t, **kw):
        captured_prompt["text"] = prompt
        return (True, '{"chosen_index": 0, "reasoning": "x", "rejected": []}', [])

    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", _fake_stream)
    run.select_recommendation(
        tmp_path, "pkg", [geo, count],
        env_body="AST search via `ccc` is pre-installed.",
    )
    assert "__ENVIRONMENT_HINT__" not in captured_prompt["text"], "placeholder not substituted"
    assert "AST search via `ccc`" in captured_prompt["text"]
    assert "Workflow-attached tooling" in captured_prompt["text"]


# ─── path verification of selection reasoning (confabulation check) ───────


def test_extract_referenced_paths_basic():
    """Paths ending in .py or trailing slash are extracted; single-slash
    org/repo shapes and URLs are filtered out."""
    text = (
        "grep of unsloth/ + unsloth_cli/ for retrieval/MCP/agent = no hits. "
        "The reference impl at github.com/pilancilab/Riemannian_Preconditioned_LoRA "
        "and the module at routes/inference.py:_truncate_middle_messages both matter. "
        "The org/repo shape shouldn't match."
    )
    paths = run._extract_referenced_paths(text)
    assert "unsloth/" in paths
    assert "unsloth_cli/" in paths
    assert "routes/inference.py" in paths
    assert "retrieval/MCP/agent" in paths  # 2+ slashes → looks path-shaped
    # github.com URL should be filtered
    assert not any("github.com" in p for p in paths)
    # single-slash org/repo shape filtered
    assert "org/repo" not in paths


def test_extract_referenced_paths_empty():
    assert run._extract_referenced_paths("") == []
    assert run._extract_referenced_paths(None) == []


def test_verify_paths_in_workdir_splits_verified_and_not_found(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("")
    (tmp_path / "existing_dir").mkdir()
    result = run._verify_paths_in_workdir(
        tmp_path, ["src/foo.py", "existing_dir/", "made_up/", "nowhere/bar.py"]
    )
    assert "src/foo.py" in result["verified"]
    assert "existing_dir/" in result["verified"]
    assert "made_up/" in result["not_found"]
    assert "nowhere/bar.py" in result["not_found"]
    assert len(result["cited"]) == 4


def test_check_selection_paths_catches_confabulation(tmp_path):
    """The exact reasoning shape that caused today's unsloth-retry misfire:
    grepped nonexistent directories, concluded 'no such code exists'.
    Path check flags 0 of 2 verified — an operator can spot this."""
    # Workdir simulates smellslikeml/unsloth's actual shape (studio/, no unsloth/)
    (tmp_path / "studio").mkdir()
    (tmp_path / "studio" / "backend").mkdir()
    reasoning = "grep of unsloth/ + unsloth_cli/ for X = no hits"
    check = run._check_selection_paths(tmp_path, reasoning)
    assert check["cited"] == ["unsloth/", "unsloth_cli/"]
    assert check["verified"] == []
    assert set(check["not_found"]) == {"unsloth/", "unsloth_cli/"}


def test_check_selection_paths_verifies_real_paths(tmp_path):
    """The opik-SAFARI shape: reasoning cites a real path, verification
    finds it — an operator sees the reasoning is grounded."""
    (tmp_path / "sdks").mkdir()
    (tmp_path / "sdks" / "python").mkdir()
    (tmp_path / "sdks" / "python" / "opik").mkdir(parents=True, exist_ok=True)
    (tmp_path / "suite_evaluators").mkdir()
    (tmp_path / "suite_evaluators" / "agentic").mkdir()
    reasoning = "Opik's suite_evaluators/agentic/ evaluator IS SAFARI's architecture"
    check = run._check_selection_paths(tmp_path, reasoning)
    assert "suite_evaluators/agentic/" in check["verified"]
    assert not check["not_found"]


def test_select_prompt_without_env_body_has_no_hint_block(tmp_path, monkeypatch):
    """Backwards-compat: runs with no env_body render the prompt without
    the hint section, no dangling placeholder."""
    geo, count = _make_candidates()
    captured_prompt = {}

    def _fake_stream(wd, prompt, t, **kw):
        captured_prompt["text"] = prompt
        return (True, '{"chosen_index": 0, "reasoning": "x", "rejected": []}', [])

    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", _fake_stream)
    run.select_recommendation(tmp_path, "pkg", [geo, count])
    assert "__ENVIRONMENT_HINT__" not in captured_prompt["text"]
    assert "Workflow-attached tooling" not in captured_prompt["text"]


def test_select_parses_chosen_index(tmp_path, monkeypatch):
    geo, count = _make_candidates()
    # The selection pass runs in streaming mode so it can parse the tool
    # transcript into coverage; the runner returns `(ok, text, events)`.
    # Empty events here.
    monkeypatch.setattr(
        run, "_run_claude_oneshot_streaming",
        lambda wd, p, t, **kw: (True, '{"chosen_index": 1, "reasoning": "clear call '
                                      'site in prompts.py", "rejected": [{"index": 0, '
                                      '"why": "model architecture, no call site"}]}', []),
    )
    sel = run.select_recommendation(tmp_path, "pkg", [geo, count])
    assert sel is not None
    assert sel["chosen_index"] == 1                 # picked the lower-ranked, implementable one
    assert "call site" in sel["reasoning"]
    assert sel["rejected"][0]["index"] == 0


def test_select_attaches_coverage_telemetry(tmp_path, monkeypatch):
    """Every parseable verdict carries selection_coverage +
    selection_context_efficiency, computed from the transcript."""
    geo, count = _make_candidates()
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": 'gh search code "load_dataset" --repo o/r'}},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "Bash",
             "input": {"command": "gh api repos/o/r/contents/src/x.py"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t2",
             "content": "\n".join(f"line{i}" for i in range(200))},
        ]}},
    ]
    monkeypatch.setattr(
        run, "_run_claude_oneshot_streaming",
        lambda wd, p, t, **kw: (
            True,
            '{"chosen_index": 1, "reasoning": "verified at src/x.py:10", '
            '"rejected": []}',
            events,
        ),
    )
    sel = run.select_recommendation(tmp_path, "pkg", [geo, count])
    assert sel is not None
    cov = sel["selection_coverage"]
    assert cov["searches"] == 1
    assert cov["file_reads"] == 1
    assert cov["visible_lines"] == 200
    assert cov["under_explored"] is False           # 200 ≥ 150 in-pool floor
    assert sel["selection_context_efficiency"] == round(1 / 200, 4)


def test_select_out_of_range_falls_back(tmp_path, monkeypatch):
    geo, count = _make_candidates()
    monkeypatch.setattr(run, "_run_claude_oneshot_streaming",
                        lambda wd, p, t, **kw: (True, '{"chosen_index": 9}', []))
    # Out-of-range → None → caller falls back to candidates[0].
    assert run.select_recommendation(tmp_path, "pkg", [geo, count]) is None


def test_select_non_int_index_falls_back(tmp_path, monkeypatch):
    geo, count = _make_candidates()
    monkeypatch.setattr(run, "_run_claude_oneshot_streaming",
                        lambda wd, p, t, **kw: (True, '{"chosen_index": "two"}', []))
    assert run.select_recommendation(tmp_path, "pkg", [geo, count]) is None


def test_select_claude_failure_falls_back(tmp_path, monkeypatch):
    geo, count = _make_candidates()
    monkeypatch.setattr(run, "_run_claude_oneshot_streaming",
                        lambda wd, p, t, **kw: (False, "claude CLI timed out", []))
    assert run.select_recommendation(tmp_path, "pkg", [geo, count]) is None


def test_select_unparseable_output_falls_back(tmp_path, monkeypatch):
    """Prose on both the initial call AND the format-only retry → fall
    through to None. Caller's job to substitute a fallback candidate."""
    geo, count = _make_candidates()
    monkeypatch.setattr(run, "_run_claude_oneshot_streaming",
                        lambda wd, p, t, **kw: (True, "I think candidate 1 is best", []))
    assert run.select_recommendation(tmp_path, "pkg", [geo, count]) is None


# ─── _fallback_candidate: license_compat as tiebreaker (REMYX-169) ────────


def _cand(title: str, relevance: float, license_compat: float) -> "run.Recommendation":
    """Minimal Recommendation for fallback-tiebreaker tests."""
    rec = run._paper_to_recommendation(
        {"title": title, "resource_id": f"9999.{hash(title) & 0xFFFF:04x}v1",
         "relevance_score": relevance},
        fallback_interest_name="fb", interest_context="", experiment_history="",
    )
    rec.license_compat = license_compat
    return rec


def test_fallback_picks_highest_relevance():
    """Baseline behavior: highest-relevance candidate wins regardless of
    list order."""
    low = _cand("Low", 0.60, license_compat=1.00)
    high = _cand("High", 0.95, license_compat=0.30)
    # `high` last in list — old `viable[0]` bug would pick `low`. max() picks
    # `high` by relevance.
    assert run._fallback_candidate([low, high]) is high


def test_fallback_breaks_relevance_ties_by_license_compat():
    """When two candidates share the top relevance, prefer the one with
    higher license_compat (permissive+code-link > no-code-link)."""
    no_code = _cand("No code link", 0.99, license_compat=0.30)
    permissive = _cand("Apache-2.0 with gh source", 0.99, license_compat=1.00)
    # Order the no-code-link candidate FIRST — old max() would pick it by
    # order-of-arrival luck. With the fix, permissive wins on tiebreaker.
    assert run._fallback_candidate([no_code, permissive]) is permissive
    # And symmetric — order shouldn't matter.
    assert run._fallback_candidate([permissive, no_code]) is permissive


def test_fallback_tiebreak_does_not_override_relevance():
    """A slightly higher relevance beats a lower relevance even if the
    lower has better license_compat — license_compat is TIEBREAK only."""
    lower_relevance_permissive = _cand("perm", 0.98, license_compat=1.00)
    higher_relevance_no_code = _cand("no code", 0.99, license_compat=0.30)
    assert (
        run._fallback_candidate(
            [lower_relevance_permissive, higher_relevance_no_code]
        )
        is higher_relevance_no_code
    )


def test_fallback_all_equal_falls_back_to_list_order():
    """When both relevance and license_compat tie, list order is fine —
    the candidates are genuinely equivalent by the ranking we care about."""
    a = _cand("a", 0.99, license_compat=1.00)
    b = _cand("b", 0.99, license_compat=1.00)
    # Whichever appears first wins — that's max()'s documented behavior on
    # ties, and we're OK with it once the license tiebreak is settled.
    assert run._fallback_candidate([a, b]) is a
    assert run._fallback_candidate([b, a]) is b


def test_select_unparseable_initial_then_clean_retry(tmp_path, monkeypatch):
    """The model finishes reasoning out loud on the first attempt; the
    format-only retry then emits the JSON. Should succeed (preserving
    the model's real pick) instead of falling through to a fallback."""
    geo, count = _make_candidates()
    calls = {"n": 0}

    def fake_oneshot(wd, p, t, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return True, ("I now have enough evidence to decide. Let me "
                          "consolidate the maintainer's stated preferences "
                          "with the candidate pool ..."), []
        # Second call gets the format reminder appended → returns clean JSON.
        assert "OUTPUT FORMAT REMINDER" in p
        return True, ('{"chosen_index": 1, "reasoning": "matches an open RFC", '
                      '"rejected": []}'), []

    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", fake_oneshot)
    sel = run.select_recommendation(tmp_path, "pkg", [geo, count])
    assert calls["n"] == 2, "retry should have fired exactly once"
    assert sel is not None
    assert sel["chosen_index"] == 1
    assert "matches an open RFC" in sel["reasoning"]


def test_select_retry_fires_only_once(tmp_path, monkeypatch):
    """If both the initial call AND the retry return prose, we must
    not loop — fall through after exactly two attempts."""
    geo, count = _make_candidates()
    calls = {"n": 0}

    def fake_oneshot(wd, p, t, **kw):
        calls["n"] += 1
        return True, "still just prose, no JSON here", []

    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", fake_oneshot)
    assert run.select_recommendation(tmp_path, "pkg", [geo, count]) is None
    assert calls["n"] == 2, "should call exactly twice (initial + 1 retry)"


def test_select_retry_skipped_when_first_call_fails(tmp_path, monkeypatch):
    """If the initial Claude call returns ok=False (timeout / CLI gone),
    don't waste another call on a retry — the failure isn't a parse
    issue, it's an infra one. Fall through immediately."""
    geo, count = _make_candidates()
    calls = {"n": 0}

    def fake_oneshot(wd, p, t, **kw):
        calls["n"] += 1
        return False, "claude CLI timed out", []

    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", fake_oneshot)
    assert run.select_recommendation(tmp_path, "pkg", [geo, count]) is None
    assert calls["n"] == 1, "no retry on ok=False — that's not a parse problem"


def test_select_retry_handles_second_call_infra_failure(tmp_path, monkeypatch):
    """Initial parse fails → retry fires → retry's Claude call itself
    fails (ok=False) → fall through cleanly without crashing."""
    geo, count = _make_candidates()
    calls = {"n": 0}

    def fake_oneshot(wd, p, t, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return True, "first attempt prose, no JSON", []
        return False, "claude CLI crashed on retry", []

    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", fake_oneshot)
    assert run.select_recommendation(tmp_path, "pkg", [geo, count]) is None
    assert calls["n"] == 2


def test_pr_body_includes_selection_note_when_present():
    geo, _ = _make_candidates()
    tgt = Target(repo="remyxai/VQASynth", interest_id="x")
    body = run.build_pr_body(tgt, geo, True, "ok",
                             selection_note="picked for a clear call site in prompts.py")
    assert "Why this candidate" in body
    assert "clear call site" in body


def test_pr_body_suppresses_parenthetical_fallback_note():
    geo, _ = _make_candidates()
    tgt = Target(repo="remyxai/VQASynth", interest_id="x")
    # The fallback note ("(selection pass unavailable …)") starts with "(" and
    # is suppressed so the PR body doesn't show a non-explanation.
    body = run.build_pr_body(
        tgt, geo, True, "ok",
        selection_note="(selection pass unavailable — used top-ranked candidate)",
    )
    assert "Why this candidate" not in body


def test_pr_body_without_selection_note():
    geo, _ = _make_candidates()
    tgt = Target(repo="remyxai/VQASynth", interest_id="x")
    body = run.build_pr_body(tgt, geo, True, "ok")
    assert "Why this candidate" not in body


def test_spec_bundle_threads_selection_rationale(tmp_path):
    # The selection rationale must land in SPEC.md so pre-flight and the
    # implementer see the same scoped framing the selection pass reasoned
    # about (rather than re-deriving from the abstract).
    geo, _ = _make_candidates()
    tgt = Target(repo="remyxai/VQASynth", interest_id="x")
    run.write_spec_bundle(
        tmp_path, tgt, geo, "vqasynth",
        selection_note="Implementable subset: load the released benchmark and "
                       "score via existing inference; call sites benchmarks.py / "
                       "evaluation.py.",
    )
    spec = (tmp_path / ".remyx-recommendation" / "SPEC.md").read_text()
    assert "How this maps onto your repo (candidate selection)" in spec
    assert "call sites benchmarks.py" in spec


def test_spec_bundle_neutral_note_on_fallback(tmp_path):
    geo, _ = _make_candidates()
    tgt = Target(repo="remyxai/VQASynth", interest_id="x")
    run.write_spec_bundle(
        tmp_path, tgt, geo, "vqasynth",
        selection_note="(selection pass unavailable — used top-ranked candidate)",
    )
    spec = (tmp_path / ".remyx-recommendation" / "SPEC.md").read_text()
    assert "no separate selection rationale" in spec


# ─── selection timeout inherits target.claude_timeout_s ────────────────


def test_selection_timeout_defaults_to_target_claude_timeout_s(tmp_path, monkeypatch):
    """When REMYX_SELECTION_TIMEOUT_S is unset and no explicit timeout_s
    is passed, selection uses target.claude_timeout_s — so a customer
    who bumped `claude-timeout` for a slower backend gets the same
    headroom on the agentic selection pass. Same pattern as preflight
    (v1.6.28) and audit (v1.6.29)."""
    monkeypatch.delenv("REMYX_SELECTION_TIMEOUT_S", raising=False)
    geo, count = _make_candidates()
    captured = {}

    def fake_streaming(wd, prompt, timeout_s, max_turns=None):
        captured["timeout_s"] = timeout_s
        return (True, '{"chosen_index": 0, "reasoning": "r"}', [])

    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", fake_streaming)
    target = Target(repo="example/repo", interest_id="iid", claude_timeout_s=1500)
    run.select_recommendation(tmp_path, "pkg", [geo, count], target=target)
    assert captured["timeout_s"] == 1500


def test_selection_timeout_env_var_overrides_target(tmp_path, monkeypatch):
    """REMYX_SELECTION_TIMEOUT_S, if set, takes precedence — escape
    hatch for CI scenarios that want a tighter selection ceiling while
    leaving implementation timeout large."""
    monkeypatch.setenv("REMYX_SELECTION_TIMEOUT_S", "300")
    geo, count = _make_candidates()
    captured = {}

    def fake_streaming(wd, prompt, timeout_s, max_turns=None):
        captured["timeout_s"] = timeout_s
        return (True, '{"chosen_index": 0, "reasoning": "r"}', [])

    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", fake_streaming)
    target = Target(repo="example/repo", interest_id="iid", claude_timeout_s=1500)
    run.select_recommendation(tmp_path, "pkg", [geo, count], target=target)
    assert captured["timeout_s"] == 300


def test_selection_timeout_falls_back_to_480_without_target(tmp_path, monkeypatch):
    """Test/ad-hoc callers that don't pass a target keep the legacy
    480s default — preserves backwards compatibility for direct callers."""
    monkeypatch.delenv("REMYX_SELECTION_TIMEOUT_S", raising=False)
    geo, count = _make_candidates()
    captured = {}

    def fake_streaming(wd, prompt, timeout_s, max_turns=None):
        captured["timeout_s"] = timeout_s
        return (True, '{"chosen_index": 0, "reasoning": "r"}', [])

    monkeypatch.setattr(run, "_run_claude_oneshot_streaming", fake_streaming)
    run.select_recommendation(tmp_path, "pkg", [geo, count])  # no target
    assert captured["timeout_s"] == 480
