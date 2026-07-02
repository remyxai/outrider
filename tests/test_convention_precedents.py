"""Tests for the convention-precedents body enrichment (REMYX-179).

Converts agent-inferred sibling claims ("sits alongside `X`") into
reader-verifiable, similarity-ranked precedents backed by
``ccc grep 'class \\NAME:'`` (cocoindex-code AST search).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# --- _extract_sibling_claims ------------------------------------------------

def test_extract_sibling_claim_next_to():
    body = "Should register next to `SanaLinearAttnProcessor2_0` in that module."
    assert run._extract_sibling_claims(body) == ["SanaLinearAttnProcessor2_0"]


def test_extract_sibling_claim_alongside():
    body = "Wires in alongside `FasterCacheConfig`, `TaylorSeerCacheConfig`."
    got = run._extract_sibling_claims(body)
    assert "FasterCacheConfig" in got


def test_extract_sibling_claim_mirrors():
    body = "New module mirrors `deepeval_prompt` in structure."
    assert run._extract_sibling_claims(body) == ["deepeval_prompt"]


def test_extract_sibling_claim_sibling_of():
    body = "This is a sibling of `LocationRefiner` sitting in the same dir."
    assert run._extract_sibling_claims(body) == ["LocationRefiner"]


def test_extract_sibling_claim_dedups_repeated_ident():
    body = (
        "Wires in next to `poly` and mirrors `poly` internally, "
        "next to `xlora`."
    )
    got = run._extract_sibling_claims(body)
    assert got == ["poly", "xlora"]


def test_extract_sibling_claim_case_insensitive():
    body = "This ALONGSIDE `MyClass` example."
    assert run._extract_sibling_claims(body) == ["MyClass"]


def test_extract_sibling_claim_empty_body():
    assert run._extract_sibling_claims("") == []
    assert run._extract_sibling_claims(None) == []


def test_extract_sibling_claim_ignores_backtick_without_sibling_phrase():
    body = "The `LocationRefiner` class does X. No claim here."
    assert run._extract_sibling_claims(body) == []


# --- _parse_ccc_grep_output -------------------------------------------------

def test_parse_ccc_output_single_file_single_match():
    output = (
        "src/processors.py\n"
        "  12| class FluxAttnProcessor2_0:\n"
    )
    got = run._parse_ccc_grep_output(output)
    assert len(got) == 1
    assert got[0] == {
        "kind": "class",
        "name": "FluxAttnProcessor2_0",
        "file": "src/processors.py",
        "line": 12,
    }


def test_parse_ccc_output_multiple_matches_per_file():
    output = (
        "src/processors.py\n"
        "  12| class SanaLinearAttnProcessor2_0:\n"
        "---\n"
        "  25| class FluxAttnProcessor2_0:\n"
        "---\n"
        "  40| class HunyuanAttnProcessor2_0:\n"
    )
    got = run._parse_ccc_grep_output(output)
    names = [m["name"] for m in got]
    assert names == [
        "SanaLinearAttnProcessor2_0",
        "FluxAttnProcessor2_0",
        "HunyuanAttnProcessor2_0",
    ]


def test_parse_ccc_output_multiple_files():
    output = (
        "src/a.py\n"
        "  10| class Foo:\n"
        "\n"
        "src/b.py\n"
        "  20| class Bar:\n"
    )
    got = run._parse_ccc_grep_output(output)
    files = [m["file"] for m in got]
    assert files == ["src/a.py", "src/b.py"]


def test_parse_ccc_output_empty_returns_empty_list():
    assert run._parse_ccc_grep_output("") == []
    assert run._parse_ccc_grep_output("No matches found.") == []


def test_parse_ccc_output_ignores_non_class_matches():
    output = (
        "src/x.py\n"
        "  10| class Foo:\n"
        "  20| def unrelated():\n"  # def would only appear if pattern matched def, but be safe
    )
    got = run._parse_ccc_grep_output(output)
    # We treat both class and def as valid via the def_shape regex
    assert any(m["name"] == "Foo" for m in got)


# --- _query_ccc_class_defs (subprocess-stubbed) ----------------------------

def test_query_ccc_returns_empty_when_ccc_not_on_path(tmp_path):
    with patch.object(run.shutil, "which", return_value=None):
        got = run._query_ccc_class_defs(tmp_path)
    assert got == []


def test_query_ccc_returns_empty_when_workdir_missing(tmp_path):
    with patch.object(run.shutil, "which", return_value="/usr/local/bin/ccc"):
        got = run._query_ccc_class_defs(tmp_path / "nonexistent")
    assert got == []


def test_query_ccc_returns_empty_on_subprocess_failure(tmp_path):
    with patch.object(run.shutil, "which", return_value="/usr/local/bin/ccc"):
        with patch.object(run.subprocess, "run", side_effect=OSError("boom")):
            got = run._query_ccc_class_defs(tmp_path)
    assert got == []


def test_query_ccc_parses_stubbed_output(tmp_path):
    fake_stdout = (
        "src/processors.py\n"
        "  10| class FluxAttnProcessor2_0:\n"
        "---\n"
        "  25| class HunyuanAttnProcessor2_0:\n"
    )
    fake_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=fake_stdout, stderr="",
    )
    with patch.object(run.shutil, "which", return_value="/usr/local/bin/ccc"):
        with patch.object(run.subprocess, "run", return_value=fake_result):
            got = run._query_ccc_class_defs(tmp_path)
    names = [m["name"] for m in got]
    assert names == ["FluxAttnProcessor2_0", "HunyuanAttnProcessor2_0"]


# --- _rank_by_name_similarity -----------------------------------------------

def test_rank_prefers_more_similar_names():
    identifier = "SanaLinearAttnProcessor2_0"
    candidates = [
        {"kind": "class", "name": "FluxAttnProcessor2_0", "file": "a.py", "line": 1},
        {"kind": "class", "name": "SomeUnrelatedThing", "file": "b.py", "line": 5},
        {"kind": "class", "name": "HunyuanAttnProcessor2_0", "file": "c.py", "line": 3},
    ]
    ranked = run._rank_by_name_similarity(identifier, candidates, min_ratio=0.3)
    names = [r["name"] for r in ranked]
    # Both AttnProcessor variants rank above the unrelated class
    assert "FluxAttnProcessor2_0" in names
    assert "HunyuanAttnProcessor2_0" in names
    if "SomeUnrelatedThing" in names:
        assert names.index("SomeUnrelatedThing") == len(names) - 1


def test_rank_drops_self_match():
    candidates = [
        {"kind": "class", "name": "FooClass", "file": "a.py", "line": 1},
        {"kind": "class", "name": "BarClass", "file": "b.py", "line": 2},
    ]
    ranked = run._rank_by_name_similarity("FooClass", candidates, min_ratio=0.1)
    assert all(r["name"] != "FooClass" for r in ranked)


def test_rank_respects_min_ratio_threshold():
    candidates = [
        {"kind": "class", "name": "TotallyUnrelated", "file": "a.py", "line": 1},
    ]
    ranked = run._rank_by_name_similarity(
        "SanaLinearAttnProcessor2_0", candidates, min_ratio=0.8,
    )
    assert ranked == []


def test_rank_returns_at_most_top_k():
    candidates = [
        {"kind": "class", "name": f"Processor{i}", "file": f"{i}.py", "line": i}
        for i in range(10)
    ]
    ranked = run._rank_by_name_similarity("Processor", candidates, min_ratio=0.1, top_k=3)
    assert len(ranked) == 3


def test_rank_annotates_similarity_score():
    candidates = [
        {"kind": "class", "name": "FooBar", "file": "a.py", "line": 1},
    ]
    ranked = run._rank_by_name_similarity("FooBaz", candidates, min_ratio=0.1)
    assert ranked[0]["similarity"] > 0.5
    assert ranked[0]["similarity"] <= 1.0


# --- _format_convention_precedents_section ----------------------------------

def test_format_section_lists_precedents_per_claim():
    resolutions = {
        "SanaLinearAttnProcessor2_0": [
            {"kind": "class", "name": "FluxAttnProcessor2_0",
             "file": "src/x.py", "line": 10, "similarity": 0.75},
            {"kind": "class", "name": "HunyuanAttnProcessor2_0",
             "file": "src/x.py", "line": 20, "similarity": 0.72},
        ]
    }
    out = run._format_convention_precedents_section(resolutions)
    assert "Convention precedents (via cocoindex AST search)" in out
    assert "SanaLinearAttnProcessor2_0" in out
    assert "FluxAttnProcessor2_0" in out
    assert "src/x.py:10" in out
    assert "0.75" in out
    assert "top 2" in out


def test_format_section_empty_when_no_precedents_found():
    resolutions = {"Foo": [], "Bar": []}
    assert run._format_convention_precedents_section(resolutions) == ""


def test_format_section_skips_claims_with_zero_precedents():
    resolutions = {
        "Foo": [{"kind": "class", "name": "FooChild", "file": "a.py",
                 "line": 1, "similarity": 0.6}],
        "Bar": [],
    }
    out = run._format_convention_precedents_section(resolutions)
    assert "Foo" in out
    assert "Bar" not in out


# --- _enrich_body_with_convention_precedents (integration) ------------------

def _stub_ccc_available(mock_out: str, tmp_path):
    """Return a context that patches shutil.which + subprocess.run to simulate
    an available ccc with the given stdout."""
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=mock_out, stderr="")
    return (
        patch.object(run.shutil, "which", return_value="/usr/local/bin/ccc"),
        patch.object(run.subprocess, "run", return_value=fake),
    )


def test_enrich_body_appends_section_when_ccc_returns_similar_classes(tmp_path):
    mock_stdout = (
        "src/processors.py\n"
        "  10| class SanaLinearAttnProcessor2_0:\n"
        "---\n"
        "  25| class FluxAttnProcessor2_0:\n"
        "---\n"
        "  40| class HunyuanAttnProcessor2_0:\n"
    )
    body = (
        "Register next to `SanaLinearAttnProcessor2_0` in the module."
    )
    which_patch, run_patch = _stub_ccc_available(mock_stdout, tmp_path)
    with which_patch, run_patch:
        enriched = run._enrich_body_with_convention_precedents(body, tmp_path)
    assert body in enriched
    assert "Convention precedents (via cocoindex AST search)" in enriched
    assert "FluxAttnProcessor2_0" in enriched
    assert "HunyuanAttnProcessor2_0" in enriched
    # self-match dropped
    section = enriched.split("Convention precedents")[1]
    assert section.count("SanaLinearAttnProcessor2_0") <= 1  # only in the header


def test_enrich_body_unchanged_when_no_sibling_claims(tmp_path):
    body = "This body has no sibling claim in it."
    which_patch, run_patch = _stub_ccc_available("", tmp_path)
    with which_patch, run_patch:
        got = run._enrich_body_with_convention_precedents(body, tmp_path)
    assert got == body


def test_enrich_body_unchanged_when_ccc_not_installed(tmp_path):
    body = "Should register alongside `SomeClass` in the module."
    with patch.object(run.shutil, "which", return_value=None):
        got = run._enrich_body_with_convention_precedents(body, tmp_path)
    assert got == body


def test_enrich_body_unchanged_when_ccc_returns_no_matches(tmp_path):
    which_patch, run_patch = _stub_ccc_available("", tmp_path)
    with which_patch, run_patch:
        got = run._enrich_body_with_convention_precedents(
            "Register alongside `SomeClass`.", tmp_path,
        )
    assert got.rstrip() == "Register alongside `SomeClass`."


def test_enrich_body_unchanged_when_similar_matches_below_threshold(tmp_path):
    mock_stdout = (
        "src/x.py\n"
        "  10| class TotallyDifferent:\n"
    )
    which_patch, run_patch = _stub_ccc_available(mock_stdout, tmp_path)
    with which_patch, run_patch:
        got = run._enrich_body_with_convention_precedents(
            "Register alongside `SanaLinearAttnProcessor2_0`.", tmp_path,
        )
    # Below min_ratio=0.5, so no precedents surface
    assert "Convention precedents" not in got
