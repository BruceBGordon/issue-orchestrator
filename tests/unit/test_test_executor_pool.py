"""Behavior tests for the cross-worktree test executor pool."""

from __future__ import annotations

import os
import selectors
import subprocess
import sys
from pathlib import Path


POOL_DIR_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR"
HOST_CPU_BUSY_FILE_ENV = "ISSUE_ORCHESTRATOR_TEST_HOST_CPU_BUSY_FILE"
PROCESS_RUNNER = Path(__file__).with_name("executor_process_runner.py")


def _executor_environment(pool_dir: Path) -> dict[str, str]:
    busy_file = pool_dir / "test-host-cpu-busy-percent"
    busy_file.parent.mkdir(parents=True, exist_ok=True)
    busy_file.write_text("0\n", encoding="utf-8")
    return {
        **os.environ,
        POOL_DIR_ENV: str(pool_dir),
        HOST_CPU_BUSY_FILE_ENV: str(busy_file),
    }


def _pool_command(
    *,
    concurrency: int,
    work_key: str,
    group: str,
    command: list[str],
    exclusive: str | None = None,
    host_cpu_slots: int = 2,
) -> list[str]:
    argv = [
        sys.executable,
        str(PROCESS_RUNNER),
        "--host-cpu-slots",
        str(host_cpu_slots),
        "--min-concurrency",
        str(concurrency),
        "--max-concurrency",
        str(concurrency),
        "--work-key",
        work_key,
        "--group",
        group,
    ]
    if exclusive is not None:
        argv.extend(["--exclusive", exclusive])
    return [*argv, "--", *command]


def _holding_command(label: str) -> list[str]:
    return [
        sys.executable,
        "-u",
        "-c",
        f"import sys; print({label!r}, flush=True); sys.stdin.readline()",
    ]


def _start_holder(
    pool_dir: Path,
    *,
    concurrency: int,
    label: str,
    group: str,
    exclusive: str | None = None,
) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        _pool_command(
            concurrency=concurrency,
            work_key=f"test:{label.lower()}",
            command=_holding_command(label),
            exclusive=exclusive,
            group=group,
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_executor_environment(pool_dir),
    )
    assert process.stdout is not None
    assert process.stdout.readline() == f"{label}\n"
    return process


def _release(process: subprocess.Popen[str]) -> None:
    if process.stdin is not None:
        process.stdin.write("\n")
        process.stdin.flush()
    process.wait(timeout=5)


def _assert_no_stdout(process: subprocess.Popen[str]) -> None:
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    assert selector.select(timeout=0) == []


def test_command_fails_fast_outside_a_git_repository(tmp_path: Path) -> None:
    result = subprocess.run(
        _pool_command(
            concurrency=1,
            work_key="test:no-repository",
            group="no-repository-run",
            command=[sys.executable, "-c", "print('ran')"],
        ),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=_executor_environment(tmp_path / "pool"),
    )

    assert result.returncode == 2
    assert "executor work must run inside a Git repository" in result.stdout


def test_failed_admission_commit_releases_lease_for_next_command(
    tmp_path: Path,
) -> None:
    pool_dir = tmp_path / "pool"
    group_service_path = pool_dir / "group-service.json"
    group_service_path.mkdir(parents=True)
    environment = _executor_environment(pool_dir)

    failed = subprocess.run(
        _pool_command(
            concurrency=2,
            work_key="test:failed-commit",
            group="failed-commit-run",
            command=[sys.executable, "-c", "raise AssertionError('must not run')"],
        ),
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert failed.returncode == 2
    assert "executor-run failed" in failed.stdout
    assert tuple((pool_dir / "leases").glob("*.json")) == ()

    group_service_path.rmdir()
    recovered = subprocess.run(
        _pool_command(
            concurrency=2,
            work_key="test:recovered",
            group="recovered-run",
            command=[sys.executable, "-c", "print('RECOVERED')"],
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=environment,
    )

    assert recovered.returncode == 0
    assert recovered.stdout == "RECOVERED\n"


def test_lease_size_becomes_xdist_auto_worker_limit(tmp_path: Path) -> None:
    result = subprocess.run(
        _pool_command(
            concurrency=3,
            host_cpu_slots=4,
            work_key="test:xdist-limit",
            group="xdist-limit-run",
            command=[
                sys.executable,
                "-c",
                "import os; print(os.environ['PYTEST_XDIST_AUTO_NUM_WORKERS'])",
            ],
        ),
        capture_output=True,
        text=True,
        check=False,
        env=_executor_environment(tmp_path / "pool"),
    )

    assert result.returncode == 0
    assert result.stdout == "3\n"


def test_executor_capacity_bounds_independent_processes(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    first = _start_holder(
        pool_dir,
        concurrency=2,
        label="FIRST",
        group="first-run",
    )
    second = subprocess.Popen(
        _pool_command(
            concurrency=1,
            work_key="test:second",
            group="second-run",
            command=[sys.executable, "-u", "-c", "print('SECOND', flush=True)"],
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_executor_environment(pool_dir),
    )
    try:
        assert second.stderr is not None
        assert "reason=capacity available=0/2" in second.stderr.readline()
        _assert_no_stdout(second)

        _release(first)
        stdout, _stderr = second.communicate(timeout=5)
        assert second.returncode == 0
        assert stdout == "SECOND\n"
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)
        if second.poll() is None:
            second.kill()
            second.wait(timeout=5)


def test_capacity_change_fails_while_work_is_active_then_succeeds(
    tmp_path: Path,
) -> None:
    pool_dir = tmp_path / "pool"
    holder = _start_holder(
        pool_dir,
        concurrency=2,
        label="CAPACITY_HOLDER",
        group="capacity-holder",
    )
    environment = _executor_environment(pool_dir)
    try:
        refused = subprocess.run(
            _pool_command(
                concurrency=1,
                host_cpu_slots=3,
                work_key="test:capacity-change-refused",
                group="capacity-change-refused",
                command=[
                    sys.executable,
                    "-c",
                    "raise AssertionError('must not run')",
                ],
            ),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=environment,
        )

        assert refused.returncode == 2
        assert "cannot change host executor capacity while leases are active" in (
            refused.stdout
        )

        _release(holder)
        recovered = subprocess.run(
            _pool_command(
                concurrency=3,
                host_cpu_slots=3,
                work_key="test:capacity-change-recovered",
                group="capacity-change-recovered",
                command=[sys.executable, "-c", "print('RECOVERED')"],
            ),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=environment,
        )

        assert recovered.returncode == 0
        assert recovered.stdout == "RECOVERED\n"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_named_resources_serialize_same_provider_only(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    claude = _start_holder(
        pool_dir,
        concurrency=1,
        label="CLAUDE",
        group="claude-run",
        exclusive="claude",
    )
    second_claude = subprocess.Popen(
        _pool_command(
            concurrency=1,
            work_key="test:claude-second",
            group="claude-second-run",
            exclusive="claude",
            command=[sys.executable, "-u", "-c", "print('CLAUDE_2', flush=True)"],
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_executor_environment(pool_dir),
    )
    try:
        assert second_claude.stderr is not None
        assert "reason=exclusive-resource available=1/2" in (
            second_claude.stderr.readline()
        )

        codex = subprocess.run(
            _pool_command(
                concurrency=1,
                work_key="test:codex",
                group="codex-run",
                exclusive="codex",
                command=[sys.executable, "-u", "-c", "print('CODEX', flush=True)"],
            ),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_executor_environment(pool_dir),
        )
        assert codex.returncode == 0
        assert codex.stdout == "CODEX\n"

        _release(claude)
        stdout, _stderr = second_claude.communicate(timeout=5)
        assert second_claude.returncode == 0
        assert stdout == "CLAUDE_2\n"
    finally:
        if claude.poll() is None:
            claude.kill()
            claude.wait(timeout=5)
        if second_claude.poll() is None:
            second_claude.kill()
            second_claude.wait(timeout=5)


def test_new_light_group_runs_before_more_work_from_a_heavy_group(
    tmp_path: Path,
) -> None:
    pool_dir = tmp_path / "pool"
    heavy = _start_holder(
        pool_dir,
        concurrency=2,
        label="HEAVY_ACTIVE",
        group="io-validation",
    )
    heavy_next = subprocess.Popen(
        _pool_command(
            concurrency=2,
            work_key="test:heavy-next",
            group="io-validation",
            command=_holding_command("HEAVY_NEXT"),
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_executor_environment(pool_dir),
    )
    light = subprocess.Popen(
        _pool_command(
            concurrency=1,
            work_key="test:light",
            group="porchpin-validation",
            command=_holding_command("LIGHT"),
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_executor_environment(pool_dir),
    )
    try:
        assert heavy_next.stderr is not None
        assert "available=0/2" in heavy_next.stderr.readline()
        assert light.stderr is not None
        assert "available=0/2" in light.stderr.readline()

        _release(heavy)

        assert light.stdout is not None
        assert light.stdout.readline() == "LIGHT\n"
        _assert_no_stdout(heavy_next)

        _release(light)
        assert heavy_next.stdout is not None
        assert heavy_next.stdout.readline() == "HEAVY_NEXT\n"
        _release(heavy_next)
    finally:
        for process in (heavy, heavy_next, light):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_large_old_request_drains_capacity_instead_of_starving(
    tmp_path: Path,
) -> None:
    pool_dir = tmp_path / "pool"
    holder = _start_holder(
        pool_dir,
        concurrency=1,
        label="HOLDER",
        group="existing",
    )
    large = subprocess.Popen(
        _pool_command(
            concurrency=2,
            work_key="test:large",
            group="large",
            command=_holding_command("LARGE"),
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_executor_environment(pool_dir),
    )
    small: subprocess.Popen[str] | None = None
    try:
        assert large.stderr is not None
        assert "reason=capacity available=1/2" in large.stderr.readline()

        small = subprocess.Popen(
            _pool_command(
                concurrency=1,
                work_key="test:small",
                group="small",
                command=[sys.executable, "-u", "-c", "print('SMALL', flush=True)"],
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_executor_environment(pool_dir),
        )
        assert small.stderr is not None
        assert "reason=fairness available=1/2" in small.stderr.readline()
        _assert_no_stdout(small)

        _release(holder)
        assert large.stdout is not None
        assert large.stdout.readline() == "LARGE\n"
        _assert_no_stdout(small)

        _release(large)
        stdout, _stderr = small.communicate(timeout=5)
        assert small.returncode == 0
        assert stdout == "SMALL\n"
    finally:
        for process in (holder, large, small):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
