# pyright: strict
"""Inbound half of the anti-corruption layer for pool inspection.

Translates what ``condor_status`` and ``condor_q`` report into the
backend-neutral vocabulary of ``ports.executor_pool``. Nothing here
escapes upward: job status codes, ClassAd attribute names, and slot
partitioning are all scheduler concepts that stop at this file, exactly
as submit-description syntax stops at the compiler.

Read-only by construction: the only tools it invokes are queries.
"""

from __future__ import annotations

import json
import time
from typing import cast

from ...domain.lane_execution import (
    LaneExecutorError,
    LaneExecutorUnavailableError,
    LaneWorkKey,
)
from ...ports.executor_pool import (
    ExecutorPoolInspector,
    ForeignJobOrigin,
    LaneJobOrigin,
    PoolCapacity,
    PoolInspectionError,
    PoolJob,
    PoolJobOrigin,
    PoolJobState,
    PoolOffline,
    PoolOnline,
    PoolState,
    PoolUnknownHealth,
)
from .tools import CondorTools

# Only the attributes the snapshot actually renders are requested. This
# is a privacy boundary as much as an efficiency one: Cmd, Args, Env,
# and Iwd are never asked for, so a command line or a secret in the
# environment cannot reach the operator's terminal through this path.
_JOB_ATTRIBUTES = (
    "JobStatus",
    "JobBatchName",
    "LaneSubmitter",
    "Owner",
    "JobPrio",
    "EnteredCurrentStatus",
    "RequestCpus",
    "ConcurrencyLimits",
    "ServerTime",
)
# ``LastHeardFrom`` is stamped by the collector when it last received
# this resource's advertisement — the only attribute that distinguishes
# a live machine from a dead one whose ad the collector is still
# serving from cache (finding 3, #7138).
_SLOT_ATTRIBUTES = (
    "Name",
    "Machine",
    "TotalSlotCpus",
    "DynamicSlot",
    "LastHeardFrom",
)

# How often a resource is required to re-advertise. Read from the pool
# rather than assumed, so the staleness bound is the pool's own promise.
_UPDATE_INTERVAL_SETTING = "UPDATE_INTERVAL"
# One missed advertisement is jitter; two is a daemon that stopped
# talking. Kept well under the collector's cache expiry, which is what
# makes a dead startd's ad linger in the first place.
_MISSED_UPDATES_BEFORE_STALE = 2
# An ad stamped in the future means the clock this reader is using and
# the clock the collector used disagree, so no age computed from them
# means anything. Small tolerance absorbs ordinary drift.
_CLOCK_SKEW_TOLERANCE_SECONDS = 60.0

# HTCondor's JobStatus codes, translated once. Every code the scheduler
# defines is mapped: an unmapped code means the scheduler grew a state
# this adapter has never seen, which is a defect to surface rather than
# a job to quietly drop.
_JOB_STATUS_STATES = {
    1: PoolJobState.QUEUED,
    2: PoolJobState.RUNNING,
    3: PoolJobState.FINISHING,
    4: PoolJobState.FINISHING,
    5: PoolJobState.HELD,
    6: PoolJobState.FINISHING,
    7: PoolJobState.SUSPENDED,
}
# One timestamp answers "how long has it been like this" for every
# state, because the scheduler stamps it on every status transition.
# A per-state table of *other* timestamps is how a suspended job came
# to be aged from when it started RUNNING (finding 4, #7138): that is
# time-in-run, not time-in-state, and the two diverge exactly when an
# operator most needs the difference.
_STATE_SINCE_ATTRIBUTE = "EnteredCurrentStatus"


def resolve_pool_inspector() -> ExecutorPoolInspector:
    """The inspector for this machine's pool, or one that says why not.

    Absence is reported, never raised: an operator running the status
    command on a machine without a pool installed must get the sentence
    explaining that, alongside the dispatch history that does exist.
    """
    try:
        return CondorPoolInspector(CondorTools.resolve())
    except LaneExecutorUnavailableError as error:
        return _AbsentPoolInspector(str(error))


class CondorPoolInspector:
    """Read pool capacity and queue through the scheduler's query tools."""

    def __init__(self, tools: CondorTools) -> None:
        if type(tools) is not CondorTools:
            raise ValueError("CondorPoolInspector.tools must be CondorTools")
        self._tools = tools

    def inspect(self) -> PoolState:
        slots = self._query(
            (
                str(self._tools.pool_query),
                "-json",
                "-attributes",
                ",".join(_SLOT_ATTRIBUTES),
            )
        )
        if type(slots) is PoolOffline:
            return slots
        jobs = self._query(
            (
                str(self._tools.query),
                "-allusers",
                "-json",
                "-attributes",
                ",".join(_JOB_ATTRIBUTES),
            )
        )
        if type(jobs) is PoolOffline:
            return jobs
        counted = _counted_slots(cast(tuple[dict[str, object], ...], slots))
        capacity = _read_capacity(counted)
        queue = tuple(
            _read_job(ad) for ad in cast(tuple[dict[str, object], ...], jobs)
        )
        unhealthy = self._health_objection(counted)
        if unhealthy is not None:
            return PoolUnknownHealth(
                capacity=capacity, jobs=queue, detail=unhealthy
            )
        return PoolOnline(capacity=capacity, jobs=queue)

    def _health_objection(
        self, counted: tuple[dict[str, object], ...]
    ) -> str | None:
        """Why this pool must not be called online, or ``None`` if it may.

        Answering the query is not evidence of health: the collector
        keeps serving a stopped daemon's advertisement until it expires,
        so a pool with no running execute daemon reports full capacity
        for minutes. Health is therefore asserted only from evidence —
        resources exist, and the collector heard from each of them
        recently enough — and every other outcome, including "could not
        tell", is an objection (finding 3, #7138).
        """
        if not counted:
            return (
                "the pool answered but advertises no execute resources: "
                "nothing can run here (is condor_startd running?)"
            )
        interval = self._update_interval_seconds()
        if interval is None:
            return (
                "the pool did not report its "
                f"{_UPDATE_INTERVAL_SETTING}, so how recently it last "
                "heard from its machines cannot be established"
            )
        allowed = interval * _MISSED_UPDATES_BEFORE_STALE
        now = time.time()
        for slot in counted:
            heard = slot.get("LastHeardFrom")
            if type(heard) is not int:
                return (
                    "the pool did not say when it last heard from "
                    f"{_slot_name(slot)}, so its liveness cannot be "
                    "established"
                )
            age = now - heard
            if age < -_CLOCK_SKEW_TOLERANCE_SECONDS:
                return (
                    f"{_slot_name(slot)} was last heard from "
                    f"{-age:.0f}s in the future: this reader's clock and "
                    "the pool's disagree, so no age here is meaningful"
                )
            if age > allowed:
                return (
                    f"the pool last heard from {_slot_name(slot)} "
                    f"{age:.0f}s ago, more than "
                    f"{_MISSED_UPDATES_BEFORE_STALE} advertisement "
                    f"intervals ({allowed:.0f}s): this record is STALE and "
                    "its capacity may belong to a machine that is gone"
                )
        return None

    def _update_interval_seconds(self) -> float | None:
        """The pool's own re-advertisement interval, or None if unreadable.

        Read through :meth:`CondorTools.read_configuration`, not
        :meth:`~CondorTools.invoke`, because this is a question ABOUT
        the pool: a per-process ``_CONDOR_UPDATE_INTERVAL`` export would
        otherwise let the caller's own environment widen the staleness
        window and certify a dead pool as fresh. The queue and slot
        queries above keep using ``invoke`` deliberately — those ask
        the pool for its state, and must reach the SAME pool a
        submission would, overrides included.
        """
        try:
            completed = self._tools.read_configuration(_UPDATE_INTERVAL_SETTING)
        except LaneExecutorError:
            return None
        if completed.returncode != 0:
            return None
        try:
            interval = float(completed.stdout.strip())
        except ValueError:
            return None
        return interval if interval > 0 else None

    def _query(
        self, arguments: tuple[str, ...]
    ) -> tuple[dict[str, object], ...] | PoolOffline:
        """Run one query tool and decode its ads, or say why the pool is out.

        A tool that cannot run, times out, or reports failure means the
        pool is not answering — an ordinary state for an opt-in backend,
        so it becomes an offline reason rather than an exception. Only a
        tool that succeeds and then emits undecodable output is a defect.
        """
        try:
            completed = self._tools.invoke(arguments)
        except LaneExecutorError as error:
            return PoolOffline(f"the pool did not answer: {error}")
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            return PoolOffline(
                "the scheduler is installed but not reachable: "
                f"{detail or f'{arguments[0]} exited {completed.returncode}'}"
            )
        text = completed.stdout.strip()
        if not text:
            # An empty queue prints nothing at all rather than "[]".
            return ()
        try:
            payload = cast(object, json.loads(text))
        except json.JSONDecodeError as error:
            raise PoolInspectionError(
                f"the pool query {arguments[0]} did not return JSON: {error}"
            ) from error
        if not isinstance(payload, list):
            raise PoolInspectionError(
                f"the pool query {arguments[0]} did not return a list of records"
            )
        ads: list[dict[str, object]] = []
        for ad in cast(list[object], payload):
            if not isinstance(ad, dict):
                raise PoolInspectionError(
                    f"the pool query {arguments[0]} returned a non-record entry"
                )
            ads.append(cast(dict[str, object], ad))
        return tuple(ads)


class _AbsentPoolInspector:
    """Stand-in for a machine with no pool: always offline, always says why."""

    def __init__(self, detail: str) -> None:
        self._detail = detail

    def inspect(self) -> PoolState:
        return PoolOffline(self._detail)


def _counted_slots(
    slots: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    """The advertisements that represent real capacity.

    A partitionable slot carves dynamic child slots out of itself while
    jobs run, and both appear in the listing; counting the children too
    would report a machine as larger the busier it gets. Selecting once
    here means capacity and the liveness check judge the same records.
    """
    return tuple(slot for slot in slots if slot.get("DynamicSlot") is not True)


def _slot_name(slot: dict[str, object]) -> str:
    name = slot.get("Name")
    return name if type(name) is str and name else str(slot.get("Machine"))


def _read_capacity(counted: tuple[dict[str, object], ...]) -> PoolCapacity:
    """Total the machines and cpus the counted advertisements describe."""
    machines: set[str] = set()
    total_cpus = 0
    for slot in counted:
        machine = slot.get("Machine")
        if type(machine) is not str or not machine:
            raise PoolInspectionError("a pool slot reported no machine name")
        machines.add(machine)
        total_cpus += _read_int(slot, "TotalSlotCpus")
    return PoolCapacity(machines=len(machines), total_cpus=total_cpus)


def _read_job(ad: dict[str, object]) -> PoolJob:
    return PoolJob(
        origin=_read_origin(ad),
        state=_read_state(ad),
        seconds_in_state=_read_seconds_in_state(ad),
        # Reported as-is, never floored to one: rounding a job's request
        # up would overstate how much of the machine is spoken for, and
        # "in use" is the number an operator acts on.
        request_cpus=_read_int(ad, "RequestCpus"),
        priority=_read_int(ad, "JobPrio"),
        exclusive=_read_exclusive(ad),
    )


def _read_state(ad: dict[str, object]) -> PoolJobState:
    code = _read_int(ad, "JobStatus")
    state = _JOB_STATUS_STATES.get(code)
    if state is None:
        raise PoolInspectionError(
            f"the pool reported an unknown job status code {code}"
        )
    return state


def _read_origin(ad: dict[str, object]) -> PoolJobOrigin:
    """Attribute the job to a lane, or to whoever else is sharing the pool.

    A lane job is one this system tagged at submission *and* whose batch
    name is a well-formed work key. Both must hold: the tag alone would
    let a foreign job wearing our attribute name be reported as a lane.
    """
    submitter = ad.get("LaneSubmitter")
    batch_name = ad.get("JobBatchName")
    if type(submitter) is str and submitter:
        if type(batch_name) is not str:
            raise PoolInspectionError(
                "a job tagged as a lane carries no work key"
            )
        try:
            work_key = LaneWorkKey(batch_name)
        except ValueError as error:
            raise PoolInspectionError(
                f"a job tagged as a lane carries an unusable work key: {error}"
            ) from error
        return LaneJobOrigin(work_key=work_key, submitter_worktree=submitter)
    owner = ad.get("Owner")
    if type(owner) is not str or not owner:
        raise PoolInspectionError("a pool job reported no owner")
    return ForeignJobOrigin(owner=owner)


def _read_seconds_in_state(ad: dict[str, object]) -> float:
    """Age the job against the scheduler's own clock, not this process's.

    ``ServerTime`` travels with every record precisely so a reader on a
    machine with a skewed clock cannot report a negative wait. A missing
    stamp raises rather than reading as zero: "just entered this state"
    is a specific claim, and inventing it is how a frozen lane gets
    reported as one that only just froze.
    """
    return max(
        0.0,
        float(_read_int(ad, "ServerTime") - _read_int(ad, _STATE_SINCE_ATTRIBUTE)),
    )


def _read_exclusive(ad: dict[str, object]) -> tuple[str, ...]:
    limits = ad.get("ConcurrencyLimits")
    if limits is None:
        return ()
    if type(limits) is not str:
        raise PoolInspectionError(
            "a pool job reported unreadable exclusive resources"
        )
    return tuple(token.strip() for token in limits.split(",") if token.strip())


def _read_int(ad: dict[str, object], attribute: str) -> int:
    """Read a whole-number attribute, however the scheduler spelled it.

    The scheduler renders some counts as JSON floats (``18.0``) and
    others as integers, for the same underlying whole number — so an
    integral float is the same fact, while a fractional one is not a
    count at all and says so. ``bool`` is an int subclass and is
    rejected: a boolean here would mean the attribute is not the number
    this adapter believes it is.
    """
    value = ad.get(attribute)
    if type(value) is int:
        return value
    if type(value) is float and value.is_integer():
        return int(value)
    raise PoolInspectionError(
        f"the pool reported {attribute!r} as {value!r}, not a whole number"
    )
