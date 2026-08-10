"""Demo: hit real GitHub and render the ``## Recent architecture Discussions``
orientation block for a fork whose Discussions live upstream.

Target: ``smellslikeml/lm-evaluation-harness`` (fork, Discussions disabled).
Parent: ``EleutherAI/lm-evaluation-harness`` (Discussions enabled, active
RFC-style threads on eval methodology). The fork-fallback path is the whole
point of this demo — for a fresh fork the design conversation is upstream.

Paper used for ranking: 2607.27083 — "Scores Are Not Decisions: Cost-Aware
Stopping for Tool Acquisition in LLM Agents". It's a genuine candidate the
GitRank ranker surfaced this week (#23 in the ``outrider`` interest), so
the query terms are realistic.

Requires GITHUB_TOKEN (or gh CLI auth) — this hits the real GraphQL API.

Run with: python3 scripts/demo_discussions_injection.py
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# gh CLI keeps the token in its config; the fallback lets the demo run
# without exporting anything manually.
if not os.environ.get("GITHUB_TOKEN"):
    try:
        tok = subprocess.check_output(
            ["gh", "auth", "token"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        if tok:
            os.environ["GITHUB_TOKEN"] = tok
    except Exception:
        pass

import run  # noqa: E402


TARGET_REPO = "smellslikeml/lm-evaluation-harness"
PAPER_TITLE = (
    "Scores Are Not Decisions: Cost-Aware Stopping for Tool Acquisition "
    "in LLM Agents"
)
PAPER_ABSTRACT = (
    "We study cost-aware marginal decision-focused stopping for LLM agents "
    "that must decide when to stop acquiring tools or context. The framework "
    "(CAM-DF) treats tool acquisition as a bandit problem and provides a "
    "principled stopping rule for agents evaluating heterogeneously-costed "
    "tools during multi-step tasks. Applied to language-model evaluation "
    "harnesses, the mechanism reduces token spend by 30% at fixed answer "
    "quality on MMLU, GPQA, and a subset of BigBench-Hard."
)


def main() -> int:
    resolved = run._resolve_discussions_repo(TARGET_REPO)
    print(f"[resolve] target={TARGET_REPO} → {resolved}")
    if resolved is None:
        print("Neither target nor parent has Discussions enabled. Bailing.")
        return 1

    discussions_repo, is_fallback = resolved
    print(f"[fetch]   fetching newest 15 discussions from {discussions_repo}"
          f" (fallback={is_fallback})")
    fetched = run._fetch_recent_discussions(discussions_repo, limit=15)
    print(f"[fetch]   got {len(fetched)} discussion(s)")
    for d in fetched[:5]:
        print(f"          · [{d['category'] or '-'}] {d['title'][:80]}")

    kws = run._paper_keywords(PAPER_TITLE, PAPER_ABSTRACT)
    print(f"\n[kwd]     paper → {len(kws)} keywords: "
          f"{', '.join(kws[:15])}{'…' if len(kws) > 15 else ''}")

    ranked = run._rank_discussions_by_paper(
        fetched, PAPER_TITLE, PAPER_ABSTRACT, top_k=5,
    )
    print(f"\n[rank]    {len(ranked)} discussion(s) survived scoring")
    for d in ranked:
        print(f"          · score={d['_overlap']:2d}  {d['title'][:80]}")

    print("\n" + "=" * 78)
    print("RENDERED ORIENTATION BLOCK")
    print("=" * 78)
    block = run._orient_recent_discussions(
        TARGET_REPO, PAPER_TITLE, PAPER_ABSTRACT,
    )
    if not block:
        print("(empty — no lexical overlap with any recent discussion)")
        return 0
    print(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
