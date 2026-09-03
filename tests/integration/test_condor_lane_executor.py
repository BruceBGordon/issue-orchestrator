"""HTCondor backend against the shared lane contract, on a live pool.

Requires a reachable personal pool (``scripts/condor-personal.sh up``).
Marked ``requires_infra``: the backend is opt-in, so these run in the
dedicated condor CI job and on developer machines with a pool — never
silently skipped inside the default gate, simply not selected by it.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor import CondorLaneExecutor, CondorTools
from issue_orchestrator.domain.lane_execution import (
    LaneSuspendability,
    LaneCommand,
    LaneCompleted,
    LaneDeadline,
    LaneResources,
    LaneWorkKey,
)
from issue_orchestrator.ports.lane_executor import LaneExecutor
from tests.load_fixture import cpu_load, reap_marked_processes
from tests.unit.lane_executor_contract import LaneExecutorContract

pytestmark = [
    pytest.mark.timeout(600),
    pytest.mark.requires_infra,
]

# Ceiling on the owner-load spike if every cleanup path fails: long enough to
# cover the 180s suspension wait it has to outlast, short enough that an
# escaped burner cannot become someone else's unexplained gate.
_LOAD_SPIKE_MAX_SECONDS = 240.0

# Ceiling on the setsid escapee, which no group signal can reach. Must outlast
# this test's ~260s observation path; must not outlast the day.
_ESCAPE_LIFETIME_SECONDS = 600.0

# A double-forked, setsid-detached grandchild: what agent jobs spawn, and what
# ADR-0001 says macOS cannot contain. Because nothing can signal it as a group,
# its own deadline (argv[2]) is the last line of defence when the harness that
# was going to sweep it dies first (#7142). Module scope so
# tests/unit/test_fixture_script_deadlines can prove that deadline holds.
_ESCAPE_SCRIPT = (
    "import os, sys, time\n"
    "deadline = time.monotonic() + float(sys.argv[2])\n"
    "if os.fork() == 0:\n"
    "    os.setsid()\n"
    "    if os.fork() == 0:\n"
    "        open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "        while time.monotonic() < deadline:\n"
    "            time.sleep(0.5)\n"
    "        os._exit(0)\n"
    "    os._exit(0)\n"
    "while time.monotonic() < deadline:\n"
    "    time.sleep(0.5)\n"
)


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
    import time

    marker = tmp_path / "grandchild.pid"
    executor = CondorLaneExecutor(CondorTools.resolve())
    import threading

    outcome_box: list[object] = []

    def run_lane() -> None:
        outcome_box.append(
            executor.run(
                LaneCommand(
                    work_key=LaneWorkKey("contract.session-escape"),
                    arguments=(
                        sys.executable,
                        "-c",
                        _ESCAPE_SCRIPT,
                        str(marker),
                        str(_ESCAPE_LIFETIME_SECONDS),
                    ),
                    working_directory=tmp_path,
                    deadline=LaneDeadline(20.0),
                ),
                LaneResources(request_cpus=1),
            )
        )

    thread = threading.Thread(target=run_lane)
    thread.start()
    try:
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
    finally:
        # #7142: this test spawns an hour-long escapee ON PURPOSE and asserts
        # macOS cannot contain it, so the only thing standing between it and
        # the next nine gates is cleanup that runs on every path. `setsid`
        # puts it beyond any group signal; the argv is what still identifies
        # it, and the lane parent that never exits is swept with it.
        reap_marked_processes(str(marker))


def test_queue_wait_is_never_billed_to_the_lane_deadline(tmp_path: Path) -> None:
    """Contract: scheduling wait is machinery, not lane budget. A lane
    queued behind an exclusive token for longer than its own runtime
    deadline must still run and complete once the token frees."""
    import threading
    import time as _time

    def run_lane(name: str, sleep_seconds: float, deadline: float) -> object:
        return CondorLaneExecutor(CondorTools.resolve()).run(
            LaneCommand(
                work_key=LaneWorkKey(f"contract.queuebill-{name}"),
                arguments=(sys.executable, "-c", f"import time; time.sleep({sleep_seconds})"),
                working_directory=tmp_path,
                deadline=LaneDeadline(deadline),
            ),
            LaneResources(request_cpus=1, exclusive=("queuebilltoken",)),
        )

    results: dict[str, object] = {}
    holder = threading.Thread(
        target=lambda: results.__setitem__("holder", run_lane("holder", 12.0, 60.0))
    )
    holder.start()
    _time.sleep(1.0)
    # Queued behind a 12s holder with only an 8s deadline of its own:
    # the ~11s queue wait exceeds the deadline, which is the
    # discriminator. The runtime margin (8s budget for ~1s of work) is
    # deliberately generous: under emulation (this amd64 execution
    # environment on Apple Silicon) interpreter startup alone can eat
    # several seconds, and a native-calibrated margin turns this test
    # into an emulation-speed test - observed live: a 4s deadline
    # removed a healthy 1s lane 5s after its execute event.
    results["queued"] = run_lane("queued", 1.0, 8.0)
    holder.join(timeout=120)

    assert type(results["holder"]) is LaneCompleted
    queued = results["queued"]
    assert type(queued) is LaneCompleted, (
        "queue wait was billed to the lane deadline: "
        f"{queued!r}"
    )
    # The learning loop's precondition: the ~7s token wait must not
    # appear in observed runtime (the lane slept 1s). A queue-inflated
    # number here would make learned ordering chase its own delays.
    # Bound matches the widened emulation margins: the ~11s token wait
    # is the discriminator, and a completed lane already proves the 8s
    # deadline charged runtime only.
    assert queued.observed_runtime_seconds < 8.0, (
        "observed runtime includes queue wait: "
        f"{queued.observed_runtime_seconds:.1f}s for a 1s lane"
    )
    # The excluded wait is not discarded — it is reported separately.
    # This lane provably queued behind the holder's ~11s token, so a
    # real (multi-second) wait must appear in queue_wait_seconds: the
    # dispatch-quality signal the gate log surfaces per lane.
    assert queued.queue_wait_seconds > 2.0, (
        "a lane that queued behind an 11s token reported "
        f"queue_wait={queued.queue_wait_seconds:.1f}s"
    )


def test_higher_priority_lane_dispatches_first_from_a_contended_queue(
    tmp_path: Path,
) -> None:
    """The scheduler must honor the priority hint when choosing among
    idle lanes: with two lanes queued behind a token holder, the
    higher-priority one runs first regardless of submission order.
    This is the dispatch half of the learning loop — the submit half
    (history median becomes the hint) is proven at the CLI boundary."""
    journal = tmp_path / "journal.txt"
    journal.write_text("")
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "with Path(sys.argv[1]).open('a') as handle:\n"
        "    handle.write(f'start:{sys.argv[2]}\\n')\n"
        "time.sleep(float(sys.argv[3]))\n"
    )

    def run_lane(name: str, sleep_seconds: float, priority: int) -> None:
        outcome = CondorLaneExecutor(CondorTools.resolve()).run(
            LaneCommand(
                work_key=LaneWorkKey(f"contract.dispatch-{name}"),
                arguments=(
                    sys.executable,
                    "-c",
                    script,
                    str(journal),
                    name,
                    str(sleep_seconds),
                ),
                working_directory=tmp_path,
                deadline=LaneDeadline(300.0),
            ),
            LaneResources(
                request_cpus=1,
                exclusive=("dispatchordertoken",),
                priority=priority,
            ),
        )
        assert type(outcome) is LaneCompleted and outcome.exit_code == 0

    threads = [threading.Thread(target=run_lane, args=("blocker", 6.0, 0))]
    threads[0].start()
    time.sleep(1.5)
    # Deliberately submit the LOW-priority lane first: only the
    # priority hint, not arrival order, may decide who runs next.
    threads.append(threading.Thread(target=run_lane, args=("low", 2.0, 1)))
    threads[1].start()
    time.sleep(1.0)
    threads.append(threading.Thread(target=run_lane, args=("high", 2.0, 100)))
    threads[2].start()
    for thread in threads:
        thread.join(timeout=180)
        assert not thread.is_alive(), "a dispatch-order lane never concluded"

    started = [
        line.split(":", 1)[1]
        for line in journal.read_text().splitlines()
        if line.startswith("start:")
    ]
    assert started[0] == "blocker", started
    assert started[1:] == ["high", "low"], (
        "the scheduler ignored the priority hint under contention: "
        f"{started}"
    )


def test_run_directory_lifecycle_deletes_on_success_retains_on_failure(
    tmp_path: Path,
) -> None:
    """Clean completions leave nothing behind; failures keep their
    diagnostics and say where (the retention owner is the adapter)."""
    import glob as _glob
    import tempfile as _tempfile

    def lane_directories() -> set[str]:
        return set(
            _glob.glob(str(Path(_tempfile.gettempdir()) / "lane-contract.lifecycle*"))
        )

    executor = CondorLaneExecutor(CondorTools.resolve())
    before = lane_directories()
    ok = executor.run(
        LaneCommand(
            work_key=LaneWorkKey("contract.lifecycle-ok"),
            arguments=(sys.executable, "-c", "pass"),
            working_directory=tmp_path,
            deadline=LaneDeadline(60.0),
        ),
        LaneResources(request_cpus=1),
    )
    assert type(ok) is LaneCompleted and ok.exit_code == 0
    assert lane_directories() == before, "clean completion leaked its run directory"

    failed = executor.run(
        LaneCommand(
            work_key=LaneWorkKey("contract.lifecycle-fail"),
            arguments=(sys.executable, "-c", "raise SystemExit(3)"),
            working_directory=tmp_path,
            deadline=LaneDeadline(60.0),
        ),
        LaneResources(request_cpus=1),
    )
    assert type(failed) is LaneCompleted and failed.exit_code == 3
    retained = [d for d in lane_directories() - before if "lifecycle-fail" in d]
    assert retained, "failed lane did not retain its diagnostics"
    for directory in retained:
        assert (Path(directory) / "lane.events").exists()
        import shutil as _shutil

        _shutil.rmtree(directory, ignore_errors=True)


def test_failed_lane_retains_the_pools_own_per_job_accounting(
    tmp_path: Path,
) -> None:
    """Acceptance for #7127's collection half, on the live pool.

    ``scripts/condor-personal.sh`` configures PER_JOB_HISTORY_DIR, so
    when a lane exits nonzero its retained run directory must also hold
    ``lane.classad`` — the scheduler's COMPLETE final ClassAd for that
    exact job (exit status, memory, CPU, slot, every timestamp) — beside
    the event log, instead of only inside a rotating global history that
    nothing correlates back to the lane.

    Against a pool started without the knob this FAILS rather than
    skips: silently absent accounting is precisely what this exists to
    prevent.
    """
    import glob as _glob
    import shutil as _shutil
    import tempfile as _tempfile

    configured = _run_pool_tool("condor_config_val", "PER_JOB_HISTORY_DIR")
    assert configured.returncode == 0 and configured.stdout.strip(), (
        "this pool sets no PER_JOB_HISTORY_DIR. The helper writes it only "
        "for a directory it proved world-writable first (an unwritable one "
        "EXCEPTs the schedd, PR #7135), so re-run "
        "scripts/condor-personal.sh up and read its stderr for the reason "
        f"it turned accounting off (condor_config_val said: "
        f"{configured.stdout.strip()!r} {configured.stderr.strip()!r})"
    )

    pattern = str(Path(_tempfile.gettempdir()) / "lane-contract.accounting*")
    before = set(_glob.glob(pattern))
    outcome = CondorLaneExecutor(CondorTools.resolve()).run(
        LaneCommand(
            work_key=LaneWorkKey("contract.accounting"),
            arguments=(sys.executable, "-c", "raise SystemExit(3)"),
            working_directory=tmp_path,
            deadline=LaneDeadline(60.0),
        ),
        LaneResources(request_cpus=1),
    )
    assert type(outcome) is LaneCompleted and outcome.exit_code == 3
    retained = set(_glob.glob(pattern)) - before
    assert retained, "the failed lane did not retain its diagnostics"
    try:
        for directory in retained:
            classad = Path(directory) / "lane.classad"
            assert classad.is_file(), (
                "the failed lane retained no per-job accounting; the pool "
                f"writes it to {configured.stdout.strip()}"
            )
            text = classad.read_text(encoding="utf-8")
            assert "ClusterId" in text, text[:400]
            assert "ExitCode = 3" in text, text[:400]
    finally:
        for directory in retained:
            _shutil.rmtree(directory, ignore_errors=True)


def _pool_tool(name: str) -> tuple[Path, dict[str, str]]:
    """Locate a scheduler tool beside the resolved submit binary, with
    the environment its pool configuration requires."""
    import os

    tools = CondorTools.resolve()
    binary = tools.submit.parent / name
    assert binary.is_file(), f"{name} not found beside {tools.submit}"
    environment = dict(os.environ)
    if tools.pool_config is not None:
        environment["CONDOR_CONFIG"] = str(tools.pool_config)
    return binary, environment


def _unique_lane_key(prefix: str) -> str:
    """A per-submission unique work key for tests that address their
    own job through the queue. The stable logical work keys are shared
    by concurrent gates of the same repo (B4, #7118 review): targeting
    one would let this test freeze or remove ANOTHER worktree's run of
    the same lane. Uniqueness makes the batch constraint an execution
    identity."""
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _batch_constraint(work_key: str) -> str:
    return f'JobBatchName == "{work_key}"'


def _run_pool_tool(name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    binary, environment = _pool_tool(name)
    return subprocess.run(
        [str(binary), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _job_status(work_key: str) -> str:
    """The job's JobStatus for this lane's batch ('' when not queued)."""
    result = _run_pool_tool(
        "condor_q", "-constraint", _batch_constraint(work_key), "-af", "JobStatus"
    )
    return result.stdout.strip()

_RUNNING = "2"
_SUSPENDED_STATUS = "7"


def _await_status(work_key: str, wanted: str, deadline_seconds: float) -> None:
    deadline = time.monotonic() + deadline_seconds
    last = ""
    while time.monotonic() < deadline:
        last = _job_status(work_key)
        if last == wanted:
            return
        time.sleep(0.5)
    raise AssertionError(
        f"lane {work_key} never reached JobStatus {wanted}; last={last!r}"
    )


def _release_batch(work_key: str) -> None:
    """Guaranteed cleanup: nothing of this lane stays frozen or queued."""
    _run_pool_tool("condor_continue", "-constraint", _batch_constraint(work_key))
    _run_pool_tool("condor_rm", "-constraint", _batch_constraint(work_key))


def test_suspension_charges_neither_deadline_nor_observed_runtime(
    tmp_path: Path,
) -> None:
    """Freeze THIS lane's job (targeted by batch name — never the whole
    shared queue) for ~6s mid-run. The lane sleeps 4s under an 8s
    deadline: wall time (~10s+) exceeds the deadline, so a deadline
    charging frozen time would remove it. The suspended state is
    asserted as observed scheduler fact, not assumed from the command's
    exit code."""
    work_key = _unique_lane_key("contract.suspension")
    marker = tmp_path / "running"
    script = (
        "import sys, time, pathlib\n"
        "pathlib.Path(sys.argv[1]).write_text('up')\n"
        "time.sleep(4)\n"
    )
    outcomes: list[object] = []

    def run_lane() -> None:
        outcomes.append(
            CondorLaneExecutor(CondorTools.resolve()).run(
                LaneCommand(
                    work_key=LaneWorkKey(work_key),
                    arguments=(sys.executable, "-c", script, str(marker)),
                    working_directory=tmp_path,
                    deadline=LaneDeadline(8.0),
                ),
                LaneResources(request_cpus=1),
            )
        )

    thread = threading.Thread(target=run_lane)
    thread.start()
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.2)
        assert marker.exists(), "suspension lane never started"

        suspended = _run_pool_tool(
            "condor_suspend", "-constraint", _batch_constraint(work_key)
        )
        assert suspended.returncode == 0, suspended.stderr
        _await_status(work_key, _SUSPENDED_STATUS, 30.0)
        time.sleep(6.0)
        resumed = _run_pool_tool(
            "condor_continue", "-constraint", _batch_constraint(work_key)
        )
        assert resumed.returncode == 0, resumed.stderr

        thread.join(timeout=120)
        assert not thread.is_alive(), "suspension lane never concluded"
    finally:
        _release_batch(work_key)
        thread.join(timeout=30)
    outcome = outcomes[0]
    assert type(outcome) is LaneCompleted, (
        "the deadline charged frozen time and removed a healthy lane: "
        f"{outcome!r}"
    )
    assert outcome.exit_code == 0
    assert outcome.observed_runtime_seconds < 8.0, (
        "observed runtime includes frozen time: "
        f"{outcome.observed_runtime_seconds:.1f}s for a 4s lane"
    )


def test_true_overrun_is_still_enforced_across_a_suspension(
    tmp_path: Path,
) -> None:
    """The suspension subtraction must not disable the deadline: a lane
    genuinely exceeding its executing-time budget is still removed —
    across a targeted freeze/thaw of exactly this lane's job."""
    work_key = _unique_lane_key("contract.overrun")
    marker = tmp_path / "running-overrun"
    script = (
        "import sys, time, pathlib\n"
        "pathlib.Path(sys.argv[1]).write_text('up')\n"
        "time.sleep(600)\n"
    )
    outcomes: list[object] = []

    def run_lane() -> None:
        outcomes.append(
            CondorLaneExecutor(CondorTools.resolve()).run(
                LaneCommand(
                    work_key=LaneWorkKey(work_key),
                    arguments=(sys.executable, "-c", script, str(marker)),
                    working_directory=tmp_path,
                    deadline=LaneDeadline(6.0),
                ),
                LaneResources(request_cpus=1),
            )
        )

    thread = threading.Thread(target=run_lane)
    thread.start()
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.2)
        assert marker.exists(), "overrun lane never started"

        suspended = _run_pool_tool(
            "condor_suspend", "-constraint", _batch_constraint(work_key)
        )
        assert suspended.returncode == 0, suspended.stderr
        _await_status(work_key, _SUSPENDED_STATUS, 30.0)
        time.sleep(3.0)
        resumed = _run_pool_tool(
            "condor_continue", "-constraint", _batch_constraint(work_key)
        )
        assert resumed.returncode == 0, resumed.stderr

        thread.join(timeout=180)
        assert not thread.is_alive(), "overrun lane never concluded"
    finally:
        _release_batch(work_key)
        thread.join(timeout=30)
    from issue_orchestrator.domain.lane_execution import LaneTimedOut

    assert type(outcomes[0]) is LaneTimedOut, (
        "the suspension subtraction disabled the deadline: "
        f"{outcomes[0]!r}"
    )


@pytest.mark.requires_backoff_pool
def test_owner_load_spike_freezes_only_suspendable_lanes(tmp_path: Path) -> None:
    """Acceptance for the opt-in policy itself (B4, #7118 review): with
    a pool started under IO_CONDOR_LOAD_BACKOFF=1, a machine-owner load
    spike (busy processes OUTSIDE the scheduler) must freeze a
    suspendable lane, leave a non-suspendable lane running, and thaw
    the frozen lane to completion when the load clears."""
    import os

    # condor_config_val returns the EXPANDED expression — macro names
    # like OwnerLoadAvg do not survive expansion (B6, #7118 review).
    # Assert the effective policy: the machine-wide owner-load pair and
    # the per-lane eligibility guard.
    policy = _run_pool_tool("condor_config_val", "SUSPEND").stdout
    assert "TotalLoadAvg - TotalCondorLoadAvg" in policy, (
        "this test requires a pool started with IO_CONDOR_LOAD_BACKOFF=1 "
        "and the machine-wide owner-load policy; the running pool's "
        f"effective SUSPEND is: {policy!r}"
    )
    assert "SuspendableLane" in policy, (
        "the effective SUSPEND lacks the per-lane eligibility guard: "
        f"{policy!r}"
    )

    freezable_key = _unique_lane_key("backoff.freezable")
    exempt_key = _unique_lane_key("backoff.exempt")
    script = "import time; time.sleep(90)"
    outcomes: dict[str, object] = {}

    def run_lane(
        work_key: str, suspendability: LaneSuspendability
    ) -> None:
        outcomes[work_key] = CondorLaneExecutor(CondorTools.resolve()).run(
            LaneCommand(
                work_key=LaneWorkKey(work_key),
                arguments=(sys.executable, "-c", script),
                working_directory=tmp_path,
                deadline=LaneDeadline(300.0),
            ),
            LaneResources(request_cpus=1, suspendability=suspendability),
        )

    threads = [
        threading.Thread(
            target=run_lane,
            args=(freezable_key, LaneSuspendability.ANYWHERE),
        ),
        threading.Thread(
            target=run_lane, args=(exempt_key, LaneSuspendability.NEVER)
        ),
    ]
    try:
        for thread in threads:
            thread.start()
        _await_status(freezable_key, _RUNNING, 90.0)
        _await_status(exempt_key, _RUNNING, 90.0)

        # The spike is scoped to the block that needs it: leaving the block is
        # how the load clears. Reaping is the helper's guarantee (#7142) — the
        # previous `while True: pass` burners had no deadline of their own, so
        # an interrupt here left them spinning until someone found them.
        with cpu_load(
            workers=(os.cpu_count() or 4) + 2,
            max_lifetime_seconds=_LOAD_SPIKE_MAX_SECONDS,
        ):
            _await_status(freezable_key, _SUSPENDED_STATUS, 180.0)
            assert _job_status(exempt_key) == _RUNNING, (
                "the owner-load policy froze a lane that declared itself "
                "not suspendable"
            )
        _await_status(freezable_key, _RUNNING, 300.0)

        for thread in threads:
            thread.join(timeout=300)
            assert not thread.is_alive(), "a backoff lane never concluded"
    finally:
        _release_batch(freezable_key)
        _release_batch(exempt_key)
        for thread in threads:
            thread.join(timeout=30)

    frozen_outcome = outcomes[freezable_key]
    exempt_outcome = outcomes[exempt_key]
    assert type(frozen_outcome) is LaneCompleted and frozen_outcome.exit_code == 0, (
        f"the frozen lane did not thaw to completion: {frozen_outcome!r}"
    )
    assert type(exempt_outcome) is LaneCompleted and exempt_outcome.exit_code == 0
    # Frozen time must not appear in the learning signal either.
    assert frozen_outcome.observed_runtime_seconds < 150.0


@pytest.mark.requires_backoff_pool
def test_cooperative_lanes_are_not_freeze_eligible_even_when_marked_safe(
    tmp_path: Path,
) -> None:
    """The SHIPPED cooperative contract (B2/#7134, held closed by
    #7139): cooperative lanes are NOT freeze-eligible, full stop —
    the intended runtime-chirp gate was disproven live (set_job_attr
    reaches the schedd's ad but never the startd copy that evaluates
    SUSPEND). This pin uses a job whose ad carries SafeToSuspend=True
    FROM SUBMISSION — strictly more visible to the startd than any
    runtime chirp could be — and it must still run unfrozen while an
    anywhere control under the identical load window suspends. When
    #7139 opens eligibility, this test flips to the open contract in
    the same change."""
    import os

    policy = _run_pool_tool("condor_config_val", "SUSPEND").stdout
    assert "TotalLoadAvg - TotalCondorLoadAvg" in policy, (
        "this test requires a pool started with IO_CONDOR_LOAD_BACKOFF=1; "
        f"effective SUSPEND: {policy!r}"
    )
    assert "SafeToSuspend" not in policy, (
        "the pool's policy references the disproven chirp gate - the "
        "closed contract (#7139) no longer holds and this pin must be "
        f"rewritten to the proven open contract: {policy!r}"
    )

    coop_key = _unique_lane_key("backoff.coop-closed")
    control_key = _unique_lane_key("backoff.anywhere-ctl")

    def _submit_sleeper(work_key: str, classification: str, extra: str) -> None:
        submit_path = tmp_path / f"{work_key}.sub"
        submit_path.write_text(
            "universe = vanilla\n"
            "executable = /bin/sleep\n"
            "arguments = 240\n"
            f"initialdir = {tmp_path}\n"
            f"batch_name = {work_key}\n"
            "request_cpus = 1\n"
            "getenv = true\n"
            f'+SuspendableLane = "{classification}"\n'
            f"{extra}"
            "queue\n"
        )
        submitted = _run_pool_tool("condor_submit", str(submit_path))
        assert submitted.returncode == 0, submitted.stderr

    burners: list[subprocess.Popen[bytes]] = []
    try:
        # Submit-time True: the most freeze-eligible a cooperative job
        # can ever look; the closed policy must still ignore it.
        _submit_sleeper(coop_key, "cooperative", "+SafeToSuspend = True\n")
        _submit_sleeper(control_key, "anywhere", "")
        _await_status(coop_key, _RUNNING, 90.0)
        _await_status(control_key, _RUNNING, 90.0)

        burner_count = (os.cpu_count() or 4) + 2
        burners = [
            subprocess.Popen([sys.executable, "-c", "while True: pass"])
            for _ in range(burner_count)
        ]
        # The control suspending proves load and policy are live...
        _await_status(control_key, _SUSPENDED_STATUS, 180.0)
        # ...and through a full minute of that proven window (multiple
        # PERIODIC_EXPR_INTERVAL=5 evaluation cycles), the cooperative
        # job must never leave RUNNING.
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            status = _job_status(coop_key)
            assert status == _RUNNING, (
                "a cooperative lane was frozen under the closed contract "
                f"(JobStatus={status!r}) - eligibility must stay closed "
                "until #7139 proves a startd-visible channel"
            )
            time.sleep(5.0)
    finally:
        for burner in burners:
            burner.kill()
        _release_batch(coop_key)
        _release_batch(control_key)
