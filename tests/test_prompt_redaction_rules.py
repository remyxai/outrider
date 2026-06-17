"""Tests for the narrow redaction rules in the self-review + pre-flight prompts (REMYX-129 Follow-up 1).

The self-review and pre-flight one-shot passes produce JSON whose
fields flow verbatim into the PR / Issue body. If those fields
include verbatim tool-output that contained an Authorization header,
env-var dump, or token-shaped string, that text lands in a
public-repo body via the GitHub API.

The v1.6.4 outbound scrubber catches credential-shaped content at
egress and the v1.6.7 env-strip stops most secrets from being
reachable by the agent's subprocess in the first place. Both are
load-bearing. This prompt-level layer is **belt-and-suspenders** — it
asks the model to redact known shapes before writing the JSON, so the
common case (model summarizing what tools did) doesn't have to rely
on the egress scrubber to catch a token quote.

The rules are deliberately NARROW: forbid the specific bad output
(token shapes / Authorization headers / env-var dumps), not the
broader category of tool usage. Honest summarization of what tools
did stays encouraged.

Run with: pytest tests/test_prompt_redaction_rules.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


# Both prompts share the same redaction rules so tests can pin both
# at once via parametrize.
_PROMPTS = {
    "self_review": run._SELF_REVIEW_PROMPT_TEMPLATE,
    "preflight":   run._PREFLIGHT_PROMPT_TEMPLATE,
}


# ─── Each prompt includes the redaction section ──────────────────


def test_self_review_has_redaction_section():
    assert "NEVER include token-shaped strings" in _PROMPTS["self_review"]


def test_preflight_has_redaction_section():
    assert "NEVER include token-shaped strings" in _PROMPTS["preflight"]


# ─── Each prompt names the four canonical redaction targets ──────


def test_each_prompt_names_authorization_header_rule():
    """The Authorization-header rule is the most-common false-positive
    source per the v1.6.4 scrubber experience; both prompts must name
    it explicitly so the model knows the failure mode."""
    for name, prompt in _PROMPTS.items():
        assert "Authorization" in prompt, f"missing in {name}"
        assert "curl -v" in prompt, f"curl -v not cited in {name}"


def test_each_prompt_names_git_extraheader_rule():
    """`git config --list` exposing `http.https://github.com/.extraheader`
    is a real leak vector from prior incidents; both prompts must
    name it so the model recognizes the specific value to skip."""
    for name, prompt in _PROMPTS.items():
        assert "git config --list" in prompt, f"missing in {name}"
        assert "extraheader" in prompt, f"extraheader not cited in {name}"


def test_each_prompt_forbids_env_dumping():
    """Forbid `env` / `printenv` / `cat` on credentials files. Each is
    a routine command the agent might reach for that has no legitimate
    use during selection / self-review."""
    for name, prompt in _PROMPTS.items():
        assert "printenv" in prompt, f"missing in {name}"
        assert ".env" in prompt, f".env not cited in {name}"


def test_each_prompt_names_token_prefixes():
    """The token-shape prefixes name the credential families the agent
    is most likely to encounter (Anthropic, GitHub variants, Remyx).
    Without these, the model has to recognize tokens by shape — naming
    the prefixes makes the rule mechanical."""
    for name, prompt in _PROMPTS.items():
        for prefix in ("sk-ant-", "ghp_", "ghs_", "rmxu_", "github_pat_"):
            assert prefix in prompt, f"prefix {prefix} missing in {name}"


def test_each_prompt_directs_to_redact_marker():
    """The redaction target is `[REDACTED]` — a specific marker that
    won't itself match any scrubber pattern. The prompt names this
    explicitly so the model uses the agreed-upon convention."""
    for name, prompt in _PROMPTS.items():
        assert "[REDACTED]" in prompt, f"missing in {name}"


def test_each_prompt_allows_honest_summarization():
    """The rules forbid verbatim quotes, NOT summarization. The prompt
    explicitly preserves summarization so the model doesn't over-react
    and produce useless 'I cannot describe what tools did' outputs."""
    for name, prompt in _PROMPTS.items():
        assert "summarization" in prompt or "summarize" in prompt, (
            f"summarization not preserved in {name}"
        )


# ─── Rule scope: narrow, not broad ───────────────────────────────


def test_no_prompt_forbids_all_tool_output_quoting():
    """The original Follow-up 1 sketch suggested forbidding all
    verbatim tool-output quoting. That's too broad — it breaks
    legitimate summarization (test counts, file paths, error
    messages). Pin against that scope creeping back in."""
    for name, prompt in _PROMPTS.items():
        # Verify the prompt does NOT contain the broad forbid.
        assert "no verbatim tool output" not in prompt.lower(), (
            f"broad rule sneaked into {name}"
        )


def test_no_prompt_forbids_env_var_references():
    """The agent legitimately discusses code that READS env vars
    (customer repos commonly use `os.environ.get(...)`). Forbidding
    all env-var references would break those discussions. The narrow
    rule forbids `env` / `printenv` (the *commands*) — not the
    *concept* of env vars."""
    for name, prompt in _PROMPTS.items():
        # Verify the prompt does NOT broadly forbid env-var references.
        for bad in [
            "do not reference any environment variable",
            "do not mention environment variables",
            "do not discuss env vars",
        ]:
            assert bad not in prompt.lower(), (
                f"too-broad rule sneaked into {name}: {bad!r}"
            )
