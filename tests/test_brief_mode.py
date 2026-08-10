"""Lead-content-only ``mode=brief`` — draft-a-PR-from-a-design-brief.

The paper-anchored ``process_target`` and the paper-less
``run_brief_mode`` compose the same leaf helpers (prepare_workdir,
write_spec_bundle, invoke_claude_code, validate_changes,
commit_and_push, build_pr_body, open_pr). This file covers the
brief-mode surface end-to-end using mocked leaves so we exercise the
real composition without a live git / API round-trip.

Coverage:

  1. ``_recommendation_from_brief`` — factory contract: arxiv_id="",
     brief lands in suggested_experiment, title extraction, empty-input
     rejection.
  2. Mode allowlist — ``mode=brief`` is recognized in main's dispatch
     guard.
  3. ``run_brief_mode`` skip-path — empty brief returns a clean
     ``skipped_brief_missing_lead_content`` result.
  4. Renderer variants — brief-mode SPEC.md, INVOCATION.md, and PR
     body drop paper-anchored references and emit their brief
     counterparts. PAPER.md is not written.
  5. ``_brief_branch_name`` — the branch-slug derivation.
  6. ``run_brief_mode`` composition — mocked leaves confirm the flow
     wires bundle → agent → validate → push → PR-open in order and
     returns the expected status dict.

Run with: pytest tests/test_brief_mode.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# ─── _recommendation_from_brief ─────────────────────────────────────


def test_factory_populates_suggested_experiment_from_brief():
    """The brief lands verbatim in ``suggested_experiment`` — that's the
    slot the bundle-writer already treats as the primary target when
    ``INPUT_LEAD_CONTENT`` is set (see ``write_spec_bundle``)."""
    brief = "Add exponential backoff to the HTTP client\n\nDetails follow…"
    rec = run._recommendation_from_brief(brief)
    assert rec.suggested_experiment == brief.strip()


def test_factory_empty_arxiv_id_gates_paper_attribution():
    """``arxiv_id == ''`` is the invariant every downstream renderer
    branches on. Assert it holds — otherwise the PR body would still
    render ``Implements arXiv:`` with an empty id."""
    rec = run._recommendation_from_brief("Anything")
    assert rec.arxiv_id == ""
    assert rec.paper_abstract == ""
    assert rec.raw_paper_md == ""
    assert rec.tier == "brief"


def test_factory_extracts_short_first_line_as_title():
    """A human-authored heading becomes the PR title / branch slug —
    stripped of leading ``#`` and trailing whitespace."""
    brief = "# Add exponential backoff to the HTTP client\n\nMotivation…"
    rec = run._recommendation_from_brief(brief)
    assert rec.paper_title == "Add exponential backoff to the HTTP client"


def test_factory_truncates_when_first_line_is_prose():
    """When the first line reads as prose (ends with a period, or is
    outside the 5-100 char band), fall back to a truncated marker so
    the branch name / PR title stay legible."""
    brief = (
        "This is a long paragraph of prose that never got a heading. "
        "It just keeps going without any clear title on the first line."
    )
    rec = run._recommendation_from_brief(brief)
    assert rec.paper_title.startswith("Design brief: ")
    assert len(rec.paper_title) <= 80


def test_factory_carries_interest_context_through():
    """A configured ResearchInterest body still flows to the coding
    agent even when no paper anchors the dispatch."""
    rec = run._recommendation_from_brief(
        "Do a thing", interest_context="Focus: rate limiting"
    )
    assert rec.interest_context == "Focus: rate limiting"


def test_factory_rejects_empty_brief():
    """An empty brief is a caller bug — the brief IS the spec in this
    mode; there's nothing to implement without it."""
    with pytest.raises(ValueError, match="non-empty design brief"):
        run._recommendation_from_brief("")
    with pytest.raises(ValueError, match="non-empty design brief"):
        run._recommendation_from_brief("   \n  \n\t")


# ─── run_brief_mode dispatch ────────────────────────────────────────


def _minimal_target():
    return run.Target(repo="owner/name")


def test_brief_mode_empty_input_returns_skipped_status(monkeypatch, caplog):
    """Missing INPUT_LEAD_CONTENT yields a clean skip — the runner's
    generic ``except Exception`` in ``main`` doesn't need to catch."""
    monkeypatch.delenv("INPUT_LEAD_CONTENT", raising=False)
    result = run.run_brief_mode(_minimal_target())
    assert result["status"] == "skipped_brief_missing_lead_content"
    assert result["target_repo"] == "owner/name"


# ─── Mode allowlist ─────────────────────────────────────────────────


def test_brief_is_in_mode_allowlist():
    """``mode=brief`` is a recognized dispatch — the guard in ``main``
    that rejects unknown modes must let it through."""
    source = (Path(__file__).resolve().parent.parent / "src" / "run.py").read_text()
    # Cheap check that "brief" appears in the mode-allowlist tuple next
    # to the other modes. Full string-match is brittle; this asserts the
    # token is present in a context that only exists in main()'s guard.
    assert '"issue-convention", "brief"' in source


# ─── Renderer variants ──────────────────────────────────────────────


def test_brief_invocation_forbids_git_commands():
    """The brief-mode INVOCATION.md must include the same git-guard
    the paper-anchored one has — without it, the coding agent may
    commit its in-progress work on the checked-out branch, tripping
    the commit_and_push HEAD-matches-origin sanity check and
    surfacing brief_failed.

    Regression: the pre-merge VQASynth dispatch against issue #33
    crashed with ``local HEAD (...) doesn't match origin/main`` after
    the coding agent ran commits during its session. The paper-anchored
    template guards against this; brief mode was missing the block."""
    template = run._INVOCATION_MD_TEMPLATE_BRIEF
    assert "CRITICAL: do not run git commands" in template
    assert "MUST NOT run any `git` command" in template


def test_pr_body_brief_variant_omits_arxiv_attribution():
    """No paper anchor → no ``Implements [paper](arxiv)`` line, no
    "Discovery context" collapse. The brief itself carries the "why"
    in a "Design brief" collapse."""
    rec = run._recommendation_from_brief("# Add exponential backoff\n\nDetails.")
    body = run.build_pr_body(_minimal_target(), rec, tests_status="unvalidated", test_output="")
    assert "arxiv.org/abs/" not in body
    assert "Discovery context" not in body
    assert "Design brief" in body
    assert "Add exponential backoff" in body
    assert "Remyx Recommendation" in body  # attribution footer still lands


def test_pr_body_paper_variant_unchanged_by_brief_gating():
    """Paper-anchored rec (arxiv_id populated) still renders the
    classic body — the gating shouldn't leak into the paper path."""
    rec = run.Recommendation(
        paper_title="Test Paper", arxiv_id="2401.12345",
        tier="high", z_score=0.0, spec_md="", paper_abstract="abstract",
        domain_summary="", raw_paper_md="",
    )
    body = run.build_pr_body(_minimal_target(), rec, tests_status="passed", test_output="")
    assert "arxiv.org/abs/2401.12345" in body
    assert "Discovery context" in body
    assert "Design brief" not in body


# ─── _brief_branch_name ─────────────────────────────────────────────


def test_brief_branch_name_slugifies_title():
    branch = run._brief_branch_name("Add exponential backoff to the HTTP client")
    # Length cap is 40 chars on the slug tail so the branch stays
    # readable in the GitHub UI; the exact truncation point depends on
    # the input length. Anchor on prefix + human-readable prefix of the
    # slug rather than pin the whole string.
    assert branch.startswith(run.BRANCH_PREFIX)
    slug = branch.removeprefix(run.BRANCH_PREFIX)
    assert slug.startswith("add-exponential-backoff-to-the-http")
    assert len(slug) <= 40


def test_brief_branch_name_strips_pr_title_prefix():
    """The ``[Remyx Recommendation]`` prefix that PR titles carry is
    stripped from the branch slug — otherwise every brief-mode branch
    would start with the same 20 characters."""
    branch = run._brief_branch_name(
        f"{run.PR_TITLE_PREFIX} Fix the pagination bug"
    )
    assert "fix-the-pagination-bug" in branch
    assert "remyx-recommendation" == branch.split("/")[0]


def test_brief_branch_name_falls_back_on_empty_slug():
    """Punctuation-only titles slug to nothing → fall back to ``brief``
    rather than emit ``remyx-recommendation/`` with an empty tail."""
    branch = run._brief_branch_name("!!!???")
    assert branch == "remyx-recommendation/brief"


# ─── run_brief_mode composition (mocked leaves) ─────────────────────


def test_run_brief_mode_composes_leaves(monkeypatch, tmp_path):
    """Happy-path: a real brief flows through prepare_workdir →
    write_spec_bundle → invoke_claude_code → validate_changes →
    commit_and_push → build_pr_body → open_pr, and the result dict
    carries the expected keys."""
    monkeypatch.setenv("INPUT_LEAD_CONTENT", "# Add exp backoff\n\nDetails.")

    workdir = tmp_path / "clone"
    workdir.mkdir()
    calls = []

    def fake_prepare_workdir(target):
        calls.append("prepare_workdir")
        return workdir

    def fake_detect_package(w):
        calls.append("detect_package_name")
        return "mypkg"

    def fake_detect_branch(w):
        calls.append("detect_default_branch")
        return "main"

    def fake_load_environments(w):
        return ""

    def fake_write_spec_bundle(w, target, rec, package, **kw):
        calls.append("write_spec_bundle")
        assert rec.arxiv_id == ""
        assert rec.suggested_experiment == "# Add exp backoff\n\nDetails."
        assert package == "mypkg"

    def fake_invoke_claude(w, timeout_s):
        calls.append("invoke_claude_code")
        return (True, "ok")

    def fake_validate_changes(w, target, package):
        calls.append("validate_changes")
        return (True, [])

    def fake_format_pr_title(rec, workdir=None):
        calls.append("format_pr_title")
        return f"{run.PR_TITLE_PREFIX} Add exp backoff"

    def fake_commit_and_push(w, branch, title, repo, base_branch="main"):
        calls.append("commit_and_push")
        assert branch.startswith("remyx-recommendation/")

    def fake_open_pr(target, branch, title, body, draft, base="main"):
        calls.append("open_pr")
        assert "Design brief" in body
        return ("https://github.com/owner/name/pull/42", 42)

    monkeypatch.setattr(run, "prepare_workdir", fake_prepare_workdir)
    monkeypatch.setattr(run, "detect_package_name", fake_detect_package)
    monkeypatch.setattr(run, "detect_default_branch", fake_detect_branch)
    monkeypatch.setattr(run, "_load_environments_md", fake_load_environments)
    monkeypatch.setattr(run, "write_spec_bundle", fake_write_spec_bundle)
    monkeypatch.setattr(run, "invoke_claude_code", fake_invoke_claude)
    monkeypatch.setattr(run, "validate_changes", fake_validate_changes)
    monkeypatch.setattr(run, "format_pr_title", fake_format_pr_title)
    monkeypatch.setattr(run, "commit_and_push", fake_commit_and_push)
    monkeypatch.setattr(run, "_check_canary_ack_file", lambda *a: True)
    monkeypatch.setattr(run, "open_pr", fake_open_pr)
    monkeypatch.setenv("DEBUG_KEEP_WORKDIR", "1")  # skip rmtree of tmp_path

    result = run.run_brief_mode(_minimal_target())

    assert result["status"] == "pr_opened_brief"
    assert result["pr_url"] == "https://github.com/owner/name/pull/42"
    assert result["pr_number"] == 42
    assert result["package"] == "mypkg"
    assert result["base_branch"] == "main"
    # Composition order — bundle before agent, agent before validation,
    # validation before push, push before PR-open.
    order = [c for c in calls if c in
             ("write_spec_bundle", "invoke_claude_code", "validate_changes",
              "commit_and_push", "open_pr")]
    assert order == [
        "write_spec_bundle", "invoke_claude_code", "validate_changes",
        "commit_and_push", "open_pr",
    ]


def test_run_brief_mode_short_circuits_on_claude_failure(monkeypatch, tmp_path):
    """Agent CLI failure → status=claude_failed, no push, no PR."""
    monkeypatch.setenv("INPUT_LEAD_CONTENT", "brief text here")
    workdir = tmp_path / "clone"
    workdir.mkdir()
    push_ran = {"v": False}
    open_pr_ran = {"v": False}

    monkeypatch.setattr(run, "prepare_workdir", lambda t: workdir)
    monkeypatch.setattr(run, "detect_package_name", lambda w: "pkg")
    monkeypatch.setattr(run, "detect_default_branch", lambda w: "main")
    monkeypatch.setattr(run, "_load_environments_md", lambda w: "")
    monkeypatch.setattr(run, "write_spec_bundle",
                        lambda *a, **kw: None)
    monkeypatch.setattr(run, "invoke_claude_code",
                        lambda w, timeout_s: (False, "claude error output"))

    def _push(*a, **kw): push_ran["v"] = True
    def _open(*a, **kw): open_pr_ran["v"] = True; return ("", 0)
    monkeypatch.setattr(run, "commit_and_push", _push)
    monkeypatch.setattr(run, "_check_canary_ack_file", lambda *a: True)
    monkeypatch.setattr(run, "open_pr", _open)
    monkeypatch.setenv("DEBUG_KEEP_WORKDIR", "1")

    result = run.run_brief_mode(_minimal_target())
    assert result["status"] == "claude_failed"
    assert not push_ran["v"]
    assert not open_pr_ran["v"]


def test_run_brief_mode_routes_to_issue_fallback(monkeypatch, tmp_path):
    """Agent wrote OPEN_AS_ISSUE.md (brief too broad / no call site) →
    status=brief_declined_open_as_issue, no push, no PR. The
    Issue-open plumbing itself is not wired yet in this draft; the
    branch is left un-pushed for manual review."""
    monkeypatch.setenv("INPUT_LEAD_CONTENT", "add a whole subsystem end to end")
    workdir = tmp_path / "clone"
    workdir.mkdir()
    (workdir / run.BUNDLE_DIR_NAME).mkdir()
    (workdir / run.ISSUE_FALLBACK_FILENAME).write_text(
        "The brief spans too many surfaces for a single PR."
    )
    push_ran = {"v": False}
    open_pr_ran = {"v": False}

    monkeypatch.setattr(run, "prepare_workdir", lambda t: workdir)
    monkeypatch.setattr(run, "detect_package_name", lambda w: "pkg")
    monkeypatch.setattr(run, "detect_default_branch", lambda w: "main")
    monkeypatch.setattr(run, "_load_environments_md", lambda w: "")
    monkeypatch.setattr(run, "write_spec_bundle", lambda *a, **kw: None)
    monkeypatch.setattr(run, "invoke_claude_code",
                        lambda w, timeout_s: (True, "wrote OPEN_AS_ISSUE.md"))
    def _push(*a, **kw): push_ran["v"] = True
    def _open(*a, **kw): open_pr_ran["v"] = True; return ("", 0)
    monkeypatch.setattr(run, "commit_and_push", _push)
    monkeypatch.setattr(run, "_check_canary_ack_file", lambda *a: True)
    monkeypatch.setattr(run, "open_pr", _open)
    monkeypatch.setenv("DEBUG_KEEP_WORKDIR", "1")

    result = run.run_brief_mode(_minimal_target())
    assert result["status"] == "brief_declined_open_as_issue"
    assert "too many surfaces" in result["issue_fallback_body"]
    assert not push_ran["v"]
    assert not open_pr_ran["v"]


def test_run_brief_mode_fetches_interest_context_when_id_set(monkeypatch, tmp_path):
    """Target with interest_id configured → lightweight interest fetch
    runs and populates rec.interest_context / interest_name /
    experiment_history. Coding agent gets the same repo-shaping
    context the paper-anchored path has, minus the ranker output."""
    monkeypatch.setenv("INPUT_LEAD_CONTENT", "brief text")
    fetch_calls = []

    def fake_fetch(interest_id):
        fetch_calls.append(interest_id)
        return ("MyResearchInterest", "focus body here", "history bullets")

    bundle_seen = {}
    def fake_write_spec_bundle(w, target, rec, package, **kw):
        bundle_seen["rec"] = rec

    workdir = tmp_path / "clone"
    workdir.mkdir()
    monkeypatch.setattr(run, "prepare_workdir", lambda t: workdir)
    monkeypatch.setattr(run, "detect_package_name", lambda w: "pkg")
    monkeypatch.setattr(run, "detect_default_branch", lambda w: "main")
    monkeypatch.setattr(run, "_load_environments_md", lambda w: "")
    monkeypatch.setattr(run, "_fetch_interest_context", fake_fetch)
    monkeypatch.setattr(run, "write_spec_bundle", fake_write_spec_bundle)
    monkeypatch.setattr(run, "invoke_claude_code",
                        lambda w, timeout_s: (True, "ok"))
    monkeypatch.setattr(run, "validate_changes", lambda w, t, p: (True, []))
    monkeypatch.setattr(run, "format_pr_title",
                        lambda rec, workdir=None: f"{run.PR_TITLE_PREFIX} T")
    monkeypatch.setattr(run, "commit_and_push", lambda *a, **kw: None)
    monkeypatch.setattr(run, "_check_canary_ack_file", lambda *a: True)
    monkeypatch.setattr(run, "open_pr",
                        lambda *a, **kw: ("https://x/pull/1", 1))
    monkeypatch.setenv("DEBUG_KEEP_WORKDIR", "1")

    target = run.Target(repo="owner/name", interest_id="abc-123")
    result = run.run_brief_mode(target)

    assert result["status"] == "pr_opened_brief"
    assert fetch_calls == ["abc-123"]
    rec = bundle_seen["rec"]
    assert rec.interest_context == "focus body here"
    assert rec.interest_name == "MyResearchInterest"
    assert rec.experiment_history == "history bullets"


def test_run_brief_mode_skips_interest_fetch_when_no_id(monkeypatch, tmp_path):
    """Self-hosted / setup-local paths without a Remyx interest still
    work — the fetch is skipped entirely rather than hitting the
    engine with an empty id (which would 404 and log noise)."""
    monkeypatch.setenv("INPUT_LEAD_CONTENT", "brief text")
    fetch_calls = []
    monkeypatch.setattr(run, "_fetch_interest_context",
                        lambda i: fetch_calls.append(i) or ("", "", ""))

    workdir = tmp_path / "clone"
    workdir.mkdir()
    monkeypatch.setattr(run, "prepare_workdir", lambda t: workdir)
    monkeypatch.setattr(run, "detect_package_name", lambda w: "pkg")
    monkeypatch.setattr(run, "detect_default_branch", lambda w: "main")
    monkeypatch.setattr(run, "_load_environments_md", lambda w: "")
    monkeypatch.setattr(run, "write_spec_bundle", lambda *a, **kw: None)
    monkeypatch.setattr(run, "invoke_claude_code",
                        lambda w, timeout_s: (True, "ok"))
    monkeypatch.setattr(run, "validate_changes", lambda w, t, p: (True, []))
    monkeypatch.setattr(run, "format_pr_title",
                        lambda rec, workdir=None: f"{run.PR_TITLE_PREFIX} T")
    monkeypatch.setattr(run, "commit_and_push", lambda *a, **kw: None)
    monkeypatch.setattr(run, "_check_canary_ack_file", lambda *a: True)
    monkeypatch.setattr(run, "open_pr",
                        lambda *a, **kw: ("https://x/pull/1", 1))
    monkeypatch.setenv("DEBUG_KEEP_WORKDIR", "1")

    result = run.run_brief_mode(_minimal_target())  # no interest_id
    assert result["status"] == "pr_opened_brief"
    assert fetch_calls == []


def test_run_brief_mode_survives_interest_fetch_failure(monkeypatch, tmp_path):
    """_fetch_interest_context is best-effort — a network / auth failure
    must not block the brief-mode flow. Empty context still lets the
    coding agent read the brief + ORIENTATION.md."""
    monkeypatch.setenv("INPUT_LEAD_CONTENT", "brief text")

    # _fetch_interest_context has its own try/except that swallows
    # failures and returns empty tuple. Simulate that outer contract
    # here.
    monkeypatch.setattr(run, "_fetch_interest_context", lambda i: ("", "", ""))

    workdir = tmp_path / "clone"
    workdir.mkdir()
    monkeypatch.setattr(run, "prepare_workdir", lambda t: workdir)
    monkeypatch.setattr(run, "detect_package_name", lambda w: "pkg")
    monkeypatch.setattr(run, "detect_default_branch", lambda w: "main")
    monkeypatch.setattr(run, "_load_environments_md", lambda w: "")
    monkeypatch.setattr(run, "write_spec_bundle", lambda *a, **kw: None)
    monkeypatch.setattr(run, "invoke_claude_code",
                        lambda w, timeout_s: (True, "ok"))
    monkeypatch.setattr(run, "validate_changes", lambda w, t, p: (True, []))
    monkeypatch.setattr(run, "format_pr_title",
                        lambda rec, workdir=None: f"{run.PR_TITLE_PREFIX} T")
    monkeypatch.setattr(run, "commit_and_push", lambda *a, **kw: None)
    monkeypatch.setattr(run, "_check_canary_ack_file", lambda *a: True)
    monkeypatch.setattr(run, "open_pr",
                        lambda *a, **kw: ("https://x/pull/1", 1))
    monkeypatch.setenv("DEBUG_KEEP_WORKDIR", "1")

    target = run.Target(repo="owner/name", interest_id="abc-123")
    result = run.run_brief_mode(target)
    assert result["status"] == "pr_opened_brief"


# ─── Issue-ref extraction ───────────────────────────────────────────


def test_extract_issue_refs_short_form():
    """Bare ``#N`` inside prose becomes a short ref."""
    brief = "Motivated by #53. Should also touch #77 downstream."
    refs = run._extract_issue_refs(brief, target_repo="owner/name")
    assert refs == ["#53", "#77"]


def test_extract_issue_refs_full_url_same_repo():
    """A full issue URL pointing at the target repo becomes a short
    ref — GitHub renders these the same as ``#N`` inside the same
    repo but the short form is what maintainers write."""
    brief = "See https://github.com/remyxai/VQASynth/issues/53 for context."
    refs = run._extract_issue_refs(brief, target_repo="remyxai/VQASynth")
    assert refs == ["#53"]


def test_extract_issue_refs_full_url_cross_repo():
    """Cross-repo URLs get the qualified form so the link resolves
    against the right issue tracker."""
    brief = "See https://github.com/some-org/upstream/issues/9 in upstream."
    refs = run._extract_issue_refs(brief, target_repo="remyxai/VQASynth")
    assert refs == ["some-org/upstream#9"]


def test_extract_issue_refs_deduplicates():
    """Same ref via URL + short form is one entry."""
    brief = (
        "Fix requested in #53 — see "
        "https://github.com/remyxai/VQASynth/issues/53."
    )
    refs = run._extract_issue_refs(brief, target_repo="remyxai/VQASynth")
    assert refs == ["#53"]


def test_extract_issue_refs_ignores_bare_text():
    """No refs in a brief with no ``#N`` or issue URLs."""
    refs = run._extract_issue_refs("Add exponential backoff.", "o/n")
    assert refs == []


# ─── License enrichment for brief mode ──────────────────────────────


def test_enrich_brief_licenses_populates_from_github_url(monkeypatch):
    """A GitHub URL in the brief → license fetched, license_class set."""
    monkeypatch.setattr(run, "_fetch_repo_license",
                        lambda slug: "CC-BY-4.0" if slug == "SpatialVision/Orient-Anything" else "Apache-2.0")
    monkeypatch.setattr(run, "_fetch_hf_license", lambda slug: "")

    rec = run._recommendation_from_brief(
        "# orient anything\n\nSee https://github.com/SpatialVision/Orient-Anything for context."
    )
    target = run.Target(repo="remyxai/VQASynth")
    run._enrich_brief_licenses(rec, rec.suggested_experiment, target)

    assert rec.paper_github_url == "https://github.com/SpatialVision/Orient-Anything"
    assert rec.paper_license == "CC-BY-4.0"
    # The classifier currently buckets CC-BY-4.0 as "permissive" (it's
    # attribution-only, no share-alike). Debatable for source-code
    # licensing — CC-BY lacks patent-grant terms MIT/Apache-2.0 carry —
    # but that's a classifier-level policy call, not a brief-mode issue.
    # What matters here: the license fetch ran and the SPDX ID is
    # visible to the maintainer in the rendered PR body.
    assert rec.license_class in ("permissive", "copyleft", "nc", "unknown")
    assert rec.license_source == "github"


def test_enrich_brief_licenses_skips_self_link(monkeypatch):
    """A brief that references the target repo itself doesn't produce
    a spurious license warning against the target."""
    fetches = []
    monkeypatch.setattr(
        run, "_fetch_repo_license",
        lambda slug: fetches.append(slug) or "Apache-2.0",
    )
    monkeypatch.setattr(run, "_fetch_hf_license", lambda slug: "")

    rec = run._recommendation_from_brief(
        "See https://github.com/remyxai/VQASynth for the repo we're editing."
    )
    target = run.Target(repo="remyxai/VQASynth")
    run._enrich_brief_licenses(rec, rec.suggested_experiment, target)

    assert rec.paper_github_url == ""
    # Only the target's own license fetch happened, not the self-link.
    assert fetches == ["remyxai/VQASynth"]


def test_enrich_brief_licenses_survives_fetch_failure(monkeypatch):
    """License fetch is best-effort per URL — a failure on one repo
    lands as class=missing / empty SPDX and enrichment proceeds. The
    brief-mode flow must never block on network flakes."""
    def boom(slug):
        raise RuntimeError("network fetch failed")
    monkeypatch.setattr(run, "_fetch_repo_license", boom)
    monkeypatch.setattr(run, "_fetch_hf_license", lambda slug: "")

    rec = run._recommendation_from_brief(
        "Brief referencing https://github.com/some/repo more text"
    )
    target = run.Target(repo="owner/name")
    # No raise — swallowed by _fetch_referenced_license
    run._enrich_brief_licenses(rec, rec.suggested_experiment, target)
    assert rec.referenced_licenses == [("github", "some/repo", "", "missing")]


def test_pr_body_brief_variant_renders_license_section(monkeypatch):
    """When the brief cites an external repo with a non-permissive
    license, the PR body carries the license warning section — same
    format the paper-anchored path uses."""
    monkeypatch.setattr(run, "_fetch_repo_license",
                        lambda slug: "CC-BY-4.0" if "Orient" in slug else "Apache-2.0")
    monkeypatch.setattr(run, "_fetch_hf_license", lambda slug: "")

    rec = run._recommendation_from_brief(
        "# orient anything\n\nSee https://github.com/SpatialVision/Orient-Anything."
    )
    target = run.Target(repo="remyxai/VQASynth")
    run._enrich_brief_licenses(rec, rec.suggested_experiment, target)
    body = run.build_pr_body(target, rec, tests_status="unvalidated", test_output="")
    assert "License & code availability" in body
    assert "CC-BY-4.0" in body


# ─── Multi-license enrichment ───────────────────────────────────────


def test_extract_all_referenced_repos_dedupes_and_orders():
    """Every distinct GitHub + HF URL surfaces, preserving text order,
    with (kind, slug) tuples. Duplicates collapse."""
    brief = (
        "See https://github.com/microsoft/LLM2CLIP\n"
        "and https://github.com/google-deepmind/magiclens\n"
        "plus https://huggingface.co/salma-remyx/PoseText\n"
        "and again https://github.com/microsoft/LLM2CLIP (dup)"
    )
    refs = run._extract_all_referenced_repos(brief, target_repo="o/n")
    assert refs == [
        ("github", "microsoft/LLM2CLIP"),
        ("github", "google-deepmind/magiclens"),
        ("huggingface", "salma-remyx/PoseText"),
    ]


def test_extract_all_referenced_repos_caps_at_limit():
    """A brief legitimately citing more than the cap gets truncated to
    the first N; the tail rarely changes the adoption verdict."""
    brief = "\n".join(
        f"https://github.com/org/repo{i}" for i in range(_expected_max_refs() + 3)
    )
    refs = run._extract_all_referenced_repos(brief, target_repo="o/n")
    assert len(refs) == _expected_max_refs()


def _expected_max_refs():
    return run._BRIEF_LICENSE_CAP


def test_extract_all_referenced_repos_excludes_target_self_link():
    brief = "See https://github.com/owner/target and https://github.com/other/repo more"
    refs = run._extract_all_referenced_repos(brief, target_repo="owner/target")
    assert refs == [("github", "other/repo")]


def test_enrich_brief_licenses_populates_referenced_licenses_list(monkeypatch):
    """Multi-repo brief → each cited repo gets its own entry in
    ``rec.referenced_licenses``, with best-effort license fetched."""
    def fake_fetch_repo(slug):
        return {
            "microsoft/LLM2CLIP": "MIT",
            "google-deepmind/magiclens": "Apache-2.0",
            "remyxai/VQASynth": "Apache-2.0",
        }.get(slug, "")
    def fake_fetch_hf(slug):
        return "CC-BY-4.0" if slug == "salma-remyx/PoseText" else ""
    monkeypatch.setattr(run, "_fetch_repo_license", fake_fetch_repo)
    monkeypatch.setattr(run, "_fetch_hf_license", fake_fetch_hf)

    rec = run._recommendation_from_brief(
        "https://github.com/microsoft/LLM2CLIP\n"
        "https://github.com/google-deepmind/magiclens\n"
        "https://huggingface.co/salma-remyx/PoseText"
    )
    target = run.Target(repo="remyxai/VQASynth")
    run._enrich_brief_licenses(rec, rec.suggested_experiment, target)

    assert len(rec.referenced_licenses) == 3
    kinds_slugs_spdx = [
        (k, s, sp) for (k, s, sp, _cl) in rec.referenced_licenses
    ]
    assert kinds_slugs_spdx == [
        ("github", "microsoft/LLM2CLIP", "MIT"),
        ("github", "google-deepmind/magiclens", "Apache-2.0"),
        ("huggingface", "salma-remyx/PoseText", "CC-BY-4.0"),
    ]


def test_pr_body_brief_variant_renders_all_referenced_licenses(monkeypatch):
    """The multi-license section shows one line per cited repo, each
    with its SPDX id and classifier verdict — a maintainer scanning
    the PR sees the full referenced-code inventory at once."""
    monkeypatch.setattr(
        run, "_fetch_repo_license",
        lambda slug: {
            "vikhyat/moondream": "Apache-2.0",
            "fkryan/gazelle": "MIT",
            "remyxai/VQASynth": "Apache-2.0",
        }.get(slug, ""),
    )
    monkeypatch.setattr(
        run, "_fetch_hf_license",
        lambda slug: "cc-by-4.0" if slug == "salma-remyx/PoseText" else "",
    )

    rec = run._recommendation_from_brief(
        "https://github.com/vikhyat/moondream\n"
        "https://github.com/fkryan/gazelle\n"
        "https://huggingface.co/salma-remyx/PoseText"
    )
    target = run.Target(repo="remyxai/VQASynth")
    run._enrich_brief_licenses(rec, rec.suggested_experiment, target)
    body = run.build_pr_body(target, rec, tests_status="unvalidated", test_output="")

    assert "License & code availability" in body
    assert "vikhyat/moondream" in body
    assert "fkryan/gazelle" in body
    assert "salma-remyx/PoseText" in body
    # Each SPDX id appears
    for token in ("Apache-2.0", "MIT", "cc-by-4.0"):
        assert token in body


def test_pr_body_no_refs_omits_license_section():
    """A brief that cites no external repos → no License section
    pollutes the body. Same behavior as the paper-anchored path when
    the license fetch didn't run."""
    rec = run._recommendation_from_brief("Add feature X against the local module.")
    body = run.build_pr_body(_minimal_target(), rec, tests_status="unvalidated", test_output="")
    assert "License & code availability" not in body


def test_user_attachments_url_not_treated_as_repo():
    """Issue-body image uploads (github.com/user-attachments/assets/UUID)
    were surfacing as 'user-attachments/assets' repos with '(no
    LICENSE)' warnings. Regression: VQASynth #31 body carries an
    image URL of this shape. It must be filtered."""
    brief = (
        "Description with an image "
        "![img](https://github.com/user-attachments/assets/abc-123-def)"
    )
    refs = run._extract_all_referenced_repos(brief, target_repo="o/n")
    assert refs == []


def test_huggingface_dataset_url_surfaces_as_dataset_kind():
    """HF Hub dataset URLs (huggingface.co/datasets/<owner>/<name>)
    surface with kind='huggingface-dataset'. Previously filtered by
    the non-model-owners list, which meant dataset citations never
    got a license line — real gap since briefs often anchor on a
    dataset (VQASynth #31 → salma-remyx/PoseText)."""
    brief = "See https://huggingface.co/datasets/salma-remyx/PoseText"
    refs = run._extract_all_referenced_repos(brief, target_repo="o/n")
    assert refs == [("huggingface-dataset", "salma-remyx/PoseText")]


def test_fetch_referenced_license_dispatches_to_dataset_fetcher(monkeypatch):
    """kind='huggingface-dataset' calls the dataset endpoint fetcher,
    not the models one — the endpoints are distinct and a model-name
    slug for a dataset would 404."""
    calls = []
    def fake_dataset(slug):
        calls.append(("dataset", slug))
        return "cc-by-nc-4.0"
    def fake_model(slug):
        calls.append(("model", slug))
        return "apache-2.0"  # should NOT be called
    monkeypatch.setattr(run, "_fetch_hf_dataset_license", fake_dataset)
    monkeypatch.setattr(run, "_fetch_hf_license", fake_model)

    result = run._fetch_referenced_license("huggingface-dataset", "owner/ds")
    assert result == "cc-by-nc-4.0"
    assert calls == [("dataset", "owner/ds")]


def test_multi_license_renders_dataset_url_shape(monkeypatch):
    """A dataset ref renders as ``[datasets/owner/name](full-url)`` so
    a maintainer can tell at a glance whether the reference is a
    model or a dataset."""
    monkeypatch.setattr(run, "_fetch_repo_license", lambda slug: "Apache-2.0")
    monkeypatch.setattr(run, "_fetch_hf_license", lambda slug: "")
    monkeypatch.setattr(
        run, "_fetch_hf_dataset_license",
        lambda slug: "cc-by-4.0" if slug == "salma-remyx/PoseText" else "",
    )
    rec = run._recommendation_from_brief(
        "Ref: https://huggingface.co/datasets/salma-remyx/PoseText"
    )
    target = run.Target(repo="remyxai/VQASynth")
    run._enrich_brief_licenses(rec, rec.suggested_experiment, target)
    body = run.build_pr_body(target, rec, tests_status="unvalidated", test_output="")
    assert "datasets/salma-remyx/PoseText" in body
    assert "huggingface.co/datasets/salma-remyx/PoseText" in body
    assert "cc-by-4.0" in body


def test_multi_license_survives_individual_fetch_failure(monkeypatch):
    """One repo's fetch failure doesn't stop the others from surfacing.
    The failing repo lands with class=missing and empty SPDX."""
    def fake_fetch_repo(slug):
        if slug == "broken/repo":
            raise RuntimeError("network fetch failed")
        return "Apache-2.0"
    monkeypatch.setattr(run, "_fetch_repo_license", fake_fetch_repo)
    monkeypatch.setattr(run, "_fetch_hf_license", lambda slug: "")

    rec = run._recommendation_from_brief(
        "https://github.com/broken/repo\n"
        "https://github.com/ok/repo"
    )
    target = run.Target(repo="o/n")
    run._enrich_brief_licenses(rec, rec.suggested_experiment, target)

    slugs = [s for (_k, s, _sp, _cl) in rec.referenced_licenses]
    classes = [cl for (_k, _s, _sp, cl) in rec.referenced_licenses]
    assert "broken/repo" in slugs
    assert "ok/repo" in slugs
    assert "missing" in classes  # broken/repo fell back to missing
    assert "permissive" in classes  # ok/repo classified as expected


def test_pr_body_brief_variant_renders_issue_refs():
    """A brief that cites ``#53`` or a full issue URL produces a
    ``Refs: #53`` line in the PR body."""
    rec = run._recommendation_from_brief("Motivated by #53 — add feature X.")
    body = run.build_pr_body(_minimal_target(), rec, tests_status="unvalidated", test_output="")
    assert "Refs: #53" in body


def test_pr_body_brief_variant_omits_refs_line_when_none():
    """No refs in the brief → no ``Refs:`` line pollutes the body."""
    rec = run._recommendation_from_brief("Add feature X.")
    body = run.build_pr_body(_minimal_target(), rec, tests_status="unvalidated", test_output="")
    assert "Refs:" not in body


# ─── Regression coverage for prior bugs ─────────────────────────────


def test_run_brief_mode_honors_publish_branch(monkeypatch, tmp_path):
    """INPUT_PUBLISH=branch short-circuits after the branch push — no
    PR object gets created, and the result carries a
    ``branch_pushed_no_pr`` status matching the paper-anchored path.

    Regression: the first v1.7.46 draft always called ``open_pr``,
    ignoring publish=branch — surfaced by the pre-merge VQASynth
    dispatch (issue #53) opening PR #121 despite publish=branch on
    the wire."""
    monkeypatch.setenv("INPUT_LEAD_CONTENT", "brief")
    monkeypatch.setenv("INPUT_PUBLISH", "branch")
    workdir = tmp_path / "clone"
    workdir.mkdir()
    open_pr_ran = {"v": False}

    monkeypatch.setattr(run, "prepare_workdir", lambda t: workdir)
    monkeypatch.setattr(run, "detect_package_name", lambda w: "pkg")
    monkeypatch.setattr(run, "detect_default_branch", lambda w: "main")
    monkeypatch.setattr(run, "_load_environments_md", lambda w: "")
    monkeypatch.setattr(run, "write_spec_bundle", lambda *a, **kw: None)
    monkeypatch.setattr(run, "invoke_claude_code",
                        lambda w, timeout_s: (True, "ok"))
    monkeypatch.setattr(run, "validate_changes", lambda w, t, p: (True, []))
    monkeypatch.setattr(run, "format_pr_title",
                        lambda rec, workdir=None: f"{run.PR_TITLE_PREFIX} T")
    monkeypatch.setattr(run, "commit_and_push", lambda *a, **kw: None)
    monkeypatch.setattr(run, "_check_canary_ack_file", lambda *a: True)
    def _open(*a, **kw): open_pr_ran["v"] = True; return ("", 0)
    monkeypatch.setattr(run, "open_pr", _open)
    monkeypatch.setenv("DEBUG_KEEP_WORKDIR", "1")

    result = run.run_brief_mode(_minimal_target())
    assert result["status"] == "branch_pushed_no_pr"
    assert result["branch_url"].startswith("https://github.com/")
    assert not open_pr_ran["v"], "publish=branch must skip open_pr"


def test_run_brief_mode_rejects_path_violations(monkeypatch, tmp_path):
    """Diff touches out-of-bounds paths → status=rejected_path_violations,
    no push, no PR. Same gate the paper-anchored path uses."""
    monkeypatch.setenv("INPUT_LEAD_CONTENT", "brief")
    workdir = tmp_path / "clone"
    workdir.mkdir()
    push_ran = {"v": False}

    monkeypatch.setattr(run, "prepare_workdir", lambda t: workdir)
    monkeypatch.setattr(run, "detect_package_name", lambda w: "pkg")
    monkeypatch.setattr(run, "detect_default_branch", lambda w: "main")
    monkeypatch.setattr(run, "_load_environments_md", lambda w: "")
    monkeypatch.setattr(run, "write_spec_bundle", lambda *a, **kw: None)
    monkeypatch.setattr(run, "invoke_claude_code",
                        lambda w, timeout_s: (True, "ok"))
    monkeypatch.setattr(run, "validate_changes",
                        lambda w, t, p: (False, [".github/workflows/x.yml"]))
    def _push(*a, **kw): push_ran["v"] = True
    monkeypatch.setattr(run, "commit_and_push", _push)
    monkeypatch.setattr(run, "_check_canary_ack_file", lambda *a: True)
    monkeypatch.setenv("DEBUG_KEEP_WORKDIR", "1")

    result = run.run_brief_mode(_minimal_target())
    assert result["status"] == "rejected_path_violations"
    assert ".github/workflows/x.yml" in result["path_violations"]
    assert not push_ran["v"]
