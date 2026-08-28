"""Lifecycle honesty of the execenv host driver, against a stub docker.

The stub records invocations and simulates outcomes, so the driver's
control flow — not Docker — is what these tests exercise.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "condor-execenv.sh"


def _stub_docker(tmp_path: Path, script_body: str) -> dict[str, str]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "docker"
    stub.write_text("#!/bin/sh\n" + script_body)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    environment = dict(os.environ)
    environment["PATH"] = f"{stub_dir}:{environment['PATH']}"
    return environment


def _run_down(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), "down"],
        env=environment,
        capture_output=True,
        text=True,
    )


def test_down_on_absent_container_is_idempotent_and_says_so(
    tmp_path: Path,
) -> None:
    environment = _stub_docker(
        tmp_path,
        'case "$1" in inspect) exit 1 ;; *) exit 0 ;; esac\n',
    )
    result = _run_down(environment)
    assert result.returncode == 0, result.stderr
    assert "already absent" in result.stdout
    assert "removed" not in result.stdout


def test_down_reports_successful_removal(tmp_path: Path) -> None:
    """inspect: exists before removal, gone after (the stub counts)."""
    environment = _stub_docker(
        tmp_path,
        (
            'state_file="${TMPDIR:-/tmp}/execenv-stub-$PPID"\n'
            'case "$1" in\n'
            "inspect)\n"
            '  if [ -f "$state_file" ]; then exit 1; else exit 0; fi ;;\n'
            "rm)\n"
            '  touch "$state_file"; exit 0 ;;\n'
            "*) exit 0 ;;\n"
            "esac\n"
        ),
    )
    result = _run_down(environment)
    assert result.returncode == 0, result.stderr
    assert "container removed" in result.stdout


def test_down_fails_loudly_when_stop_fails(tmp_path: Path) -> None:
    """A daemon/authorization failure must not print a comforting
    'removed' (B4, #7119 review: reproduced false success)."""
    environment = _stub_docker(
        tmp_path,
        'case "$1" in inspect) exit 0 ;; stop) exit 1 ;; *) exit 0 ;; esac\n',
    )
    result = _run_down(environment)
    assert result.returncode != 0
    assert "container removed" not in result.stdout


def test_down_fails_loudly_when_container_survives_removal(
    tmp_path: Path,
) -> None:
    environment = _stub_docker(
        tmp_path,
        'case "$1" in inspect) exit 0 ;; *) exit 0 ;; esac\n',
    )
    result = _run_down(environment)
    assert result.returncode != 0
    assert "FAILED to remove" in result.stderr


def test_privileged_launch_is_an_explicit_opt_in(tmp_path: Path) -> None:
    """The up recipe uses CAP_SYS_ADMIN by default and --privileged
    only under IO_EXECENV_PRIVILEGED=1 (the GitHub-runner path)."""
    log = tmp_path / "invocations.log"
    environment = _stub_docker(
        tmp_path,
        f'echo "$@" >> "{log}"\nexit 0\n',
    )
    subprocess.run(
        [str(SCRIPT), "up"], env=environment, capture_output=True, text=True
    )
    default_launch = log.read_text()
    assert "--cap-add SYS_ADMIN" in default_launch
    assert "--privileged" not in default_launch

    log.write_text("")
    environment["IO_EXECENV_PRIVILEGED"] = "1"
    subprocess.run(
        [str(SCRIPT), "up"], env=environment, capture_output=True, text=True
    )
    privileged_launch = log.read_text()
    assert "--privileged" in privileged_launch
    assert "--cap-add SYS_ADMIN" not in privileged_launch
