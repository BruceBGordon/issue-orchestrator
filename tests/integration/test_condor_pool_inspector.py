"""Pool inspection against a live pool.

Requires a reachable personal pool (``scripts/condor-personal.sh up``).
Marked ``requires_infra``, like the executor's live suite: the backend is
opt-in, so these run in the dedicated condor CI job and on developer
machines with a pool — never silently skipped inside the default gate,
simply not selected by it.

The hermetic translation rules are covered in
``tests/unit/adapters/condor/test_pool_inspector.py``. What only a live
pool can prove is that the attributes this adapter asks for are the ones
a real scheduler actually reports.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor import (
    CondorLaneExecutor,
    CondorPoolInspector,
    CondorTools,
)
from issue_orchestrator.domain.lane_execution import (
    LaneCommand,
    LaneCompleted,
    LaneDeadline,
    LaneResources,
    LaneWorkKey,
)
from issue_orchestrator.ports.executor_pool import (
    LaneJobOrigin,
    PoolJobState,
    PoolOnline,
)

pytestmark = [
    pytest.mark.timeout(300),
    pytest.mark.requires_infra,
]

_WORK_KEY = LaneWorkKey("inspector.livejob")
_LANE_SECONDS = 20
_OBSERVE_TIMEOUT_SECONDS = 120.0
_OBSERVE_INTERVAL_SECONDS = 0.5


def test_a_live_pool_reports_real_capacity() -> None:
    state = CondorPoolInspector(CondorTools.resolve()).inspect()

    assert type(state) is PoolOnline, f"expected a reachable pool, got {state}"
    assert state.capacity.machines >= 1
    assert state.capacity.total_cpus >= 1


def test_a_submitted_lane_is_visible_with_its_work_key_and_submitter(
    tmp_path: Path,
) -> None:
    """The attribution the submit compiler writes must be readable back.

    Only a live pool proves this: the compiler's ``+LaneSubmitter`` tag
    and batch name have to survive the scheduler and come back through
    the query with the names this adapter asks for.
    """
    inspector = CondorPoolInspector(CondorTools.resolve())
    outcome: list[object] = []

    def run_lane() -> None:
        outcome.append(
            CondorLaneExecutor(CondorTools.resolve()).run(
                LaneCommand(
                    work_key=_WORK_KEY,
                    arguments=(sys.executable, "-c", f"import time; time.sleep({_LANE_SECONDS})"),
                    working_directory=tmp_path,
                    deadline=LaneDeadline(180.0),
                ),
                LaneResources(request_cpus=1, exclusive=("inspectorlivemutex",)),
            )
        )

    lane = threading.Thread(target=run_lane)
    lane.start()
    try:
        observed = _await_job(inspector)
    finally:
        lane.join(timeout=_OBSERVE_TIMEOUT_SECONDS)

    assert type(observed.origin) is LaneJobOrigin
    assert observed.origin.work_key == _WORK_KEY
    # The submitter tag is the lane's working-directory name.
    assert observed.origin.submitter_worktree == tmp_path.name
    assert observed.state in (PoolJobState.QUEUED, PoolJobState.RUNNING)
    assert observed.seconds_in_state >= 0.0
    assert observed.request_cpus == 1
    assert observed.exclusive == ("inspectorlivemutex",)
    assert outcome and type(outcome[0]) is LaneCompleted


def _await_job(inspector: CondorPoolInspector):
    """Poll until the lane's job appears, bounded.

    A live scheduler is a real external system, so a bounded wait is the
    only readiness signal available; it fails loudly rather than
    skipping when the pool never admits the job.
    """
    deadline = time.monotonic() + _OBSERVE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        state = inspector.inspect()
        assert type(state) is PoolOnline, f"the pool went away mid-test: {state}"
        for job in state.jobs:
            origin = job.origin
            if type(origin) is LaneJobOrigin and origin.work_key == _WORK_KEY:
                return job
        time.sleep(_OBSERVE_INTERVAL_SECONDS)
    raise AssertionError(
        f"the pool never reported the submitted lane {_WORK_KEY.value} "
        f"within {_OBSERVE_TIMEOUT_SECONDS:.0f}s"
    )
