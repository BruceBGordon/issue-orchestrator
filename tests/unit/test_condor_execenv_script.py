"""Lifecycle honesty of the execenv host driver, against a stub docker.

The stub records invocations and simulates outcomes (state confined to
each test's tmp_path), so the driver's control flow — not Docker — is
what these tests exercise. A `sleep` shim keeps the bounded-poll paths
deterministic and instant per tests/unit/AGENTS.md.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "condor-execenv.sh"


def _stub_bin(tmp_path: Path, docker_body: str) -> dict[str, str]:
    """A PATH-front dir with a scripted docker and an instant sleep."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    docker = stub_dir / "docker"
    docker.write_text("#!/bin/sh\n" + docker_body)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    instant_sleep = stub_dir / "sleep"
    instant_sleep.write_text("#!/bin/sh\nexit 0\n")
    instant_sleep.chmod(instant_sleep.stat().st_mode | stat.S_IEXEC)
    environment = dict(os.environ)
    environment["PATH"] = f"{stub_dir}:{environment['PATH']}"
    environment["STUB_STATE"] = str(tmp_path / "state")
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
    """ps succeeds with EMPTY output = true absence."""
    environment = _stub_bin(
        tmp_path,
        'case "$1" in ps) exit 0 ;; *) exit 0 ;; esac\n',
    )
    result = _run_down(environment)
    assert result.returncode == 0, result.stderr
    assert "already absent" in result.stdout
    assert "removed" not in result.stdout


def test_down_with_no_docker_fails_loudly_never_reports_absence(
    tmp_path: Path,
) -> None:
    """B4 round two's exact reproduction: a missing docker binary (or
    unreachable daemon) must be a loud error, never 'already absent' +
    exit 0 - command failure and empty result are distinct outcomes.

    PATH contains ONLY this test's stub dir (real dirname symlinked in,
    sleep shimmed, docker deliberately absent): appending /usr/bin
    found the runner's real docker and made the scenario vanish - the
    original form of this test failed on CI for exactly that reason."""
    import shutil

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    instant_sleep = stub_dir / "sleep"
    instant_sleep.write_text("#!/bin/sh\nexit 0\n")
    instant_sleep.chmod(instant_sleep.stat().st_mode | stat.S_IEXEC)
    real_dirname = shutil.which("dirname")
    assert real_dirname, "dirname must exist for the script's REPO_ROOT"
    (stub_dir / "dirname").symlink_to(real_dirname)
    environment = dict(os.environ)
    environment["PATH"] = str(stub_dir)
    result = _run_down(environment)
    assert result.returncode != 0
    assert "already absent" not in result.stdout
    assert "cannot query docker" in result.stderr


def test_down_with_failing_daemon_fails_loudly(tmp_path: Path) -> None:
    environment = _stub_bin(
        tmp_path,
        'case "$1" in ps) echo "Cannot connect to the Docker daemon" >&2; exit 1 ;; *) exit 0 ;; esac\n',
    )
    result = _run_down(environment)
    assert result.returncode != 0
    assert "already absent" not in result.stdout
    assert "cannot query docker" in result.stderr


def test_down_reports_successful_removal(tmp_path: Path) -> None:
    """ps lists the container until rm; empty afterwards."""
    environment = _stub_bin(
        tmp_path,
        (
            'case "$1" in\n'
            "ps)\n"
            '  if [ -f "$STUB_STATE" ]; then exit 0; fi\n'
            '  echo "abc123"; exit 0 ;;\n'
            "rm)\n"
            '  touch "$STUB_STATE"; exit 0 ;;\n'
            "*) exit 0 ;;\n"
            "esac\n"
        ),
    )
    result = _run_down(environment)
    assert result.returncode == 0, result.stderr
    assert "container removed" in result.stdout


def test_down_fails_loudly_when_stop_fails(tmp_path: Path) -> None:
    environment = _stub_bin(
        tmp_path,
        'case "$1" in ps) echo "abc123"; exit 0 ;; stop) exit 1 ;; *) exit 0 ;; esac\n',
    )
    result = _run_down(environment)
    assert result.returncode != 0
    assert "container removed" not in result.stdout


def test_down_fails_loudly_when_container_survives_removal(
    tmp_path: Path,
) -> None:
    """ps keeps listing the container after rm: bounded verification
    (instant via the sleep shim) must end in a loud failure."""
    environment = _stub_bin(
        tmp_path,
        'case "$1" in ps) echo "abc123"; exit 0 ;; *) exit 0 ;; esac\n',
    )
    result = _run_down(environment)
    assert result.returncode != 0
    assert "FAILED to remove" in result.stderr


def test_down_fails_loudly_when_verification_query_dies(
    tmp_path: Path,
) -> None:
    """A daemon failure DURING post-removal verification is its own
    loud error - never proof of removal."""
    environment = _stub_bin(
        tmp_path,
        (
            'case "$1" in\n'
            "ps)\n"
            '  if [ -f "$STUB_STATE" ]; then echo "daemon gone" >&2; exit 1; fi\n'
            '  touch "$STUB_STATE"; echo "abc123"; exit 0 ;;\n'
            "*) exit 0 ;;\n"
            "esac\n"
        ),
    )
    result = _run_down(environment)
    assert result.returncode != 0
    assert "cannot verify removal" in result.stderr
    assert "container removed" not in result.stdout


def test_existence_queries_are_container_scoped(tmp_path: Path) -> None:
    """The 07881a2 regression, pinned: existence must go through a
    container-scoped query (docker ps), never a bare object inspect
    that resolves the io-execenv IMAGE when the container is absent.
    The stub asserts the query shape itself."""
    log = tmp_path / "invocations.log"
    environment = _stub_bin(
        tmp_path,
        f'echo "$@" >> "{log}"\ncase "$1" in ps) exit 0 ;; *) exit 0 ;; esac\n',
    )
    result = _run_down(environment)
    assert result.returncode == 0, result.stderr
    queries = [
        line for line in log.read_text().splitlines() if line.startswith("ps")
    ]
    assert queries, "down never issued a container-scoped existence query"
    for query in queries:
        assert "-a" in query and "--filter" in query and "name=^" in query, query
    assert "inspect" not in log.read_text(), (
        "down still uses object inspect for existence - the image-name "
        "collision class returns"
    )


def test_privileged_launch_is_an_explicit_opt_in(tmp_path: Path) -> None:
    """The up recipe uses CAP_SYS_ADMIN by default and --privileged
    only under IO_EXECENV_PRIVILEGED=1 (the GitHub-runner path)."""
    log = tmp_path / "invocations.log"
    environment = _stub_bin(
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
