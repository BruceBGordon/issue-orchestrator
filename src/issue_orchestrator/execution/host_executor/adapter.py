# pyright: strict
"""Deep host adapter implementing the public machine-wide executor port."""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

from ...control.executor_admission import (
    ExecutorAdmissionGrant,
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
    ExecutorCommandFinalizationFailure,
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
from ...domain.executor_guardian import (
    ExecutorGuardianActivationTimedOut,
    ExecutorGuardianBoundedBudget,
    ExecutorGuardianBudget,
    ExecutorGuardianCommandCompleted,
    ExecutorGuardianCommandInterrupted,
    ExecutorGuardianCommandResourceUsage,
    ExecutorGuardianCommandStartError,
    ExecutorGuardianCommandStartFailed,
    ExecutorGuardianCommandTimedOut,
    ExecutorGuardianInternalError,
    ExecutorGuardianInternalFailed,
    ExecutorGuardianPostContainmentError,
    ExecutorGuardianResourceObservationError,
    ExecutorGuardianResourceObservationFailed,
    ExecutorGuardianSerializedFailureError,
    ExecutorGuardianTerminal,
    ExecutorGuardianUnboundedBudget,
)
from ...ports.executor import Executor
from ...ports.executor_command_guardian import (
    ExecutorCommandGuardian,
    ExecutorGuardianRequest,
)
from ...ports.atomic_path_replacement import AtomicPathReplacement
from ...ports.executor_history_lock import ExecutorHistoryRetentionLock
from ...ports.host_cpu_utilization import HostCpuUtilizationObserver
from ._history import ExecutorWorkHistoryStore
from ._host_observation import observe_host_load
from ._journal import ExecutorEventStore
from ._deadline import ExecutorDeadlineOwner, ExecutorDeadlineReport
from ._repository import ExecutorRepositoryResolver
from ._state import (
    HostAdmissionGranted,
    HostExecutorLease,
    HostExecutorState,
    OwnedQueuedRequest,
)
from ._types import (
    ExecutorCommandResourceObservationFailed,
    ExecutorCommandResourceObservationNotApplicable,
    ExecutorCommandWithoutResourceObservation,
    ExecutorResourceObservationOmissionReason,
    ExecutedExecutorCommand,
    ExecutorWorkIdentity,
    FinalizableExecutorCommand,
)
from ._completion import (
    ExecutorCommandCompletion,
    build_executor_command_finalizer,
)
from .host_policy import ExecutorPolicyStore
from .request_identity import ExecutorRequestIdentityFactory
from ..atomic_record_store import AtomicRecordStore
from ..independent_cleanup import (
    CleanupAction,
    CleanupFailed,
    CleanupOutcome,
    CleanupSucceeded,
    IndependentCleanupPlan,
    raise_primary_with_cleanup,
)


EXECUTOR_CONCURRENCY_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_CONCURRENCY"


def _require_host_cpu_observer(value: object) -> None:
    if not isinstance(value, HostCpuUtilizationObserver):
        raise ValueError(
            "HostExecutor.host_cpu_observer must implement HostCpuUtilizationObserver"
        )


def _require_command_guardian(value: object) -> None:
    if not isinstance(value, ExecutorCommandGuardian):
        raise ValueError(
            "HostExecutor.command_guardian must implement ExecutorCommandGuardian"
        )


def _require_deadline_owner(value: object) -> ExecutorDeadlineOwner:
    if not isinstance(value, ExecutorDeadlineOwner):
        raise ValueError(
            "HostExecutor.deadline_owner must implement ExecutorDeadlineOwner"
        )
    return value


def _require_history_retention_lock(value: object) -> None:
    if not isinstance(value, ExecutorHistoryRetentionLock):
        raise ValueError(
            "HostExecutor.history_retention_lock must implement "
            "ExecutorHistoryRetentionLock"
        )


def _require_atomic_path_replacement(value: object) -> None:
    if not isinstance(value, AtomicPathReplacement):
        raise ValueError(
            "HostExecutor.atomic_path_replacement must implement AtomicPathReplacement"
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
        command_guardian: ExecutorCommandGuardian,
        deadline_owner: ExecutorDeadlineOwner,
        atomic_path_replacement: AtomicPathReplacement,
        history_retention_lock: ExecutorHistoryRetentionLock,
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
        _require_command_guardian(command_guardian)
        self._command_guardian = command_guardian
        self._deadline_owner = _require_deadline_owner(deadline_owner)
        if type(history_retention_policy) is not ExecutorHistoryRetentionPolicy:
            raise ValueError(
                "HostExecutor.history_retention_policy must be an "
                "ExecutorHistoryRetentionPolicy"
            )
        _require_history_retention_lock(history_retention_lock)
        _require_atomic_path_replacement(atomic_path_replacement)
        self._queue_settle_seconds = queue_settle_seconds
        self._queue_poll_seconds = queue_poll_seconds
        pool_records = AtomicRecordStore(
            pool_dir,
            atomic_path_replacement,
        )
        self._state = HostExecutorState(
            pool_dir,
            host_cpu_slots,
            pool_records,
        )
        self._history = ExecutorWorkHistoryStore(
            pool_dir / "work-history",
            history_retention_policy,
            history_retention_lock,
            AtomicRecordStore(
                pool_dir / "work-history",
                atomic_path_replacement,
            ),
        )
        self._policy_store = ExecutorPolicyStore(pool_dir, pool_records)
        self._events = ExecutorEventStore(pool_dir)
        self._command_finalizer = build_executor_command_finalizer(
            self._history,
            self._events,
            demand_estimator,
            host_cpu_slots,
        )
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
        return self._run_owned(specification, command)

    def _run_owned(
        self,
        specification: ExecutorRunSpecification,
        command: ExecutorCommand,
    ) -> ExecutorRunResult:
        """Execute while machine coordination owns admission and containment."""
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
        owned_request = self._state.enqueue(work)
        lease: HostExecutorLease
        try:
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
                        "an unbounded executor command cannot exceed admission deadline"
                    ) from exc
                elapsed = time.monotonic() - submitted_at_monotonic
                self._deadline_owner.admission_exceeded(
                    ExecutorDeadlineReport(
                        identity=identity,
                        work=work,
                        deadline=deadline,
                        reason=exc.reason,
                        phase=ExecutorDeadlinePhase.ADMISSION,
                        elapsed_seconds=elapsed,
                    ),
                    exc,
                )
        except BaseException as queue_body_error:
            owned_request.release_after_failure(queue_body_error)
        owned_request.release_after_grant(lease)
        result = self._run_command(
            identity,
            work,
            lease,
            command,
            submitted_at_monotonic,
        )
        return self._command_finalizer.finalize(
            ExecutorCommandCompletion(
                identity=identity,
                work=work,
                command=result.command,
                previous_demand=previous_demand,
                aggressiveness_percent=effective_policy.aggressiveness.percent,
                initial_failures=result.initial_failures,
            )
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
                    self._report_admission(
                        "acquired",
                        owned_request.work,
                        outcome.lease,
                        f" wait={waited:.3f}s",
                    )
                except BaseException as admission_publication_error:
                    raise_primary_with_cleanup(
                        "executor admission publication and lease cleanup failures",
                        admission_publication_error,
                        IndependentCleanupPlan(
                            (
                                CleanupAction(
                                    "release unpublished executor lease",
                                    outcome.lease.release,
                                ),
                            )
                        ).run(),
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
    ) -> FinalizableExecutorCommand:
        """Own the admitted lease across every command preparation outcome."""
        try:
            result = self._run_command_with_local_lease(
                identity,
                work,
                lease,
                command,
                submitted_at_monotonic,
            )
        except BaseException as command_error:
            raise_primary_with_cleanup(
                "executor command and lease cleanup failures",
                command_error,
                IndependentCleanupPlan(
                    (CleanupAction("release executor command lease", lease.release),)
                ).run(),
            )
        release = IndependentCleanupPlan(
            (CleanupAction("release executor command lease", lease.release),)
        ).run()
        if type(release) is CleanupSucceeded:
            return result
        return FinalizableExecutorCommand(
            result.command,
            (*result.initial_failures, *self._finalization_failures(release)),
        )

    def _run_command_with_local_lease(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        lease: HostExecutorLease,
        command: ExecutorCommand,
        submitted_at_monotonic: float,
    ) -> FinalizableExecutorCommand:
        """Execute while the caller retains authoritative lease ownership."""
        child_env = os.environ.copy()
        granted_concurrency = str(lease.grant.concurrency)
        child_env[EXECUTOR_CONCURRENCY_ENV] = granted_concurrency
        child_env["PYTEST_XDIST_AUTO_NUM_WORKERS"] = granted_concurrency
        started = time.monotonic()
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
            failures = self._record_command_deadline(
                identity,
                work,
                lease,
                command.deadline,
                exc.reason,
                time.monotonic() - started,
            )
            return FinalizableExecutorCommand(
                ExecutorCommandWithoutResourceObservation(
                    124,
                    lease.grant,
                    ExecutorCommandResourceObservationNotApplicable(
                        ExecutorResourceObservationOmissionReason.DEADLINE
                    ),
                ),
                failures,
            )
        guardian_budget = (
            ExecutorGuardianUnboundedBudget()
            if command_budget is None
            else ExecutorGuardianBoundedBudget(
                command_budget.expires_at_monotonic,
                command_budget.reason,
            )
        )
        post_containment_failures: tuple[ExecutorCommandFinalizationFailure, ...] = ()
        try:
            terminal = self._command_guardian.run(
                ExecutorGuardianRequest(
                    arguments=command.arguments,
                    environment=child_env,
                    lease=lease,
                    budget=guardian_budget,
                    lifecycle=command.lifecycle,
                    cancellation=command.cancellation,
                )
            )
        except ExecutorGuardianPostContainmentError as exc:
            terminal = exc.terminal
            post_containment_failures = tuple(
                ExecutorCommandFinalizationFailure(
                    failure.attempt_name,
                    failure.error,
                )
                for failure in exc.failures
            )
        except BaseException as exc:
            raise_primary_with_cleanup(
                "executor command lifecycle and evidence failures",
                exc,
                IndependentCleanupPlan(
                    (
                        CleanupAction(
                            "publish executor command lifecycle failure",
                            lambda: self._events.command_lifecycle_failed(
                                identity,
                                work,
                                lease.grant,
                                exc,
                            ),
                        ),
                    )
                ).run(),
            )
        return self._interpret_guardian_terminal(
            identity=identity,
            work=work,
            lease=lease,
            command=command,
            guardian_budget=guardian_budget,
            terminal=terminal,
            started_at_monotonic=started,
            post_containment_failures=post_containment_failures,
        )

    def _interpret_guardian_terminal(
        self,
        *,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        lease: HostExecutorLease,
        command: ExecutorCommand,
        guardian_budget: ExecutorGuardianBudget,
        terminal: ExecutorGuardianTerminal,
        started_at_monotonic: float,
        post_containment_failures: tuple[ExecutorCommandFinalizationFailure, ...],
    ) -> FinalizableExecutorCommand:
        """Translate one contained guardian outcome into executor semantics."""
        if type(terminal) is ExecutorGuardianCommandCompleted:
            return self._completed_guardian_command(
                terminal,
                lease.grant,
                post_containment_failures,
            )
        if type(terminal) is ExecutorGuardianCommandInterrupted:
            interrupted = ExecutorCommandWithoutResourceObservation(
                -terminal.signal_number,
                lease.grant,
                ExecutorCommandResourceObservationNotApplicable(
                    ExecutorResourceObservationOmissionReason.INTERRUPTION,
                ),
            )
            publication = IndependentCleanupPlan(
                (
                    CleanupAction(
                        "publish executor command interruption",
                        lambda: self._events.command_interrupted(
                            identity,
                            work,
                            lease.grant,
                            terminal.signal_number,
                        ),
                    ),
                )
            ).run()
            return FinalizableExecutorCommand(
                interrupted,
                (
                    *post_containment_failures,
                    *self._finalization_failures(publication),
                ),
            )
        if type(terminal) is ExecutorGuardianActivationTimedOut:
            activation_recovery_failures = tuple(
                ExecutorCommandFinalizationFailure(
                    failure.operation,
                    ExecutorGuardianSerializedFailureError(failure),
                )
                for failure in terminal.recovery_failures
            )
            return self._timed_out_guardian_command(
                identity=identity,
                work=work,
                lease=lease,
                command=command,
                guardian_budget=guardian_budget,
                reason=terminal.reason,
                started_at_monotonic=started_at_monotonic,
                post_containment_failures=(
                    *post_containment_failures,
                    *activation_recovery_failures,
                ),
            )
        if type(terminal) is ExecutorGuardianCommandTimedOut:
            return self._timed_out_guardian_command(
                identity=identity,
                work=work,
                lease=lease,
                command=command,
                guardian_budget=guardian_budget,
                reason=terminal.reason,
                started_at_monotonic=started_at_monotonic,
                post_containment_failures=post_containment_failures,
            )
        if type(terminal) is ExecutorGuardianCommandStartFailed:
            error: RuntimeError = ExecutorGuardianCommandStartError(terminal)
        elif type(terminal) is ExecutorGuardianInternalFailed:
            error = ExecutorGuardianInternalError(terminal)
        else:
            raise AssertionError("ExecutorGuardianTerminal is a closed union")
        publication = IndependentCleanupPlan(
            (
                CleanupAction(
                    "publish executor command lifecycle failure",
                    lambda: self._events.command_lifecycle_failed(
                        identity,
                        work,
                        lease.grant,
                        error,
                    ),
                ),
            )
        ).run()
        failures = (
            *post_containment_failures,
            *self._finalization_failures(publication),
        )
        if failures:
            raise BaseExceptionGroup(
                "executor guardian terminal and evidence failures",
                (error, *(failure.error for failure in failures)),
            )
        raise error

    def _timed_out_guardian_command(
        self,
        *,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        lease: HostExecutorLease,
        command: ExecutorCommand,
        guardian_budget: ExecutorGuardianBudget,
        reason: ExecutorDeadlineReason,
        started_at_monotonic: float,
        post_containment_failures: tuple[ExecutorCommandFinalizationFailure, ...],
    ) -> FinalizableExecutorCommand:
        """Finalize either pre-activation or active-command deadline expiry."""
        if type(guardian_budget) is not ExecutorGuardianBoundedBudget:
            raise AssertionError("an unbounded executor command cannot time out")
        if not isinstance(command.deadline, ExecutorBoundedDeadline):
            raise AssertionError(
                "a timed-out executor command must have a bounded deadline"
            )
        failures = self._record_command_deadline(
            identity,
            work,
            lease,
            command.deadline,
            reason,
            time.monotonic() - started_at_monotonic,
        )
        return FinalizableExecutorCommand(
            ExecutorCommandWithoutResourceObservation(
                124,
                lease.grant,
                ExecutorCommandResourceObservationNotApplicable(
                    ExecutorResourceObservationOmissionReason.DEADLINE
                ),
            ),
            (*post_containment_failures, *failures),
        )

    @staticmethod
    def _completed_guardian_command(
        terminal: ExecutorGuardianCommandCompleted,
        grant: ExecutorAdmissionGrant,
        failures: tuple[ExecutorCommandFinalizationFailure, ...],
    ) -> FinalizableExecutorCommand:
        resources = terminal.resources
        if type(resources) is ExecutorGuardianCommandResourceUsage:
            command: (
                ExecutedExecutorCommand | ExecutorCommandWithoutResourceObservation
            ) = ExecutedExecutorCommand(
                terminal.exit_code,
                grant,
                ExecutorResourceObservation(
                    concurrency=grant.concurrency,
                    wall_seconds=resources.wall_seconds,
                    cpu_seconds=resources.cpu_seconds,
                    guardian_process_lifetime_children_max_rss_bytes=(
                        resources.guardian_process_lifetime_children_max_rss_bytes
                    ),
                    input_blocks=resources.input_blocks,
                    output_blocks=resources.output_blocks,
                ),
            )
        elif type(resources) is ExecutorGuardianResourceObservationFailed:
            command = ExecutorCommandWithoutResourceObservation(
                terminal.exit_code,
                grant,
                ExecutorCommandResourceObservationFailed(
                    ExecutorGuardianResourceObservationError(resources)
                ),
            )
        else:
            raise AssertionError("guardian command resources are a closed union")
        return FinalizableExecutorCommand(command, failures)

    def _record_command_deadline(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        lease: HostExecutorLease,
        deadline: ExecutorBoundedDeadline,
        reason: ExecutorDeadlineReason,
        elapsed_seconds: float,
    ) -> tuple[ExecutorCommandFinalizationFailure, ...]:
        """Publish one terminal command-deadline decision through both seams."""
        return self._finalization_failures(
            IndependentCleanupPlan(
                (
                    CleanupAction(
                        "publish executor command deadline",
                        lambda: self._events.command_deadline_exceeded(
                            identity,
                            work,
                            lease.grant,
                            deadline,
                            reason,
                            elapsed_seconds,
                        ),
                    ),
                    CleanupAction(
                        "report executor command deadline",
                        lambda: self._deadline_owner.report(
                            ExecutorDeadlineReport(
                                identity=identity,
                                work=work,
                                deadline=deadline,
                                reason=reason,
                                phase=ExecutorDeadlinePhase.COMMAND,
                                elapsed_seconds=elapsed_seconds,
                            )
                        ),
                    ),
                )
            ).run()
        )

    @staticmethod
    def _finalization_failures(
        outcome: CleanupOutcome,
    ) -> tuple[ExecutorCommandFinalizationFailure, ...]:
        if type(outcome) is CleanupSucceeded:
            return ()
        if type(outcome) is not CleanupFailed:
            raise AssertionError("executor evidence attempts are a closed union")
        return tuple(
            ExecutorCommandFinalizationFailure(
                failure.action_name,
                failure.error,
            )
            for failure in outcome.failures
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
