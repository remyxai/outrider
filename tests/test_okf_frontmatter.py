"""Tests for the Open Knowledge Format (OKF) frontmatter on Outrider artifacts.

OKF is Google Cloud's open spec for portable knowledge representation
(https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing).
The format is intentionally minimal: a directory of markdown files,
each with a YAML frontmatter block whose only required field is
``type``. Everything else is producer-defined.

Outrider's existing artifacts are already markdown — the
``.remyx-recommendation/`` bundle templates and the user-facing
``CONTEXT.md`` at the repo root. Adding YAML frontmatter blocks
turns them into OKF-conformant documents without changing how
they're consumed by Claude Code (it still reads the prose). The
frontmatter is purely a no-cost-to-us bet on the format gaining
adoption — if it does, our artifacts are already there; if not,
the frontmatter blocks are inert.

This test file pins the frontmatter contract:

  - Every Outrider artifact template carries a frontmatter block
  - Every block has a ``type`` field (the OKF requirement)
  - Every block is valid YAML (no broken indentation, no unescaped
    characters that would crash a parser)
  - The body content after the frontmatter is preserved (we didn't
    accidentally strip the human-readable prose)

Run with: pytest tests/test_okf_frontmatter.py -q
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

import run  # noqa: E402


# ─── Frontmatter parsing helper ──────────────────────────────────


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict:
    """Parse a YAML frontmatter block at the top of `text` into a dict.

    Returns ``None`` when no frontmatter block is present. Uses a
    minimal hand-rolled YAML parser (key: value lines + bracketed
    list values) so the tests don't take a yaml-library dependency.
    The templates use only the simple subset that this parser
    handles; any future field that needs richer YAML (anchors,
    multi-line scalars, deeply nested mappings) would need either
    the yaml library or a switch to a less-rich representation."""
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return None
    body = m.group(1)
    out: dict = {}
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if ": " not in line and not line.endswith(":"):
            # Bare key (no value) — record as empty.
            out[line.rstrip(":")] = ""
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            out[key.strip()] = (
                [v.strip().strip("\"'") for v in inner.split(",")]
                if inner else []
            )
        else:
            out[key.strip()] = value.strip("\"'")
    return out


# ─── Each bundle template carries OKF frontmatter ────────────────


_BUNDLE_TEMPLATES = {
    "SPEC.md":         (run._SPEC_MD_TEMPLATE,         "implementation_spec"),
    "PAPER.md":        (run._PAPER_MD_TEMPLATE,        "paper"),
    "CONTEXT.md":      (run._CONTEXT_MD_TEMPLATE,      "team_history"),
    "GUARDRAILS.md":   (run._GUARDRAILS_MD_TEMPLATE,   "path_guardrails"),
    "ORIENTATION.md":  (run._ORIENTATION_MD_TEMPLATE,  "repo_orientation"),
    "INVOCATION.md":   (run._INVOCATION_MD_TEMPLATE,   "agent_invocation"),
}


@pytest.mark.parametrize("name,template,expected_type", [
    (n, t, ty) for n, (t, ty) in _BUNDLE_TEMPLATES.items()
])
def test_bundle_template_has_frontmatter(name, template, expected_type):
    """Every artifact in the .remyx-recommendation/ bundle starts with
    an OKF frontmatter block whose `type` is the documented value."""
    fm = _parse_frontmatter(template)
    assert fm is not None, f"{name} has no frontmatter block"
    assert fm.get("type") == expected_type, (
        f"{name} frontmatter `type` is {fm.get('type')!r}, expected {expected_type!r}"
    )


def test_bundle_frontmatter_uses_unique_types():
    """Each template has a distinct OKF type — collision would mean
    two artifacts can't be told apart by frontmatter alone, defeating
    the point of the `type` field."""
    types = [t for _, t in _BUNDLE_TEMPLATES.values()]
    assert len(set(types)) == len(types), "duplicate types in bundle templates"


# ─── Body content survives the frontmatter addition ──────────────


def test_spec_template_body_preserved():
    """Adding frontmatter must not strip the prose body that the agent
    actually reads."""
    template = run._SPEC_MD_TEMPLATE
    assert "Implementation spec — drafted by Remyx Recommendation" in template
    assert "{paper_title}" in template
    assert "{arxiv_id}" in template
    assert "{reasoning}" in template


def test_invocation_template_body_preserved():
    """The agent invocation prompt is the most behavior-sensitive
    template — verify its instructions weren't disturbed by adding
    frontmatter at the top."""
    template = run._INVOCATION_MD_TEMPLATE
    assert "You are a coding agent implementing a recommendation" in template
    assert ".remyx-recommendation/SPEC.md" in template


def test_orientation_template_body_preserved():
    template = run._ORIENTATION_MD_TEMPLATE
    assert "Repo orientation" in template
    assert "{contributor_guides_block}" in template


# ─── Frontmatter doesn't break under .format() substitution ──────


def test_spec_template_renders_without_error():
    """The SPEC.md template has `.format()` placeholders. The
    frontmatter must not interfere with `.format()` — e.g. by
    introducing literal `{` braces that .format() interprets as
    field markers, or by failing on arxiv_id-shaped values."""
    rendered = run._SPEC_MD_TEMPLATE.format(
        paper_title="Test Paper",
        arxiv_id="2606.13449v1",
        tier="high",
        relevance_score=0.93,
        interest_name="test-interest",
        interest_context_block="(no context)",
        reasoning="(no reasoning)",
        selection_block="(no selection)",
        suggested_experiment="(none)",
        paper_abstract="(no abstract)",
    )
    fm = _parse_frontmatter(rendered)
    assert fm is not None
    assert fm["type"] == "implementation_spec"
    assert fm["arxiv_id"] == "2606.13449v1"
    assert fm["tier"] == "high"
    # Body preserved
    assert "# Implementation spec" in rendered
    assert "Test Paper" in rendered


def test_paper_template_renders_without_error():
    rendered = run._PAPER_MD_TEMPLATE.format(
        paper_title="Another Paper",
        arxiv_id="2606.11976v1",
        paper_abstract="(abstract)",
    )
    fm = _parse_frontmatter(rendered)
    assert fm is not None
    assert fm["type"] == "paper"
    assert fm["arxiv_id"] == "2606.11976v1"


# ─── Outrider's CONTEXT.md at repo root is also OKF-conformant ──


def test_repo_context_md_is_okf():
    """The user-facing CONTEXT.md at the repo root is the artifact
    Outrider's self-dogfood selection-pass agent reads to identify
    active investigation areas. Adding OKF frontmatter makes it
    publishable as an OKF document while preserving the prose."""
    path = Path(__file__).resolve().parent.parent / "CONTEXT.md"
    assert path.is_file(), "CONTEXT.md missing from repo root"
    content = path.read_text()
    fm = _parse_frontmatter(content)
    assert fm is not None, "CONTEXT.md has no frontmatter block"
    assert fm["type"] == "project_context"
    # Body sections preserved.
    assert "## What this file is" in content
    assert "## Active investigation areas" in content
    assert "## Stable architecture" in content


# ─── Future-OKF-consumers can find every bundle artifact by type ─


def test_bundle_types_cover_the_canonical_set():
    """The `type` values across the bundle must match the documented
    OKF artifact taxonomy — `implementation_spec`, `paper`,
    `team_history`, `path_guardrails`, `repo_orientation`,
    `agent_invocation`. New artifact types added to the bundle in
    future should also be added here so consumers know what to expect."""
    expected = {
        "implementation_spec", "paper", "team_history",
        "path_guardrails", "repo_orientation", "agent_invocation",
    }
    actual = {t for _, t in _BUNDLE_TEMPLATES.values()}
    assert actual == expected, (
        f"bundle types drifted from canonical set: "
        f"missing={expected - actual}, extra={actual - expected}"
    )
