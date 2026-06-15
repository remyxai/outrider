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


def _module_info(abs_path: str) -> tuple[str, str, str, str | None]:
    """Derive the importable name for a ``.py`` file from its package layout.

    Returns ``(full, parent, leaf, top_pkg_dir)``. The dotted name is built by
    walking *up* through directories that contain ``__init__.py``, so a
    src-layout file ``src/agents/agent.py`` yields ``full="agents.agent"`` —
    not ``"src.agents.agent"``, which nothing imports. ``top_pkg_dir`` is the
    outermost package directory (used to scope relative-import search); it is
    None when the file isn't inside any package.

    For ``pkg/localize.py`` (pkg is a package): full="pkg.localize",
    parent="pkg", leaf="localize". For a package's own ``__init__.py``: full
    and leaf collapse to the package name.
    """
    abs_path = os.path.abspath(abs_path)
    stem = os.path.basename(abs_path)
    stem = stem[:-3] if stem.endswith(".py") else stem
    comps: list[str] = []
    top_pkg_dir: str | None = None
    d = os.path.dirname(abs_path)
    while os.path.isfile(os.path.join(d, "__init__.py")):
        comps.insert(0, os.path.basename(d))
        top_pkg_dir = d
        d = os.path.dirname(d)
    if stem == "__init__":
        full = ".".join(comps)
        leaf = comps[-1] if comps else ""
        parent = ".".join(comps[:-1]) if len(comps) > 1 else ""
    else:
        full = ".".join(comps + [stem]) if comps else stem
        leaf = stem
        parent = ".".join(comps) if comps else ""
    return full, parent, leaf, top_pkg_dir


def _grep_imports(patterns: list[str], where: str) -> list[tuple[str, int]]:
    """Run ``grep -rnE`` for ``patterns`` under ``where``; return (abs_file,
    lineno) pairs. grep exit 1 (no matches) is not an error; >1 is.
    """
    if not patterns:
        return []
    cmd = ["grep", "-rnE", "--include=*.py"]
    for pat in patterns:
        cmd += ["-e", pat]
    cmd.append(where)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode > 1:
        return []
    out: list[tuple[str, int]] = []
    for line in proc.stdout.splitlines():
        bits = line.split(":", 2)
        if len(bits) < 2:
            continue
        try:
            out.append((os.path.abspath(bits[0]), int(bits[1])))
        except ValueError:
            continue
    return out


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

    Returns ``(repo_relative_file, lineno)`` pairs, excluding the file itself.
    Implemented with ``grep -rnE`` over ``*.py`` so it stays cheap and
    dependency-free. Matches both absolute spellings —

      from <full> import ...      import <full>      from <parent> import <leaf>

    — and the intra-package relative spellings (``from .<leaf> import …``,
    ``from . import <leaf>``, and deeper ``from ..<leaf>``), scoped to the
    file's package so a same-named module elsewhere doesn't false-match.
    """
    root = root or "."
    abs_path = os.path.join(root, rel_path)
    full, parent, leaf, top_pkg_dir = _module_info(abs_path)
    if not full and not leaf:
        return []

    found: list[tuple[str, int]] = []
    # Absolute imports, across the whole tree.
    abs_pats: list[str] = []
    if full:
        abs_pats.append(
            rf"^[[:space:]]*(from|import)[[:space:]]+{full}([[:space:]]|$|\.|,)")
    if parent and leaf:
        abs_pats.append(
            rf"^[[:space:]]*from[[:space:]]+{parent}[[:space:]]+import[[:space:]].*\b{leaf}\b")
    found += _grep_imports(abs_pats, root)
    # Relative imports, scoped to the package directory (where they're valid).
    if top_pkg_dir and leaf:
        rel_pats = [
            rf"^[[:space:]]*from[[:space:]]+\.+{leaf}([[:space:]]|$|\.|,)",
            rf"^[[:space:]]*from[[:space:]]+\.+[[:space:]]+import[[:space:]].*\b{leaf}\b",
        ]
        found += _grep_imports(rel_pats, top_pkg_dir)

    self_abs = os.path.abspath(abs_path)
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for fabs, lineno in found:
        if fabs == self_abs:
            continue
        key = (os.path.relpath(fabs, root), lineno)
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
