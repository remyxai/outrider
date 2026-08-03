"""Tests for the bot co-author trailer in outbound PR bodies.

A maintainer who squash-merges gets a commit message built from the PR
title + body, so a trailer that lives only in the branch commits is
dropped at merge and the bot earns no contributor credit. These cover:

  - `_ensure_coauthor_trailer` puts the trailer last, is idempotent, and
    lifts an already-present (or reformatted) trailer to the end
  - other accounts' co-author lines survive untouched
  - `build_pr_body` emits the trailer as the body's final line
  - the convention-pass body rewrite re-asserts the trailer even when the
    rewrite dropped it, and the rewrite prompt tells the model to keep it

Run with: pytest tests/test_bot_coauthor_trailer.py -q
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Recommendation, Target  # noqa: E402


OTHER_TRAILER = "Co-authored-by: someone <1234+someone@users.noreply.github.com>"


def _rec():
    return Recommendation(
        paper_title="Sample Paper", arxiv_id="2601.00001", tier="high",
        z_score=0.0, spec_md="",
        paper_abstract="abstract text", domain_summary="", raw_paper_md="",
        relevance_score=0.92,
        reasoning="paper anchors on the localize stage",
        suggested_experiment="enable the new flag and benchmark",
        interest_name="ExampleInterest",
    )


# ─── _ensure_coauthor_trailer ──────────────────────────────────────────────


def test_trailer_appended_as_final_line():
    out = run._ensure_coauthor_trailer("## Description\n\nSome content.\n")
    assert out.rstrip().splitlines()[-1] == run.BOT_COAUTHOR_TRAILER
    assert "## Description" in out


def test_trailer_separated_by_blank_line():
    # Trailers are credited from the message's last paragraph, so the
    # trailer must not be glued to the preceding prose line.
    out = run._ensure_coauthor_trailer("Body text.")
    assert out == f"Body text.\n\n{run.BOT_COAUTHOR_TRAILER}\n"


def test_idempotent_no_duplicate():
    once = run._ensure_coauthor_trailer("Body text.")
    twice = run._ensure_coauthor_trailer(once)
    assert once == twice
    assert twice.count(run.BOT_NOREPLY_EMAIL) == 1


def test_stranded_trailer_lifted_to_end():
    stranded = (
        f"Intro.\n\n{run.BOT_COAUTHOR_TRAILER}\n\n"
        "## Coverage\n\nappended later by a refinement phase\n"
    )
    out = run._ensure_coauthor_trailer(stranded)
    assert out.count(run.BOT_NOREPLY_EMAIL) == 1
    assert out.rstrip().splitlines()[-1] == run.BOT_COAUTHOR_TRAILER
    assert "## Coverage" in out


def test_reformatted_trailer_is_normalized_not_duplicated():
    # A rewrite may lowercase the trailer key; match case-insensitively so
    # it gets normalized rather than duplicated.
    reformatted = f"Body.\n\nco-authored-by: {run.BOT_GIT_NAME} <{run.BOT_NOREPLY_EMAIL}>\n"
    out = run._ensure_coauthor_trailer(reformatted)
    assert out.count(run.BOT_NOREPLY_EMAIL) == 1
    assert out.rstrip().splitlines()[-1] == run.BOT_COAUTHOR_TRAILER


def test_other_coauthors_preserved():
    out = run._ensure_coauthor_trailer(f"Body.\n\n{OTHER_TRAILER}\n")
    assert OTHER_TRAILER in out
    assert out.rstrip().splitlines()[-1] == run.BOT_COAUTHOR_TRAILER


def test_empty_body_yields_bare_trailer():
    assert run._ensure_coauthor_trailer("") == f"{run.BOT_COAUTHOR_TRAILER}\n"
    assert run._ensure_coauthor_trailer("   \n") == f"{run.BOT_COAUTHOR_TRAILER}\n"


def test_trailer_email_is_id_keyed():
    # GitHub resolves the account from the leading numeric id; a bracketed
    # login with no id prefix resolves to nobody and earns no credit.
    local = run.BOT_NOREPLY_EMAIL.split("@")[0]
    assert local.split("+")[0].isdigit()
    assert run.BOT_NOREPLY_EMAIL.endswith("@users.noreply.github.com")


# ─── build_pr_body ─────────────────────────────────────────────────────────


def test_build_pr_body_ends_with_trailer():
    body = run.build_pr_body(
        Target(repo="owner/name"), _rec(),
        tests_status="passed", test_output="",
    )
    assert body.rstrip().splitlines()[-1] == run.BOT_COAUTHOR_TRAILER
    assert body.count(run.BOT_NOREPLY_EMAIL) == 1


def test_build_pr_body_brief_mode_ends_with_trailer():
    # Brief mode (no arxiv_id) returns from a separate template earlier in
    # the function; it is an outbound PR body too and needs the trailer.
    rec = _rec()
    rec.arxiv_id = ""
    rec.suggested_experiment = "Wire the retry budget into the scheduler. Refs #12."
    body = run.build_pr_body(
        Target(repo="owner/name"), rec,
        tests_status="passed", test_output="",
    )
    assert body.rstrip().splitlines()[-1] == run.BOT_COAUTHOR_TRAILER
    assert body.count(run.BOT_NOREPLY_EMAIL) == 1


def test_build_pr_body_trailer_survives_failing_tests_path():
    body = run.build_pr_body(
        Target(repo="owner/name"), _rec(),
        tests_status="failed", test_output="E   assert False",
        review_section="## Self-review\n\nstubbed the trainer\n",
        selection_note="higher-ranked candidate needed infra the repo lacks",
        test_integration_warning=True,
    )
    assert body.rstrip().splitlines()[-1] == run.BOT_COAUTHOR_TRAILER


# ─── convention-pass body rewrite ──────────────────────────────────────────


def test_rewrite_prompt_instructs_preserving_the_trailer():
    prompt = run._build_pr_body_rewrite_prompt(
        pr_title="Add a thing",
        current_body=f"## Description\n\nx\n\n{run.BOT_COAUTHOR_TRAILER}\n",
        pr_body_pattern={"description": "use ## Description", "canonical_example": "## Description"},
        all_patterns={"patterns": []},
    )
    assert "Co-Authored-By:" in prompt
    assert "trailer verbatim as the final" in prompt
    assert "Do not reword it" in prompt


def test_convention_update_reasserts_dropped_trailer():
    import json

    current = f"## Description\n\noriginal\n\n{run.BOT_COAUTHOR_TRAILER}\n"
    # Rewrite drops the trailer entirely — the PATCH must still carry it.
    rewritten = "## Description\n\nrestructured to convention\n"
    raw = json.dumps({"updated_body": rewritten, "rationale": "aligned"})
    patched = {}

    with patch.object(run, "_run_claude_oneshot", return_value=(True, raw)), \
         patch.object(
             run, "_update_pr_body",
             side_effect=lambda t, n, b: patched.update({"body": b}),
         ):
        ok, rationale, err = run._apply_pr_body_convention_update(
            Target(repo="owner/name"), 1, current,
            {"description": "d", "canonical_example": "## Description"},
            {"patterns": []},
            "Add a thing", 600, Path("/tmp"),
        )

    assert ok, err
    assert patched["body"].rstrip().splitlines()[-1] == run.BOT_COAUTHOR_TRAILER
    assert "restructured to convention" in patched["body"]
