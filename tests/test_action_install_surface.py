"""The action's install surface: what it fetches at runtime and how a
failure to fetch is allowed to affect the run (#128).

Every scheduled run across every install died for five days at
``sudo apt-get install poppler-utils``: the runner image ships a stale
apt index, Ubuntu rotated the build out of the mirror, the cached .deb
URL 404'd, and a tool the code already handled being absent took the
whole job down before the agent started.

Two rules fall out of that, pinned here so the next optional dependency
cannot repeat it:

1. The action installs through pip and npm only. No ``apt``, ``brew``,
   ``snap`` or piped-to-shell installers in any ``run:`` block, except
   the explicitly allowlisted step below.
2. A step that installs something the run can live without is marked
   ``continue-on-error: true``. The code behind it must degrade to a
   no-op, and the step may not fail the job.
"""
import re
from pathlib import Path

import yaml

ACTION_PATH = Path(__file__).resolve().parent.parent / "action.yml"
ACTION = yaml.safe_load(ACTION_PATH.read_text())
STEPS = ACTION["runs"]["steps"]

# One pattern per installer we refuse. Matched against each run: block
# line by line with comments stripped, so the explanation of *why* apt is
# banned (which names apt) does not trip the test.
_FORBIDDEN = {
    "apt": re.compile(r"(^|[\s;&|])(sudo\s+)?(apt|apt-get|aptitude|dpkg)\s"),
    "brew": re.compile(r"(^|[\s;&|])brew\s+(install|tap)\b"),
    "snap": re.compile(r"(^|[\s;&|])(sudo\s+)?snap\s+install\b"),
    "pipe-to-shell": re.compile(r"\b(curl|wget)\b[^\n]*\|\s*(sudo\s+)?(ba|z)?sh\b"),
}

# Steps allowed to break rule 1, with the reason. Adding to this list is
# a review decision, not a convenience.
_ALLOWLIST = {
    # Backboard ships only a curl|sh installer. It runs inside the
    # agent-CLI step, which is required: no agent, no run.
    "Install the coding-agent CLI": {"pipe-to-shell"},
}

# Steps that install something the run can live without. Each must be
# continue-on-error so a fetch failure degrades the run instead of
# ending it. Keep in sync with the "Optional:" comments in action.yml.
_OPTIONAL_INSTALL_STEPS = {
    "Install pypdf (optional, arxiv PDF license retry)",
    "Install cocoindex-code AST search",
}


def _run_lines(step):
    run = step.get("run")
    if not run:
        return []
    out = []
    for line in str(run).splitlines():
        code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        if code.strip():
            out.append(code)
    return out


def _step_names():
    return [s.get("name", "") for s in STEPS]


def test_no_system_package_manager_in_any_run_block():
    offenders = []
    for step in STEPS:
        name = step.get("name", "<unnamed>")
        allowed = _ALLOWLIST.get(name, set())
        for line in _run_lines(step):
            for kind, pat in _FORBIDDEN.items():
                if kind in allowed:
                    continue
                if pat.search(line):
                    offenders.append(f"{name!r}: [{kind}] {line.strip()}")
    assert not offenders, (
        "action.yml installs through pip/npm only (#128). Offending lines:\n  "
        + "\n  ".join(offenders)
    )


def test_allowlist_entries_still_exist_and_still_need_the_exception():
    """An allowlist entry for a step that no longer exists, or that no
    longer uses the pattern, is dead weight that hides drift."""
    names = set(_step_names())
    for name, kinds in _ALLOWLIST.items():
        assert name in names, f"allowlisted step {name!r} not in action.yml"
        step = next(s for s in STEPS if s.get("name") == name)
        for kind in kinds:
            assert any(_FORBIDDEN[kind].search(l) for l in _run_lines(step)), (
                f"{name!r} no longer uses [{kind}]; drop it from the allowlist"
            )


def test_optional_install_steps_are_continue_on_error():
    names = set(_step_names())
    missing = _OPTIONAL_INSTALL_STEPS - names
    assert not missing, f"optional steps renamed or removed: {sorted(missing)}"
    not_soft = [
        s["name"] for s in STEPS
        if s.get("name") in _OPTIONAL_INSTALL_STEPS
        and s.get("continue-on-error") is not True
    ]
    assert not not_soft, (
        "optional install steps must be continue-on-error: true so a fetch "
        f"failure cannot end the run (#128): {not_soft}"
    )


def test_required_install_steps_are_not_continue_on_error():
    """The inverse guard: a required step silently soft-failing is worse
    than a loud failure, because the agent then runs without its CLI."""
    required = [
        s["name"] for s in STEPS
        if s.get("name", "").startswith("Install")
        and s.get("name") not in _OPTIONAL_INSTALL_STEPS
    ]
    assert required, "expected at least one required Install step"
    soft = [
        s["name"] for s in STEPS
        if s.get("name") in required and s.get("continue-on-error") is True
    ]
    assert not soft, f"required install steps marked continue-on-error: {soft}"


def test_forbidden_patterns_catch_the_original_failure():
    """Self-check: the exact line from #128 is rejected, and the pip line
    that replaced it is not."""
    assert _FORBIDDEN["apt"].search(
        "sudo apt-get install -y --no-install-recommends poppler-utils"
    )
    assert _FORBIDDEN["pipe-to-shell"].search(
        "curl -fsSL https://example.com/install | sh"
    )
    for ok in ("pip install pytest remyxai pyyaml", "npm install -g @openai/codex",
               "pipx install 'cocoindex-code[full]'"):
        assert not any(p.search(ok) for p in _FORBIDDEN.values()), ok


# ─── rule 3: everything installed is pinned exactly, in a manifest ─────────
#
# A floating install resolves fresh on every scheduled run across every
# install, so one breaking upstream release lands everywhere at once with
# nobody watching. Pins live in manifests Dependabot can read, and the
# action only installs from those manifests.

import json

ROOT = ACTION_PATH.parent
REQ_FILES = [ROOT / "requirements-action.txt", ROOT / "requirements-action-optional.txt"]
PKG_JSON = ROOT / "package.json"

_EXACT_PIP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==\d[\w.]*$")
_EXACT_NPM = re.compile(r"^\d+\.\d+\.\d+$")


def _req_lines(path):
    return [
        l.strip() for l in path.read_text().splitlines()
        if l.strip() and not l.lstrip().startswith("#")
    ]


def test_requirements_manifests_exist_and_pin_exactly():
    for path in REQ_FILES:
        assert path.exists(), f"{path.name} missing"
        lines = _req_lines(path)
        assert lines, f"{path.name} has no requirements"
        loose = [l for l in lines if not _EXACT_PIP.match(l)]
        assert not loose, f"{path.name}: not pinned with ==: {loose}"


def test_package_json_pins_agent_clis_exactly():
    deps = json.loads(PKG_JSON.read_text())["dependencies"]
    assert {"@anthropic-ai/claude-code", "@openai/codex"} <= set(deps)
    loose = {k: v for k, v in deps.items() if not _EXACT_NPM.match(v)}
    assert not loose, f"package.json: ranges are not pins: {loose}"


def test_action_installs_only_from_the_manifests():
    """No bare ``pip install <name>``, ``pipx install <name>`` or
    ``npm install -g <name>`` in any run block: each must either install
    from a requirements file (``-r``) or carry a version taken from one
    (``$(grep ...requirements-action...)`` / ``@$(pin ...)``)."""
    offenders = []
    for step in STEPS:
        name = step.get("name", "<unnamed>")
        for line in _run_lines(step):
            s = line.strip()
            if re.match(r"(sudo\s+)?pip3?\s+install\b", s):
                if " -r " not in s and "requirements-action" not in s:
                    offenders.append(f"{name!r}: {s}")
            elif re.match(r"pipx\s+install\b", s):
                if "requirements-action" not in s:
                    offenders.append(f"{name!r}: {s}")
            elif re.match(r"npm\s+(install|i)\s+-g\b", s):
                if "@$(pin " not in s:
                    offenders.append(f"{name!r}: {s}")
    assert not offenders, (
        "install from a pinned manifest, never a floating name (#128):\n  "
        + "\n  ".join(offenders)
    )


def test_every_manifest_entry_is_actually_installed_by_the_action():
    """The inverse: a pin nobody installs is drift waiting to mislead the
    next reader. Each requirements file and each npm dependency must be
    referenced from action.yml."""
    runs = "\n".join(l for s in STEPS for l in _run_lines(s))
    for path in REQ_FILES:
        assert path.name in runs, f"{path.name} is never installed by action.yml"
    for dep in json.loads(PKG_JSON.read_text())["dependencies"]:
        assert f"{dep}@$(pin {dep})" in runs, f"{dep} pinned but not installed"
    # optional pins are installed one at a time by grep; each must be found
    for req in _req_lines(ROOT / "requirements-action-optional.txt"):
        key = re.split(r"[\[=]", req, 1)[0]
        assert f"'^{key}'" in runs, f"optional pin {key} has no grep in action.yml"


def test_dependabot_watches_every_manifest():
    cfg = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    ecosystems = {u["package-ecosystem"] for u in cfg["updates"]}
    assert {"pip", "npm", "github-actions"} <= ecosystems, ecosystems
    for u in cfg["updates"]:
        assert u["directory"] == "/", u


def test_release_moves_major_tag_automatically():
    """Pins only ship when @v1 moves. The workflow that moves it must stay
    wired to published releases with write access to tags."""
    wf = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-major-tag.yml").read_text()
    )
    trigger = wf.get("on") or wf.get(True)  # pyyaml parses bare `on:` as True
    assert "published" in trigger["release"]["types"]
    assert wf["permissions"]["contents"] == "write"
    body = (ROOT / ".github" / "workflows" / "release-major-tag.yml").read_text()
    assert 'git push -f origin "refs/tags/$MAJOR"' in body
