"""Tests for specification compiler integration (PaperCompiler adaptation).

The spec_compiler module enriches Outrider's specification bundle with
structured metadata: non-degradation requirements, cross-file dependencies,
and implementation constraints. These tests verify that the compilation
exports proper metadata and integrates cleanly into write_spec_bundle.

Run with: pytest tests/test_spec_compiler.py -q
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run
from spec_compiler import (
    CrossFileDependency,
    FileConstraint,
    NonDegradationRequirement,
    SpecificationCompilation,
    compile_specification,
    render_specification_metadata,
)


# ─── Specification compilation ───────────────────────────────────────────


def test_compile_specification_returns_requirements():
    """Specification compilation extracts non-degradation requirements."""
    compilation = compile_specification(
        paper_title="Test Paper",
        paper_abstract="A test paper about foo",
    )
    assert len(compilation.non_degradation_requirements) >= 1
    assert any(
        r.aspect == "implementation_fidelity"
        for r in compilation.non_degradation_requirements
    )


def test_compile_specification_tracks_evidence():
    """Specification tracks evidence provenance (paper-supported vs inferred)."""
    compilation = compile_specification(
        paper_title="Test Paper",
        paper_abstract="Abstract",
    )
    requirements_by_evidence = {}
    for req in compilation.non_degradation_requirements:
        requirements_by_evidence.setdefault(req.evidence, []).append(req)

    assert "paper-supported" in requirements_by_evidence
    assert "inferred" in requirements_by_evidence


def test_compile_specification_resolvedness_metadata():
    """Specification tracks provenance distribution in resolvedness field."""
    compilation = compile_specification(
        paper_title="Test Paper",
        paper_abstract="Abstract",
    )
    assert "paper_supported" in compilation.resoluteness
    assert "inferred" in compilation.resoluteness
    assert compilation.resoluteness["paper_supported"] >= 0
    assert compilation.resoluteness["inferred"] >= 0


def test_compile_specification_with_experiment_adds_cross_file_deps():
    """Compilation with suggested_experiment adds cross-file dependencies."""
    compilation = compile_specification(
        paper_title="Test Paper",
        paper_abstract="Abstract",
        suggested_experiment="Integration into src/run.py",
    )
    assert len(compilation.cross_file_dependencies) >= 1
    deps_to_new_module = [
        d for d in compilation.cross_file_dependencies
        if "(new module)" in d.to_file
    ]
    assert len(deps_to_new_module) >= 1


def test_specification_compilation_to_dict():
    """Specification compilation serializes to dict."""
    compilation = compile_specification("Test", "Abstract")
    d = compilation.to_dict()
    assert "non_degradation_requirements" in d
    assert "cross_file_dependencies" in d
    assert "file_constraints" in d
    assert "resoluteness" in d
    assert isinstance(d["non_degradation_requirements"], list)


def test_specification_compilation_to_json():
    """Specification compilation serializes to valid JSON."""
    compilation = compile_specification("Test", "Abstract")
    json_str = compilation.to_json()
    parsed = json.loads(json_str)
    assert "non_degradation_requirements" in parsed


def test_render_specification_metadata():
    """Specification metadata renders to markdown."""
    compilation = compile_specification("Test Paper", "Abstract")
    markdown = render_specification_metadata(compilation)
    assert "Specification compilation" in markdown
    assert "PaperCompiler" in markdown
    assert "Non-degradation requirements" in markdown
    assert "implementation_fidelity" in markdown


def test_render_specification_metadata_includes_all_sections():
    """Rendered metadata includes all compilation sections."""
    compilation = SpecificationCompilation(
        non_degradation_requirements=[
            NonDegradationRequirement("test", "desc", "paper-supported")
        ],
        cross_file_dependencies=[
            CrossFileDependency("a.py", "b.py", "imports", "test dep")
        ],
        file_constraints=[
            FileConstraint("src/test.py", "must_preserve", "test constraint")
        ],
    )
    markdown = render_specification_metadata(compilation)
    assert "Non-degradation requirements" in markdown
    assert "Cross-file dependencies" in markdown
    assert "File-level constraints" in markdown


# ─── Integration with write_spec_bundle ───────────────────────────────────


def test_write_spec_bundle_creates_compilation_json(tmp_path, monkeypatch):
    """write_spec_bundle creates SPEC_COMPILATION.json for paper-anchored specs."""
    workdir = tmp_path / "work"
    workdir.mkdir()

    # Mock dependencies
    monkeypatch.setattr(run, "_mark_bundle_gitignored", lambda x: None)
    monkeypatch.setattr(run, "effective_allowlist", lambda t, p: ["**/*"])
    monkeypatch.setattr(run, "_load_environments_md", lambda x: "")
    monkeypatch.setattr(run, "_load_fork_repo_intel", lambda x: None)
    monkeypatch.setattr(run, "_collect_repo_orientation", lambda *a, **kw: "")
    monkeypatch.setattr(run, "_canary_for_run", lambda x, y: "canary123")
    monkeypatch.setattr(run, "_canary_directive_text", lambda x: "")

    target = MagicMock()
    target.repo = "test/repo"

    rec = MagicMock()
    rec.arxiv_id = "2609.02272v1"
    rec.paper_title = "Test Paper"
    rec.paper_abstract = "Test abstract"
    rec.tier = "high"
    rec.relevance_score = 0.95
    rec.interest_name = "test-interest"
    rec.interest_context = "Test context"
    rec.reasoning = "Test reasoning"
    rec.suggested_experiment = "Experiment"
    rec.experiment_history = None

    run.write_spec_bundle(workdir, target, rec, "test_package")

    bundle = workdir / run.BUNDLE_DIR_NAME
    compilation_file = bundle / "SPEC_COMPILATION.json"
    assert compilation_file.exists(), "SPEC_COMPILATION.json should be created"

    # Verify JSON is valid
    compilation_data = json.loads(compilation_file.read_text())
    assert "non_degradation_requirements" in compilation_data
    assert "cross_file_dependencies" in compilation_data
    assert "file_constraints" in compilation_data
    assert "resoluteness" in compilation_data


def test_write_spec_bundle_skips_compilation_for_brief_mode(tmp_path, monkeypatch):
    """write_spec_bundle skips SPEC_COMPILATION.json in brief mode."""
    workdir = tmp_path / "work"
    workdir.mkdir()

    monkeypatch.setattr(run, "_mark_bundle_gitignored", lambda x: None)
    monkeypatch.setattr(run, "effective_allowlist", lambda t, p: ["**/*"])
    monkeypatch.setattr(run, "_load_environments_md", lambda x: "")
    monkeypatch.setattr(run, "_load_fork_repo_intel", lambda x: None)
    monkeypatch.setattr(run, "_collect_repo_orientation", lambda *a, **kw: "")
    monkeypatch.setattr(run, "_canary_for_run", lambda x, y: "canary123")
    monkeypatch.setattr(run, "_canary_directive_text", lambda x: "")

    target = MagicMock()
    target.repo = "test/repo"

    rec = MagicMock()
    rec.arxiv_id = ""  # Brief mode
    rec.interest_context = "Test context"
    rec.interest_name = "test-interest"
    rec.suggested_experiment = "Experiment"

    run.write_spec_bundle(workdir, target, rec, "test_package")

    bundle = workdir / run.BUNDLE_DIR_NAME
    compilation_file = bundle / "SPEC_COMPILATION.json"
    assert not compilation_file.exists(), "Brief mode should not create SPEC_COMPILATION.json"


def test_write_spec_bundle_creates_standard_files(tmp_path, monkeypatch):
    """write_spec_bundle still creates all standard files (SPEC.md, etc)."""
    workdir = tmp_path / "work"
    workdir.mkdir()

    monkeypatch.setattr(run, "_mark_bundle_gitignored", lambda x: None)
    monkeypatch.setattr(run, "effective_allowlist", lambda t, p: ["**/*"])
    monkeypatch.setattr(run, "_load_environments_md", lambda x: "")
    monkeypatch.setattr(run, "_load_fork_repo_intel", lambda x: None)
    monkeypatch.setattr(run, "_collect_repo_orientation", lambda *a, **kw: "")
    monkeypatch.setattr(run, "_canary_for_run", lambda x, y: "canary123")
    monkeypatch.setattr(run, "_canary_directive_text", lambda x: "")

    target = MagicMock()
    target.repo = "test/repo"

    rec = MagicMock()
    rec.arxiv_id = "2609.02272v1"
    rec.paper_title = "Test Paper"
    rec.paper_abstract = "Test abstract"
    rec.tier = "high"
    rec.relevance_score = 0.95
    rec.interest_name = "test-interest"
    rec.interest_context = "Test context"
    rec.reasoning = "Test reasoning"
    rec.suggested_experiment = "Experiment"
    rec.experiment_history = None

    run.write_spec_bundle(workdir, target, rec, "test_package")

    bundle = workdir / run.BUNDLE_DIR_NAME
    assert (bundle / "SPEC.md").exists()
    assert (bundle / "PAPER.md").exists()
    assert (bundle / "GUARDRAILS.md").exists()
    assert (bundle / "INVOCATION.md").exists()
