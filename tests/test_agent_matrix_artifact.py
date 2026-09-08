"""The published matrix must match the registries that generate it.

docs/agent-matrix.json is the contract three repos read: this action, the
remyxai CLI, and the engine. If it can go stale silently, they drift — so a
registry change without a regenerate fails here.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agents import available  # noqa: E402

ARTIFACT = ROOT / "docs" / "agent-matrix.json"


def test_artifact_exists():
    assert ARTIFACT.exists(), "run python scripts/gen_agent_matrix.py"


def test_artifact_is_not_stale():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_agent_matrix.py"), "--check"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_artifact_covers_every_registered_agent():
    doc = json.loads(ARTIFACT.read_text())
    assert set(doc["agents"]) == set(available())


def test_every_agent_row_carries_what_a_consumer_needs():
    """The CLI needs the key env var to validate a secret before dispatch;
    the docs need the install hint; the engine needs the capabilities."""
    doc = json.loads(ARTIFACT.read_text())
    for name, row in doc["agents"].items():
        assert row["key_env"], f"{name} has no key_env"
        assert row["install"], f"{name} has no install hint"
        assert row["capabilities"], f"{name} declares no capabilities"
        assert row["api_family"], f"{name} declares no api_family"


def test_pairs_name_their_secret():
    """A consumer must be able to tell the user which secret to set."""
    doc = json.loads(ARTIFACT.read_text())
    for pair in doc["pairs"]:
        assert pair["secret"], pair


def test_backends_doc_agent_section_is_not_stale():
    """docs/backends.md's agent tables are generated from the same registry.

    The prose around them is hand-written; only the marked block is
    generated, so the tables cannot drift while the explanation stays
    editable.
    """
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_backends_doc.py"), "--check"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_backends_doc_documents_the_new_cost_basis_values():
    """A reader hitting `unavailable` in telemetry must be able to look it
    up; it is a value this branch introduced."""
    doc = (ROOT / "docs" / "backends.md").read_text()
    for value in ("agent_envelope", "unavailable"):
        assert f"`{value}`" in doc, f"{value} undocumented"
