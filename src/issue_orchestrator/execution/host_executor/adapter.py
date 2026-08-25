# pyright: strict
"""Deep host adapter implementing the public machine-wide executor port."""

from __future__ import annotations

import math
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

from ...control.executor_admission import (
    ExecutorAdmissionPolicy,
    ExecutorResourceObservation,
    ExecutorWorkDemandEstimator,
    QueuedExecutorWork,
)
from ...domain.executor import (
    ExecutorAggressiveness,
    ExecutorBoundedDeadline,
    ExecutorCommand,
    ExecutorCommandBudget,
    ExecutorConcurrencyGrant,
    ExecutorDeadlineExceededError,
    ExecutorDeadlinePhase,
    ExecutorDeadlineReason,
    ExecutorHistoryRetentionPolicy,
    ExecutorPolicy,
    ExecutorPolicyChange,
    ExecutorRunResult,
    ExecutorRunSpecification,
    ExecutorUnboundedDeadline,
)
from ...domain.process_group import OwnedProcessGroupLeader
from ...ports.executor import Executor
from ...ports.host_cpu_utilization import HostCpuUtilizationObserver
from ...ports.process_group_terminator import ProcessGroupTerminator
from ._history import ExecutorWorkHistoryStore
from ._host_observation import observe_host_load
from ._journal import ExecutorEventStore
from ._repository import ExecutorRepositoryResolver
from ._state import (
    HostAdmissionGranted,
    HostExecutorLease,
    HostExecutorState,
    OwnedQueuedRequest,
)
from ._types import (
    ExecutedExecutorCommand,
    ExecutorWorkIdentity,
    RecordedExecutorObservation,
)
from .host_policy import ExecutorPolicyStore
from .request_identity import ExecutorRequestIdentityFactory


EXECUTOR_CONCURRENCY_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_CONCURRENCY"


def _require_host_cpu_observer(value: object) -> None:
    if not isinstance(value, HostCpuUtilizationObserver):
        raise ValueError(
            "HostExecutor.host_cpu_observer must implement "
            "HostCpuUtilizationObserver"
        )


def _require_process_group_terminator(value: object) -> None:
    if not isinstance(value, ProcessGroupTerminator):
        raise ValueError(
            "HostExecutor.process_group_terminator must implement "
            "ProcessGroupTerminator"
        )


class HostExecutor(Executor):
    """Coordinate, execute, observe, and learn behind one narrow interface."""

    def __init__(
        self,
        *,
        pool_dir: Path,
        host_cpu_slots: int,
        admission_policy: ExecutorAdmissionPolicy,
        demand_estimator: ExecutorWorkDemandEstimator,
        host_cpu_observer: HostCpuUtilizationObserver,
        request_identity_factory: ExecutorRequestIdentityFactory,
        process_group_terminator: ProcessGroupTerminator,
        history_retention_policy: ExecutorHistoryRetentionPolicy,
        queue_settle_seconds: float,
        queue_poll_seconds: float,
    ) -> None:
        if type(host_cpu_slots) is not int or host_cpu_slots < 1:
            raise ValueError("HostExecutor.host_cpu_slots must be positive")
        _require_host_cpu_observer(host_cpu_observer)
        if type(request_identity_factory) is not ExecutorRequestIdentityFactory:
            raise ValueError(
                "HostExecutor.request_identity_factory must be an "
                "ExecutorRequestIdentityFactory"
            )
        if (
            type(queue_settle_seconds) is not float
            or not math.isfinite(queue_settle_seconds)
            or queue_settle_seconds <= 0
        ):
            raise ValueError(
                "HostExecutor.queue_settle_seconds must be finite and positive"
            )
        if (
            type(queue_poll_seconds) is not float
            or not math.isfinite(queue_poll_seconds)
            or queue_poll_seconds <= 0
        ):
            raise ValueError(
                "HostExecutor.queue_poll_seconds must be finite and positive"
            )
        self._host_cpu_slots = host_cpu_slots
        self._admission_policy = admission_policy
        self._demand_estimator = demand_estimator
        self._host_cpu_observer = host_cpu_observer
        self._request_identity_factory = request_identity_factory
        _require_process_group_terminator(process_group_terminator)
        self._process_group_terminator = process_group_terminator
        if type(history_retention_policy) is not ExecutorHistoryRetentionPolicy:
            raise ValueError(
                "HostExecutor.history_retention_policy must be an "
                "ExecutorHistoryRetentionPolicy"
            )
        self._queue_settle_seconds = queue_settle_seconds
        self._queue_poll_seconds = queue_poll_seconds
        self._state = HostExecutorState(pool_dir, host_cpu_slots)
        self._history = ExecutorWorkHistoryStore(
            pool_dir / "work-history", history_retention_policy
        )
        self._policy_store = ExecutorPolicyStore(pool_dir)
        self._events = ExecutorEventStore(pool_dir)
        self._repository_resolver = ExecutorRepositoryResolver()

    def policy(self) -> ExecutorPolicy:
        return self._policy_store.effective()

    def configure_policy(
        self,
        aggressiveness: ExecutorAggressiveness,
    ) -> ExecutorPolicyChange:
        change = self._policy_store.configure(aggressiveness)
        self._events.policy_changed(change)
        return change

    def run(
        self,
        specification: ExecutorRunSpecification,
        command: ExecutorCommand,
    ) -> ExecutorRunResult:
        """Run one complete public specification under shared host policy."""
        submitted_at_monotonic = time.monotonic()
        self._state.configure_capacity()
        repository = self._repository_resolver.resolve(Path.cwd())
        identity = ExecutorWorkIdentity(repository, specification.work_key)
        effective_policy = self.policy()
        previous_resources = self._history.successful_resources(identity)
        previous_demand = self._demand_estimator.estimate(previous_resources)
        invocation_identity = self._request_identity_factory.create()
        work = QueuedExecutorWork(
            request_id=invocation_identity.request_id,
            sequence=invocation_identity.queue_sequence,
            work_key=specification.work_key,
            fairness_group=specification.fairness_group,
            concurrency_range=specification.concurrency_range,
            learned_demand=previous_demand,
            aggressiveness=effective_policy.aggressiveness,
            exclusive_resources=specification.exclusive_resources,
        )
        with self._state.enqueue(work) as owned_request:
            self._events.enqueued(
                identity=identity,
                work=work,
                successful_observation_count=len(previous_resources),
                queue_settle_seconds=self._queue_settle_seconds,
                policy_source=effective_policy.source,
                host_capacity_units=self._host_cpu_slots,
                host_load=observe_host_load(),
            )
            self._host_cpu_observer.reset()
            time.sleep(self._queue_settle_seconds)
            try:
                lease = self._acquire(
                    identity,
                    owned_request,
                    command,
                    submitted_at_monotonic,
                )
            except ExecutorDeadlineExceededError as exc:
                deadline = command.deadline
                if not isinstance(deadline, ExecutorBoundedDeadline):
                    raise AssertionError(
                        "an unbounded executor command cannot exceed admission "
                        "deadline"
                    ) from exc
                elapsed = time.monotonic() - submitted_at_monotonic
                self._events.admission_deadline_exceeded(
                    identity,
                    work,
                    deadline,
                    elapsed,
                )
                self._report_deadline_exceeded(
                    identity=identity,
                    work=work,
                    deadline=deadline,
                    reason=exc.reason,
                    phase=ExecutorDeadlinePhase.ADMISSION,
                )
                raise
        result = self._run_command(
            identity,
            work,
            lease,
            command,
            submitted_at_monotonic,
        )
        recorded = RecordedExecutorObservation(
            resources=result.resources,
            exit_code=result.exit_code,
            recorded_at_unix=time.time(),
        )
        if recorded.exit_code == 0:
            self._history.record_successful(identity, recorded)
        updated_resources = self._history.successful_resources(identity)
        updated_demand = self._demand_estimator.estimate(updated_resources)
        self._events.completed(
            identity=identity,
            work=work,
            grant=result.admission_grant,
            aggressiveness_percent=effective_policy.aggressiveness.percent,
            observation=recorded,
            previous_cores_per_concurrency=(previous_demand.cores_per_concurrency),
            updated_cores_per_concurrency=updated_demand.cores_per_concurrency,
            successful_observation_count=len(updated_resources),
            host_load=observe_host_load(),
        )
        self._report_completion(identity, result)
        return ExecutorRunResult(
            result.exit_code,
            ExecutorConcurrencyGrant(result.admission_grant.concurrency),
        )

    def _acquire(
        self,
        identity: ExecutorWorkIdentity,
        owned_request: OwnedQueuedRequest,
        command: ExecutorCommand,
        submitted_at_monotonic: float,
    ) -> HostExecutorLease:
        started = time.monotonic()
        previous_wait_reason = None
        while True:
            self._require_within_absolute_deadline(
                command,
                submitted_at_monotonic,
            )
            host_cpu_utilization = self._host_cpu_observer.observe()
            outcome = self._state.attempt_admission(
                owned_request,
                self._admission_policy,
                host_cpu_utilization,
            )
            host_load = observe_host_load()
            if isinstance(outcome, HostAdmissionGranted):
                waited = time.monotonic() - started
                decision = outcome.decision
                try:
                    self._events.admitted(
                        identity=identity,
                        work=owned_request.work,
                        grant=outcome.lease.grant,
                        decision=decision,
                        leased_capacity_units_before=(decision.leased_cpu_slots_before),
                        available_capacity_units_before=(
                            decision.available_cpu_slots_before
                        ),
                        host_capacity_units=self._host_cpu_slots,
                        wait_seconds=waited,
                        host_load=host_load,
                        host_cpu_utilization=host_cpu_utilization,
                    )
                except BaseException:
                    outcome.lease.release()
                    raise
                self._report_admission(
                    "acquired",
                    owned_request.work,
                    outcome.lease,
                    f" wait={waited:.3f}s",
                )
                return outcome.lease
            decision = outcome.decision
            if decision.reason != previous_wait_reason:
                self._events.waiting(
                    identity=identity,
                    work=owned_request.work,
                    reason=decision.reason,
                    leased_capacity_units=decision.leased_cpu_slots,
                    available_capacity_units=decision.available_cpu_slots,
                    host_capacity_units=self._host_cpu_slots,
                    host_load=host_load,
                    host_cpu_utilization=host_cpu_utilization,
                )
                print(
                    "[executor] waiting "
                    f"work={identity.work_key.value} "
                    f"group={owned_request.work.fairness_group.value} "
                    f"reason={decision.reason.value} "
                    f"available={decision.available_cpu_slots}/"
                    f"{self._host_cpu_slots} "
                    f"host_cpu_busy={host_cpu_utilization.busy_percent:.1f}% "
                    f"sample={host_cpu_utilization.observation_seconds:.3f}s",
                    file=sys.stderr,
                    flush=True,
                )
                previous_wait_reason = decision.reason
            time.sleep(self._queue_poll_seconds)

    def _run_command(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        lease: HostExecutorLease,
        command: ExecutorCommand,
        submitted_at_monotonic: float,
    ) -> ExecutedExecutorCommand:
        child_env = os.environ.copy()
        granted_concurrency = str(lease.grant.concurrency)
        child_env[EXECUTOR_CONCURRENCY_ENV] = granted_concurrency
        child_env["PYTEST_XDIST_AUTO_NUM_WORKERS"] = granted_concurrency
        usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.monotonic()
        try:
            try:
                command_budget = self._command_budget(
                    command,
                    submitted_at_monotonic,
                )
            except ExecutorDeadlineExceededError as exc:
                if not isinstance(command.deadline, ExecutorBoundedDeadline):
                    raise AssertionError(
                        "an expired command budget must have a bounded deadline"
                    ) from exc
                self._record_command_deadline(
                    identity,
                    work,
                    lease,
                    command.deadline,
                    exc.reason,
                    time.monotonic() - started,
                )
                return_code = 124
            else:
                try:
                    process = subprocess.Popen(
                        list(command.arguments),
                        env=child_env,
                        pass_fds=lease.child_file_descriptors(),
                        start_new_session=True,
                    )
                except OSError as exc:
                    self._events.command_start_failed(
                        identity,
                        work,
                        lease.grant,
                        exc,
                    )
                    raise
                try:
                    return_code = process.wait(
                        timeout=(
                            None
                            if command_budget is None
                            else command_budget.timeout_seconds
                        )
                    )
                except subprocess.TimeoutExpired:
                    if command_budget is None:
                        raise AssertionError(
                            "an unbounded executor command cannot time out"
                        )
                    if not isinstance(command.deadline, ExecutorBoundedDeadline):
                        raise AssertionError(
                            "a timed-out executor command must have a bounded deadline"
                        )
                    termination = self._process_group_terminator.terminate(
                        OwnedProcessGroupLeader(process.pid)
                    )
                    process.returncode = termination.leader_exit_code
                    return_code = 124
                    self._record_command_deadline(
                        identity,
                        work,
                        lease,
                        command.deadline,
                        command_budget.reason,
                        time.monotonic() - started,
                    )
        finally:
            elapsed = time.monotonic() - started
            usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
            lease.release()
        observation = ExecutorResourceObservation(
            concurrency=lease.grant.concurrency,
            wall_seconds=elapsed,
            cpu_seconds=(usage_after.ru_utime - usage_before.ru_utime)
            + (usage_after.ru_stime - usage_before.ru_stime),
            max_rss_bytes=self._max_rss_bytes(usage_after.ru_maxrss),
            input_blocks=max(0, usage_after.ru_inblock - usage_before.ru_inblock),
            output_blocks=max(0, usage_after.ru_oublock - usage_before.ru_oublock),
        )
        return ExecutedExecutorCommand(return_code, lease.grant, observation)

    def _record_command_deadline(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        lease: HostExecutorLease,
        deadline: ExecutorBoundedDeadline,
        reason: ExecutorDeadlineReason,
        elapsed_seconds: float,
    ) -> None:
        """Publish one terminal command-deadline decision through both seams."""
        self._events.command_deadline_exceeded(
            identity,
            work,
            lease.grant,
            deadline,
            reason,
            elapsed_seconds,
        )
        self._report_deadline_exceeded(
            identity=identity,
            work=work,
            deadline=deadline,
            reason=reason,
            phase=ExecutorDeadlinePhase.COMMAND,
        )

    @staticmethod
    def _require_within_absolute_deadline(
        command: ExecutorCommand,
        submitted_at_monotonic: float,
    ) -> None:
        if isinstance(command.deadline, ExecutorUnboundedDeadline):
            return
        command.deadline.require_pending_at(
            submitted_at_monotonic=submitted_at_monotonic,
            observed_at_monotonic=time.monotonic(),
        )

    @staticmethod
    def _command_budget(
        command: ExecutorCommand,
        submitted_at_monotonic: float,
    ) -> ExecutorCommandBudget | None:
        deadline = command.deadline
        if isinstance(deadline, ExecutorUnboundedDeadline):
            return None
        return deadline.command_budget(
            submitted_at_monotonic=submitted_at_monotonic,
            admitted_at_monotonic=time.monotonic(),
        )

    @staticmethod
    def _report_deadline_exceeded(
        *,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        deadline: ExecutorBoundedDeadline,
        reason: ExecutorDeadlineReason,
        phase: ExecutorDeadlinePhase,
    ) -> None:
        print(
            "[executor] deadline-exceeded "
            f"work={identity.work_key.value} "
            f"group={work.fairness_group.value} "
            f"phase={phase.value} "
            f"reason={reason.value} "
            f"active_timeout={deadline.active_timeout_seconds:.3f}s "
            f"absolute_timeout={deadline.absolute_timeout_seconds:.3f}s",
            file=sys.stderr,
            flush=True,
        )

    def _report_admission(
        self,
        state: str,
        work: QueuedExecutorWork,
        lease: HostExecutorLease,
        suffix: str,
    ) -> None:
        resources = ",".join(resource.value for resource in work.exclusive_resources)
        exclusive = f" exclusive={resources}" if resources else ""
        print(
            f"[executor] {state} cpu_slots={lease.grant.cpu_slots}/"
            f"{self._host_cpu_slots} concurrency={lease.grant.concurrency} "
            f"work={work.work_key.value} group={work.fairness_group.value}"
            f"{exclusive}{suffix}",
            file=sys.stderr,
            flush=True,
        )

    def _report_completion(
        self,
        identity: ExecutorWorkIdentity,
        result: ExecutedExecutorCommand,
    ) -> None:
        print(
            f"[executor] completed work={identity.work_key.value} "
            f"cpu_slots={result.admission_grant.cpu_slots}/"
            f"{self._host_cpu_slots} "
            f"concurrency={result.admission_grant.concurrency} "
            f"exit={result.exit_code} "
            f"wall={result.resources.wall_seconds:.3f}s "
            f"child_cpu={result.resources.cpu_seconds:.3f}s "
            f"max_rss={result.resources.max_rss_bytes}",
            file=sys.stderr,
            flush=True,
        )

    @staticmethod
    def _max_rss_bytes(raw_max_rss: int) -> int:
        return raw_max_rss if sys.platform == "darwin" else raw_max_rss * 1024
