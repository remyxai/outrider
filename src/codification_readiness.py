"""Codification readiness assessment for research-method specifications.

Adapted from IdeaAMBIG: Benchmarking Implementation-Critical Gaps in
Research-Idea Specifications (https://arxiv.org/abs/2609.10539v1).

Evaluates whether a paper's method specification provides sufficient
methodological information for an implementer or coding agent to construct
the intended method without unsupported assumptions. Identifies gaps that
are implementation-critical and flags them as advisory text for the
specification bundle.
"""


def assess_codification_readiness(
    paper_title: str,
    paper_abstract: str,
    suggested_experiment: str,
) -> str:
    """Assess codification readiness of a method specification.

    Analyzes the clarity and completeness of a paper's method
    specification based on the abstract and suggested experiment scope.
    Returns a gap assessment summary as markdown advisory text, or an
    empty string if no critical gaps are detected.

    Args:
        paper_title: Title of the paper being assessed.
        paper_abstract: Abstract text from the paper.
        suggested_experiment: Experiment scope from the recommendation.

    Returns:
        Markdown text describing any implementation-critical gaps found,
        or empty string if the specification is sufficiently clear.
    """
    if not paper_abstract or not suggested_experiment:
        return ""

    gaps = []

    # Check for under-specification patterns common in research papers:
    # - Vague algorithmic steps ("use suitable", "appropriately choose")
    # - Missing hyperparameter values or selection strategies
    # - Unspecified training procedures or dataset handling
    # - Ambiguous architectural details

    vague_terms = [
        "suitable",
        "appropriately",
        "typically",
        "generally",
        "usually",
        "often",
        "can be",
        "may be",
        "similar to",
        "analogous to",
    ]

    abstract_lower = paper_abstract.lower()
    experiment_lower = suggested_experiment.lower()
    full_spec = (abstract_lower + " " + experiment_lower).lower()

    # Detect vague language density
    vague_count = sum(
        full_spec.count(term) for term in vague_terms
    )
    if vague_count >= 3:
        gaps.append(
            "Method contains multiple vague specification patterns "
            "(e.g., 'suitable', 'appropriately', 'typically') that may "
            "require implementer assumptions."
        )

    # Check for missing critical implementation details
    critical_missing = {
        "hyperparameter": [
            "learning rate",
            "batch size",
            "dropout",
            "regularization",
        ],
        "training": [
            "train",
            "optimizer",
            "loss function",
            "convergence",
        ],
        "architecture": [
            "layer",
            "dimension",
            "activation",
            "normalization",
        ],
    }

    # Look for specification patterns (not absence, which is harder to
    # detect reliably). If abstract/experiment lack detail on multiple
    # critical aspects, flag as potentially under-specified.
    detail_categories_present = 0
    for category, terms in critical_missing.items():
        if any(term in full_spec for term in terms):
            detail_categories_present += 1

    # If fewer than 2 of the 3 critical categories are mentioned, likely
    # under-specified for training-dependent methods.
    if (
        detail_categories_present < 2
        and any(
            keyword in full_spec
            for keyword in ["method", "model", "train", "neural"]
        )
    ):
        gaps.append(
            "Specification lacks detail in training or architectural setup "
            "— may require assumptions about optimizer, hyperparameters, "
            "or model dimensions."
        )

    # Check for evaluation gaps
    if "eval" not in full_spec and "metric" not in full_spec:
        gaps.append(
            "Evaluation approach not described in abstract/experiment — "
            "success criteria and benchmarks may need to be inferred."
        )

    if not gaps:
        return ""

    # Format as markdown advisory block
    gap_text = "### Codification Readiness Advisory\n\n"
    gap_text += (
        "_IdeaAMBIG analysis: The specification contains patterns typical "
        "of under-specified research methods._\n\n"
    )
    gap_text += (
        "**Implementation gaps detected:**\n\n"
    )
    for gap in gaps:
        gap_text += f"- {gap}\n"
    gap_text += (
        "\n**Note:** These are advisory flags from pre-flight analysis. "
        "The recommended experiment scope may be implementable despite "
        "gaps in the paper's broader framing. Proceed with caution if "
        "detailed method steps are critical for reproducibility.\n"
    )

    return gap_text
