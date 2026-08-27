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


def test_detached_session_escape_states_the_platform_boundary(
    tmp_path: Path,
) -> None:
    """Executable statement of ADR-0001 (docs/architecture/execenv/).

    A double-forked, setsid-detached grandchild — what agent jobs spawn
    (dev servers, watchers) — escapes the scheduler's process tracking on
    macOS, where cgroups do not exist: reproduced live 2026-08-27. On
    Linux, cgroup tracking must kill it with the job. The macOS pool is
    therefore scoped to validation lanes (non-detaching workloads); agent
    jobs require the Linux execution environment.

    The assertion is per-platform so the boundary stays DOCUMENTED TRUTH:
    if macOS ever starts containing the escape, or Linux ever stops, the
    record here is what fails.
    """
    import os
    import signal
    import time

    marker = tmp_path / "grandchild.pid"
    escape = (
        "import os, sys, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    if os.fork() == 0:\n"
        "        open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "        time.sleep(3600)\n"
        "    os._exit(0)\n"
        "time.sleep(3600)\n"
    )
    executor = CondorLaneExecutor(CondorTools.resolve())
    import threading

    outcome_box: list[object] = []

    def run_lane() -> None:
        outcome_box.append(
            executor.run(
                LaneCommand(
                    work_key=LaneWorkKey("contract.session-escape"),
                    arguments=(sys.executable, "-c", escape, str(marker)),
                    working_directory=tmp_path,
                    deadline=LaneDeadline(20.0),
                ),
                LaneResources(request_cpus=1),
            )
        )

    thread = threading.Thread(target=run_lane)
    thread.start()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.2)
    assert marker.exists(), "escape grandchild never started"
    grandchild = int(marker.read_text())
    thread.join(timeout=180)
    assert not thread.is_alive(), "lane did not conclude"

    def alive() -> bool:
        try:
            os.kill(grandchild, 0)
            return True
        except ProcessLookupError:
            return False

    settle = time.monotonic() + 20
    while time.monotonic() < settle and alive():
        time.sleep(0.5)
    survived = alive()
    if survived:
        os.kill(grandchild, signal.SIGKILL)
    if sys.platform == "darwin":
        assert survived, (
            "macOS unexpectedly contained the setsid escape - if process "
            "tracking gained this, update ADR-0001 and the pool scoping"
        )
    else:
        assert not survived, (
            "Linux cgroup tracking failed to contain the setsid escape - "
            "the execution environment's core guarantee has regressed"
        )
