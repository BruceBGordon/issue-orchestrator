"""HTCondor backend against the shared lane contract, on a live pool.

Requires a reachable personal pool (``scripts/condor-personal.sh up``).
Marked ``requires_infra``: the backend is opt-in, so these run in the
dedicated condor CI job and on developer machines with a pool — never
silently skipped inside the default gate, simply not selected by it.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor import CondorLaneExecutor, CondorTools
from issue_orchestrator.domain.lane_execution import (
    LaneCommand,
    LaneCompleted,
    LaneDeadline,
    LaneResources,
    LaneWorkKey,
)
from issue_orchestrator.ports.lane_executor import LaneExecutor
from tests.unit.lane_executor_contract import LaneExecutorContract

pytestmark = [
    pytest.mark.timeout(600),
    pytest.mark.requires_infra,
]


class TestCondorLaneExecutorContract(LaneExecutorContract):
    def build_executor(self) -> LaneExecutor:
        return CondorLaneExecutor(CondorTools.resolve())


def test_exclusive_token_serializes_concurrent_lanes(tmp_path: Path) -> None:
    """Two lanes sharing an exclusive token must never overlap.

    Each lane appends a start marker, sleeps, then appends an end
    marker; overlap would interleave the markers. Relies on the pool
    setting CONCURRENCY_LIMIT_DEFAULT = 1 (the personal-pool helper
    does), which is exactly what the compiler documents.
    """
    journal = tmp_path / "journal.txt"
    journal.write_text("")
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "journal = Path(sys.argv[1])\n"
        "name = sys.argv[2]\n"
        "with journal.open('a') as handle:\n"
        "    handle.write(f'start:{name}\\n')\n"
        "time.sleep(3)\n"
        "with journal.open('a') as handle:\n"
        "    handle.write(f'end:{name}\\n')\n"
    )

    def run_lane(name: str) -> None:
        outcome = CondorLaneExecutor(CondorTools.resolve()).run(
            LaneCommand(
                work_key=LaneWorkKey(f"contract.exclusive-{name}"),
                arguments=(sys.executable, "-c", script, str(journal), name),
                working_directory=tmp_path,
                deadline=LaneDeadline(300.0),
            ),
            LaneResources(request_cpus=1, exclusive=("lanetestmutex",)),
        )
        assert type(outcome) is LaneCompleted and outcome.exit_code == 0

    threads = [
        threading.Thread(target=run_lane, args=(name,)) for name in ("a", "b")
    ]
    for thread in threads:
        thread.start()
        time.sleep(0.2)
    for thread in threads:
        thread.join()

    lines = journal.read_text().splitlines()
    assert len(lines) == 4, lines
    # Serialized execution is exactly start/end pairs, never interleaved.
    assert lines[0].startswith("start:") and lines[1].startswith("end:"), lines
    assert lines[2].startswith("start:") and lines[3].startswith("end:"), lines
    assert lines[0].split(":")[1] == lines[1].split(":")[1], lines
    assert lines[2].split(":")[1] == lines[3].split(":")[1], lines
