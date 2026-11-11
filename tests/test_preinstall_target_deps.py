"""``_preinstall_target_deps``: the test env the agent gets before it runs.

Before this, the step ran ``pip install -e .`` and nothing else, swallowed
the failure output, and named only the executable in the warning. On
openpi that left ``pytest`` collection failing on the first missing test
dep; on lerobot (``requires-python >=3.12`` on a 3.11 runner) the install
died in two seconds with no visible reason. Every draft PR then shipped
with "Tests could not run in CI".

Subprocess calls are intercepted; nothing is installed.

Run with: pytest tests/test_preinstall_target_deps.py -q
"""
import logging
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import run  # noqa: E402


class _Recorder:
    """Stand-in for ``subprocess.run`` that records commands and can fail
    one of them with a CalledProcessError carrying stderr."""

    def __init__(self, fail_prefix=None, stderr=""):
        self.calls: list[list[str]] = []
        self.fail_prefix = fail_prefix
        self.stderr = stderr

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if self.fail_prefix and list(cmd[: len(self.fail_prefix)]) == self.fail_prefix:
            if kwargs.get("check"):
                raise subprocess.CalledProcessError(
                    1, cmd, output="", stderr=self.stderr
                )
            return subprocess.CompletedProcess(cmd, 1, "", self.stderr)
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def ci_env(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("INPUT_PREINSTALL", raising=False)


def _no_uv(monkeypatch):
    monkeypatch.setattr(run.shutil, "which", lambda name: None)


def _with_uv(monkeypatch):
    monkeypatch.setattr(
        run.shutil, "which", lambda name: "/opt/bin/uv" if name == "uv" else None
    )


PIP = [sys.executable, "-m", "pip", "install"]


# ─── gating ───────────────────────────────────────────────────────────────


def test_outside_ci_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("INPUT_PREINSTALL", raising=False)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    rec = _Recorder()
    monkeypatch.setattr(run.subprocess, "run", rec)
    summary = run._preinstall_target_deps(tmp_path)
    assert rec.calls == []
    assert summary["ran"] == [] and summary["failed"] == []


def test_nothing_to_install_runs_nothing(tmp_path, ci_env, monkeypatch):
    _no_uv(monkeypatch)
    rec = _Recorder()
    monkeypatch.setattr(run.subprocess, "run", rec)
    summary = run._preinstall_target_deps(tmp_path)
    assert rec.calls == []
    assert summary["pytest_importable"] is None


# ─── test extras ──────────────────────────────────────────────────────────


def test_pyproject_test_extras_are_installed_with_the_package(tmp_path, ci_env, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n'
        '[project.optional-dependencies]\n'
        'dev = ["pytest"]\ntest = ["pytest-cov"]\ndocs = ["sphinx"]\n'
    )
    _no_uv(monkeypatch)
    rec = _Recorder()
    monkeypatch.setattr(run.subprocess, "run", rec)
    summary = run._preinstall_target_deps(tmp_path)
    assert rec.calls[0] == [*PIP, "-e", ".[dev,test]"]
    # The docs extra is not test tooling and is left alone.
    assert not any("docs" in part for call in rec.calls for part in call)
    assert summary["pytest_importable"] is True


def test_pyproject_without_test_extras_installs_bare(tmp_path, ci_env, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    _no_uv(monkeypatch)
    rec = _Recorder()
    monkeypatch.setattr(run.subprocess, "run", rec)
    run._preinstall_target_deps(tmp_path)
    assert rec.calls[0] == [*PIP, "-e", "."]


def test_dev_and_test_requirements_files_are_installed(tmp_path, ci_env, monkeypatch):
    (tmp_path / "setup.py").write_text("")
    (tmp_path / "requirements.txt").write_text("numpy\n")
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    (tmp_path / "requirements_test.txt").write_text("hypothesis\n")
    _no_uv(monkeypatch)
    rec = _Recorder()
    monkeypatch.setattr(run.subprocess, "run", rec)
    run._preinstall_target_deps(tmp_path)
    installed = [c[-1] for c in rec.calls if c[:4] == PIP and c[4] == "-r"]
    assert installed == ["requirements.txt", "requirements-dev.txt", "requirements_test.txt"]


# ─── uv.lock ──────────────────────────────────────────────────────────────


def test_uv_lock_is_exported_and_pip_installed_into_the_runner_python(tmp_path, ci_env, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[project.optional-dependencies]\ndev = ["pytest"]\n'
    )
    (tmp_path / "uv.lock").write_text("")
    _with_uv(monkeypatch)
    rec = _Recorder()
    monkeypatch.setattr(run.subprocess, "run", rec)
    run._preinstall_target_deps(tmp_path)

    export, install, project = rec.calls[:3]
    assert export[:4] == ["/opt/bin/uv", "export", "--frozen", "--no-hashes"]
    assert "--no-emit-project" in export
    assert export[export.index("--extra") + 1] == "dev"
    exported_path = export[export.index("-o") + 1]
    assert install == [*PIP, "-r", exported_path]
    assert project == [*PIP, "--no-deps", "-e", "."]
    # Never `uv sync`: that builds a .venv the runner's pytest does not use.
    assert not any(c[:2] == ["/opt/bin/uv", "sync"] for c in rec.calls)


def test_uv_lock_without_uv_on_path_falls_back_to_pip(tmp_path, ci_env, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    (tmp_path / "uv.lock").write_text("")
    _no_uv(monkeypatch)
    rec = _Recorder()
    monkeypatch.setattr(run.subprocess, "run", rec)
    run._preinstall_target_deps(tmp_path)
    assert rec.calls[0] == [*PIP, "-e", "."]


def test_failed_export_falls_back_to_the_manifest_install(tmp_path, ci_env, monkeypatch, caplog):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    (tmp_path / "uv.lock").write_text("")
    _with_uv(monkeypatch)
    rec = _Recorder(fail_prefix=["/opt/bin/uv", "export"], stderr="error: lockfile needs update")
    monkeypatch.setattr(run.subprocess, "run", rec)
    with caplog.at_level(logging.WARNING, logger=run.log.name):
        summary = run._preinstall_target_deps(tmp_path)
    assert [*PIP, "-e", "."] in rec.calls
    assert summary["failed"] and summary["failed"][0].startswith("/opt/bin/uv export")
    assert "lockfile needs update" in caplog.text


# ─── diagnosability ───────────────────────────────────────────────────────


def test_failure_output_reaches_the_log(tmp_path, ci_env, monkeypatch, caplog):
    """The old warning named only the executable; the reason was lost."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    _no_uv(monkeypatch)
    rec = _Recorder(
        fail_prefix=PIP,
        stderr="ERROR: Package 'lerobot' requires a different Python: 3.11.18 not in '>=3.12'",
    )
    monkeypatch.setattr(run.subprocess, "run", rec)
    with caplog.at_level(logging.WARNING, logger=run.log.name):
        summary = run._preinstall_target_deps(tmp_path)
    assert "requires a different Python" in caplog.text
    assert "exit 1" in caplog.text
    assert summary["failed"] == [" ".join([*PIP, "-e", "."])]


def test_requires_python_floor_above_the_runner_is_called_out(tmp_path, ci_env, monkeypatch, caplog):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.99"\n'
    )
    _no_uv(monkeypatch)
    rec = _Recorder()
    monkeypatch.setattr(run.subprocess, "run", rec)
    with caplog.at_level(logging.WARNING, logger=run.log.name):
        summary = run._preinstall_target_deps(tmp_path)
    assert summary["python_floor_unmet"] == "3.99"
    assert "python-version" in caplog.text


def test_requires_python_floor_met_is_silent(tmp_path, ci_env, monkeypatch, caplog):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.8"\n'
    )
    _no_uv(monkeypatch)
    monkeypatch.setattr(run.subprocess, "run", _Recorder())
    with caplog.at_level(logging.WARNING, logger=run.log.name):
        summary = run._preinstall_target_deps(tmp_path)
    assert summary["python_floor_unmet"] is None
    assert "python-version" not in caplog.text


@pytest.mark.parametrize("spec, floor", [
    (">=3.12", (3, 12)),
    (">=3.10,<3.13", (3, 10)),
    ("~=3.11", (3, 11)),
    ("==3.9.*", (3, 9)),
    ("<3.13", None),
    ("", None),
])
def test_requires_python_floor_parsing(spec, floor):
    assert run._requires_python_floor({"project": {"requires-python": spec}}) == floor


def test_pytest_importability_is_reported(tmp_path, ci_env, monkeypatch, caplog):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    _no_uv(monkeypatch)
    rec = _Recorder(fail_prefix=[sys.executable, "-c", "import pytest"], stderr="ModuleNotFoundError")
    monkeypatch.setattr(run.subprocess, "run", rec)
    with caplog.at_level(logging.INFO, logger=run.log.name):
        summary = run._preinstall_target_deps(tmp_path)
    assert summary["pytest_importable"] is False
    assert "pytest importable: NO" in caplog.text
