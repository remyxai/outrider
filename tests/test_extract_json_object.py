"""``_extract_json_object`` — pull a well-formed JSON object out of a
prose/fence-wrapped model response.

Direct unit coverage; the helper is called from every JSON-parsing site
in ``run.py`` (candidate selection, misalignment, PR body rewrite,
downgrade, self-review, pre-PR fidelity, extraction), so a regression
here can silently drop model output on any of those paths.

Regression origin: outrider#110 — a convention-pass on VQASynth PR #128
returned ``convention_failed_misalignment`` because the old
first-``{``/last-``}`` heuristic picked a stray ``{`` in prose before
the JSON envelope and produced an unparseable slice.

Run with: pytest tests/test_extract_json_object.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# ─── Positive: plain, fenced, wrapped ───────────────────────────────


def test_plain_json():
    """Response is exactly a JSON object — parse straight through."""
    assert run._extract_json_object('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_json_wrapped_in_prose():
    """Common case: a short prose lead-in before the JSON envelope."""
    s = 'Sure — here is the analysis: {"actions": [], "count": 0}'
    assert run._extract_json_object(s) == {"actions": [], "count": 0}


def test_fenced_json_block():
    """Model wraps output in a ``` ```json … ``` ``` fence."""
    s = 'Here you go:\n\n```json\n{"a": 1}\n```\n'
    assert run._extract_json_object(s) == {"a": 1}


def test_fenced_plain_block():
    """Fence with no language tag — also handled."""
    s = '```\n{"a": 1}\n```'
    assert run._extract_json_object(s) == {"a": 1}


def test_fenced_json_with_leading_and_trailing_prose():
    """Prose both before and after the fence — takes the fenced payload."""
    s = (
        "Sure, here is the analysis of the misalignments:\n\n"
        "```json\n"
        '{"actions": [{"id": 1, "verdict": "misaligned"}]}\n'
        "```\n\n"
        "Let me know if you'd like me to elaborate."
    )
    result = run._extract_json_object(s)
    assert result == {"actions": [{"id": 1, "verdict": "misaligned"}]}


# ─── Negative-space: prose with stray braces ────────────────────────


def test_prose_with_stray_open_brace_before_envelope():
    """Regression fixture (outrider#110): prose contains ``{title}``
    template placeholders BEFORE the actual JSON envelope. The old
    heuristic sliced from the wrong ``{`` and failed to parse."""
    s = (
        "The PR body currently reads `{title}: {summary}` but the "
        "canonical convention on this repo is different. Actions:\n\n"
        '{"actions": [{"description": "rewrite body"}]}'
    )
    result = run._extract_json_object(s)
    assert result == {"actions": [{"description": "rewrite body"}]}


def test_bracket_matching_survives_strings_with_braces():
    """The JSON envelope's own string values may contain ``{`` or ``}``
    (e.g. describing a template shape). Bracket matcher must respect
    string quoting so it doesn't count those as structural braces."""
    s = '{"description": "use the template `{title}` here", "ok": true}'
    result = run._extract_json_object(s)
    assert result == {
        "description": "use the template `{title}` here", "ok": True,
    }


def test_bracket_matching_survives_escaped_quotes_in_strings():
    """String contents containing escaped quotes shouldn't confuse the
    in-string tracker."""
    s = r'{"a": "she said \"hi\" then {x} appeared", "b": 1}'
    result = run._extract_json_object(s)
    assert result["a"] == 'she said "hi" then {x} appeared'
    assert result["b"] == 1


def test_pr_128_regression_shape():
    """Fixture mirroring the shape of the PR #128 misalignment call —
    lettered list items ``a. ... b. ... e. ...`` inside a JSON envelope
    with backtick-fenced markdown inside string values (`` `## Training`
    `` etc.). The old heuristic parsed nothing; the new one recovers
    the envelope."""
    s = (
        "Here are the actionable misalignments identified for the PR:\n\n"
        "```json\n"
        "{\n"
        '  "actions": [\n'
        '    {"id": "a", "description": "point at the training README"},\n'
        '    {"id": "e", "description": '
        '"Add a short README section (under the existing '
        "experiments/training area, or a new `## Training` / "
        "`## Fine-tuning` section) pointing at "
        "`experiments/train_qwen2_5_vl_3b/README.md` with a "
        'one-line summary and the quick-start command.", '
        '"files_likely_touched": ["README.md"]}\n'
        "  ]\n"
        "}\n"
        "```"
    )
    result = run._extract_json_object(s)
    assert result is not None
    assert "actions" in result
    ids = [a["id"] for a in result["actions"]]
    assert ids == ["a", "e"]
    assert result["actions"][1]["files_likely_touched"] == ["README.md"]


# ─── Falsy cases ────────────────────────────────────────────────────


def test_empty_string_returns_none():
    assert run._extract_json_object("") is None


def test_no_braces_returns_none():
    assert run._extract_json_object("plain prose, no braces here") is None


def test_unparseable_returns_none():
    """Non-JSON content wrapped in braces — should return None, not
    accidentally succeed."""
    assert run._extract_json_object("{ this is not JSON }") is None


def test_returns_none_when_only_arrays_present():
    """Contract: return a ``dict`` or ``None``. A response that only
    contains a JSON array (not wrapped in an object) is not what
    callers expect."""
    assert run._extract_json_object('[1, 2, 3]') is None


def test_prefers_first_valid_object_when_multiple_present():
    """If two candidate objects appear, return the first that parses.
    Callers only ever consume one; being deterministic here keeps
    behavior predictable."""
    s = '{"first": true} then {"second": true}'
    result = run._extract_json_object(s)
    assert result == {"first": True}
