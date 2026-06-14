#!/usr/bin/env python3
"""
gh_graph.py — dependency-navigation helper for the Outrider selection pass.

Exposed to the selection agent as the `gh-graph <file_path>` tool. Given a
Python file, it lists:

  * the modules that file imports (forward imports, via ``ast``), and
  * the files in the repo that import *it* (reverse imports, via ``grep``).

The reverse-imports query is the load-bearing part: shell-style navigation
lets the agent grep *inward* from a name, but not walk *outward* from a
module to the call sites that depend on it. Surfacing imported-by edges
gives the agent the structural step it needs to find where a module plugs in
and verify the I/O contract against real callers.

Usage:
    gh-graph <file_path>      # path is resolved relative to the cwd (repo root)
    gh-graph --selftest       # smoke check, exits 0

Scope: Python-only (v1.5.x). Non-Python files, missing files, and syntax
errors degrade gracefully — a one-line note and exit 0, never a crash, so a
bad path never derails the agent's Bash call.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys


def _module_candidates(rel_path: str) -> tuple[str, str, str]:
    """Derive the importable names for a repo-relative ``.py`` path.

    Returns ``(full, parent, leaf)`` where, for ``vqasynth/localize.py``:
      full   = "vqasynth.localize"   (dotted module path)
      parent = "vqasynth"            (containing package, "" at repo root)
      leaf   = "localize"            (module name; package name for __init__)

    A package ``vqasynth/__init__.py`` collapses to full="vqasynth",
    parent="", leaf="vqasynth" so reverse-import search keys on the package.
    """
    parts = rel_path[:-3].split(os.sep) if rel_path.endswith(".py") else \
        rel_path.split(os.sep)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    full = ".".join(parts)
    leaf = parts[-1] if parts else ""
    parent = ".".join(parts[:-1]) if len(parts) > 1 else ""
    return full, parent, leaf


def forward_imports(path: str) -> list[tuple[str, int]]:
    """Modules ``path`` imports, as ``(module, lineno)`` sorted by line.

    Relative imports keep their leading dots (``.depth``). Returns ``[]`` on
    unreadable / unparseable files — the caller decides how to surface that.
    """
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError):
        return []
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * (node.level or 0)) + (node.module or "")
            out.append((mod, node.lineno))
    # De-dup while preserving first-seen line, then sort by line number.
    seen: dict[str, int] = {}
    for mod, line in out:
        seen.setdefault(mod, line)
    return sorted(((m, l) for m, l in seen.items()), key=lambda t: t[1])


def reverse_imports(rel_path: str, root: str) -> list[tuple[str, int]]:
    """Files under ``root`` that import the module at ``rel_path``.

    Returns ``(repo_relative_file, lineno)`` pairs, excluding the file
    itself. Implemented with ``grep -rnE`` over ``*.py`` so it stays cheap
    and dependency-free; matches the three common import spellings:

      from <full> import ...      import <full>
      from <parent> import <leaf>

    Relative imports (``from . import x``) are not resolved — an accepted
    limitation of the grep approach.
    """
    full, parent, leaf = _module_candidates(rel_path)
    if not full:
        return []
    patterns = [
        rf"^[[:space:]]*(from|import)[[:space:]]+{full}([[:space:]]|$|\.|,)",
    ]
    if parent and leaf:
        patterns.append(
            rf"^[[:space:]]*from[[:space:]]+{parent}[[:space:]]+import[[:space:]].*\b{leaf}\b"
        )
    cmd = ["grep", "-rnE", "--include=*.py"]
    for pat in patterns:
        cmd += ["-e", pat]
    cmd.append(root or ".")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    # grep exit 1 == "no matches" (not an error); >1 is a real failure.
    if proc.returncode > 1:
        return []
    self_abs = os.path.abspath(os.path.join(root or ".", rel_path))
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for line in proc.stdout.splitlines():
        # grep -rn format: <file>:<lineno>:<matched text>
        bits = line.split(":", 2)
        if len(bits) < 2:
            continue
        fname, lineno_s = bits[0], bits[1]
        if os.path.abspath(fname) == self_abs:
            continue
        try:
            lineno = int(lineno_s)
        except ValueError:
            continue
        rel = os.path.relpath(fname, root or ".")
        key = (rel, lineno)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return sorted(out)


def render(path: str, root: str | None = None) -> str:
    """Format the imports / imported-by block for ``path``.

    Non-Python or missing paths return a single explanatory line so the
    agent gets an actionable note rather than an empty result.
    """
    root = root or os.getcwd()
    if not path.endswith(".py"):
        return f"gh-graph: {path} is not a Python file (Python-only for now)."
    if not os.path.isfile(path):
        return f"gh-graph: {path} not found (resolved against {root})."

    rel_path = os.path.relpath(path, root)
    fwd = forward_imports(path)
    rev = reverse_imports(rel_path, root)

    lines = ["Imports (this file uses):"]
    if fwd:
        lines += [f"  - {mod} (line {ln})" for mod, ln in fwd]
    else:
        lines.append("  (none found)")
    lines.append("Imported-by (files that use this file):")
    if rev:
        lines += [f"  - {f} (line {ln})" for f, ln in rev]
    else:
        lines.append("  (none found)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: gh-graph <file_path>", file=sys.stderr)
        return 2
    if argv[0] == "--selftest":
        print("gh-graph: ok")
        return 0
    print(render(argv[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
