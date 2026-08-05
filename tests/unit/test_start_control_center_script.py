from __future__ import annotations

import os
import shlex
import subprocess
import sys
import venv
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "start_control_center.sh"


def _write_fake_python(venv_path: Path) -> Path:
    python_path = venv_path / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "-I" && "${2:-}" == "-c" ]]; then
  shift
fi

if [[ "${1:-}" == "-c" ]]; then
  if [[ ! -f "${FAKE_INSTALLED_PATH_FILE}" ]]; then
    exit 1
  fi
  cat "${FAKE_INSTALLED_PATH_FILE}"
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
  printf '%s\\n' "${PWD}/src/issue_orchestrator/__init__.py" > "${FAKE_INSTALLED_PATH_FILE}"
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
if [[ "${1:-}" == "sync" ]]; then
  printf '%s\\n' "$(pwd -P)/src/issue_orchestrator/__init__.py" > "${FAKE_INSTALLED_PATH_FILE}"
fi
""",
        encoding="utf-8",
    )
    uv_path.chmod(0o755)
    return uv_path


def _write_environment_targeting_fake_uv(tools_path: Path) -> Path:
    uv_path = tools_path / "uv"
    uv_path.parent.mkdir(parents=True, exist_ok=True)
    uv_path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
target_environment="${UV_PROJECT_ENVIRONMENT:-${PWD}/.venv}"
printf 'uv target=%s %s\\n' "${target_environment}" "$*" >> "${INSTALL_LOG}"
site_packages=$(
  "${target_environment}/bin/python" \
    -c 'import site; print(site.getsitepackages()[0])'
)
printf '%s\\n' "$(pwd -P)/src" > "${site_packages}/issue_orchestrator_editable.pth"
""",
        encoding="utf-8",
    )
    uv_path.chmod(0o755)
    return uv_path


def _write_noop_fake_uv(tools_path: Path) -> Path:
    uv_path = tools_path / "uv"
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
    package_path = repo / "src" / "issue_orchestrator"
    package_path.mkdir(parents=True)
    package_path.joinpath("__init__.py").write_text("", encoding="utf-8")
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
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    installed_path_file = venv_path / ".fake-installed-path"
    if import_fails:
        installed_path_file.unlink(missing_ok=True)
    else:
        installed_path_file.write_text(
            str(
                installed_path
                if installed_path is not None
                else repo / "src" / "issue_orchestrator" / "__init__.py"
            ),
            encoding="utf-8",
        )

    env = os.environ.copy()
    env.update(
        {
            "FAKE_PIP_PRESENT": "1" if pip_present else "0",
            "FAKE_INSTALLED_PATH_FILE": str(installed_path_file),
            "INSTALL_LOG": str(install_log),
            "HOME": str(home_path),
            "PATH": f"{tools_path}{os.pathsep}/usr/bin{os.pathsep}/bin",
        }
    )
    if env_overrides:
        env.update(env_overrides)
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


def _write_importable_package(repo: Path) -> None:
    package_path = repo / "src" / "issue_orchestrator"
    package_path.mkdir(parents=True)
    package_path.joinpath("__init__.py").write_text("", encoding="utf-8")
    repo.joinpath("pyproject.toml").write_text(
        "[project]\nname='issue-orchestrator'\nversion='0.0.0'\n",
        encoding="utf-8",
    )
    repo.joinpath("uv.lock").write_text("# deterministic fake lock\n", encoding="utf-8")


def _site_packages(venv_path: Path) -> Path:
    result = subprocess.run(
        [
            str(venv_path / "bin" / "python"),
            "-c",
            "import site; print(site.getsitepackages()[0])",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def _imported_package_path(
    venv_path: Path,
    *,
    env_overrides: dict[str, str] | None = None,
) -> Path:
    env = os.environ.copy()
    # This probe asks which install the venv itself resolves to, so an ambient
    # PYTHONPATH would silently answer for a different checkout — and agent
    # sessions always export one pointing at the Control Center snapshot. Drop
    # it; tests that need a foreign PYTHONPATH pass it via env_overrides.
    env.pop("PYTHONPATH", None)
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        [
            str(venv_path / "bin" / "python"),
            "-c",
            "import issue_orchestrator; print(issue_orchestrator.__file__)",
        ],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


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


def test_ensure_deps_repairs_stale_install_when_uv_target_is_overridden(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    sibling_repo = tmp_path / "sibling"
    _write_importable_package(repo)
    _write_importable_package(sibling_repo)

    venv_path = repo / ".venv"
    redirected_venv = tmp_path / "redirected-venv"
    venv.EnvBuilder(with_pip=False).create(venv_path)
    venv.EnvBuilder(with_pip=False).create(redirected_venv)

    stale_pth = _site_packages(venv_path) / "issue_orchestrator_editable.pth"
    stale_pth.write_text(f"{sibling_repo / 'src'}\n", encoding="utf-8")
    assert _imported_package_path(venv_path).is_relative_to(sibling_repo.resolve())

    install_log = tmp_path / "install.log"
    tools_path = tmp_path / "tools"
    home_path = tmp_path / "home"
    home_path.mkdir()
    _write_environment_targeting_fake_uv(tools_path)

    result = _run_ensure_deps(
        repo,
        venv_path,
        install_log,
        tools_path,
        home_path,
        env_overrides={"UV_PROJECT_ENVIRONMENT": str(redirected_venv)},
    )

    _assert_ok(result)
    assert _imported_package_path(venv_path).is_relative_to(repo.resolve())
    log = install_log.read_text(encoding="utf-8")
    assert f"uv target={venv_path} sync --frozen --extra dev" in log


def test_ensure_deps_rejects_sync_that_leaves_stale_install(tmp_path: Path) -> None:
    repo, venv_path, install_log, tools_path, home_path = _make_fake_repo(tmp_path)
    _write_noop_fake_uv(tools_path)

    result = _run_ensure_deps(
        repo,
        venv_path,
        install_log,
        tools_path,
        home_path,
        installed_path=tmp_path / "other" / "issue_orchestrator" / "__init__.py",
    )

    assert result.returncode != 0
    assert "Dependency sync did not install issue_orchestrator" in result.stderr
    assert not (venv_path / ".deps-fingerprint").exists()
    assert not (venv_path / ".deps-synced").exists()


def test_ensure_deps_accepts_repaired_install_from_symlinked_checkout(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    sibling_repo = tmp_path / "sibling"
    linked_repo = tmp_path / "linked-repo"
    _write_importable_package(repo)
    _write_importable_package(sibling_repo)
    linked_repo.symlink_to(repo, target_is_directory=True)

    venv_path = repo / ".venv"
    venv.EnvBuilder(with_pip=False).create(venv_path)
    stale_pth = _site_packages(venv_path) / "issue_orchestrator_editable.pth"
    stale_pth.write_text(f"{sibling_repo / 'src'}\n", encoding="utf-8")
    assert _imported_package_path(venv_path).is_relative_to(sibling_repo.resolve())

    install_log = tmp_path / "install.log"
    tools_path = tmp_path / "tools"
    home_path = tmp_path / "home"
    home_path.mkdir()
    _write_environment_targeting_fake_uv(tools_path)

    result = _run_ensure_deps(
        linked_repo,
        linked_repo / ".venv",
        install_log,
        tools_path,
        home_path,
    )

    _assert_ok(result)
    assert _imported_package_path(venv_path).is_relative_to(repo.resolve())


def test_ensure_deps_ignores_foreign_pythonpath_when_probing_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    foreign_repo = tmp_path / "foreign"
    _write_importable_package(repo)
    _write_importable_package(foreign_repo)

    venv_path = repo / ".venv"
    venv.EnvBuilder(with_pip=False).create(venv_path)
    editable_pth = _site_packages(venv_path) / "issue_orchestrator_editable.pth"
    editable_pth.write_text(f"{repo / 'src'}\n", encoding="utf-8")
    foreign_pythonpath = str(foreign_repo / "src")
    # Poison the ambient environment the way an agent session does, so this
    # stays a real assertion wherever it runs rather than depending on the
    # caller's shell happening to have PYTHONPATH unset.
    monkeypatch.setenv("PYTHONPATH", foreign_pythonpath)
    assert _imported_package_path(
        venv_path,
        env_overrides={"PYTHONPATH": foreign_pythonpath},
    ).is_relative_to(foreign_repo.resolve())

    install_log = tmp_path / "install.log"
    tools_path = tmp_path / "tools"
    home_path = tmp_path / "home"
    home_path.mkdir()
    _write_environment_targeting_fake_uv(tools_path)

    result = _run_ensure_deps(
        repo,
        venv_path,
        install_log,
        tools_path,
        home_path,
        env_overrides={"PYTHONPATH": foreign_pythonpath},
    )

    _assert_ok(result)
    assert "Stale install detected" not in result.stdout
    assert "sync --frozen --extra dev" in install_log.read_text(encoding="utf-8")
    assert _imported_package_path(venv_path).is_relative_to(repo.resolve())


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
