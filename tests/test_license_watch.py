"""Tests for the licensing-watch helpers (weekly-summary side).

Covers parsing the license snapshot from Outrider Issue bodies + the
transition logic that fires when a previously-blocked recommendation's
upstream license becomes permissive.

Run with: pytest tests/test_license_watch.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402
from run import Target  # noqa: E402


# Sample issue body shapes that match the templates Outrider writes via
# `_render_license_section`.

ISSUE_BODY_BLOCKED_NO_CODE = """\
**Recommended paper**: [Some Paper](https://arxiv.org/abs/2310.99999v1)
**Confidence**: 🟡 moderate (Remyx relevance 0.65)

## License & code availability

🟡 No code repository surfaced — couldn't fetch a LICENSE to evaluate.

- **Code / model**: no repository or model URL surfaced in the paper.
- **License**: `(none detected)` (class: `no-code-link`, compat: 0.30)
"""

ISSUE_BODY_BLOCKED_MISSING_LICENSE = """\
**Recommended paper**: [Other Paper](https://arxiv.org/abs/2606.11111v2)
**Confidence**: 🟡 moderate (Remyx relevance 0.72)

## License & code availability

🟠 **No LICENSE file detected** — no legal permission to redistribute.

- **Code**: https://github.com/some-author/some-repo
- **License**: `(none detected)` (class: `missing`, compat: 0.00, source: `github`)
"""

ISSUE_BODY_PERMISSIVE = """\
**Recommended paper**: [Permissive Paper](https://arxiv.org/abs/2606.22222v1)

## License & code availability

🟢 Permissive license — safe to adopt.

- **Code**: https://github.com/some-author/permissive-repo
- **License**: `Apache-2.0` (class: `permissive`, compat: 1.00, source: `github`)
"""

ISSUE_BODY_NO_LICENSE_SECTION = """\
This is an Issue that doesn't have the License section
(maybe an older Outrider format or a stub Issue).
"""


# ─── _parse_license_state_from_issue_body ─────────────────────────────────


def test_parse_extracts_blocked_no_code() -> None:
    snap = run._parse_license_state_from_issue_body(ISSUE_BODY_BLOCKED_NO_CODE)
    assert snap is not None
    assert snap["spdx"] == "(none detected)"
    assert snap["klass"] == "no-code-link"
    assert snap["compat"] == 0.30
    assert "code_url" not in snap  # no URL surfaced
    assert "model_url" not in snap


def test_parse_extracts_blocked_missing_license_with_code() -> None:
    snap = run._parse_license_state_from_issue_body(
        ISSUE_BODY_BLOCKED_MISSING_LICENSE
    )
    assert snap is not None
    assert snap["klass"] == "missing"
    assert snap["compat"] == 0.00
    assert snap["source"] == "github"
    assert snap["code_url"] == "https://github.com/some-author/some-repo"


def test_parse_extracts_permissive() -> None:
    snap = run._parse_license_state_from_issue_body(ISSUE_BODY_PERMISSIVE)
    assert snap is not None
    assert snap["spdx"] == "Apache-2.0"
    assert snap["klass"] == "permissive"
    assert snap["compat"] == 1.00


def test_parse_returns_none_on_unrecognized_body() -> None:
    assert run._parse_license_state_from_issue_body(
        ISSUE_BODY_NO_LICENSE_SECTION
    ) is None
    assert run._parse_license_state_from_issue_body("") is None
    assert run._parse_license_state_from_issue_body(None) is None  # type: ignore[arg-type]


# ─── Fallback: body-scan when structured License section is absent ───────


def test_parse_fallback_picks_up_github_url_without_license_section() -> None:
    """Older Issue bodies opened before license enrichment was always-on
    may reference the paper's code repo without the structured License
    section. The fallback returns a synthetic 'no-enrichment' snapshot
    with the discovered URL, so the watch can re-check it."""
    body = (
        "**Recommended paper**: [Some Paper](https://arxiv.org/abs/2606.99999v1)\n"
        "## Why this paper is interesting\n\n"
        "The reference implementation is at https://github.com/some-author/some-repo "
        "and it covers exactly this approach.\n"
    )
    snap = run._parse_license_state_from_issue_body(body)
    assert snap is not None
    assert snap["klass"] == "no-enrichment"
    assert snap["compat"] == 0.30
    assert snap["source"] == "body-scan"
    assert snap["code_url"] == "https://github.com/some-author/some-repo"


def test_parse_fallback_filters_remyx_self_urls() -> None:
    """The orchestrator's footer references github.com/remyxai/... links
    (e.g. the Outrider repo discussions URL). Those should NOT be used
    as the paper's reference repo — filter them out."""
    body = (
        "**Recommended paper**: [Paper](https://arxiv.org/abs/2606.11111v1)\n"
        "Some discussion at https://github.com/remyxai/outrider/discussions/19.\n"
    )
    assert run._parse_license_state_from_issue_body(body) is None


def test_parse_fallback_picks_up_hf_model_url() -> None:
    """HuggingFace model card URLs in the body should also trigger the
    fallback path."""
    body = (
        "**Recommended paper**: [Paper](https://arxiv.org/abs/2606.22222v1)\n"
        "The released checkpoint is at https://huggingface.co/some-org/some-model\n"
    )
    snap = run._parse_license_state_from_issue_body(body)
    assert snap is not None
    assert snap["klass"] == "no-enrichment"
    assert snap["model_url"] == "https://huggingface.co/some-org/some-model"


def test_parse_fallback_no_urls_returns_none() -> None:
    """Body with no License section AND no code URLs → graceful None
    (matches the canonical 'paper without code release' Issue shape
    where there's nothing to watch)."""
    body = (
        "**Recommended paper**: [Theory-only Paper](https://arxiv.org/abs/2606.33333v1)\n"
        "Pure theory paper — no code release. The maintainer should decide whether "
        "the conceptual contribution is worth a write-up.\n"
    )
    assert run._parse_license_state_from_issue_body(body) is None


# ─── _is_license_newly_viable ─────────────────────────────────────────────


def test_transition_blocked_to_permissive_fires() -> None:
    prev = {"compat": 0.30, "klass": "no-code-link", "spdx": ""}
    curr = {"compat": 1.00, "klass": "permissive", "spdx": "MIT"}
    assert run._is_license_newly_viable(prev, curr)


def test_transition_missing_to_permissive_fires() -> None:
    prev = {"compat": 0.00, "klass": "missing", "spdx": ""}
    curr = {"compat": 1.00, "klass": "permissive", "spdx": "Apache-2.0"}
    assert run._is_license_newly_viable(prev, curr)


def test_no_transition_when_was_already_permissive() -> None:
    prev = {"compat": 1.00, "klass": "permissive", "spdx": "MIT"}
    curr = {"compat": 1.00, "klass": "permissive", "spdx": "MIT"}
    assert not run._is_license_newly_viable(prev, curr)


def test_no_transition_when_still_blocked() -> None:
    prev = {"compat": 0.30, "klass": "no-code-link", "spdx": ""}
    curr = {"compat": 0.30, "klass": "no-code-link", "spdx": ""}
    assert not run._is_license_newly_viable(prev, curr)


def test_no_transition_to_copyleft_only() -> None:
    """Copyleft into a permissive-target repo stays a yellow flag, not green —
    don't fire 'newly viable' for AGPL/GPL added to upstream."""
    prev = {"compat": 0.30, "klass": "no-code-link", "spdx": ""}
    curr = {"compat": 0.50, "klass": "copyleft", "spdx": "AGPL-3.0"}
    assert not run._is_license_newly_viable(prev, curr)


# ─── _arxiv_id_from_outrider_body ─────────────────────────────────────────


def test_arxiv_id_extracted_from_body() -> None:
    assert run._arxiv_id_from_outrider_body(ISSUE_BODY_BLOCKED_NO_CODE) == "2310.99999v1"
    assert run._arxiv_id_from_outrider_body(ISSUE_BODY_PERMISSIVE) == "2606.22222v1"


def test_arxiv_id_empty_when_no_link() -> None:
    assert run._arxiv_id_from_outrider_body("just some text without arxiv link") == ""
    assert run._arxiv_id_from_outrider_body("") == ""


# ─── _recheck_outrider_license_state ──────────────────────────────────────


def test_recheck_returns_none_when_no_code_url(monkeypatch) -> None:
    """A previously-no-code-link snapshot has nothing to re-check
    in-band — return None so caller skips."""
    snap = {"spdx": "", "klass": "no-code-link", "compat": 0.30, "source": None}
    assert run._recheck_outrider_license_state(snap) is None


def test_recheck_picks_up_newly_added_github_license(monkeypatch) -> None:
    """Snapshot had missing license; upstream now publishes Apache-2.0."""
    monkeypatch.setattr(
        run, "_fetch_repo_license",
        lambda owner_repo: "Apache-2.0" if owner_repo == "some-author/some-repo" else "",
    )
    snap = {
        "spdx": "", "klass": "missing", "compat": 0.00, "source": "github",
        "code_url": "https://github.com/some-author/some-repo",
    }
    curr = run._recheck_outrider_license_state(snap)
    assert curr is not None
    assert curr["spdx"] == "Apache-2.0"
    assert curr["klass"] == "permissive"
    assert curr["compat"] == 1.00


def test_recheck_handles_fetch_failure(monkeypatch) -> None:
    """If _fetch_repo_license raises, recheck still returns a structured
    result with klass='missing' instead of crashing."""

    def boom(owner_repo):
        raise RuntimeError("simulated transport error")

    monkeypatch.setattr(run, "_fetch_repo_license", boom)
    snap = {
        "spdx": "", "klass": "missing", "compat": 0.00, "source": "github",
        "code_url": "https://github.com/x/y",
    }
    curr = run._recheck_outrider_license_state(snap)
    assert curr is not None
    assert curr["klass"] == "missing"
    assert curr["compat"] == 0.00


# ─── _newly_viable_outrider_artifacts ─────────────────────────────────────


def _target() -> Target:
    return Target(repo="owner/repo", interest_id="iid")


def test_newly_viable_surfaces_transition(monkeypatch) -> None:
    """End-to-end: open Outrider Issue with missing-license snapshot, upstream
    now publishes MIT — should surface as newly viable."""
    monkeypatch.setattr(
        run, "_remyx_issues",
        lambda target, state="open": [
            {
                "number": 87,
                "title": "[Remyx Recommendation] InstructSAM",
                "html_url": "https://github.com/o/r/issues/87",
                "body": ISSUE_BODY_BLOCKED_MISSING_LICENSE,
            },
        ],
    )
    monkeypatch.setattr(
        run, "_fetch_repo_license",
        lambda owner_repo: "MIT",
    )

    out = run._newly_viable_outrider_artifacts(_target())
    assert len(out) == 1
    item = out[0]
    assert item["number"] == 87
    assert item["arxiv_id"] == "2606.11111v2"
    assert item["prev"]["klass"] == "missing"
    assert item["curr"]["klass"] == "permissive"
    assert item["curr"]["spdx"] == "MIT"


def test_newly_viable_skips_already_permissive(monkeypatch) -> None:
    """An Issue whose body recorded compat=1.00 at recommendation time
    isn't blocked — should NOT be re-checked or surfaced."""
    monkeypatch.setattr(
        run, "_remyx_issues",
        lambda target, state="open": [
            {
                "number": 5,
                "title": "[Remyx Recommendation] Already Permissive",
                "html_url": "https://github.com/o/r/issues/5",
                "body": ISSUE_BODY_PERMISSIVE,
            },
        ],
    )
    # Even if we mock _fetch_repo_license to MIT, the filter should
    # skip before reaching the re-check.
    monkeypatch.setattr(run, "_fetch_repo_license", lambda owner_repo: "MIT")

    out = run._newly_viable_outrider_artifacts(_target())
    assert out == []


def test_newly_viable_skips_no_license_section(monkeypatch) -> None:
    """An Issue body without the License section parses to None and is skipped."""
    monkeypatch.setattr(
        run, "_remyx_issues",
        lambda target, state="open": [
            {
                "number": 9,
                "title": "[Remyx Recommendation] Old-format Issue",
                "html_url": "https://github.com/o/r/issues/9",
                "body": ISSUE_BODY_NO_LICENSE_SECTION,
            },
        ],
    )
    monkeypatch.setattr(run, "_fetch_repo_license", lambda owner_repo: "MIT")

    out = run._newly_viable_outrider_artifacts(_target())
    assert out == []


def test_newly_viable_no_transition_no_event(monkeypatch) -> None:
    """Issue had no-code-link snapshot; current re-check finds nothing
    (recheck returns None) — no event surfaces."""
    monkeypatch.setattr(
        run, "_remyx_issues",
        lambda target, state="open": [
            {
                "number": 12,
                "title": "[Remyx Recommendation] No Code",
                "html_url": "https://github.com/o/r/issues/12",
                "body": ISSUE_BODY_BLOCKED_NO_CODE,
            },
        ],
    )

    out = run._newly_viable_outrider_artifacts(_target())
    assert out == []


def test_newly_viable_picks_up_url_from_comments(monkeypatch) -> None:
    """When the Issue body has no parseable License section AND no body
    URLs, but a maintainer's comment names the upstream repo, the
    comment-scan fallback should discover the URL and re-check it.

    Models the InstructSAM case: Issue #87 body has no License section,
    but the maintainer's licensing-audit comment names
    `github.com/CircleRadon/InstructSAM` — if that repo now publishes
    a permissive LICENSE, the watch surfaces it as newly viable."""

    issue = {
        "number": 87,
        "title": "[Remyx Recommendation] InstructSAM",
        "html_url": "https://github.com/o/r/issues/87",
        # Body lacks the structured License section entirely
        "body": ISSUE_BODY_NO_LICENSE_SECTION,
    }
    monkeypatch.setattr(run, "_remyx_issues", lambda target, state="open": [issue])

    def fake_gh_api(method, path):
        if "/issues/87/comments" in path:
            return [
                {
                    "user": {"login": "smellslikeml"},
                    "body": (
                        "## Licensing block — InstructSAM has no declared license\n\n"
                        "Checked the redistribution surface:\n\n"
                        "| Source | License |\n"
                        "|--|--|\n"
                        "| [CircleRadon/InstructSAM repo root]"
                        "(https://github.com/CircleRadon/InstructSAM) | No LICENSE file |"
                    ),
                }
            ]
        return []

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    monkeypatch.setattr(
        run, "_fetch_repo_license",
        lambda owner_repo: "Apache-2.0" if owner_repo == "CircleRadon/InstructSAM" else "",
    )

    out = run._newly_viable_outrider_artifacts(_target())
    assert len(out) == 1
    assert out[0]["number"] == 87
    assert out[0]["prev"]["source"] == "comments-scan"
    assert "CircleRadon/InstructSAM" in out[0]["prev"]["code_url"]
    assert out[0]["curr"]["klass"] == "permissive"


def test_newly_viable_comments_scan_filters_remyx_self_urls(monkeypatch) -> None:
    """Comments often reference the orchestrator's own URLs (e.g.
    'see github.com/remyxai/outrider/discussions/19 for more'). Those
    must be filtered out so we don't false-positive on a Remyx repo."""

    issue = {
        "number": 1, "title": "[Remyx Recommendation] Paper",
        "html_url": "https://github.com/o/r/issues/1",
        "body": ISSUE_BODY_NO_LICENSE_SECTION,
    }
    monkeypatch.setattr(run, "_remyx_issues", lambda target, state="open": [issue])

    def fake_gh_api(method, path):
        if "/issues/1/comments" in path:
            return [
                {
                    "user": {"login": "smellslikeml"},
                    "body": (
                        "See https://github.com/remyxai/outrider/discussions/19 "
                        "for the broader thread."
                    ),
                }
            ]
        return []

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    monkeypatch.setattr(
        run, "_fetch_repo_license", lambda owner_repo: "Apache-2.0",
    )

    out = run._newly_viable_outrider_artifacts(_target())
    # No paper-code URL found in comments → no transition surfaces
    assert out == []


def test_newly_viable_caps_at_max(monkeypatch) -> None:
    """With many transitioning Issues, the cap is enforced."""
    issues = [
        {
            "number": 100 + i,
            "title": f"[Remyx Recommendation] Paper {i}",
            "html_url": f"https://github.com/o/r/issues/{100 + i}",
            "body": ISSUE_BODY_BLOCKED_MISSING_LICENSE,
        }
        for i in range(10)
    ]
    monkeypatch.setattr(run, "_remyx_issues", lambda target, state="open": issues)
    monkeypatch.setattr(run, "_fetch_repo_license", lambda owner_repo: "MIT")

    out = run._newly_viable_outrider_artifacts(_target(), max_items=3)
    assert len(out) == 3


# ─── _render_newly_viable_section ─────────────────────────────────────────


def test_render_empty_transitions_returns_empty() -> None:
    assert run._render_newly_viable_section([]) == []


def test_render_newly_viable_section_shape() -> None:
    transitions = [{
        "number": 87,
        "title": "[Remyx Recommendation] InstructSAM",
        "url": "https://github.com/o/r/issues/87",
        "arxiv_id": "2606.11111v2",
        "prev": {"spdx": "", "klass": "missing", "compat": 0.00, "source": "github"},
        "curr": {"spdx": "Apache-2.0", "klass": "permissive", "compat": 1.00, "source": "github"},
    }]
    lines = run._render_newly_viable_section(transitions)
    body = "\n".join(lines)
    assert "Newly viable recommendations" in body
    assert "Issue #87" in body
    assert "Apache-2.0" in body
    assert "no declared license" in body  # prev_label fallback for empty spdx
    assert "missing" in body  # prev_klass
    assert "Re-run selection" in body
