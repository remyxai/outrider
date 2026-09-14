"""Specification compiler for paper-to-code implementations.

Adapts PaperCompiler (arxiv:2609.02272v1) to enrich Outrider's specification
bundle with structured metadata: non-degradation requirements, cross-file
dependencies, and implementation constraints grounded in paper evidence.

PaperCompiler compiles paper-grounded evidence into explicit repository-level
implementation specifications while preserving source provenance and
distinguishing paper-supported, inferred, externally delegated, and unresolved
information. This module adapts that core insight for Outrider's spec-generation
pipeline — enriching SPEC.md with structured constraints that guide code
generation and verification toward higher fidelity implementations.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NonDegradationRequirement:
    """A constraint that the implementation must not degrade existing behavior.

    Attributes:
        aspect: The repository aspect constrained (e.g., "performance", "api_compatibility").
        description: Natural-language constraint description.
        evidence: Source provenance — "paper-supported", "inferred", or "externally_delegated".
        source: Verbatim snippet of the paper text that grounds a paper-supported
            requirement; empty for inferred/externally_delegated ones.
    """
    aspect: str
    description: str
    evidence: str = "inferred"
    source: str = ""


@dataclass
class CrossFileDependency:
    """A dependency relationship between files in the implementation.

    Attributes:
        from_file: File that depends on or calls into the target.
        to_file: File that provides the capability.
        relationship: Type of relationship ("calls", "imports", "extends", etc.).
        description: Natural-language description of why this dependency exists.
    """
    from_file: str
    to_file: str
    relationship: str
    description: str


@dataclass
class FileConstraint:
    """A constraint or requirement scoped to a single file.

    Attributes:
        file_path: Path to the file being constrained.
        constraint_type: Type of constraint ("must_preserve", "must_add", "structure", etc.).
        description: Natural-language constraint description.
    """
    file_path: str
    constraint_type: str
    description: str


@dataclass
class SpecificationCompilation:
    """Compiled specification metadata for an implementation.

    Attributes:
        non_degradation_requirements: List of constraints the implementation must satisfy.
        cross_file_dependencies: List of dependencies between files.
        file_constraints: File-level constraints and requirements.
        resoluteness: Metadata about information provenance and gaps.
    """
    non_degradation_requirements: list[NonDegradationRequirement] = field(default_factory=list)
    cross_file_dependencies: list[CrossFileDependency] = field(default_factory=list)
    file_constraints: list[FileConstraint] = field(default_factory=list)
    resoluteness: dict[str, Any] = field(default_factory=lambda: {
        "paper_supported": 0,
        "inferred": 0,
        "externally_delegated": 0,
        "unresolved": 0,
    })

    def to_dict(self) -> dict[str, Any]:
        """Serialize the compilation to a dictionary."""
        return {
            "non_degradation_requirements": [
                {
                    "aspect": r.aspect,
                    "description": r.description,
                    "evidence": r.evidence,
                    "source": r.source,
                }
                for r in self.non_degradation_requirements
            ],
            "cross_file_dependencies": [
                {
                    "from_file": d.from_file,
                    "to_file": d.to_file,
                    "relationship": d.relationship,
                    "description": d.description,
                }
                for d in self.cross_file_dependencies
            ],
            "file_constraints": [
                {
                    "file_path": c.file_path,
                    "constraint_type": c.constraint_type,
                    "description": c.description,
                }
                for c in self.file_constraints
            ],
            "resoluteness": self.resoluteness,
        }

    def to_json(self) -> str:
        """Serialize to JSON."""
        return json.dumps(self.to_dict(), indent=2)


# Paper-relevant constraint categories PaperCompiler expects a faithful spec
# to cover. Each entry: (aspect, canonical non-degradation description, trigger
# keywords scanned against the abstract). A category matched in the abstract
# yields a paper-supported requirement carrying the grounding sentence as
# provenance; a category NOT matched counts toward the honest `unresolved`
# bucket rather than being silently invented.
_CONSTRAINT_SIGNALS: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "method_logic",
        "Preserve the paper's method logic — do not simplify, compress, or "
        "reinterpret the core algorithm when generating the repository",
        ("method logic", "algorithm", "non-degrad", "degrade", "simplif", "compress"),
    ),
    (
        "evaluation_protocol",
        "Preserve the paper's evaluation protocol; adapt the surface it runs "
        "on without weakening the metric it reports",
        ("evaluation protocol", "evaluation", "protocol", "metric"),
    ),
    (
        "cross_file_consistency",
        "Maintain cross-file consistency and coherent repository structure "
        "across the generated files",
        ("cross-file", "cross file", "consistency", "repository-level", "repository structure"),
    ),
    (
        "evidence_grounding",
        "Ground each implementation choice in paper evidence and tag its "
        "provenance (paper-supported / inferred / externally-delegated / unresolved)",
        ("provenance", "grounded", "grounds", "evidence", "paper-supported", "inferred"),
    ),
]

# Abstract signals that a concern is externally delegated (benchmark suites,
# separate eval frameworks) rather than owned by the compiled spec.
_DELEGATION_KEYWORDS: tuple[str, ...] = ("benchmark", "baselines", "baseline")


def _split_sentences(text: str) -> list[str]:
    """Split abstract text into rough sentences for provenance grounding."""
    out: list[str] = []
    for chunk in text.replace("\n", " ").split(". "):
        chunk = chunk.strip()
        if chunk:
            out.append(chunk if chunk.endswith(".") else chunk + ".")
    return out


def _grounding_sentence(sentences: list[str], keywords: tuple[str, ...]) -> str:
    """Return the first sentence containing any keyword, truncated for provenance."""
    for sent in sentences:
        low = sent.lower()
        if any(kw in low for kw in keywords):
            return sent if len(sent) <= 200 else sent[:197].rstrip() + "..."
    return ""


def _extract_src_paths(text: str) -> list[str]:
    """Pull concrete ``src/...py`` paths mentioned in the experiment scope."""
    paths: list[str] = []
    for token in text.replace(",", " ").replace("`", " ").split():
        token = token.strip("().:;'\"")
        if token.startswith("src/") and token.endswith(".py") and token not in paths:
            paths.append(token)
    return paths


def compile_specification(
    paper_title: str,
    paper_abstract: str,
    suggested_experiment: str = "",
) -> SpecificationCompilation:
    """Compile a specification from paper metadata.

    This is a Mode 2 (adapted port) implementation that compiles structured
    constraints *from the paper's own abstract text* — no training or learned
    estimators. Rather than emitting a fixed constraint list, it scans the
    abstract for the constraint categories PaperCompiler expects a faithful
    spec to cover (method logic, evaluation protocol, cross-file consistency,
    evidence grounding) and:

    - emits a ``paper-supported`` non-degradation requirement for each category
      the abstract actually mentions, carrying the grounding sentence as
      ``source`` provenance;
    - counts categories the abstract does NOT mention toward the ``unresolved``
      provenance bucket (the paper's fourth information type) instead of
      inventing constraints for them;
    - marks concerns the abstract delegates to external artifacts (benchmarks,
      separate eval frameworks) as ``externally_delegated``.

    Inferred constraints (integration shape, test coverage) remain, tagged
    ``inferred``, so the reader can tell paper-grounded requirements from
    target-native deductions.

    Args:
        paper_title: Title of the recommended paper.
        paper_abstract: Abstract or summary of the paper's contribution.
        suggested_experiment: Optional experiment scope from the recommendation.

    Returns:
        A SpecificationCompilation with extracted constraints and honest
        provenance accounting.
    """
    compilation = SpecificationCompilation()
    abstract = paper_abstract or ""
    low_abstract = abstract.lower()
    sentences = _split_sentences(abstract)

    # Core invariant — PaperCompiler's central non-degradation claim. Always
    # present and paper-supported: the whole framework exists to keep the
    # generated repo from degrading the paper's algorithm.
    compilation.non_degradation_requirements.append(
        NonDegradationRequirement(
            aspect="implementation_fidelity",
            description="Preserve the paper's core algorithmic insight when adapting auxiliaries",
            evidence="paper-supported",
            source=_grounding_sentence(sentences, ("fidelity", "faithful", "preserve", "method"))
            or (sentences[0] if sentences else ""),
        )
    )

    # Derive category-specific requirements grounded in the abstract; track
    # which paper-relevant categories the abstract left unaddressed.
    unresolved_aspects: list[str] = []
    for aspect, description, keywords in _CONSTRAINT_SIGNALS:
        grounding = _grounding_sentence(sentences, keywords)
        if grounding:
            compilation.non_degradation_requirements.append(
                NonDegradationRequirement(
                    aspect=aspect,
                    description=description,
                    evidence="paper-supported",
                    source=grounding,
                )
            )
        else:
            unresolved_aspects.append(aspect)

    # Externally-delegated concerns: benchmark / baseline evaluation is not
    # reproduced here — it routes to the repo's existing verification surface.
    if any(kw in low_abstract for kw in _DELEGATION_KEYWORDS):
        compilation.non_degradation_requirements.append(
            NonDegradationRequirement(
                aspect="evaluation_delegation",
                description="Benchmark/baseline evaluation is delegated to the repo's "
                "existing verification surface, not reproduced in this change",
                evidence="externally_delegated",
                source=_grounding_sentence(sentences, _DELEGATION_KEYWORDS),
            )
        )

    # Inferred (target-native) requirements — reasonable deductions, not from
    # the paper. Kept distinct so provenance stays honest.
    compilation.non_degradation_requirements.append(
        NonDegradationRequirement(
            aspect="integration_shape",
            description="Maintain compatibility with existing call sites and data contracts",
            evidence="inferred",
        )
    )
    compilation.non_degradation_requirements.append(
        NonDegradationRequirement(
            aspect="test_coverage",
            description="New code paths must have test coverage at public interfaces",
            evidence="inferred",
        )
    )

    # Cross-file dependencies + ownership assignments derived from the
    # experiment scope. The generic orchestrator->module edge is retained;
    # concrete src/ paths named in the scope become file-level ownership
    # constraints (the paper's "ownership assignments").
    if suggested_experiment and "src/" in suggested_experiment:
        compilation.cross_file_dependencies.append(
            CrossFileDependency(
                from_file="src/run.py",
                to_file="(new module)",
                relationship="calls",
                description="Main orchestrator invokes compiled specification logic",
            )
        )
    for path in _extract_src_paths(suggested_experiment):
        compilation.file_constraints.append(
            FileConstraint(
                file_path=path,
                constraint_type="ownership",
                description="Named in the experiment scope — owns part of the paper's "
                "contribution and must carry it faithfully",
            )
        )

    # Standing file-level constraint: the integration stays addition-shaped.
    compilation.file_constraints.append(
        FileConstraint(
            file_path="src/run.py",
            constraint_type="must_preserve",
            description="Integration must be addition-shaped: existing logic continues unchanged",
        )
    )

    # Honest provenance accounting — counts derived from what was actually
    # compiled, including the previously-always-zero `unresolved` bucket.
    reqs = compilation.non_degradation_requirements
    compilation.resoluteness["paper_supported"] = sum(
        1 for r in reqs if r.evidence == "paper-supported"
    )
    compilation.resoluteness["inferred"] = sum(1 for r in reqs if r.evidence == "inferred")
    compilation.resoluteness["externally_delegated"] = sum(
        1 for r in reqs if r.evidence == "externally_delegated"
    )
    compilation.resoluteness["unresolved"] = len(unresolved_aspects)
    compilation.resoluteness["unresolved_aspects"] = unresolved_aspects

    return compilation


def render_specification_metadata(compilation: SpecificationCompilation) -> str:
    """Render compiled specification as markdown for bundle documentation.

    Args:
        compilation: The compiled specification.

    Returns:
        Markdown text suitable for inclusion in bundle documentation.
    """
    lines: list[str] = []
    lines.append("## Specification compilation (PaperCompiler)")
    lines.append("")
    lines.append("This specification has been enriched with structured metadata")
    lines.append("to guide implementation toward higher fidelity:")
    lines.append("")

    if compilation.non_degradation_requirements:
        lines.append("### Non-degradation requirements")
        lines.append("")
        for req in compilation.non_degradation_requirements:
            lines.append(f"- **{req.aspect}** ({req.evidence})")
            lines.append(f"  {req.description}")
            if req.source:
                lines.append(f"  _grounded in:_ \"{req.source}\"")
        lines.append("")

    if compilation.cross_file_dependencies:
        lines.append("### Cross-file dependencies")
        lines.append("")
        for dep in compilation.cross_file_dependencies:
            lines.append(f"- {dep.from_file} ← {dep.relationship} → {dep.to_file}")
            lines.append(f"  {dep.description}")
        lines.append("")

    if compilation.file_constraints:
        lines.append("### File-level constraints")
        lines.append("")
        for constraint in compilation.file_constraints:
            lines.append(f"- {constraint.file_path} ({constraint.constraint_type})")
            lines.append(f"  {constraint.description}")
        lines.append("")

    res = compilation.resoluteness
    lines.append("### Provenance accounting")
    lines.append("")
    lines.append(
        "- paper-supported: {paper_supported} · inferred: {inferred} · "
        "externally-delegated: {externally_delegated} · unresolved: {unresolved}".format(
            paper_supported=res.get("paper_supported", 0),
            inferred=res.get("inferred", 0),
            externally_delegated=res.get("externally_delegated", 0),
            unresolved=res.get("unresolved", 0),
        )
    )
    unresolved_aspects = res.get("unresolved_aspects") or []
    if unresolved_aspects:
        lines.append(
            "- unresolved (abstract gives no constraint for): "
            + ", ".join(unresolved_aspects)
        )
    lines.append("")

    return "\n".join(lines)
