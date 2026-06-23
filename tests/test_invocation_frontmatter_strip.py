"""Regression tests: the INVOCATION.md prompt must not open with `---`.

INVOCATION.md carries OKF-conformant YAML frontmatter. The file is passed
verbatim as the Claude CLI's `-p` value, and the CLI's option parser reads a
leading `---` as an unknown flag (`error: unknown option '---'`), hard-failing
the implementation call in ~0.2s before any work runs. `invoke_claude_code`
must strip that leading frontmatter so the prompt opens with the instruction
body.

Run with: pytest tests/test_invocation_frontmatter_strip.py -q
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# ─── _strip_leading_frontmatter unit behavior ──────────────────────────────


def test_strips_leading_frontmatter_block():
    text = "---\ntype: agent_invocation\ndescription: x\n---\n\nYou are an agent.\n"
    out = run._strip_leading_frontmatter(text)
    assert out == "You are an agent.\n"
    assert not out.startswith("-")


def test_noop_without_frontmatter():
    text = "You are an agent.\nDo the thing.\n"
    assert run._strip_leading_frontmatter(text) == text


def test_noop_on_unterminated_fence():
    # A leading '---' with no closing fence is not a frontmatter block;
    # leave it untouched rather than eating the whole body.
    text = "---\nnot really frontmatter\nstill going\n"
    assert run._strip_leading_frontmatter(text) == text


def test_invocation_template_strips_clean():
    # The raw template (placeholders intact) is enough — we only care that it
    # opens with frontmatter and that stripping yields the instruction body.
    template = run._INVOCATION_MD_TEMPLATE
    assert template.startswith("---")  # the file itself keeps its frontmatter
    stripped = run._strip_leading_frontmatter(template)
    assert not stripped.startswith("-")
    assert stripped.lstrip().startswith("You are a coding agent")


# ─── invoke_claude_code passes a parser-safe prompt ────────────────────────


def test_invoke_passes_prompt_not_starting_with_dash(monkeypatch, tmp_path):
    """The `-p` value handed to the CLI must not begin with `-`, even when
    INVOCATION.md opens with YAML frontmatter."""
    bundle = tmp_path / run.BUNDLE_DIR_NAME
    bundle.mkdir()
    (bundle / "INVOCATION.md").write_text(
        "---\ntype: agent_invocation\ndescription: x\n---\n\nYou are a coding agent.\n"
    )

    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return MagicMock(stdout='{"result": "ok"}', stderr="", returncode=0)

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    run._reset_run_cost()
    try:
        run.invoke_claude_code(tmp_path, timeout_s=60)
        cmd = captured["cmd"]
        prompt = cmd[cmd.index("-p") + 1]
        assert not prompt.startswith("-"), f"prompt opens with a dash: {prompt[:20]!r}"
        assert prompt.startswith("You are a coding agent")
    finally:
        # invoke_claude_code records usage into the run-global accumulator;
        # reset so this test doesn't leak state into siblings.
        run._reset_run_cost()
