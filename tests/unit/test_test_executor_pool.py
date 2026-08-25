"""Behavior tests for the cross-worktree test executor pool."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.unit.executor_pool_dsl import (
    ExecutorPoolCommand,
    ExecutorPoolHeldCommand,
    ExecutorPoolHungCommand,
    ExecutorPoolPrintCommand,
    ExecutorPoolRawCommand,
    ExecutorPoolRig,
    ExecutorPoolWork,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


pytestmark = pytest.mark.timeout(180)


def _work(
    *,
    work_key: str,
    group: str,
    concurrency: int,
    command: ExecutorPoolCommand,
    host_cpu_slots: int = 2,
    exclusive_resources: tuple[str, ...] = (),
) -> ExecutorPoolWork:
    return ExecutorPoolWork(
        work_key=work_key,
        fairness_group=group,
        requested_concurrency=concurrency,
        host_cpu_slots=host_cpu_slots,
        exclusive_resources=exclusive_resources,
        command=command,
    )


def _raw_python(source: str) -> ExecutorPoolRawCommand:
    return ExecutorPoolRawCommand((sys.executable, "-c", source))


def test_command_fails_fast_outside_a_git_repository(tmp_path: Path) -> None:
    with ExecutorPoolRig(
        (tmp_path / "pool").resolve(),
        working_directory=REPO_ROOT,
    ) as rig:
        result = rig.run_in(
            _work(
                concurrency=1,
                work_key="test:no-repository",
                group="no-repository-run",
                command=_raw_python("print('ran')"),
            ),
            working_directory=tmp_path.resolve(),
        )

    assert result.exit_code == 2
    assert "executor work must run inside a Git repository" in result.stdout


def test_failed_admission_commit_releases_lease_for_next_command(
    tmp_path: Path,
) -> None:
    pool_dir = (tmp_path / "pool").resolve()
    group_service_path = pool_dir / "group-service.json"
    group_service_path.mkdir(parents=True)

    with ExecutorPoolRig(pool_dir, working_directory=REPO_ROOT) as rig:
        failed = rig.run(
            _work(
                concurrency=2,
                work_key="test:failed-commit",
                group="failed-commit-run",
                command=_raw_python("raise AssertionError('must not run')"),
            )
        )

        assert failed.exit_code == 2
        assert "executor-run failed" in failed.stdout
        assert tuple((pool_dir / "leases").glob("*.json")) == ()

        group_service_path.rmdir()
        recovered = rig.run(
            _work(
                concurrency=2,
                work_key="test:recovered",
                group="recovered-run",
                command=ExecutorPoolPrintCommand("RECOVERED"),
            )
        )

    assert recovered.exit_code == 0
    assert recovered.stdout == "RECOVERED\n"


def test_lease_size_becomes_xdist_auto_worker_limit(tmp_path: Path) -> None:
    with ExecutorPoolRig(
        (tmp_path / "pool").resolve(),
        working_directory=REPO_ROOT,
    ) as rig:
        result = rig.run(
            _work(
                concurrency=3,
                host_cpu_slots=4,
                work_key="test:xdist-limit",
                group="xdist-limit-run",
                command=_raw_python(
                    "import os; print(os.environ['PYTEST_XDIST_AUTO_NUM_WORKERS'])"
                ),
            )
        )

    assert result.exit_code == 0
    assert result.stdout == "3\n"


def test_executor_capacity_bounds_independent_processes(tmp_path: Path) -> None:
    with ExecutorPoolRig(
        (tmp_path / "pool").resolve(),
        working_directory=REPO_ROOT,
    ) as rig:
        first = rig.admit(
            _work(
                concurrency=2,
                work_key="test:first",
                group="first-run",
                command=ExecutorPoolHeldCommand("FIRST", 0),
            )
        )
        second = rig.defer(
            _work(
                concurrency=1,
                work_key="test:second",
                group="second-run",
                command=ExecutorPoolPrintCommand("SECOND"),
            ),
            expected_reason="reason=capacity available=0/2",
        )
        rig.require_not_started(second)

        rig.release(first)
        second_result = rig.complete(second)

    assert second_result.stdout == "SECOND\n"


def test_release_requires_clean_executor_completion(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="test:failing-holder exited 7"):
        with ExecutorPoolRig(
            (tmp_path / "pool").resolve(),
            working_directory=REPO_ROOT,
        ) as rig:
            holder = rig.admit(
                _work(
                    concurrency=1,
                    work_key="test:failing-holder",
                    group="failing-holder",
                    command=ExecutorPoolHeldCommand("FAILING_HOLDER", 7),
                )
            )
            rig.release(holder)


def test_scenario_failure_force_contains_hung_command_and_guardian(
    tmp_path: Path,
) -> None:
    hung = ExecutorPoolHungCommand(
        "HUNG_CLEANUP",
        (tmp_path / "hung-command.pid").resolve(),
    )
    rig = ExecutorPoolRig(
        (tmp_path / "pool").resolve(),
        working_directory=REPO_ROOT,
    )
    job = rig.submit(
        _work(
            concurrency=1,
            work_key="test:hung-cleanup",
            group="hung-cleanup",
            command=hung,
        )
    )

    with pytest.raises(RuntimeError, match="scenario failed deliberately"):
        with rig:
            rig.require_started(job)
            raise RuntimeError("scenario failed deliberately")

    rig.require_cleanup_complete(job)
    hung.require_command_contained()


def test_capacity_change_fails_while_work_is_active_then_succeeds(
    tmp_path: Path,
) -> None:
    with ExecutorPoolRig(
        (tmp_path / "pool").resolve(),
        working_directory=REPO_ROOT,
    ) as rig:
        holder = rig.admit(
            _work(
                concurrency=2,
                work_key="test:capacity-holder",
                group="capacity-holder",
                command=ExecutorPoolHeldCommand("CAPACITY_HOLDER", 0),
            )
        )
        refused = rig.run(
            _work(
                concurrency=1,
                host_cpu_slots=3,
                work_key="test:capacity-change-refused",
                group="capacity-change-refused",
                command=_raw_python("raise AssertionError('must not run')"),
            )
        )

        assert refused.exit_code == 2
        assert "cannot change host executor capacity while leases are active" in (
            refused.stdout
        )

        rig.release(holder)
        recovered = rig.run(
            _work(
                concurrency=3,
                host_cpu_slots=3,
                work_key="test:capacity-change-recovered",
                group="capacity-change-recovered",
                command=ExecutorPoolPrintCommand("RECOVERED"),
            )
        )

    assert recovered.exit_code == 0
    assert recovered.stdout == "RECOVERED\n"


def test_named_resources_serialize_same_provider_only(tmp_path: Path) -> None:
    with ExecutorPoolRig(
        (tmp_path / "pool").resolve(),
        working_directory=REPO_ROOT,
    ) as rig:
        claude = rig.admit(
            _work(
                concurrency=1,
                work_key="test:claude",
                group="claude-run",
                exclusive_resources=("claude",),
                command=ExecutorPoolHeldCommand("CLAUDE", 0),
            )
        )
        second_claude = rig.defer(
            _work(
                concurrency=1,
                work_key="test:claude-second",
                group="claude-second-run",
                exclusive_resources=("claude",),
                command=ExecutorPoolPrintCommand("CLAUDE_2"),
            ),
            expected_reason="reason=exclusive-resource available=1/2",
        )

        codex = rig.run(
            _work(
                concurrency=1,
                work_key="test:codex",
                group="codex-run",
                exclusive_resources=("codex",),
                command=ExecutorPoolPrintCommand("CODEX"),
            )
        )
        assert codex.exit_code == 0
        assert codex.stdout == "CODEX\n"

        rig.release(claude)
        second_claude_result = rig.complete(second_claude)

    assert second_claude_result.stdout == "CLAUDE_2\n"


def test_new_light_group_runs_before_more_work_from_a_heavy_group(
    tmp_path: Path,
) -> None:
    with ExecutorPoolRig(
        (tmp_path / "pool").resolve(),
        working_directory=REPO_ROOT,
    ) as rig:
        heavy = rig.admit(
            _work(
                concurrency=2,
                work_key="test:heavy-active",
                group="io-validation",
                command=ExecutorPoolHeldCommand("HEAVY_ACTIVE", 0),
            )
        )
        heavy_next = rig.defer(
            _work(
                concurrency=2,
                work_key="test:heavy-next",
                group="io-validation",
                command=ExecutorPoolHeldCommand("HEAVY_NEXT", 0),
            ),
            expected_reason="available=0/2",
        )
        light = rig.defer(
            _work(
                concurrency=1,
                work_key="test:light",
                group="porchpin-validation",
                command=ExecutorPoolHeldCommand("LIGHT", 0),
            ),
            expected_reason="available=0/2",
        )

        rig.release(heavy)
        rig.require_started(light)
        rig.require_not_started(heavy_next)

        rig.release(light)
        rig.require_started(heavy_next)
        rig.release(heavy_next)


def test_large_old_request_drains_capacity_instead_of_starving(
    tmp_path: Path,
) -> None:
    with ExecutorPoolRig(
        (tmp_path / "pool").resolve(),
        working_directory=REPO_ROOT,
    ) as rig:
        holder = rig.admit(
            _work(
                concurrency=1,
                work_key="test:holder",
                group="existing",
                command=ExecutorPoolHeldCommand("HOLDER", 0),
            )
        )
        large = rig.defer(
            _work(
                concurrency=2,
                work_key="test:large",
                group="large",
                command=ExecutorPoolHeldCommand("LARGE", 0),
            ),
            expected_reason="reason=capacity available=1/2",
        )
        small = rig.defer(
            _work(
                concurrency=1,
                work_key="test:small",
                group="small",
                command=ExecutorPoolPrintCommand("SMALL"),
            ),
            expected_reason="reason=fairness available=1/2",
        )
        rig.require_not_started(small)

        rig.release(holder)
        rig.require_started(large)
        rig.require_not_started(small)

        rig.release(large)
        small_result = rig.complete(small)

    assert small_result.stdout == "SMALL\n"
