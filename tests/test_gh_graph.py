"""Unit tests for the gh-graph dependency-navigation tool.

Covers forward imports (ast), reverse imports (grep over a temp repo), and
graceful degradation on non-Python / missing files.

Run with: pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import gh_graph  # noqa: E402


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_forward_imports_plain_and_from(tmp_path):
    f = _write(tmp_path / "mod.py",
               "import os\nfrom pkg.depth import estimate\nimport pkg.localize\n")
    fwd = gh_graph.forward_imports(str(f))
    mods = {m for m, _ in fwd}
    assert "os" in mods
    assert "pkg.depth" in mods
    assert "pkg.localize" in mods
    # line numbers are carried through, sorted ascending
    lines = [ln for _, ln in fwd]
    assert lines == sorted(lines)


def test_forward_imports_relative(tmp_path):
    f = _write(tmp_path / "pkg" / "a.py", "from . import depth\nfrom .util import x\n")
    mods = {m for m, _ in gh_graph.forward_imports(str(f))}
    assert "." in mods           # `from . import depth`
    assert ".util" in mods


def test_forward_imports_syntax_error_is_empty(tmp_path):
    f = _write(tmp_path / "bad.py", "def (:\n")
    assert gh_graph.forward_imports(str(f)) == []


def test_reverse_imports_finds_callers(tmp_path):
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "depth.py", "VALUE = 1\n")
    _write(tmp_path / "caller.py",
           "from pkg.depth import VALUE\nimport pkg.depth\n")
    _write(tmp_path / "pkg" / "sibling.py", "from pkg import depth\n")
    _write(tmp_path / "unrelated.py", "import os\n")
    rev = gh_graph.reverse_imports("pkg/depth.py", str(tmp_path))
    files = {f for f, _ in rev}
    assert "caller.py" in files
    assert "pkg/sibling.py" in files
    assert "unrelated.py" not in files


def test_reverse_imports_relative_sibling(tmp_path):
    # Intra-package relative imports must be found (both `from .a` spellings).
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "a.py", "X = 1\n")
    _write(tmp_path / "pkg" / "b.py", "from .a import X\n")
    _write(tmp_path / "pkg" / "c.py", "from . import a\n")
    files = {f for f, _ in gh_graph.reverse_imports("pkg/a.py", str(tmp_path))}
    assert "pkg/b.py" in files
    assert "pkg/c.py" in files


def test_reverse_imports_src_layout(tmp_path):
    # src-layout: the module is `pkg.agent`, not `src.pkg.agent`. Both an
    # absolute import (from outside) and a relative one (a sibling) resolve.
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(tmp_path / "src" / "pkg" / "agent.py", "class Agent: ...\n")
    _write(tmp_path / "src" / "pkg" / "run.py", "from .agent import Agent\n")
    _write(tmp_path / "tests" / "test_it.py", "from pkg.agent import Agent\n")
    files = {f for f, _ in
             gh_graph.reverse_imports("src/pkg/agent.py", str(tmp_path))}
    assert "src/pkg/run.py" in files          # relative sibling
    assert "tests/test_it.py" in files        # absolute, dotted name has no `src.`


def test_reverse_imports_excludes_self(tmp_path):
    # A file that imports a sibling shouldn't list itself as an importer.
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "depth.py", "import pkg.depth\n")
    rev = gh_graph.reverse_imports("pkg/depth.py", str(tmp_path))
    assert all(f != "pkg/depth.py" for f, _ in rev)


def test_render_block_shape(tmp_path):
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "depth.py", "import os\n")
    _write(tmp_path / "caller.py", "from pkg.depth import x\n")
    out = gh_graph.render(str(tmp_path / "pkg" / "depth.py"), root=str(tmp_path))
    assert "Imports (this file uses):" in out
    assert "Imported-by (files that use this file):" in out
    assert "os" in out
    assert "caller.py" in out


def test_render_non_python_graceful(tmp_path):
    f = _write(tmp_path / "notes.md", "# hi\n")
    out = gh_graph.render(str(f), root=str(tmp_path))
    assert "not a Python file" in out


def test_render_missing_file_graceful(tmp_path):
    out = gh_graph.render(str(tmp_path / "nope.py"), root=str(tmp_path))
    assert "not found" in out


def test_main_selftest_exits_zero(capsys):
    assert gh_graph.main(["--selftest"]) == 0
    assert "ok" in capsys.readouterr().out


def test_main_no_args_usage(capsys):
    assert gh_graph.main([]) == 2
