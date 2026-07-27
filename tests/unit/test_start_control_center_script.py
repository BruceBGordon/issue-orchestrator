from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "start_control_center.sh"


def _write_fake_python(venv_path: Path) -> Path:
    python_path = venv_path / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "-c" ]]; then
  if [[ "${FAKE_IMPORT_FAIL:-0}" == "1" ]]; then
    exit 1
  fi
  printf '%s\\n' "${FAKE_INSTALLED_PATH}"
  exit 0
fi

if [[ "${1:-}" == "-" ]]; then
  /usr/bin/env python3 "$@"
  exit $?
fi

if [[ "${1:-}" == "-m" && "${2:-}" == "pip" && "${3:-}" == "--version" ]]; then
  if [[ "${FAKE_PIP_PRESENT:-1}" == "1" ]]; then
    printf 'pip 25.0\\n'
    exit 0
  fi
  exit 1
fi

if [[ "${1:-}" == "-m" && "${2:-}" == "ensurepip" && "${3:-}" == "--upgrade" ]]; then
  printf 'ensurepip %s\\n' "$*" >> "${INSTALL_LOG}"
  exit 0
fi

if [[ "${1:-}" == "-m" && "${2:-}" == "pip" && "${3:-}" == "install" ]]; then
  printf 'pip %s\\n' "$*" >> "${INSTALL_LOG}"
  exit 0
fi

printf 'unexpected python invocation: %s\\n' "$*" >&2
exit 99
""",
        encoding="utf-8",
    )
    python_path.chmod(0o755)
    return python_path


def _write_fake_uv(tools_path: Path) -> Path:
    uv_path = tools_path / "uv"
    uv_path.parent.mkdir(parents=True, exist_ok=True)
    uv_path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'uv %s\\n' "$*" >> "${INSTALL_LOG}"
""",
        encoding="utf-8",
    )
    uv_path.chmod(0o755)
    return uv_path


def _make_fake_repo(
    tmp_path: Path,
    *,
    with_uv: bool = True,
    custom_venv: bool = False,
) -> tuple[Path, Path, Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='fake'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("# lock\n", encoding="utf-8")
    venv_path = repo / ("custom-venv" if custom_venv else ".venv")
    _write_fake_python(venv_path)
    install_log = repo / "install.log"
    tools_path = tmp_path / "tools"
    tools_path.mkdir()
    if with_uv:
        _write_fake_uv(tools_path)
    home_path = tmp_path / "home"
    home_path.mkdir()
    return repo, venv_path, install_log, tools_path, home_path


def _run_ensure_deps(
    repo: Path,
    venv_path: Path,
    install_log: Path,
    tools_path: Path,
    home_path: Path,
    *,
    installed_path: Path | None = None,
    import_fails: bool = False,
    pip_present: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "FAKE_IMPORT_FAIL": "1" if import_fails else "0",
            "FAKE_PIP_PRESENT": "1" if pip_present else "0",
            "FAKE_INSTALLED_PATH": str(
                installed_path
                if installed_path is not None
                else repo / "src" / "issue_orchestrator" / "__init__.py"
            ),
            "INSTALL_LOG": str(install_log),
            "HOME": str(home_path),
            "PATH": f"{tools_path}{os.pathsep}/usr/bin{os.pathsep}/bin",
        }
    )
    command = (
        f"source {shlex.quote(str(SCRIPT))}; "
        f"ROOT_DIR={shlex.quote(str(repo))}; "
        f"VENV_PATH={shlex.quote(str(venv_path))}; "
        f"PYTHON_BIN={shlex.quote(sys.executable)}; "
        "ensure_deps"
    )
    return subprocess.run(
        ["/bin/bash", "-c", command],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def _assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stderr


def test_start_control_center_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["/bin/bash", "-n", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )

    _assert_ok(result)


def test_ensure_deps_syncs_when_package_is_missing(tmp_path: Path) -> None:
    repo, venv_path, install_log, tools_path, home_path = _make_fake_repo(tmp_path)

    result = _run_ensure_deps(
        repo,
        venv_path,
        install_log,
        tools_path,
        home_path,
        import_fails=True,
    )

    _assert_ok(result)
    assert "uv sync --frozen --extra dev" in install_log.read_text(encoding="utf-8")
    assert (venv_path / ".deps-fingerprint").exists()
    assert (venv_path / ".deps-synced").exists()


def test_ensure_deps_syncs_when_dependency_fingerprint_is_missing(
    tmp_path: Path,
) -> None:
    repo, venv_path, install_log, tools_path, home_path = _make_fake_repo(tmp_path)

    result = _run_ensure_deps(repo, venv_path, install_log, tools_path, home_path)

    _assert_ok(result)
    assert "uv sync --frozen --extra dev" in install_log.read_text(encoding="utf-8")


def test_ensure_deps_skips_sync_when_dependency_fingerprint_matches(
    tmp_path: Path,
) -> None:
    repo, venv_path, install_log, tools_path, home_path = _make_fake_repo(tmp_path)

    first_result = _run_ensure_deps(
        repo,
        venv_path,
        install_log,
        tools_path,
        home_path,
    )
    _assert_ok(first_result)
    install_log.unlink()

    second_result = _run_ensure_deps(
        repo,
        venv_path,
        install_log,
        tools_path,
        home_path,
    )

    _assert_ok(second_result)
    assert not install_log.exists()


def test_ensure_deps_resyncs_when_dependency_metadata_changes(
    tmp_path: Path,
) -> None:
    repo, venv_path, install_log, tools_path, home_path = _make_fake_repo(tmp_path)
    first_result = _run_ensure_deps(
        repo,
        venv_path,
        install_log,
        tools_path,
        home_path,
    )
    _assert_ok(first_result)
    install_log.unlink()

    (repo / "pyproject.toml").write_text(
        "[project]\nname='fake'\ndependencies=['defusedxml>=0.7']\n",
        encoding="utf-8",
    )
    second_result = _run_ensure_deps(
        repo,
        venv_path,
        install_log,
        tools_path,
        home_path,
    )

    _assert_ok(second_result)
    assert "uv sync --frozen --extra dev" in install_log.read_text(encoding="utf-8")


def test_ensure_deps_resyncs_when_install_points_to_another_repo(
    tmp_path: Path,
) -> None:
    repo, venv_path, install_log, tools_path, home_path = _make_fake_repo(tmp_path)

    result = _run_ensure_deps(
        repo,
        venv_path,
        install_log,
        tools_path,
        home_path,
        installed_path=tmp_path / "other" / "issue_orchestrator" / "__init__.py",
    )

    _assert_ok(result)
    log = install_log.read_text(encoding="utf-8")
    assert "uv sync --frozen --extra dev" in log
    # `uv sync --frozen` alone won't repoint an already-version-satisfied editable
    # install, so detecting the stale pointer must be followed by an explicit
    # editable reinstall that binds the project's src back to ROOT_DIR. Without
    # this the CLI keeps importing the other worktree's src.
    assert (
        f"uv pip install --python {venv_path}/bin/python -e . --no-deps" in log
    ), log


def test_ensure_deps_uses_pip_for_custom_venv_path(tmp_path: Path) -> None:
    repo, venv_path, install_log, tools_path, home_path = _make_fake_repo(
        tmp_path,
        custom_venv=True,
    )

    result = _run_ensure_deps(repo, venv_path, install_log, tools_path, home_path)

    _assert_ok(result)
    assert "-m pip install -e .[dev]" in install_log.read_text(encoding="utf-8")


def test_ensure_deps_bootstraps_pip_when_pip_is_missing(tmp_path: Path) -> None:
    repo, venv_path, install_log, tools_path, home_path = _make_fake_repo(
        tmp_path,
        custom_venv=True,
    )

    result = _run_ensure_deps(
        repo,
        venv_path,
        install_log,
        tools_path,
        home_path,
        pip_present=False,
    )

    _assert_ok(result)
    log = install_log.read_text(encoding="utf-8")
    assert "ensurepip -m ensurepip --upgrade" in log
    assert "-m pip install -e .[dev]" in log


def test_ensure_deps_resyncs_when_install_mode_changes(tmp_path: Path) -> None:
    repo, venv_path, install_log, tools_path, home_path = _make_fake_repo(tmp_path)
    first_result = _run_ensure_deps(
        repo,
        venv_path,
        install_log,
        tools_path,
        home_path,
    )
    _assert_ok(first_result)
    install_log.unlink()

    tools_path.joinpath("uv").unlink()
    second_result = _run_ensure_deps(
        repo,
        venv_path,
        install_log,
        tools_path,
        home_path,
    )

    _assert_ok(second_result)
    assert "-m pip install -e .[dev]" in install_log.read_text(encoding="utf-8")
