"""Continuous verification scoring for Outrider PRs via LLM logit distributions.

Adapted from LLM-as-a-Verifier: A General-Purpose Verification Framework
(arXiv:2607.05391v1), which introduces continuous scoring via logit-space
expectations instead of discrete LLM judgments.

Where diff_risk_score provides deterministic risk bands via static-diff
features, logit_verifier provides probabilistic quality signals via LLM
reasoning. The module extracts logit distributions of scoring tokens and
computes calibrated continuous scores [0,1] as expectations, enabling
finer-grained feedback on generated code without training or sampling.

Verification dimensions scaled (per the paper):
  - Granularity: token-level logits to calibrate [0,1] scale
  - Criteria decomposition: separate scoring for correctness, style, integration

Scores feed downstream verification gates: e.g., threshold-based routing to
Issue review, or ranking candidate implementations when Outrider grows to
multi-generation synthesis.
"""
from __future__ import annotations

import json
import logging
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class VerificationScore:
    """Result of continuous-scoring a code submission via LLM logits."""

    correctness: float              # [0, 1] does the code work as intended?
    style: float                    # [0, 1] does it fit repo conventions?
    integration: float              # [0, 1] does it wire into existing code?
    overall: float                  # [0, 1] weighted aggregate
    confidence: float               # [0, 1] how certain is the signal?
    dimensions: dict = field(default_factory=dict)  # per-dimension detail
    reasoning: str = ""             # brief explanation of the score


def _logit_tokens_for_criterion(criterion: str) -> list[str]:
    """Scoring token set for a single verification criterion.

    The paper's key insight: extract logit distributions over a sparse set
    of discriminative scoring tokens (e.g., "correct", "incorrect") rather
    than forcing discrete bucketing. This provides continuous calibrated
    scores.
    """
    token_sets = {
        "correctness": ["correct", "incorrect", "broken"],
        "style": ["idiomatic", "awkward", "non-standard"],
        "integration": ["integrated", "orphaned", "disconnected"],
    }
    return token_sets.get(criterion, ["good", "bad"])


def extract_logit_score(
    logits_json: dict, criterion: str,
) -> float:
    """Extract continuous [0,1] score from logit-space expectation.

    Given token logits (as returned by Claude's logit-bias API or logprobs),
    compute the expectation over scoring tokens to yield a calibrated
    continuous score. This replaces discrete bucketing with smooth [0,1]
    calibration.

    logits_json: {token_str: logit_value, ...} from LLM inference
    criterion: "correctness" | "style" | "integration"

    Returns: float in [0, 1]
    """
    tokens = _logit_tokens_for_criterion(criterion)
    if not tokens or not logits_json:
        return 0.5  # neutral when insufficient signal

    # Softmax over positive vs negative tokens to calibrate [0,1].
    # Positive tokens (e.g. "correct", "idiomatic") → higher score.
    positive = [tokens[0]] if tokens else []
    negative = tokens[1:] if len(tokens) > 1 else []

    # Extract logits, clamping to avoid overflow in exp().
    pos_logits = [min(logits_json.get(t, -10.0), 100.0) for t in positive]
    neg_logits = [min(logits_json.get(t, -10.0), 100.0) for t in negative]

    # Exponentiate and normalize: Prob(positive) / (Prob(positive) + Prob(negative))
    pos_exp = sum(math.exp(z) for z in pos_logits) if pos_logits else 1e-10
    neg_exp = sum(math.exp(z) for z in neg_logits) if neg_logits else 1e-10
    denominator = pos_exp + neg_exp
    if denominator == 0:
        return 0.5

    return pos_exp / denominator


def score_implementation(
    diff_text: str, repo_context: str, timeout_s: int = 60,
) -> VerificationScore:
    """Continuous-score a generated implementation via LLM verification.

    Invokes Claude to evaluate the diff against three criteria (correctness,
    style, integration) with logit extraction enabled, then computes
    continuous [0,1] scores from the logit distributions.

    This is the paper's primary contribution adapted for Outrider:
    fine-grained probabilistic feedback on code quality without training.

    Args:
        diff_text: unified diff of the code changes
        repo_context: brief context (e.g., "Python library, pytest tests,
                     ruff linting")
        timeout_s: Claude API timeout in seconds

    Returns:
        VerificationScore with per-criterion calibrated scores and overall
        aggregate.
    """
    # Stub for now — full integration would call Claude with logit-bias
    # parameters to extract token-level scoring distributions. This skeleton
    # shows the interface that downstream gates would consume.
    return VerificationScore(
        correctness=0.75,
        style=0.82,
        integration=0.68,
        overall=0.75,  # weighted mean
        confidence=0.65,  # how much logit mass concentrated on top-2 tokens
        reasoning="Placeholder: would invoke Claude with logit extraction.",
    )


def score_vs_threshold(score: VerificationScore, threshold: float = 0.70) -> bool:
    """Route based on continuous score: pass if overall ≥ threshold."""
    return score.overall >= threshold
