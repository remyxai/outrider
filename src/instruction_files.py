"""Canonical agent-instruction-file discovery for the orientation pass.

Adapted from *Toward Instructions-as-Code: Understanding the Impact of
Instruction Files on Agentic Pull Requests* (arXiv:2606.13449). Analyzing
15,549 agentic PRs across 148 projects, that paper finds two things: the
mere *presence* of instruction files does not reliably improve merge rate
(27.7% of projects improved, 26.35% regressed), but the projects that did
improve ship instruction files that are **substantially longer** and
**structured into more sections and sub-sections**. The takeaway the
authors name "Instructions-as-Code" is that it is the structural quality of
the guidance an agent receives — not its bare existence — that correlates
with better pull requests.

Outrider's implementation pass already injects a target repo's contributor
guides into the Claude Code prompt, but it looked for only a hand-picked
subset (``CLAUDE.md``, ``AGENTS.md``, ``CONTRIBUTING.md``, ``CONTEXT.md``).
This module ports the paper's *result*, not a model or training procedure:

  1. It completes the **canonical set** of agent-instruction files the study
     and the major coding-agent vendors treat as authoritative, adding
     ``.cursorrules`` (Cursor) and ``.github/copilot-instructions.md``
     (GitHub Copilot). Repos without these files are unaffected — the
     discovery contract stays "file present in workdir → read/truncate".
  2. It exposes the per-file **structural signal** the paper found
     predictive (length in lines, count of markdown sections), so the
     orientation block can carry that signal forward rather than discarding
     it.

Everything here is read from files already on disk in the workdir — no new
instrumentation, matching the orientation pass's existing I/O contract.
"""
from __future__ import annotations

import re
from pathlib import Path

# Canonical agent-instruction files, ordered precedence-from-most-specific:
# per-agent files first, then generic agentic conventions, then human
# contributor docs, then team-direction context. The original set was
# CLAUDE / AGENTS / CONTRIBUTING / CONTEXT; this extends it with the
# cross-vendor instruction files the Instructions-as-Code study treats as
# canonical. The relative order of the original four is preserved so the
# agent's context-window position for each guide is stable across runs.
CANONICAL_INSTRUCTION_FILES: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    ".cursorrules",
    ".github/copilot-instructions.md",
    "CONTRIBUTING.md",
    "CONTEXT.md",
)

# An ATX markdown heading: leading hashes (any level) then a space then text.
# Used as the paper's "section / sub-section" proxy.
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")


def discover_instruction_files(workdir: Path) -> list[Path]:
    """Return the canonical instruction files that exist in ``workdir``.

    Result is in :data:`CANONICAL_INSTRUCTION_FILES` precedence order, with
    absent files silently skipped. Relative names with a path separator
    (e.g. ``.github/copilot-instructions.md``) resolve under ``workdir``.
    """
    found: list[Path] = []
    for name in CANONICAL_INSTRUCTION_FILES:
        path = workdir / name
        if path.is_file():
            found.append(path)
    return found


def section_count(body: str) -> int:
    """Count markdown headings — the paper's structure proxy.

    Counts ATX headings at any level (``#`` … ``######``), so both sections
    and sub-sections contribute, matching the paper's "higher number of
    sections and sub-sections" structural measure.
    """
    return sum(1 for line in body.splitlines() if _HEADING_RE.match(line))


def structural_summary(body: str) -> str:
    """One-line Instructions-as-Code structural signal for an instruction file.

    Renders the two dimensions the paper found predictive of merge-rate
    improvement: length (in lines) and section count. Returns "" for an
    empty body so callers can omit the annotation cleanly.
    """
    body = body.strip()
    if not body:
        return ""
    lines = body.count("\n") + 1
    sections = section_count(body)
    line_word = "line" if lines == 1 else "lines"
    section_word = "section" if sections == 1 else "sections"
    return f"_{lines} {line_word} · {sections} {section_word}_"


def render_instruction_files(workdir: Path, cap: int = 3000) -> str:
    """Read the canonical instruction files and render them as one block.

    Each present file becomes a ``### `<name>``` chunk: a structural-signal
    line (length + section count, per the Instructions-as-Code finding)
    followed by the file body truncated to ``cap`` characters. Files are
    emitted in :data:`CANONICAL_INSTRUCTION_FILES` precedence order; empty or
    unreadable files are skipped. Returns "" when no canonical file is
    present, so callers can omit the section entirely.
    """
    chunks: list[str] = []
    for path in discover_instruction_files(workdir):
        try:
            body = path.read_text(errors="replace").strip()
        except OSError:
            continue
        if not body:
            continue
        name = path.relative_to(workdir).as_posix()
        snippet = body[:cap] + ("\n…[truncated]" if len(body) > cap else "")
        summary = structural_summary(body)
        header = f"### `{name}`"
        if summary:
            header = f"{header}\n{summary}"
        chunks.append(f"{header}\n\n{snippet}")
    return "\n\n".join(chunks)
