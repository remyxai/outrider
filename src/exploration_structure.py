"""Exploration-structure dimension for the selection pass transcript.

Adapted from *Exploration Structure in LLM Agents for Multi-File Change
Localization* (arXiv:2606.11976). That paper contrasts **linear** agentic
exploration — visiting one directory or file per step, sequentially within
a single region — against **non-linear, domain-scoped** exploration that
branches across several subsystems in parallel. Its central finding is that
linear traversal is a *structural mismatch* for changes spanning multiple
subsystems: domain-scoped parallel spawning lifts all-gold recall on
multi-file localization, while a single sequential agent under-covers the
subsystems a change actually touches.

This module ports the paper's *analytical result*, not its multi-agent
orchestration: it does not spawn domain-agents or change how Outrider's
selection pass explores. Instead it reads the same ordered event stream the
selection pass already records and classifies how that exploration was
*structured* — how many distinct repo domains (top-level subsystems) the
agent touched, how often it switched between them, and whether it issued its
file reads one-per-step (linear) or batched several per turn (branching).
The output rides along as one more telemetry dimension on the existing
selection-coverage dict, so an under-covered, single-subsystem traversal is
visible alongside the count/ratio dimensions already gated.

Everything here is computed from the existing transcript — no new
instrumentation hooks, matching the selection-coverage constraint.
"""
from __future__ import annotations

import os
import re

# Tokens in a Bash command that look like a repo path: either they contain a
# directory separator, or they carry a source/doc/config extension. Quoting
# and trailing punctuation are stripped by the caller.
_PATHISH_RE = re.compile(
    r"""[\w./-]+/[\w./-]+|[\w-]+\.(?:py|md|rst|txt|toml|cfg|ini|ya?ml|json|sh)""",
)

# Path-bearing native tools and the input key holding their target path.
_PATH_INPUT_KEYS = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
    "Grep": "path",
    "Glob": "path",
}


def _domain_of(path: str) -> str:
    """Top-level subsystem a path belongs to.

    ``src/run.py`` → ``src``; ``tests/x.py`` → ``tests``; a bare ``README.md``
    → ``<root>``. Leading ``./`` and absolute prefixes are normalised away so
    the same subsystem is not double-counted.
    """
    p = path.strip().lstrip("./")
    if not p:
        return "<root>"
    head = p.split("/", 1)[0]
    return head if "/" in p else "<root>"


def _paths_in_tool_use(name: str, inp: dict) -> list[str]:
    """Repo paths referenced by one ``tool_use`` block.

    Native path tools expose their target directly; Bash commands are scanned
    for path-ish tokens. Returns ``[]`` for non-exploration tool calls.
    """
    inp = inp or {}
    key = _PATH_INPUT_KEYS.get(name)
    if key is not None:
        val = inp.get(key)
        return [val] if isinstance(val, str) and val.strip() else []
    if name == "Bash":
        cmd = inp.get("command")
        if not isinstance(cmd, str) or not cmd:
            return []
        out: list[str] = []
        for tok in _PATHISH_RE.findall(cmd):
            tok = tok.strip("'\"`,;:()")
            # Skip URLs and flag-looking tokens.
            if tok and "://" not in tok and not tok.startswith("-"):
                out.append(tok)
        return out
    return []


def _structure_label(
    domains: int, parallel_turns: int, domain_switches: int
) -> str:
    """Collapse the raw signals into one of the paper's exploration shapes.

    - ``none`` — no file exploration in the transcript.
    - ``domain-scoped`` — touched ≥2 subsystems *and* branched (batched a
      turn or repeatedly crossed domains): the paper's non-linear shape.
    - ``branching`` — batched reads but stayed within one subsystem, or
      crossed domains only sequentially: partially non-linear.
    - ``linear`` — one read per step inside a single subsystem: the paper's
      structural-mismatch shape for multi-file changes.
    """
    if domains == 0:
        return "none"
    branched = parallel_turns >= 1 or domain_switches >= 2
    if domains >= 2 and branched:
        return "domain-scoped"
    if branched:
        return "branching"
    return "linear"


def exploration_structure_from_events(events: list[dict]) -> dict:
    """Classify selection-pass exploration as linear vs domain-scoped.

    Walks the ordered transcript, extracts the repo paths each ``tool_use``
    touched, and derives the structure dimension the paper studies:

    - ``domains`` / ``domain_list`` — distinct subsystems the agent reached.
    - ``domain_switches`` — transitions between subsystems in read order.
    - ``parallel_turns`` / ``max_turn_width`` — turns issuing >1 path read
      (branching) and the widest such turn.
    - ``linearity`` — share of path-bearing turns that read exactly one path
      (1.0 = strictly one-per-step; lower = more batched).
    - ``structure`` — the collapsed label (see :func:`_structure_label`).

    Pure over ``events``; safe on a malformed or empty stream.
    """
    ordered_domains: list[str] = []
    domain_set: set[str] = set()
    single_turns = 0
    parallel_turns = 0
    max_turn_width = 0
    path_turns = 0

    for ev in events:
        msg = ev.get("message") if isinstance(ev, dict) else None
        content = (msg or {}).get("content")
        if not isinstance(content, list):
            continue
        turn_width = 0
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            paths = _paths_in_tool_use(
                block.get("name") or "", block.get("input") or {}
            )
            if not paths:
                continue
            turn_width += 1
            for p in paths:
                dom = _domain_of(p)
                ordered_domains.append(dom)
                domain_set.add(dom)
        if turn_width:
            path_turns += 1
            max_turn_width = max(max_turn_width, turn_width)
            if turn_width >= 2:
                parallel_turns += 1
            else:
                single_turns += 1

    domain_switches = sum(
        1 for a, b in zip(ordered_domains, ordered_domains[1:]) if a != b
    )
    linearity = round(single_turns / path_turns, 2) if path_turns else 0.0
    domains = len(domain_set)

    return {
        "domains": domains,
        "domain_list": sorted(domain_set),
        "domain_switches": domain_switches,
        "parallel_turns": parallel_turns,
        "max_turn_width": max_turn_width,
        "linearity": linearity,
        "structure": _structure_label(
            domains, parallel_turns, domain_switches
        ),
    }


def is_single_subsystem(structure: dict) -> bool:
    """True when exploration stayed linear within one subsystem.

    The paper's structural-mismatch case: a multi-subsystem change localized
    by a one-file-per-step traversal that never branched out. Callers use
    this as a soft under-coverage signal alongside the line-count floor.
    """
    return (
        structure.get("structure") == "linear"
        and structure.get("domains", 0) <= 1
    )


def structure_summary(structure: dict) -> str:
    """One-line human-readable summary for logs / step summary."""
    return (
        f"{structure.get('structure', 'none')} "
        f"({structure.get('domains', 0)} domains, "
        f"linearity {structure.get('linearity', 0.0)}, "
        f"{structure.get('parallel_turns', 0)} parallel turns)"
    )


# Whether the structure dimension is computed and merged into selection
# coverage. Defaults on; set REMYX_SELECTION_EXPLORATION_STRUCTURE=off to
# skip it (keeps the coverage dict to its original four keys).
def structure_enabled() -> bool:
    return os.environ.get(
        "REMYX_SELECTION_EXPLORATION_STRUCTURE", "on"
    ).lower().strip() != "off"
