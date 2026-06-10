"""Tests for v1.5.1 missing-link recovery strategies in the selection
prompt (REMYX-104).

These tests lock in the *presence* of the recovery-strategy guidance —
the selection-pass agent (Claude Code) reads the prompt and applies the
strategies with its own tool kit (WebFetch + Bash + gh). The strategies
themselves are documented in the prompt; this test file ensures the
guidance doesn't get accidentally deleted or watered down in future
edits.

A follow-up release will add CLI helpers (`arxiv-fetch`,
`web-fetch-resilient`) and instrument `failed_lookups` in the result
dict per the full REMYX-104 ticket; tests for those land then.

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


def _unwrap(s: str) -> str:
    return " ".join(s.split())


# ─── Recovery-strategy block is present and labeled ───────────────────────


def test_prompt_has_recovery_strategies_section():
    """A dedicated section labels the recovery strategies — without
    the section header the LLM may not connect "failed lookup" to
    "try alternative URLs". The header is the load-bearing pointer."""
    prompt = _unwrap(run._SELECTION_PROMPT_TEMPLATE)
    assert "Recovery strategies for missing/broken links" in prompt


# ─── Arxiv URL variant fallbacks ──────────────────────────────────────────


def test_prompt_documents_arxiv_variant_chain():
    """The four arxiv URL variants the agent should try when one 404s.
    All four must be named so the agent knows the full fallback chain.
    Order matters — abs is most reliable, ar5iv is the recent-paper
    fallback, pdf is last-resort."""
    prompt = _unwrap(run._SELECTION_PROMPT_TEMPLATE)
    assert "Arxiv URL variants" in prompt
    assert "arxiv.org/abs/" in prompt
    assert "arxiv.org/html/" in prompt
    assert "ar5iv.labs.arxiv.org/" in prompt
    assert "arxiv.org/pdf/" in prompt


# ─── Dead live URL → archive.org fallback ─────────────────────────────────


def test_prompt_documents_archive_org_fallback():
    """For non-arxiv URLs (project pages, github repos), agent should
    fall back to web.archive.org when the live URL 404s. This is the
    single most-impactful recovery pattern — project pages routinely
    rot within months of paper publication."""
    prompt = _unwrap(run._SELECTION_PROMPT_TEMPLATE)
    assert "web.archive.org/web/" in prompt
    # Calling out the regime where this matters (academic project pages)
    assert "project page" in prompt


# ─── Engine reports github_url: (none) recovery chain ─────────────────────


def test_prompt_documents_missing_github_url_recovery_chain():
    """The most common engine-side gap: paper has code but the engine's
    regex didn't catch it. The prompt must direct the agent NOT to take
    null at face value, and document the four-step recovery chain.

    This is the load-bearing protection against rejecting viable
    candidates that the engine mis-labeled as code-less — the failure
    mode that surfaced REMYX-104 in the first place."""
    prompt = _unwrap(run._SELECTION_PROMPT_TEMPLATE)
    assert "Engine reports `github_url: (none)`" in prompt
    # Tells the agent explicitly not to trust the null
    assert "Don't take the engine's null at face value" in prompt
    # Four-step chain — at least the four anchors must be present
    assert 'gh search code' in prompt
    assert "abstract page and grep for `github.com/`" in prompt
    assert "model card" in prompt  # huggingface_url → model card check
    assert "project page" in prompt  # one-hop project-page follow


def test_prompt_documents_no_code_as_post_recovery_verdict():
    """`github_url: (none)` should be a verdict the agent reaches AFTER
    exhausting recovery, not a default. The prompt must make that
    sequencing explicit so the agent doesn't shortcut to "no code
    available → reject" without trying recovery first."""
    prompt = _unwrap(run._SELECTION_PROMPT_TEMPLATE)
    assert 'Treat "no code found" as a verdict you reach AFTER exhausting' in prompt


# ─── Login-wall detection ─────────────────────────────────────────────────


def test_prompt_documents_login_wall_detection():
    """Colab/Drive/OpenReview return HTTP 200 with sign-in pages that
    LOOK like content. Without explicit detection guidance, the agent
    may treat the sign-in page as the actual paper/repo content and
    draw wrong conclusions."""
    prompt = _unwrap(run._SELECTION_PROMPT_TEMPLATE)
    assert "Login-wall detection" in prompt
    # The detection heuristic — short body + sign-in phrases
    assert "< 500 chars of non-nav body" in prompt
    assert "Sign in" in prompt  # at least one of the trigger phrases


# ─── Failure budget — don't burn turns on dead-end candidates ─────────────


def test_prompt_documents_recovery_failure_budget():
    """The recovery patterns are unbounded in principle — the agent
    could spend all 25 turns chasing broken links. The prompt must cap
    recovery effort per candidate and tell the agent that "many broken
    links + no reachable context" is itself a rejection signal."""
    prompt = _unwrap(run._SELECTION_PROMPT_TEMPLATE)
    assert "Failure budget" in prompt
    assert "at most ~3 turns on recovery per candidate" in prompt
    # The "broken links are themselves a signal" framing — protects
    # against the agent treating recovery as endless
    assert "that itself is a signal" in prompt


# ─── Recovery section placement — before "Stop iterating" guidance ────────


def test_recovery_strategies_placed_before_stop_iterating_clause():
    """Ordering matters: the agent reads top-to-bottom; recovery
    strategies must appear BEFORE the "stop iterating" guidance, or
    the agent may stop iterating prematurely on a broken-link candidate
    before applying recovery."""
    prompt = run._SELECTION_PROMPT_TEMPLATE
    recovery_idx = prompt.find("Recovery strategies for missing/broken links")
    stop_idx = prompt.find("Stop iterating once you have enough evidence")
    assert recovery_idx > 0 and stop_idx > 0
    assert recovery_idx < stop_idx, (
        "Recovery-strategies section must precede the stop-iterating "
        "clause so the agent applies recovery before deciding to stop."
    )
