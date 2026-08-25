"""Thin CLI translation for the machine-wide executor deep module."""

from __future__ import annotations

import argparse
import os
from datetime import datetime

from rich.console import Console

from ..domain.executor import (
    ExecutorCommand,
    ExecutorCommandLifecycle,
    ExecutorConcurrencyRange,
    ExecutorExclusiveResource,
    ExecutorFairnessGroup,
    ExecutorNoCommandCancellation,
    ExecutorRunSpecification,
    ExecutorWorkKey,
)
from ..domain.executor_monitoring import (
    ExecutorAllRepositories,
    ExecutorAdmissionDeadlineExceeded,
    ExecutorCommandDeadlineExceeded,
    ExecutorCommandLifecycleFailed,
    ExecutorEvent,
    ExecutorPolicyChanged,
    ExecutorRecentEventsQuery,
    ExecutorRepositoryLabelFilter,
    ExecutorStatusQuery,
    ExecutorWorkAdmitted,
    ExecutorWorkCompleted,
    ExecutorWorkEnqueued,
    ExecutorWorkWaiting,
    ExecutorHostLoad,
)
from .command_exit_status import forward_command_exit_status
from ..infra.executor_deadline_environment import EXECUTOR_DEADLINE_ENVIRONMENT
from ..infra.validation_executor_handshake import (
    VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT,
)

EXECUTOR_GROUP_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_GROUP"

console = Console()


def cmd_executor_run(args: argparse.Namespace) -> int:
    """Run one repository command under the machine-owned executor pool."""
    from .bootstrap import build_executor

    command_arguments = tuple(args.executor_command)
    if command_arguments[:1] == ("--",):
        command_arguments = command_arguments[1:]
    if not command_arguments:
        console.print("[red]executor-run requires a command after --[/red]")
        return 2
    try:
        VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT.acknowledge_if_requested(os.environ)
        concurrency_range = _executor_concurrency_range(
            minimum_concurrency=args.min_concurrency,
            maximum_concurrency=args.max_concurrency,
        )
        specification = ExecutorRunSpecification(
            work_key=ExecutorWorkKey(args.work_key),
            fairness_group=_executor_group(
                command_line_group=args.executor_group,
                environment_group=os.environ.get(EXECUTOR_GROUP_ENV),
            ),
            concurrency_range=concurrency_range,
            exclusive_resources=tuple(
                ExecutorExclusiveResource(resource) for resource in args.exclusive
            ),
        )
        result = build_executor().run(
            specification,
            ExecutorCommand(
                command_arguments,
                EXECUTOR_DEADLINE_ENVIRONMENT.decode(os.environ),
                ExecutorCommandLifecycle.DETACHED,
                ExecutorNoCommandCancellation(),
            ),
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]executor-run failed: {exc}[/red]")
        return 2
    return forward_command_exit_status(result.exit_code)


def cmd_executor_policy(args: argparse.Namespace) -> int:
    """Inspect or change the machine-wide executor aggressiveness dial."""
    from .bootstrap import build_executor

    try:
        from ..domain.executor import ExecutorAggressiveness

        executor = build_executor()
        change = None
        if args.aggressiveness is not None:
            change = executor.configure_policy(
                ExecutorAggressiveness(args.aggressiveness)
            )
        effective = change.effective if change is not None else executor.policy()
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]executor-policy failed: {exc}[/red]")
        return 2

    if (
        change is not None
        and change.saved.aggressiveness.percent != effective.aggressiveness.percent
    ):
        console.print(
            "Executor aggressiveness saved as "
            f"{change.saved.aggressiveness.percent}%; "
            f"effective value is {effective.aggressiveness.percent}% "
            f"from {effective.source.value}."
        )
    else:
        console.print(
            f"Executor aggressiveness: {effective.aggressiveness.percent}% "
            f"({effective.source.value})"
        )
    return 0


def cmd_executor_events(args: argparse.Namespace) -> int:
    """Render recent typed executor events for human diagnosis."""
    from .bootstrap import build_executor_monitor

    try:
        timeline = build_executor_monitor().recent_events(
            ExecutorRecentEventsQuery(args.limit)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]executor-events failed: {exc}[/red]")
        return 2
    if not timeline.events:
        console.print("No executor events recorded.")
        return 0
    for event in timeline.events:
        console.print(_format_executor_event(event), markup=False, soft_wrap=True)
    return 0


def cmd_executor_status(args: argparse.Namespace) -> int:
    """Render current host policy and retained learning through the monitor."""
    from .bootstrap import build_executor_monitor

    try:
        repository_selection = (
            ExecutorAllRepositories()
            if args.repository is None
            else ExecutorRepositoryLabelFilter(args.repository)
        )
        query = ExecutorStatusQuery(
            repository_selection=repository_selection,
            offset=args.offset,
            limit=args.limit,
        )
        status = build_executor_monitor().status(query)
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]executor-status failed: {exc}[/red]")
        return 2
    console.print(
        f"Executor host CPU slots: {status.host_cpu_slots}\n"
        f"Executor aggressiveness: {status.policy.aggressiveness.percent}% "
        f"({status.policy.source.value})\n"
        f"Successful learning samples: "
        f"{status.learning.successful_observation_count}\n"
        f"Excluded historical failure samples: "
        f"{status.learning.failed_observation_count}\n"
        f"Learning fingerprint: {status.learning.fingerprint_sha256}",
        markup=False,
    )
    shown_profiles = min(
        max(status.learning.matching_profile_count - status.learning.page_offset, 0),
        query.limit,
    )
    console.print(
        f"Profile page: offset={status.learning.page_offset} "
        f"shown={shown_profiles} "
        f"matching={status.learning.matching_profile_count} "
        f"total={status.learning.total_profile_count}",
        markup=False,
    )
    for learned in status.learning.learned_work:
        console.print(
            f"repo={learned.repository.label} work={learned.work_key.value} "
            f"successful_samples={learned.successful_observation_count} "
            f"estimated_cores_per_worker="
            f"{learned.estimated_cores_per_concurrency:.3f}",
            markup=False,
        )
    for excluded in status.learning.excluded_failure_history:
        console.print(
            f"repo={excluded.repository.label} work={excluded.work_key.value} "
            f"excluded_failed_samples={excluded.failed_observation_count}",
            markup=False,
        )
    return 0


def _format_executor_event(event: ExecutorEvent) -> str:
    timestamp = (
        datetime.fromtimestamp(event.metadata.recorded_at_unix)
        .astimezone()
        .isoformat(timespec="seconds")
    )
    if isinstance(event, ExecutorPolicyChanged):
        return (
            f"{timestamp} policy-changed saved={event.saved.percent}% "
            f"effective={event.effective.percent}% "
            f"source={event.effective_source.value}"
        )
    work = event.work
    prefix = (
        f"{timestamp} repo={work.repository.label} work={work.work_key.value} "
        f"group={work.fairness_group.value} request={work.request_id.value}"
    )
    if isinstance(event, ExecutorWorkEnqueued):
        exclusive_resources = ",".join(
            resource.value for resource in event.exclusive_resources
        )
        exclusive = f" exclusive={exclusive_resources}" if exclusive_resources else ""
        return (
            f"{prefix} enqueued "
            f"concurrency={event.concurrency_range.minimum_concurrency}-"
            f"{event.concurrency_range.maximum_concurrency} "
            f"learned_cores_per_worker="
            f"{event.learned_cores_per_concurrency:.3f} "
            f"successful_samples={event.successful_observation_count} "
            f"queue_settle={event.queue_settle_seconds:.3f}s "
            f"aggressiveness={event.aggressiveness.percent}% "
            f"policy_source={event.policy_source.value} "
            f"host_cpu_slots={event.host_cpu_slots} "
            f"{_format_host_load(event.host_load)}{exclusive}"
        )
    if isinstance(event, ExecutorWorkWaiting):
        return (
            f"{prefix} waiting reason={event.reason.value} "
            f"cpu_slots={event.cpu_slots.leased}/{event.cpu_slots.total} "
            f"available={event.cpu_slots.available} "
            f"host_cpu_busy={event.host_cpu_utilization.busy_percent:.1f}% "
            f"sample={event.host_cpu_utilization.observation_seconds:.3f}s "
            f"{_format_host_load(event.host_load)}"
        )
    if isinstance(event, ExecutorWorkAdmitted):
        return (
            f"{prefix} admitted concurrency={event.concurrency} "
            f"charged_cpu_slots={event.charged_cpu_slots} "
            f"reserved_for_queued_peers="
            f"{event.reserved_cpu_slots_for_queued_peers} "
            f"available_before={event.cpu_slots_before.available}/"
            f"{event.cpu_slots_before.total} wait={event.wait_seconds:.3f}s "
            f"host_cpu_busy={event.host_cpu_utilization.busy_percent:.1f}% "
            f"sample={event.host_cpu_utilization.observation_seconds:.3f}s "
            f"{_format_host_load(event.host_load)}"
        )
    if isinstance(event, ExecutorCommandLifecycleFailed):
        return (
            f"{prefix} command-lifecycle-failed concurrency={event.concurrency} "
            f"error={event.error_type}: {event.error_message}"
        )
    if isinstance(event, ExecutorAdmissionDeadlineExceeded):
        return (
            f"{prefix} deadline-exceeded phase=admission "
            f"reason={event.reason.value} elapsed={event.elapsed_seconds:.3f}s "
            f"active_timeout={event.active_timeout_seconds:.3f}s "
            f"absolute_timeout={event.absolute_timeout_seconds:.3f}s"
        )
    if isinstance(event, ExecutorCommandDeadlineExceeded):
        return (
            f"{prefix} deadline-exceeded phase=command "
            f"reason={event.reason.value} concurrency={event.concurrency} "
            f"elapsed={event.elapsed_seconds:.3f}s "
            f"active_timeout={event.active_timeout_seconds:.3f}s "
            f"absolute_timeout={event.absolute_timeout_seconds:.3f}s"
        )
    if isinstance(event, ExecutorWorkCompleted):
        return (
            f"{prefix} completed exit={event.exit_code} "
            f"concurrency={event.concurrency} "
            f"charged_cpu_slots={event.charged_cpu_slots} "
            f"wall={event.resources.wall_seconds:.3f}s "
            f"child_cpu={event.resources.cpu_seconds:.3f}s "
            "executor_process_lifetime_children_max_rss="
            f"{event.resources.executor_process_lifetime_children_max_rss_bytes} "
            f"successful_samples={event.successful_observation_count} "
            f"learned_cores_per_worker="
            f"{event.previous_cores_per_concurrency:.3f}->"
            f"{event.updated_cores_per_concurrency:.3f} "
            f"{_format_host_load(event.host_load)}"
        )
    raise AssertionError(f"unsupported executor event: {type(event).__name__}")


def _format_host_load(host_load: ExecutorHostLoad) -> str:
    """Render the diagnostic load-average triple consistently."""
    return (
        f"host_load_1m={host_load.one_minute:.2f} "
        f"host_load_5m={host_load.five_minutes:.2f} "
        f"host_load_15m={host_load.fifteen_minutes:.2f}"
    )


def _executor_concurrency_range(
    *,
    minimum_concurrency: int | None,
    maximum_concurrency: int | None,
) -> ExecutorConcurrencyRange:
    if minimum_concurrency is None or maximum_concurrency is None:
        raise ValueError(
            "--min-concurrency and --max-concurrency must be supplied together"
        )
    return ExecutorConcurrencyRange(minimum_concurrency, maximum_concurrency)


def _executor_group(
    *,
    command_line_group: str | None,
    environment_group: str | None,
) -> ExecutorFairnessGroup:
    if command_line_group is not None and environment_group is not None:
        if command_line_group != environment_group:
            raise ValueError(f"--group conflicts with {EXECUTOR_GROUP_ENV}")
    resolved = command_line_group or environment_group
    if resolved is None:
        raise ValueError(f"--group or {EXECUTOR_GROUP_ENV} is required")
    return ExecutorFairnessGroup(resolved)
