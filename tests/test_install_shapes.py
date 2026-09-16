"""What every workflow already installed on a customer repo gets from this action.

The compatibility tests next door check the *input surface* — that no input
vanished, none became required, defaults held. They invent their own input
combinations, and that is the gap this file closes: v1.8.0 shipped a
regression because nobody had ever put `provider` and `model-base-url` in the
same run, which is exactly what the pre-axis CLI template does.

So the shapes here are not invented. Each one is the `with:` block of a
template that actually generated installs, recorded in
`fixtures/install_shapes.json` with its provenance. An install in the wild
does not update itself: its shape is history. Add new shapes, never edit old
ones.

Each case runs the real `configure_backend` entry point in a subprocess, the
way a job step does, and asserts what it writes to the job environment. Fast:
no network, no agent, the whole file runs in about a second.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHAPES = json.loads((ROOT / "tests" / "fixtures" / "install_shapes.json").read_text())["shapes"]

_INPUT_ENV = {
    "agent": "INPUT_AGENT", "provider": "INPUT_PROVIDER",
    "model": "INPUT_MODEL", "model-base-url": "INPUT_MODEL_BASE_URL",
}


def _resolve(shape, tmp_path):
    """Run the action's own routing for one install shape."""
    job_env = tmp_path / "github_env"
    job_env.write_text("")
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_ENV": str(job_env),
        "GITHUB_OUTPUT": str(tmp_path / "github_output"),
        **{var: "" for var in _INPUT_ENV.values()},
        **{_INPUT_ENV[k]: v for k, v in shape["inputs"].items()},
        **shape["env"],
    }
    result = subprocess.run(
        [sys.executable, str(ROOT / "src" / "configure_backend.py")],
        env=env, capture_output=True, text=True,
    )
    written = dict(
        line.split("=", 1) for line in job_env.read_text().splitlines() if "=" in line
    )
    return result, written


def _launch_endpoint(shape, routing_wrote):
    """The endpoint the agent is actually launched against.

    Routing and launch are two steps. `configure_backend` resolves the
    provider's endpoint into the job env; `run.main` then applies an explicit
    `model-base-url` on top, in-process, before the agent starts. The v1.8.0
    regression lived in that second step, so a check that stopped after
    routing would have waved it through — which is why both are asserted.
    """
    sys.path.insert(0, str(ROOT / "src"))
    # Restore the session's own `run` module afterwards, never just drop it:
    # other test files bind names from it at collection time, and handing them
    # a freshly imported module breaks identity checks in ways that surface as
    # an unrelated failure three files away.
    original = sys.modules.get("run")
    sys.modules.pop("run", None)
    saved = dict(os.environ)
    try:
        os.environ.update({"INPUT_AGENT": shape["inputs"].get("agent", ""),
                           "ANTHROPIC_API_KEY": "k", "CODEX_API_KEY": "k",
                           "BACKBOARD_API_KEY": "k"})
        import run
        applied, _warning = run.endpoint_override(
            shape["inputs"].get("model-base-url", ""),
            provider_id=shape["inputs"].get("provider", ""),
        )
    finally:
        os.environ.clear()
        os.environ.update(saved)
        if original is not None:
            sys.modules["run"] = original
        else:
            sys.modules.pop("run", None)
    return applied or routing_wrote.get("ANTHROPIC_BASE_URL") or \
        routing_wrote.get("CODEX_BASE_URL") or None


@pytest.mark.parametrize("shape", SHAPES, ids=[s["name"] for s in SHAPES])
def test_an_installed_workflow_still_gets_what_it_got(shape, tmp_path):
    result, written = _resolve(shape, tmp_path)
    assert result.returncode == 0, (
        f"{shape['name']} no longer routes:\n{result.stdout}\n{result.stderr}"
    )
    routing_wrote = {k: v for k, v in written.items()
                     if k != "OUTRIDER_CLAUDE_AUTH_VAR"}
    assert routing_wrote == shape["expects"]["routing_wrote"], (
        f"{shape['name']}: what routing hands this install has changed"
    )
    assert _launch_endpoint(shape, routing_wrote) == shape["expects"]["effective_endpoint"], (
        f"{shape['name']}: the agent would be launched against a different endpoint"
    )


def test_every_shape_records_where_it_came_from():
    """A shape with no provenance is an invented combination, which is the
    thing this file exists to stop being our only coverage."""
    for shape in SHAPES:
        assert shape.get("provenance"), f"{shape['name']} has no provenance"


def test_a_caller_supplied_endpoint_survives_a_named_provider():
    """The v1.8.0 regression, pinned on its own so the reason is legible: the
    pre-axis CLI template passes `provider` and `model-base-url` together, and
    discarding the second sent those installs to the vendor's public endpoint
    instead of their own gateway."""
    gateway = next(s for s in SHAPES if "self-hosted gateway" in s["name"])
    assert gateway["expects"]["effective_endpoint"] == "https://gateway.internal/anthropic"
