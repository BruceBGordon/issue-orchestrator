"""Inbound translation: scheduler answers become pool contracts.

Hermetic: the scheduler tools are shell stubs that print canned output,
so no pool is required. What is under test is the anti-corruption layer,
not HTCondor.
"""

from __future__ import annotations

import json
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
)

_SERVER_TIME = 1_787_959_500

_IDLE_SLOT = {
    "Machine": "host.local",
    "TotalSlotCpus": 18,
    "PartitionableSlot": True,
}
_DYNAMIC_SLOT = {
    "Machine": "host.local",
    "TotalSlotCpus": 3,
    "DynamicSlot": True,
}
_RUNNING_LANE_JOB = {
    "JobStatus": 2,
    "JobBatchName": "test-unit",
    "LaneSubmitter": "issue-orchestrator-wt-alpha",
    "Owner": "operator",
    "JobPrio": 59,
    "QDate": _SERVER_TIME - 90,
    "JobCurrentStartDate": _SERVER_TIME - 60,
    "RequestCpus": 3,
    "ServerTime": _SERVER_TIME,
}
_QUEUED_LANE_JOB = {
    "JobStatus": 1,
    "JobBatchName": "test-integration-core-local",
    "LaneSubmitter": "issue-orchestrator-wt-beta",
    "Owner": "operator",
    "JobPrio": 82,
    "QDate": _SERVER_TIME - 30,
    "ConcurrencyLimits": "codexlogin, claudelogin",
    "RequestCpus": 2,
    "ServerTime": _SERVER_TIME,
}
_FOREIGN_JOB = {
    "JobStatus": 2,
    "Owner": "someone-else",
    "JobPrio": 0,
    "QDate": _SERVER_TIME - 500,
    "JobCurrentStartDate": _SERVER_TIME - 400,
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
) -> CondorTools:
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    bodies = {
        "condor_submit": "#!/bin/sh\nexit 0\n",
        "condor_rm": "#!/bin/sh\nexit 0\n",
        "condor_q": f"#!/bin/sh\ncat <<'JSON'\n{jobs}\nJSON\nexit {jobs_exit}\n",
        "condor_config_val": "#!/bin/sh\nexit 0\n",
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
    # Running time is measured from the start, not from submission.
    assert running.seconds_in_state == 60.0
    assert running.request_cpus == 3
    assert running.priority == 59
    assert running.exclusive == ()

    assert queued.state is PoolJobState.QUEUED
    # A queued job's clock starts when it was submitted.
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
        slots=json.dumps([{"Machine": "host.local", "TotalSlotCpus": 18.0}]),
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
