"""Subprocess boundary used by host executor cross-process behavior tests."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from issue_orchestrator.control.executor_admission import (
    ExecutorAdmissionPolicy,
    ExecutorLearningPolicy,
    ExecutorSaturationPolicy,
    ExecutorWorkDemandEstimator,
)
from issue_orchestrator.domain.executor import (
    ExecutorCommand,
    ExecutorConcurrencyRange,
    ExecutorExclusiveResource,
    ExecutorFairnessGroup,
    ExecutorHistoryRetentionPolicy,
    ExecutorRunSpecification,
    ExecutorWorkKey,
    ExecutorProcessTerminationPolicy,
    ExecutorUnboundedDeadline,
)
from issue_orchestrator.domain.executor_host import ExecutorHostCpuUtilization
from issue_orchestrator.execution.host_executor import (
    ExecutorRequestIdentityFactory,
    HostExecutor,
)
from issue_orchestrator.execution.atomic_record_store import OsAtomicPathReplacement
from issue_orchestrator.execution.process_group_terminator import (
    PosixProcessGroupTerminator,
)
from issue_orchestrator.execution.process_group_supervisor import (
    PosixProcessGroupSupervisor,
)
from issue_orchestrator.execution.executor_history_lock import (
    PosixExecutorHistoryRetentionLock,
)


ADMISSION_ATTEMPT_FD_ENV = "ISSUE_ORCHESTRATOR_TEST_ADMISSION_ATTEMPT_FD"


class AdmissionAttemptSignal(Protocol):
    """Test-owned hook called before every admission decision."""

    def publish(self) -> None: ...


class NoAdmissionAttemptSignal:
    """Explicit generic-runner mode with no pressure-test handshake."""

    def publish(self) -> None:
        pass


class PipeAdmissionAttemptSignal:
    """Acknowledge admission attempts through an inherited test descriptor."""

    def __init__(self, descriptor: int) -> None:
        if type(descriptor) is not int or descriptor < 0:
            raise ValueError("admission attempt descriptor must be non-negative")
        self._descriptor = descriptor

    def publish(self) -> None:
        written = os.write(self._descriptor, b"A")
        if written != 1:
            raise RuntimeError("short admission-attempt acknowledgement write")


class IdleHostCpuObserver:
    """Deterministic file-controlled observation at the adapter boundary."""

    def __init__(
        self,
        busy_percent_file: Path,
        admission_attempt_signal: AdmissionAttemptSignal,
    ) -> None:
        if not isinstance(busy_percent_file, Path):
            raise ValueError("busy percent file must be a Path")
        self._busy_percent_file = busy_percent_file
        self._admission_attempt_signal = admission_attempt_signal

    def reset(self) -> None:
        pass

    def observe(self) -> ExecutorHostCpuUtilization:
        self._admission_attempt_signal.publish()
        return ExecutorHostCpuUtilization(
            float(self._busy_percent_file.read_text(encoding="utf-8")),
            0.1,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-cpu-slots", type=int, required=True)
    parser.add_argument("--min-concurrency", type=int, required=True)
    parser.add_argument("--max-concurrency", type=int, required=True)
    parser.add_argument("--work-key", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--exclusive", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = tuple(args.command)
    if command[:1] == ("--",):
        command = command[1:]
    try:
        raw_attempt_fd = os.environ.get(ADMISSION_ATTEMPT_FD_ENV)
        admission_attempt_signal: AdmissionAttemptSignal = (
            NoAdmissionAttemptSignal()
            if raw_attempt_fd is None
            else PipeAdmissionAttemptSignal(int(raw_attempt_fd))
        )
        pool_dir = Path(os.environ["ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR"])
        executor = HostExecutor(
            pool_dir=pool_dir,
            host_cpu_slots=args.host_cpu_slots,
            admission_policy=ExecutorAdmissionPolicy(
                ExecutorSaturationPolicy(maximum_busy_percent=95)
            ),
            demand_estimator=ExecutorWorkDemandEstimator(
                ExecutorLearningPolicy(
                    cold_start_cores_per_concurrency=1.0,
                    minimum_cores_per_concurrency=0.05,
                    recent_observation_weight=0.3,
                )
            ),
            host_cpu_observer=IdleHostCpuObserver(
                Path(os.environ["ISSUE_ORCHESTRATOR_TEST_HOST_CPU_BUSY_FILE"]),
                admission_attempt_signal,
            ),
            request_identity_factory=ExecutorRequestIdentityFactory(
                wall_time_nanoseconds=time.time_ns,
                monotonic_nanoseconds=time.monotonic_ns,
                process_id=os.getpid,
                request_nonce=lambda: uuid4().hex,
            ),
            process_group_supervisor=PosixProcessGroupSupervisor(
                PosixProcessGroupTerminator(
                    ExecutorProcessTerminationPolicy(
                        graceful_shutdown_seconds=2.0,
                        forceful_shutdown_seconds=2.0,
                    )
                )
            ),
            atomic_path_replacement=OsAtomicPathReplacement(),
            history_retention_lock=PosixExecutorHistoryRetentionLock(
                (pool_dir / "work-history" / "retention.lock").resolve()
            ),
            history_retention_policy=ExecutorHistoryRetentionPolicy(2048, 24),
            queue_settle_seconds=0.02,
            queue_poll_seconds=0.01,
        )
        result = executor.run(
            ExecutorRunSpecification(
                work_key=ExecutorWorkKey(args.work_key),
                fairness_group=ExecutorFairnessGroup(args.group),
                concurrency_range=ExecutorConcurrencyRange(
                    args.min_concurrency,
                    args.max_concurrency,
                ),
                exclusive_resources=tuple(
                    ExecutorExclusiveResource(resource) for resource in args.exclusive
                ),
            ),
            ExecutorCommand(command, ExecutorUnboundedDeadline()),
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"executor-run failed: {exc}")
        return 2
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
