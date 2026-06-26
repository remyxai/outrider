"""Tests for the Issue-route convention pass (REMYX-146).

Covers the new helpers in `src/run.py`:
  - `_fetch_issue_templates_from_repo`: walks .github/ISSUE_TEMPLATE/,
    skips config.yml, parses both markdown frontmatter and Issue Forms
    YAML, accepts .md/.yml/.yaml.
  - `_classify_template_kind`: heuristic on name+description.
  - `_filter_eligible_templates`: drops bug/question kinds.
  - `_build_issue_body_rewrite_prompt`: embeds templates + outrider body,
    instructs canonical-first folding, schema-compatible JSON output.
  - `run_issue_convention_pass`: end-to-end orchestrator with the no-templates,
    no-fitting-template, and aligned paths.

Run with: pytest tests/test_issue_convention_pass.py -q
"""
import base64
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


# ─── Template kind classifier ──────────────────────────────────────────────


@pytest.mark.parametrize("name,description,expected", [
    ("Bug Report", "Report a bug", "bug"),
    ("Crash report", "App crashes when...", "bug"),
    ("Feature Request", "Suggest a new feature", "feature"),
    ("Enhancement", "Improve an existing thing", "feature"),
    ("🤖 New model", "Add a new model to the benchmark", "new_model"),
    ("New dataset request", "Propose a dataset", "new_model"),
    ("Eval request", "Request an evaluation", "new_model"),
    ("Question", "Ask a question", "question"),
    ("Discussion", "Open a discussion", "question"),
    ("Documentation", "Improve the docs", "other"),
])
def test_classify_template_kind(name, description, expected):
    assert run._classify_template_kind(name, description) == expected


# ─── Eligible filter ───────────────────────────────────────────────────────


def test_filter_eligible_drops_bug_and_question():
    templates = [
        {"filename": "bug.md", "kind": "bug"},
        {"filename": "feature.md", "kind": "feature"},
        {"filename": "question.md", "kind": "question"},
        {"filename": "new_model.yaml", "kind": "new_model"},
        {"filename": "docs.md", "kind": "other"},
    ]
    eligible = run._filter_eligible_templates(templates)
    kinds = [t["kind"] for t in eligible]
    assert "bug" not in kinds
    assert "question" not in kinds
    assert set(kinds) == {"feature", "new_model", "other"}


# ─── Template-fetch helper ─────────────────────────────────────────────────


def _fake_listing(files):
    """Build a Contents-API-shaped directory listing for the templates."""
    return [
        {
            "type": "file",
            "name": filename,
            "content": _b64(content),
        }
        for filename, content in files
    ]


def test_fetch_issue_templates_parses_markdown_frontmatter(monkeypatch):
    md_body = (
        "---\n"
        "name: Feature Request\n"
        "about: Suggest an idea for this project\n"
        "labels: ''\n"
        "---\n"
        "\n"
        "**Is your feature request related to a problem?**\n"
        "A clear description.\n"
    )
    monkeypatch.setattr(run, "gh_api", lambda m, p, b=None: _fake_listing([
        ("feature_request.md", md_body),
    ]))
    templates = run._fetch_issue_templates_from_repo("owner/repo")
    assert len(templates) == 1
    t = templates[0]
    assert t["filename"] == "feature_request.md"
    assert t["name"] == "Feature Request"
    assert t["kind"] == "feature"
    assert "**Is your feature request related to a problem?**" in t["raw_content"]


def test_fetch_issue_templates_parses_issue_forms_yaml(monkeypatch):
    yml_body = (
        "name: 🤖 New model\n"
        "description: Create a request for a new model to be added to MTEB\n"
        "labels: [\"new model\"]\n"
        "body:\n"
        "  - type: input\n"
        "    attributes:\n"
        "      label: Model link on Hugging Face\n"
        "  - type: input\n"
        "    attributes:\n"
        "      label: Arxiv link\n"
    )
    monkeypatch.setattr(run, "gh_api", lambda m, p, b=None: _fake_listing([
        ("new_model.yaml", yml_body),
    ]))
    templates = run._fetch_issue_templates_from_repo("owner/repo")
    assert len(templates) == 1
    t = templates[0]
    assert t["name"] == "🤖 New model"
    assert t["description"] == "Create a request for a new model to be added to MTEB"
    assert t["kind"] == "new_model"


def test_fetch_issue_templates_accepts_all_three_extensions(monkeypatch):
    # haystack uses .md, ag2/dspy/lerobot use .yml, mteb uses .yaml — all
    # must be discovered. Discovery silently drops files with other
    # extensions.
    monkeypatch.setattr(run, "gh_api", lambda m, p, b=None: _fake_listing([
        ("feature.md", "---\nname: Feature\n---\nbody"),
        ("bug.yml", "name: Bug\ndescription: bug report"),
        ("new_model.yaml", "name: New model\ndescription: model addition"),
        ("README.txt", "ignored"),  # wrong extension
    ]))
    templates = run._fetch_issue_templates_from_repo("owner/repo")
    filenames = sorted(t["filename"] for t in templates)
    assert filenames == ["bug.yml", "feature.md", "new_model.yaml"]


def test_fetch_issue_templates_skips_config_yml(monkeypatch):
    monkeypatch.setattr(run, "gh_api", lambda m, p, b=None: _fake_listing([
        ("config.yml", "contact_links:\n  - name: docs\n    url: https://x"),
        ("config.yaml", "ignored too"),
        ("feature.yml", "name: Feature Request\ndescription: x"),
    ]))
    templates = run._fetch_issue_templates_from_repo("owner/repo")
    assert {t["filename"] for t in templates} == {"feature.yml"}


def test_fetch_issue_templates_returns_empty_when_dir_missing(monkeypatch):
    def fake(m, p, b=None):
        raise RuntimeError("404: Not Found")
    monkeypatch.setattr(run, "gh_api", fake)
    assert run._fetch_issue_templates_from_repo("owner/repo") == []


# ─── Body-rewrite prompt ───────────────────────────────────────────────────


def test_rewrite_prompt_embeds_templates_and_outrider_body():
    templates = [
        {
            "filename": "feature_request.yml",
            "name": "Feature Request",
            "description": "Suggest a new feature",
            "kind": "feature",
            "raw_content": "name: Feature Request\ndescription: Suggest a new feature\nbody: []",
        },
        {
            "filename": "new_model.yaml",
            "name": "🤖 New model",
            "description": "Create a request for a new model",
            "kind": "new_model",
            "raw_content": "name: 🤖 New model\nbody:\n  - type: input\n    attributes:\n      label: Arxiv link",
        },
    ]
    prompt = run._build_issue_body_rewrite_prompt(
        issue_title="[Remyx Recommendation] Cool Paper",
        current_body="**Recommended paper**: arxiv:2606.99999\n\n## Why this paper",
        eligible_templates=templates,
    )
    # All templates surface in the prompt
    assert "feature_request.yml" in prompt
    assert "new_model.yaml" in prompt
    assert "🤖 New model" in prompt
    # Outrider body is embedded
    assert "arxiv:2606.99999" in prompt
    # Output uses delimited format (TEMPLATE_ID / RATIONALE / UPDATED_BODY
    # markers) rather than JSON — long markdown bodies break JSON parsing
    # when Claude leaves an unescaped quote in a 30k-token output.
    assert "===TEMPLATE_ID===" in prompt
    assert "===RATIONALE===" in prompt
    assert "===UPDATED_BODY===" in prompt
    assert "===END===" in prompt
    # The canonical-first folding rules carry over from the PR-route
    # design (Discovery context details block + one-line attribution).
    assert "Discovery context" in prompt
    assert "Drafted by [Outrider]" in prompt


def test_rewrite_prompt_signals_empty_template_set():
    # When all templates are filtered out (e.g. lerobot's bug-only set),
    # the prompt explicitly notes the empty set so the LLM applies only
    # the scaffolding-collapse rule rather than inventing a template.
    prompt = run._build_issue_body_rewrite_prompt(
        issue_title="x", current_body="y", eligible_templates=[],
    )
    assert "none" in prompt.lower() or "(none" in prompt


# ─── Delimited rewrite-response parser ──────────────────────────────────────


def test_parse_rewrite_response_happy_path():
    raw = (
        "===TEMPLATE_ID===\n"
        "feature_request.yml\n"
        "===RATIONALE===\n"
        "folded into Feature Request template\n"
        "===UPDATED_BODY===\n"
        "**Description**\n\n"
        "Sample paper from https://arxiv.org/abs/2606.11127v1\n\n"
        '<details><summary>Discovery context</summary>...</details>\n'
        "===END===\n"
    )
    parsed = run._parse_issue_rewrite_response(raw)
    assert parsed is not None
    assert parsed["template_id"] == "feature_request.yml"
    assert "Feature Request template" in parsed["rationale"]
    assert "arxiv.org/abs/2606.11127v1" in parsed["updated_body"]
    # Body extends across multiple lines; the parser must preserve newlines
    assert "\n\n" in parsed["updated_body"]


def test_parse_rewrite_response_normalises_no_pick_sentinels():
    # Various forms the LLM might use to signal "no template picked"
    for sentinel in ("(none)", "null", "none", "<none>", ""):
        raw = (
            f"===TEMPLATE_ID===\n{sentinel}\n"
            f"===RATIONALE===\nno fit\n"
            f"===UPDATED_BODY===\n**Body**\n"
            f"===END===\n"
        )
        parsed = run._parse_issue_rewrite_response(raw)
        assert parsed is not None
        assert parsed["template_id"] == "", f"sentinel {sentinel!r} not normalised"


def test_parse_rewrite_response_body_with_special_chars():
    # The whole point of switching from JSON to delimited: the body can
    # contain ANY chars without escape concerns. Quotes, braces, backslashes.
    raw = (
        "===TEMPLATE_ID===\n"
        "feature.yml\n"
        "===RATIONALE===\n"
        "rewrite\n"
        "===UPDATED_BODY===\n"
        'Body with "quoted strings", {braces}, and \\backslashes\\.\n'
        '<details><summary>"Discovery context"</summary>x</details>\n'
        "===END===\n"
    )
    parsed = run._parse_issue_rewrite_response(raw)
    assert parsed is not None
    assert '"quoted strings"' in parsed["updated_body"]
    assert "{braces}" in parsed["updated_body"]
    assert "\\backslashes\\" in parsed["updated_body"]


def test_parse_rewrite_response_tolerates_missing_end_marker():
    # If Claude omits the ===END=== marker, the body extends to EOF.
    raw = (
        "===TEMPLATE_ID===\n"
        "feature.yml\n"
        "===RATIONALE===\n"
        "x\n"
        "===UPDATED_BODY===\n"
        "Body content here\n"
        "with multiple lines\n"
    )
    parsed = run._parse_issue_rewrite_response(raw)
    assert parsed is not None
    assert "Body content here" in parsed["updated_body"]
    assert "with multiple lines" in parsed["updated_body"]


def test_parse_rewrite_response_returns_none_when_markers_missing():
    assert run._parse_issue_rewrite_response("just prose, no markers") is None
    # Missing UPDATED_BODY marker
    assert run._parse_issue_rewrite_response(
        "===TEMPLATE_ID===\nfeature.yml\n===RATIONALE===\nx\n"
    ) is None


def test_parse_rewrite_response_returns_none_when_body_empty():
    raw = (
        "===TEMPLATE_ID===\n"
        "feature.yml\n"
        "===RATIONALE===\n"
        "x\n"
        "===UPDATED_BODY===\n"
        "\n"
        "===END===\n"
    )
    assert run._parse_issue_rewrite_response(raw) is None


def test_parse_rewrite_response_returns_none_on_out_of_order_markers():
    # UPDATED_BODY before RATIONALE → not the expected sequence.
    raw = (
        "===TEMPLATE_ID===\n"
        "feature.yml\n"
        "===UPDATED_BODY===\n"
        "body\n"
        "===RATIONALE===\n"
        "x\n"
    )
    assert run._parse_issue_rewrite_response(raw) is None


# ─── run_issue_convention_pass end-to-end ──────────────────────────────────


def _base_env(monkeypatch, **overrides):
    """Minimal env for run_issue_convention_pass; clears the relevant vars."""
    for var in ("INPUT_PR_NUMBER", "INPUT_ISSUE_NUMBER", "REMYX_MODE", "INPUT_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TARGET_REPO", "owner/repo")
    monkeypatch.setenv("INPUT_INTEREST_ID", "11111111-1111-1111-1111-111111111111")
    for var, value in overrides.items():
        monkeypatch.setenv(var, value)


def _fake_issue_dict(number=42, title="[Remyx Recommendation] Sample paper",
                      body="**Recommended paper**: [Sample paper](https://arxiv.org/abs/2606.11127v1)\n\n## Why this paper is interesting"):
    return {
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "number": number,
        "user": {"login": "remyx-ai[bot]"},
        "title": title,
        "body": body,
    }


def test_run_issue_convention_pass_skipped_no_issue_number(monkeypatch):
    _base_env(monkeypatch)
    target = run.Target(repo="owner/repo")
    result = run.run_issue_convention_pass(target)
    assert result["status"] == "issue_convention_skipped_no_issue"


def test_run_issue_convention_pass_skipped_not_bot(monkeypatch):
    _base_env(monkeypatch, INPUT_ISSUE_NUMBER="7")
    issue = _fake_issue_dict(number=7)
    issue["user"] = {"login": "some-human"}
    monkeypatch.setattr(run, "gh_api",
        lambda m, p, b=None: issue if "/issues/7" in p else {"message": "?"})
    target = run.Target(repo="owner/repo")
    result = run.run_issue_convention_pass(target)
    assert result["status"] == "issue_convention_skipped_not_bot"


def test_run_issue_convention_pass_no_templates_still_rewrites(monkeypatch):
    """No ISSUE_TEMPLATE/ directory → status is
    issue_convention_skipped_no_templates but the rewrite still runs to
    collapse Outrider scaffolding."""
    _base_env(monkeypatch, INPUT_ISSUE_NUMBER="42")
    target = run.Target(repo="owner/repo")
    issue = _fake_issue_dict()
    captured_patches = []

    def fake_gh_api(method, path, body=None):
        if method == "GET" and "/issues/42" in path:
            return issue
        if method == "GET" and "/contents/.github/ISSUE_TEMPLATE" in path:
            raise RuntimeError("404")
        if method == "POST" and "/labels" in path:
            return {}
        if method == "PATCH" and "/issues/42" in path:
            captured_patches.append(body)
            return {}
        raise AssertionError(f"unexpected call: {method} {path}")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    # Mock the Claude one-shot to return a valid rewrite. The mock body
    # must keep the arxiv link (matches _ARXIV_URL_RE) so the sanity check
    # passes — paper provenance can't be silently dropped.
    monkeypatch.setattr(run, "_run_claude_oneshot",
        lambda wd, p, t, max_turns=4: (True,
            "===TEMPLATE_ID===\n(none)\n"
            "===RATIONALE===\nscaffolding collapsed; no templates available\n"
            "===UPDATED_BODY===\n"
            "## Description\n\nSample paper from https://arxiv.org/abs/2606.11127v1\n\n"
            "<details><summary>Discovery context</summary>...</details>\n"
            "===END===\n"
        ))
    # _resolve_upstream_for_conventions falls back to target.repo when no
    # upstream is configured.
    monkeypatch.setattr(run, "_resolve_upstream_for_conventions",
        lambda target: "owner/repo")

    result = run.run_issue_convention_pass(target)
    assert result["status"] == "issue_convention_skipped_no_templates"
    assert result["templates_found"] == 0
    assert captured_patches  # PATCH was called with the rewritten body
    assert "arxiv.org/abs/2606.11127v1" in captured_patches[0]["body"]


def test_run_issue_convention_pass_no_fitting_template(monkeypatch):
    """Templates exist but all are bug/question (e.g. lerobot's
    bug-only set) → status is issue_convention_aligned_no_fitting_template.
    Rewrite still runs with scaffolding collapsed."""
    _base_env(monkeypatch, INPUT_ISSUE_NUMBER="42")
    target = run.Target(repo="owner/repo")
    issue = _fake_issue_dict()
    bug_template_body = (
        "name: Bug Report\n"
        "description: Report a bug\n"
        "body:\n"
        "  - type: textarea\n"
        "    attributes:\n"
        "      label: Steps to reproduce\n"
    )

    def fake_gh_api(method, path, body=None):
        if method == "GET" and "/issues/42" in path:
            return issue
        if method == "GET" and "/contents/.github/ISSUE_TEMPLATE" in path:
            return _fake_listing([("bug-report.yml", bug_template_body)])
        if method == "POST" and "/labels" in path:
            return {}
        if method == "PATCH" and "/issues/42" in path:
            return {}
        raise AssertionError(f"unexpected call: {method} {path}")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    monkeypatch.setattr(run, "_run_claude_oneshot",
        lambda wd, p, t, max_turns=4: (True,
            "===TEMPLATE_ID===\n(none)\n"
            "===RATIONALE===\nno fitting template\n"
            "===UPDATED_BODY===\n"
            "## Description\n\nSample paper from https://arxiv.org/abs/2606.11127v1\n"
            "===END===\n"
        ))
    monkeypatch.setattr(run, "_resolve_upstream_for_conventions",
        lambda target: "owner/repo")

    result = run.run_issue_convention_pass(target)
    assert result["status"] == "issue_convention_aligned_no_fitting_template"
    assert result["templates_found"] == 1
    assert result["templates_eligible"] == 0
    assert "bug" in result["templates_filtered_kinds"]


def test_run_issue_convention_pass_aligned_happy_path(monkeypatch):
    """Eligible template fits + LLM picks it → issue_convention_aligned
    status with the picked template recorded."""
    _base_env(monkeypatch, INPUT_ISSUE_NUMBER="42")
    target = run.Target(repo="owner/repo")
    issue = _fake_issue_dict()
    feature_template_body = (
        "name: Feature Request\n"
        "description: Suggest a new feature\n"
        "body:\n"
        "  - type: textarea\n"
        "    attributes:\n"
        "      label: Description\n"
    )

    def fake_gh_api(method, path, body=None):
        if method == "GET" and "/issues/42" in path:
            return issue
        if method == "GET" and "/contents/.github/ISSUE_TEMPLATE" in path:
            return _fake_listing([("feature_request.yml", feature_template_body)])
        if method == "POST" and "/labels" in path:
            return {}
        if method == "PATCH" and "/issues/42" in path:
            return {}
        raise AssertionError(f"unexpected call: {method} {path}")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    monkeypatch.setattr(run, "_run_claude_oneshot",
        lambda wd, p, t, max_turns=4: (True,
            "===TEMPLATE_ID===\nfeature_request.yml\n"
            "===RATIONALE===\nfolded into Feature Request template\n"
            "===UPDATED_BODY===\n"
            "**Description**\n\nSample paper from https://arxiv.org/abs/2606.11127v1\n\n"
            "<details>...</details>\n"
            "===END===\n"
        ))
    monkeypatch.setattr(run, "_resolve_upstream_for_conventions",
        lambda target: "owner/repo")

    result = run.run_issue_convention_pass(target)
    assert result["status"] == "issue_convention_aligned"
    assert result["picked_template"] == "feature_request.yml"
    assert result["templates_eligible"] == 1


def test_run_issue_convention_pass_arxiv_sanity_check(monkeypatch):
    """If the LLM strips the arxiv link from the body, the rewrite is
    rejected (paper provenance must remain reachable)."""
    _base_env(monkeypatch, INPUT_ISSUE_NUMBER="42")
    target = run.Target(repo="owner/repo")
    issue = _fake_issue_dict()  # body has arxiv:2606.11127v1
    feature_template_body = "name: Feature Request\ndescription: x"

    def fake_gh_api(method, path, body=None):
        if method == "GET" and "/issues/42" in path:
            return issue
        if method == "GET" and "/contents/.github/ISSUE_TEMPLATE" in path:
            return _fake_listing([("feature_request.yml", feature_template_body)])
        if method == "POST" and "/labels" in path:
            return {}
        # PATCH should NOT be called — the sanity check should reject.
        raise AssertionError(f"unexpected call: {method} {path}")

    monkeypatch.setattr(run, "gh_api", fake_gh_api)
    # Rewrite drops the arxiv link entirely.
    monkeypatch.setattr(run, "_run_claude_oneshot",
        lambda wd, p, t, max_turns=4: (True,
            "===TEMPLATE_ID===\nfeature_request.yml\n"
            "===RATIONALE===\n...\n"
            "===UPDATED_BODY===\n"
            "**Description**\n\nSome paper without provenance\n"
            "===END===\n"
        ))
    monkeypatch.setattr(run, "_resolve_upstream_for_conventions",
        lambda target: "owner/repo")

    result = run.run_issue_convention_pass(target)
    assert result["status"] == "issue_convention_failed_claude"
    assert "arxiv" in result["error"].lower()
