"""Inbound translation: scheduler answers become pool contracts.

Hermetic: the scheduler tools are shell stubs that print canned output,
so no pool is required. What is under test is the anti-corruption layer,
not HTCondor.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor.pool_inspector import (
    CondorPoolInspector,
    resolve_pool_inspector,
)
from issue_orchestrator.adapters.condor.tools import (
    PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE,
    CondorTools,
)
from issue_orchestrator.domain.lane_execution import LaneWorkKey
from issue_orchestrator.ports.executor_pool import (
    ExecutorPoolInspector,
    ForeignJobOrigin,
    LaneJobOrigin,
    PoolInspectionError,
    PoolJobState,
    PoolOffline,
    PoolOnline,
    PoolState,
    PoolUnknownHealth,
)

_SERVER_TIME = 1_787_959_500
# Slot heartbeats are judged against the reader's clock, so fixtures
# stamp them relative to now rather than to the scheduler's fake clock.
_NOW = int(time.time())
_UPDATE_INTERVAL_SECONDS = 300

_IDLE_SLOT = {
    "Name": "slot1@host.local",
    "Machine": "host.local",
    "TotalSlotCpus": 18,
    "PartitionableSlot": True,
    "LastHeardFrom": _NOW - 5,
}
_DYNAMIC_SLOT = {
    "Name": "slot1_1@host.local",
    "Machine": "host.local",
    "TotalSlotCpus": 3,
    "DynamicSlot": True,
    "LastHeardFrom": _NOW - 5,
}
_RUNNING_LANE_JOB = {
    "JobStatus": 2,
    "JobBatchName": "test-unit",
    "LaneSubmitter": "issue-orchestrator-wt-alpha",
    "Owner": "operator",
    "JobPrio": 59,
    "EnteredCurrentStatus": _SERVER_TIME - 60,
    "RequestCpus": 3,
    "ServerTime": _SERVER_TIME,
}
_QUEUED_LANE_JOB = {
    "JobStatus": 1,
    "JobBatchName": "test-integration-core-local",
    "LaneSubmitter": "issue-orchestrator-wt-beta",
    "Owner": "operator",
    "JobPrio": 82,
    "EnteredCurrentStatus": _SERVER_TIME - 30,
    "ConcurrencyLimits": "codexlogin, claudelogin",
    "RequestCpus": 2,
    "ServerTime": _SERVER_TIME,
}
_FOREIGN_JOB = {
    "JobStatus": 2,
    "Owner": "someone-else",
    "JobPrio": 0,
    "EnteredCurrentStatus": _SERVER_TIME - 400,
    "RequestCpus": 4,
    "ServerTime": _SERVER_TIME,
}


def _stub_tools(
    tmp_path: Path,
    *,
    slots: str = "[]",
    jobs: str = "[]",
    slots_exit: int = 0,
    jobs_exit: int = 0,
    interval_exit: int = 0,
) -> CondorTools:
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    bodies = {
        "condor_submit": "#!/bin/sh\nexit 0\n",
        "condor_rm": "#!/bin/sh\nexit 0\n",
        "condor_q": f"#!/bin/sh\ncat <<'JSON'\n{jobs}\nJSON\nexit {jobs_exit}\n",
        "condor_config_val": (
            f"#!/bin/sh\necho {_UPDATE_INTERVAL_SECONDS}\nexit {interval_exit}\n"
        ),
        "condor_status": (
            f"#!/bin/sh\ncat <<'JSON'\n{slots}\nJSON\nexit {slots_exit}\n"
        ),
    }
    for name, body in bodies.items():
        tool = binaries / name
        tool.write_text(body)
        tool.chmod(0o755)
    return CondorTools(
        submit=binaries / "condor_submit",
        remove=binaries / "condor_rm",
        query=binaries / "condor_q",
        config_query=binaries / "condor_config_val",
        pool_query=binaries / "condor_status",
    )


def _inspect(tmp_path: Path, **kwargs) -> PoolState:
    return CondorPoolInspector(_stub_tools(tmp_path, **kwargs)).inspect()


def test_the_adapter_satisfies_the_port(tmp_path: Path) -> None:
    assert isinstance(
        CondorPoolInspector(_stub_tools(tmp_path)), ExecutorPoolInspector
    )


def test_capacity_counts_machines_once_and_ignores_carved_slots(
    tmp_path: Path,
) -> None:
    """A busy partitionable machine must not look bigger than an idle one.

    The scheduler lists the parent slot and every dynamic slot carved
    out of it; totalling both would grow reported capacity with load.
    """
    state = _inspect(tmp_path, slots=json.dumps([_IDLE_SLOT, _DYNAMIC_SLOT]))

    assert type(state) is PoolOnline
    assert state.capacity.machines == 1
    assert state.capacity.total_cpus == 18


def test_lane_jobs_carry_their_work_key_submitter_and_wait(tmp_path: Path) -> None:
    state = _inspect(
        tmp_path,
        slots=json.dumps([_IDLE_SLOT]),
        jobs=json.dumps([_RUNNING_LANE_JOB, _QUEUED_LANE_JOB]),
    )

    assert type(state) is PoolOnline
    running, queued = state.jobs
    assert running.state is PoolJobState.RUNNING
    assert running.origin == LaneJobOrigin(
        work_key=LaneWorkKey("test-unit"),
        submitter_worktree="issue-orchestrator-wt-alpha",
    )
    # Time in the current state — the only age the port promises.
    assert running.seconds_in_state == 60.0
    assert running.request_cpus == 3
    assert running.priority == 59
    assert running.exclusive == ()

    assert queued.state is PoolJobState.QUEUED
    assert queued.seconds_in_state == 30.0
    assert queued.exclusive == ("codexlogin", "claudelogin")


def test_claimed_cpus_come_from_the_jobs_that_started(tmp_path: Path) -> None:
    """One truth for "how busy": the rows printed underneath the header."""
    state = _inspect(
        tmp_path,
        slots=json.dumps([_IDLE_SLOT]),
        jobs=json.dumps([_RUNNING_LANE_JOB, _QUEUED_LANE_JOB, _FOREIGN_JOB]),
    )

    assert type(state) is PoolOnline
    assert state.claimed_cpus == 7


def test_a_job_this_system_did_not_submit_is_reported_as_foreign(
    tmp_path: Path,
) -> None:
    """Foreign jobs hold the same cpus, so they are part of the answer."""
    state = _inspect(
        tmp_path, slots=json.dumps([_IDLE_SLOT]), jobs=json.dumps([_FOREIGN_JOB])
    )

    assert type(state) is PoolOnline
    assert state.jobs[0].origin == ForeignJobOrigin(owner="someone-else")


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (1, PoolJobState.QUEUED),
        (2, PoolJobState.RUNNING),
        (3, PoolJobState.FINISHING),
        (4, PoolJobState.FINISHING),
        (5, PoolJobState.HELD),
        (6, PoolJobState.FINISHING),
        (7, PoolJobState.SUSPENDED),
    ],
)
def test_every_scheduler_status_code_translates(
    tmp_path: Path, code: int, expected: PoolJobState
) -> None:
    job = dict(_RUNNING_LANE_JOB)
    job["JobStatus"] = code
    job["EnteredCurrentStatus"] = _SERVER_TIME - 10

    state = _inspect(tmp_path, slots=json.dumps([_IDLE_SLOT]), jobs=json.dumps([job]))

    assert type(state) is PoolOnline
    assert state.jobs[0].state is expected


def test_an_unknown_status_code_is_a_defect_not_a_dropped_row(
    tmp_path: Path,
) -> None:
    """Silently skipping an untranslatable job would understate the pool."""
    job = dict(_RUNNING_LANE_JOB)
    job["JobStatus"] = 99

    with pytest.raises(PoolInspectionError, match="unknown job status"):
        _inspect(tmp_path, slots=json.dumps([_IDLE_SLOT]), jobs=json.dumps([job]))


def test_a_lane_tagged_job_with_an_unusable_work_key_is_a_defect(
    tmp_path: Path,
) -> None:
    job = dict(_RUNNING_LANE_JOB)
    job["JobBatchName"] = "Not A Work Key"

    with pytest.raises(PoolInspectionError, match="unusable work key"):
        _inspect(tmp_path, slots=json.dumps([_IDLE_SLOT]), jobs=json.dumps([job]))


def test_an_empty_queue_prints_nothing_and_means_nothing_queued(
    tmp_path: Path,
) -> None:
    """The query tool emits no output at all for an empty queue."""
    state = _inspect(tmp_path, slots=json.dumps([_IDLE_SLOT]), jobs="")

    assert type(state) is PoolOnline
    assert state.jobs == ()
    assert state.claimed_cpus == 0


def test_an_unreachable_pool_is_reported_offline_with_its_reason(
    tmp_path: Path,
) -> None:
    """Not an exception: an opt-in backend that is not running is normal."""
    state = _inspect(tmp_path, slots="CEDAR:6001 could not connect", slots_exit=1)

    assert type(state) is PoolOffline
    assert "not reachable" in state.detail
    assert "could not connect" in state.detail


def test_a_queue_that_fails_after_capacity_answers_is_still_offline(
    tmp_path: Path,
) -> None:
    state = _inspect(
        tmp_path,
        slots=json.dumps([_IDLE_SLOT]),
        jobs="schedd is down",
        jobs_exit=1,
    )

    assert type(state) is PoolOffline
    assert "schedd is down" in state.detail


@pytest.mark.parametrize("payload", ["{not json", '{"an": "object"}', "[3]"])
def test_undecodable_answers_are_defects(tmp_path: Path, payload: str) -> None:
    """A tool that succeeds and then emits garbage is a broken contract."""
    with pytest.raises(PoolInspectionError):
        _inspect(tmp_path, slots=payload)


def test_a_slot_without_a_machine_name_is_a_defect(tmp_path: Path) -> None:
    with pytest.raises(PoolInspectionError, match="machine name"):
        _inspect(tmp_path, slots=json.dumps([{"TotalSlotCpus": 4}]))


def test_a_non_numeric_attribute_is_a_defect(tmp_path: Path) -> None:
    with pytest.raises(PoolInspectionError, match="TotalSlotCpus"):
        _inspect(
            tmp_path,
            slots=json.dumps([{"Machine": "host.local", "TotalSlotCpus": "lots"}]),
        )


def test_a_count_the_scheduler_spelled_as_a_float_is_still_that_count(
    tmp_path: Path,
) -> None:
    """The scheduler renders some counts as JSON floats and others as ints.

    ``18.0`` cpus and ``18`` cpus are the same fact, and refusing one of
    the two spellings would break the command on a live pool.
    """
    job = dict(_RUNNING_LANE_JOB)
    job["RequestCpus"] = 3.0

    state = _inspect(
        tmp_path,
        slots=json.dumps(
            [{**_IDLE_SLOT, "TotalSlotCpus": 18.0}]
        ),
        jobs=json.dumps([job]),
    )

    assert type(state) is PoolOnline
    assert state.capacity.total_cpus == 18
    assert state.jobs[0].request_cpus == 3


def test_a_fractional_count_is_not_a_count(tmp_path: Path) -> None:
    with pytest.raises(PoolInspectionError, match="TotalSlotCpus"):
        _inspect(
            tmp_path,
            slots=json.dumps([{"Machine": "host.local", "TotalSlotCpus": 2.5}]),
        )


def test_a_boolean_is_never_read_as_a_number(tmp_path: Path) -> None:
    """``bool`` subclasses ``int``; letting it through would report True cpus."""
    with pytest.raises(PoolInspectionError, match="TotalSlotCpus"):
        _inspect(
            tmp_path,
            slots=json.dumps([{"Machine": "host.local", "TotalSlotCpus": True}]),
        )


def test_no_pool_installed_resolves_to_an_inspector_that_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absence must reach the operator as a sentence, not a stack trace."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv(
        PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE, str(tmp_path / "missing")
    )

    inspector = resolve_pool_inspector()

    assert isinstance(inspector, ExecutorPoolInspector)
    state = inspector.inspect()
    assert type(state) is PoolOffline
    assert "condor-personal.sh" in state.detail


def test_the_inspector_never_asks_for_command_lines_or_environments() -> None:
    """Privacy is a property of the query, not of the renderer.

    Attributes that could carry a prompt, a token, or a path into a
    private worktree are never requested, so they cannot leak even if a
    future consumer prints every field it is given.
    """
    from issue_orchestrator.adapters.condor import pool_inspector

    requested = {
        attribute.lower()
        for attribute in (
            *pool_inspector._JOB_ATTRIBUTES,
            *pool_inspector._SLOT_ATTRIBUTES,
        )
    }
    forbidden = {"cmd", "args", "arguments", "env", "environment", "iwd", "out", "err"}
    assert requested & forbidden == set()


def test_the_inspector_rejects_anything_but_resolved_tools() -> None:
    with pytest.raises(ValueError, match="CondorTools"):
        CondorPoolInspector("condor_q")  # type: ignore[arg-type]


# --- #7138 round 1, finding 4: fabricated job ages -------------------


def test_a_suspended_job_is_aged_from_when_it_was_suspended(
    tmp_path: Path,
) -> None:
    """The port promises time in the CURRENT state.

    A job that ran for 600s and was frozen 10s ago has been suspended
    for 10s. Reporting 600s reads as a ten-minute freeze and would send
    an operator hunting a stall that never happened (finding 4, #7138).
    """
    job = dict(_RUNNING_LANE_JOB)
    job["JobStatus"] = 7
    job["JobCurrentStartDate"] = _SERVER_TIME - 600
    job["EnteredCurrentStatus"] = _SERVER_TIME - 10

    state = _inspect(tmp_path, slots=json.dumps([_IDLE_SLOT]), jobs=json.dumps([job]))

    assert type(state) is PoolOnline
    assert state.jobs[0].state is PoolJobState.SUSPENDED
    assert state.jobs[0].seconds_in_state == 10.0


def test_a_requeued_job_is_aged_from_the_requeue_not_from_submission(
    tmp_path: Path,
) -> None:
    """Time in state, not time since submission."""
    job = dict(_QUEUED_LANE_JOB)
    job["QDate"] = _SERVER_TIME - 3600
    job["EnteredCurrentStatus"] = _SERVER_TIME - 45

    state = _inspect(tmp_path, slots=json.dumps([_IDLE_SLOT]), jobs=json.dumps([job]))

    assert type(state) is PoolOnline
    assert state.jobs[0].seconds_in_state == 45.0


def test_a_job_with_no_state_timestamp_is_a_defect_not_a_zero(
    tmp_path: Path,
) -> None:
    """Zero reads as "just entered", which is a fabricated fact.

    An absent required timestamp is the scheduler contradicting this
    adapter; it must surface, not be smoothed into a plausible age
    (finding 4, #7138).
    """
    job = {key: value for key, value in _RUNNING_LANE_JOB.items()}
    job.pop("EnteredCurrentStatus", None)

    with pytest.raises(PoolInspectionError, match="EnteredCurrentStatus"):
        _inspect(tmp_path, slots=json.dumps([_IDLE_SLOT]), jobs=json.dumps([job]))


# --- #7138 round 1, finding 3: dead/stale pool called online ---------


def test_a_collector_with_no_execute_slots_is_not_online(tmp_path: Path) -> None:
    """An empty answer is not a pool that can run anything.

    Reporting "online, 0 cpus" invites the reader to conclude their lane
    is merely queued behind others (finding 3, #7138).
    """
    state = _inspect(tmp_path, slots="[]", jobs="")

    assert type(state) is not PoolOnline
    assert type(state) is PoolUnknownHealth
    assert "no execute" in state.detail.lower()


def test_stale_cached_slot_ads_are_not_reported_as_online(tmp_path: Path) -> None:
    """The collector serves a dead startd's ad until it expires.

    Stopping condor_startd leaves capacity visible for up to
    CLASSAD_LIFETIME. Believing it reports a machine that cannot run a
    single lane as an idle pool with cpus to spare (finding 3, #7138).
    """
    stale = dict(_IDLE_SLOT)
    stale["LastHeardFrom"] = _NOW - 3600

    state = _inspect(tmp_path, slots=json.dumps([stale]), jobs="")

    assert type(state) is not PoolOnline
    assert type(state) is PoolUnknownHealth
    assert "stale" in state.detail.lower()


def test_a_fresh_populated_pool_is_online(tmp_path: Path) -> None:
    """The positive case still has to hold after all that."""
    state = _inspect(tmp_path, slots=json.dumps([_IDLE_SLOT]), jobs="")

    assert type(state) is PoolOnline
    assert state.capacity.total_cpus == 18


def test_freshness_that_cannot_be_established_is_never_online(
    tmp_path: Path,
) -> None:
    """Unknown health must read as unknown, never as online."""
    unstamped = {key: value for key, value in _IDLE_SLOT.items()}
    unstamped.pop("LastHeardFrom", None)

    state = _inspect(tmp_path, slots=json.dumps([unstamped]), jobs="")

    assert type(state) is not PoolOnline


# --- the scrub asymmetry, on the status path -------------------------


def test_the_freshness_threshold_is_read_from_the_pool_not_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A question ABOUT the pool must be answered BY the pool.

    ``UPDATE_INTERVAL`` decides how old an advertisement may be before
    the pool is called stale. Read through the unscrubbed path, an
    ambient ``_CONDOR_UPDATE_INTERVAL`` export would widen that window
    from the caller's own environment and certify a dead pool as fresh
    — the bypass #7132 closed for the policy check, which this path
    must not reopen.
    """
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    # Answer with whatever the environment says, so a leaked override
    # is visible in the result rather than silently absorbed.
    (binaries / "condor_config_val").write_text(
        "#!/bin/sh\necho \"${_CONDOR_UPDATE_INTERVAL:-300}\"\n"
    )
    (binaries / "condor_config_val").chmod(0o755)
    for name in ("condor_submit", "condor_rm"):
        tool = binaries / name
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
    stale = dict(_IDLE_SLOT)
    stale["LastHeardFrom"] = _NOW - 3600
    for name, payload in (
        ("condor_status", json.dumps([stale])),
        ("condor_q", ""),
    ):
        tool = binaries / name
        tool.write_text(f"#!/bin/sh\ncat <<'JSON'\n{payload}\nJSON\n")
        tool.chmod(0o755)
    tools = CondorTools(
        submit=binaries / "condor_submit",
        remove=binaries / "condor_rm",
        query=binaries / "condor_q",
        config_query=binaries / "condor_config_val",
        pool_query=binaries / "condor_status",
    )
    # An hour-old advertisement, and an override that would forgive it.
    monkeypatch.setenv("_CONDOR_UPDATE_INTERVAL", "999999")

    state = CondorPoolInspector(tools).inspect()

    assert type(state) is PoolUnknownHealth, (
        "an ambient macro override must not decide the freshness window"
    )
    assert "stale" in state.detail.lower()


def test_the_queue_and_slot_queries_keep_the_caller_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the asymmetry, deliberately.

    ``condor_status``/``condor_q`` ask the pool for its state, and must
    reach the SAME pool a submission would — including when an override
    redirects it. Scrubbing here would describe a pool the lane would
    never run on.
    """
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    seen = tmp_path / "seen"
    for name in ("condor_submit", "condor_rm", "condor_config_val"):
        tool = binaries / name
        tool.write_text("#!/bin/sh\necho 300\n")
        tool.chmod(0o755)
    (binaries / "condor_status").write_text(
        f"#!/bin/sh\nprintf '%s' \"${{_CONDOR_COLLECTOR_HOST:-unset}}\" > {seen}\n"
        "echo '[]'\n"
    )
    (binaries / "condor_status").chmod(0o755)
    (binaries / "condor_q").write_text("#!/bin/sh\n")
    (binaries / "condor_q").chmod(0o755)
    tools = CondorTools(
        submit=binaries / "condor_submit",
        remove=binaries / "condor_rm",
        query=binaries / "condor_q",
        config_query=binaries / "condor_config_val",
        pool_query=binaries / "condor_status",
    )
    monkeypatch.setenv("_CONDOR_COLLECTOR_HOST", "elsewhere.example")

    CondorPoolInspector(tools).inspect()

    assert seen.read_text() == "elsewhere.example", (
        "the state queries must see the environment a submission would"
    )
