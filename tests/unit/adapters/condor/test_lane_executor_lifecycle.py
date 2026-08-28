"""Run-directory ownership, and the per-job accounting it collects.

Hermetic: scheduler tools are shell stubs, no pool required.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor import lane_executor as lane_executor_module
from issue_orchestrator.adapters.condor import tools as tools_module
from issue_orchestrator.adapters.condor.lane_executor import CondorLaneExecutor
from issue_orchestrator.adapters.condor.tools import CondorTools
from issue_orchestrator.domain.lane_execution import (
    LaneCommand,
    LaneCompleted,
    LaneDeadline,
    LaneExecutorError,
    LaneResources,
    LaneWorkKey,
)

_JOB_ID = "7.0"
# What a lane's user log holds after a job ran and exited nonzero.
_TERMINATED_NONZERO_EVENT_LOG = (
    "000 (007.000.000) 2026-08-29 12:00:00 Job submitted from host: <127.0.0.1>\n"
    "...\n"
    "001 (007.000.000) 2026-08-29 12:00:01 Job executing on host: <127.0.0.1>\n"
    "...\n"
    "005 (007.000.000) 2026-08-29 12:00:05 Job terminated.\n"
    "\t(1) Normal termination (return value 3)\n"
    "...\n"
)


def _write_stubs(binaries: Path, stubs: dict[str, str]) -> None:
    binaries.mkdir(parents=True, exist_ok=True)
    for name, body in stubs.items():
        tool = binaries / name
        tool.write_text(body)
        tool.chmod(0o755)


def _stub_tools(tmp_path: Path, submit_exit: int) -> CondorTools:
    binaries = tmp_path / "bin"
    _write_stubs(
        binaries,
        {
            "condor_submit": (
                f"#!/bin/sh\necho 'submit refused' >&2\nexit {submit_exit}\n"
            ),
            "condor_rm": "#!/bin/sh\nexit 0\n",
            "condor_q": "#!/bin/sh\nexit 0\n",
            "condor_config_val": "#!/bin/sh\nexit 0\n",
            "condor_status": "#!/bin/sh\nexit 0\n",
        },
    )
    return CondorTools(
        submit=binaries / "condor_submit",
        remove=binaries / "condor_rm",
        query=binaries / "condor_q",
        config_query=binaries / "condor_config_val",
        pool_query=binaries / "condor_status",
    )


def _completing_tools(tmp_path: Path, *, history_directory: str) -> CondorTools:
    """Stubs for a job that runs and exits 3.

    ``condor_submit`` plays the scheduler: it reads the ``log =`` line
    out of the submit description and writes the terminal event log the
    executor polls, so the whole lifecycle runs with no pool.
    ``condor_config_val`` answers with the per-job history location the
    pool helper would have configured.
    """
    binaries = tmp_path / "bin"
    submit = (
        "#!/bin/sh\n"
        'log=$(awk -F" = " \'/^log/{print $2}\' "$2")\n'
        f"cat > \"$log\" <<'EVENTS'\n{_TERMINATED_NONZERO_EVENT_LOG}EVENTS\n"
        f"echo '{_JOB_ID}'\n"
    )
    _write_stubs(
        binaries,
        {
            "condor_submit": submit,
            "condor_rm": "#!/bin/sh\nexit 0\n",
            "condor_q": "#!/bin/sh\nexit 0\n",
            "condor_config_val": f"#!/bin/sh\necho '{history_directory}'\n",
            "condor_status": "#!/bin/sh\nexit 0\n",
        },
    )
    return CondorTools(
        submit=binaries / "condor_submit",
        remove=binaries / "condor_rm",
        query=binaries / "condor_q",
        config_query=binaries / "condor_config_val",
        pool_query=binaries / "condor_status",
    )


def _run_lane(
    tools: CondorTools, tmp_path: Path, work_key: str
) -> tuple[LaneCompleted, set[Path]]:
    """Run one stubbed lane, returning its outcome and any run
    directory it retained."""
    before = set(Path(tempfile.gettempdir()).glob(f"lane-{work_key}*"))
    outcome = CondorLaneExecutor(tools).run(
        LaneCommand(
            work_key=LaneWorkKey(work_key),
            arguments=(sys.executable, "-c", "pass"),
            working_directory=tmp_path,
            deadline=LaneDeadline(30.0),
        ),
        LaneResources(request_cpus=1),
    )
    assert type(outcome) is LaneCompleted
    retained = set(Path(tempfile.gettempdir()).glob(f"lane-{work_key}*")) - before
    return outcome, retained


def test_submission_failure_retains_diagnostics_and_names_the_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    executor = CondorLaneExecutor(_stub_tools(tmp_path, submit_exit=1))
    before = set(Path(tempfile.gettempdir()).glob("lane-lifecycle.submitfail*"))

    with pytest.raises(LaneExecutorError) as caught:
        executor.run(
            LaneCommand(
                work_key=LaneWorkKey("lifecycle.submitfail"),
                arguments=(sys.executable, "-c", "pass"),
                working_directory=tmp_path,
                deadline=LaneDeadline(30.0),
            ),
            LaneResources(request_cpus=1),
        )

    assert "submit refused" in str(caught.value)
    assert "diagnostics retained at" in str(caught.value)
    retained = set(Path(tempfile.gettempdir()).glob("lane-lifecycle.submitfail*")) - before
    assert retained, "submission failure must retain the run directory"
    for directory in retained:
        assert (directory / "lane.sub").exists(), "the submit file is the diagnostic"
        import shutil

        shutil.rmtree(directory, ignore_errors=True)
    stderr = capsys.readouterr().err
    assert "diagnostics retained at" in stderr


def test_failed_lane_collects_its_per_job_accounting(tmp_path: Path) -> None:
    """Acceptance (#7127): a lane that exits nonzero retains its run
    directory, and the scheduler's COMPLETE final ClassAd for that job
    lands in it — the accounting travels with the diagnostics instead
    of staying in a rotating global history nothing correlates back."""
    history = tmp_path / "per-job-history"
    history.mkdir()
    classad = "ExitCode = 3\nMemoryUsage = 128\nRemoteWallClockTime = 4.0\n"
    (history / f"history.{_JOB_ID}").write_text(classad)

    outcome, retained = _run_lane(
        _completing_tools(tmp_path, history_directory=str(history)),
        tmp_path,
        "lifecycle.accounting",
    )
    assert outcome.exit_code == 3
    assert retained, "a nonzero lane must retain its diagnostics"
    for directory in retained:
        assert (directory / "lane.classad").read_text() == classad
        shutil.rmtree(directory, ignore_errors=True)


def test_a_pool_without_per_job_accounting_still_reports_the_lane(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Collection is best-effort by construction: it runs while a lane
    is already failing, so an unconfigured pool must cost the ClassAd
    and a stderr line — never the lane's own result."""
    outcome, retained = _run_lane(
        _completing_tools(tmp_path, history_directory="undefined"),
        tmp_path,
        "lifecycle.noaccounting",
    )
    assert outcome.exit_code == 3
    stderr = capsys.readouterr().err
    assert "PER_JOB_HISTORY_DIR" in stderr
    assert "diagnostics retained at" in stderr
    for directory in retained:
        assert not (directory / "lane.classad").exists()
        shutil.rmtree(directory, ignore_errors=True)


def test_an_unexpected_job_identifier_is_never_turned_into_a_read_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ClassAd file is named history.<cluster>.<proc>, so the job
    identifier is also a path component. Anything not that shape is
    refused rather than joined onto the history directory."""
    history = tmp_path / "per-job-history"
    history.mkdir()
    tools = _completing_tools(tmp_path, history_directory=str(history))
    tools.submit.write_text(
        "#!/bin/sh\n"
        'log=$(awk -F" = " \'/^log/{print $2}\' "$2")\n'
        f"cat > \"$log\" <<'EVENTS'\n{_TERMINATED_NONZERO_EVENT_LOG}EVENTS\n"
        "echo '../../../etc/passwd'\n"
    )
    tools.submit.chmod(0o755)

    outcome, retained = _run_lane(tools, tmp_path, "lifecycle.badjobid")
    assert outcome.exit_code == 3
    assert "unexpected job identifier" in capsys.readouterr().err
    for directory in retained:
        assert not (directory / "lane.classad").exists()
        shutil.rmtree(directory, ignore_errors=True)


class _WaitsThatRaise:
    """Stands in for the executor module's ``time``.

    An interrupt lands in whatever the executor is waiting on: the poll
    loop for a lane that has not concluded, then the collection's own
    file-wait during cleanup. Raising from the Nth wait delivers one
    there deterministically — an interval timer races the submission it
    has to land after, and widening the timer until the race is rare is
    the flaky-test band-aid, not a deterministic test. Waits past the
    configured ones behave normally, so whatever follows still has a
    working clock.
    """

    def __init__(self, *from_each_wait: BaseException) -> None:
        self._queued = list(from_each_wait)

    def sleep(self, seconds: float) -> None:
        if self._queued:
            raise self._queued.pop(0)
        time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()


_PENDING_EVENT_LOG = (
    "000 (007.000.000) 2026-08-29 12:00:00 Job submitted from host: <127.0.0.1>\n"
    "...\n"
    "001 (007.000.000) 2026-08-29 12:00:01 Job executing on host: <127.0.0.1>\n"
    "...\n"
)
_CANCELLED_CLASSAD = 'ExitCode = undefined\nRemoveReason = "via condor_rm"\n'
# An order of magnitude beyond the cancellation budget. The separation
# is deliberately large in BOTH directions: a correct implementation
# never waits for this at all (the lookup is killed when the budget
# ends), so the passing test stays ~2s, while a bound that does not
# span the lookup is off by twenty seconds rather than by a margin that
# has to compete with subprocess-spawn jitter on a loaded parallel
# suite. Widening the tolerance instead would have been the band-aid.
_SLOW_LOOKUP_SECONDS = 20.0


def _unhurried_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the cancellation clock out of a test that is not about it.

    The real budget is two seconds, which on a loaded parallel suite is
    a genuine race against the two subprocess spawns the wind-down makes
    before it reaches the accounting stage — so a test asserting what
    happens AT that stage would be measuring machine load. The three
    duration tests own the budget; these own the behaviour.
    """
    monkeypatch.setattr(
        lane_executor_module, "_CANCELLED_ACCOUNTING_WAIT_SECONDS", 30.0
    )


def _cancellable_tools(
    tmp_path: Path,
    *,
    history: Path,
    removal_writes_classad: bool,
    lookup_body: str | None = None,
) -> CondorTools:
    """A pool whose job never concludes on its own.

    ``removal_writes_classad`` makes condor_rm play the schedd, dropping
    the ClassAd as the job leaves the queue; without it the collection
    reaches its own file-wait, which is where a SECOND interrupt lands.
    """
    binaries = tmp_path / "bin"
    removal = "#!/bin/sh\nexit 0\n"
    if removal_writes_classad:
        removal = (
            "#!/bin/sh\n"
            f"cat > '{history}/history.{_JOB_ID}' <<'AD'\n"
            f"{_CANCELLED_CLASSAD}AD\n"
        )
    _write_stubs(
        binaries,
        {
            "condor_submit": (
                "#!/bin/sh\n"
                'log=$(awk -F" = " \'/^log/{print $2}\' "$2")\n'
                f"cat > \"$log\" <<'EVENTS'\n{_PENDING_EVENT_LOG}EVENTS\n"
                f"echo '{_JOB_ID}'\n"
            ),
            "condor_rm": removal,
            "condor_q": "#!/bin/sh\nexit 0\n",
            "condor_config_val": lookup_body or f"#!/bin/sh\necho '{history}'\n",
            "condor_status": "#!/bin/sh\nexit 0\n",
        },
    )
    return CondorTools(
        submit=binaries / "condor_submit",
        remove=binaries / "condor_rm",
        query=binaries / "condor_q",
        config_query=binaries / "condor_config_val",
        pool_query=binaries / "condor_status",
    )


def test_a_cancelled_lane_also_collects_its_per_job_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 1 finding C: the cancellation path retained the run
    directory but skipped collection, so after Ctrl-C the ClassAd
    existed and lane.classad did not — contradicting the stated coupling
    of retention and accounting.

    The job never reaches a terminal event, the interrupt arrives in the
    poll loop, and the condor_rm stub plays the schedd by writing the
    ClassAd as the job leaves the queue — which is what makes collection
    on this path worth doing at all.
    """
    history = tmp_path / "per-job-history"
    history.mkdir()
    _unhurried_cancellation(monkeypatch)
    tools = _cancellable_tools(
        tmp_path, history=history, removal_writes_classad=True
    )
    work_key = "lifecycle.cancelled"
    before = set(Path(tempfile.gettempdir()).glob(f"lane-{work_key}*"))
    executor = CondorLaneExecutor(tools)
    monkeypatch.setattr(
        lane_executor_module, "time", _WaitsThatRaise(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        executor.run(
            LaneCommand(
                work_key=LaneWorkKey(work_key),
                arguments=(sys.executable, "-c", "pass"),
                working_directory=tmp_path,
                deadline=LaneDeadline(300.0),
            ),
            LaneResources(request_cpus=1),
        )

    retained = set(Path(tempfile.gettempdir()).glob(f"lane-{work_key}*")) - before
    assert retained, "a cancelled lane must retain its diagnostics"
    for directory in retained:
        assert (directory / "lane.classad").read_text() == _CANCELLED_CLASSAD
        shutil.rmtree(directory, ignore_errors=True)


def test_the_cancellation_budget_bounds_the_whole_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 2 finding 1: the budget started only AFTER the scheduler
    configuration lookup, so that lookup could spend the general tool
    timeout before a 2s "wait" even began — a 2s bound measured 2.56s
    against a 2.3s lookup, and would measure 6s+ against this one.

    The lookup uses `exec` so the killed process IS the sleeper: a
    shell that merely spawns one leaves a grandchild holding the pipe
    open, which would make even a correct implementation look slow and
    turn this into a test of subprocess plumbing.
    """
    history = tmp_path / "per-job-history"
    history.mkdir()
    (history / f"history.{_JOB_ID}").write_text(_CANCELLED_CLASSAD)
    tools = _cancellable_tools(
        tmp_path,
        history=history,
        removal_writes_classad=False,
        lookup_body=f"#!/bin/sh\nexec sleep {_SLOW_LOOKUP_SECONDS:.0f}\n",
    )
    work_key = "lifecycle.slowlookup"
    before = set(Path(tempfile.gettempdir()).glob(f"lane-{work_key}*"))
    executor = CondorLaneExecutor(tools)
    monkeypatch.setattr(
        lane_executor_module, "time", _WaitsThatRaise(KeyboardInterrupt())
    )

    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        executor.run(
            LaneCommand(
                work_key=LaneWorkKey(work_key),
                arguments=(sys.executable, "-c", "pass"),
                working_directory=tmp_path,
                deadline=LaneDeadline(300.0),
            ),
            LaneResources(request_cpus=1),
        )
    elapsed = time.monotonic() - started

    # Generous against the budget (four times it, for the process spawns
    # and scheduler jitter of a loaded parallel suite), still nowhere
    # near the twenty seconds an unbounded lookup costs.
    budget = lane_executor_module._CANCELLED_ACCOUNTING_WAIT_SECONDS
    assert elapsed < budget * 4, (
        "the cancellation budget did not span the configuration lookup: "
        f"{elapsed:.2f}s for a {budget:.0f}s budget"
    )
    retained = set(Path(tempfile.gettempdir()).glob(f"lane-{work_key}*")) - before
    assert retained, "a cancelled lane must retain its diagnostics"
    for directory in retained:
        # Spending the budget on a stuck lookup costs the ClassAd. That
        # is the trade the budget exists to make: an interrupted lane
        # exits promptly, and says what it gave up.
        assert not (directory / "lane.classad").exists()
        shutil.rmtree(directory, ignore_errors=True)


class _ToolThatRaises:
    """Stands in for the tool boundary's ``subprocess``.

    An interrupt can arrive while the executor is blocked on a scheduler
    TOOL, not only while it is sleeping — and the removal is the first
    and likeliest such window on the cancellation path. Keying on the
    tool's name rather than a call count says which window the test is
    about, and survives any reordering of the calls around it.
    """

    def __init__(self, tool_name: str, error: BaseException) -> None:
        self._tool_name = tool_name
        self._pending: BaseException | None = error

    def run(
        self, arguments: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        pending = self._pending
        if pending is not None and Path(arguments[0]).name == self._tool_name:
            self._pending = None
            raise pending
        return subprocess.run(arguments, **kwargs)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        return getattr(subprocess, name)


def _interrupted_lane(
    tools: CondorTools, tmp_path: Path, work_key: str
) -> None:
    """Drive one lane whose poll loop is interrupted; never returns."""
    CondorLaneExecutor(tools).run(
        LaneCommand(
            work_key=LaneWorkKey(work_key),
            arguments=(sys.executable, "-c", "pass"),
            working_directory=tmp_path,
            deadline=LaneDeadline(300.0),
        ),
        LaneResources(request_cpus=1),
    )


class _ClockThatFaultsAfterTheInterrupt:
    """Stands in for the executor module's ``time``.

    Raises ``interrupt`` from the poll loop's sleep to start the
    cancellation, then raises ``at_first_clock_read`` from the very next
    ``monotonic()`` — which is the wind-down's first instruction,
    building its budget. Nothing between the two reads the clock (the
    interrupt unwinds straight from the sleep into the handler), so this
    targets that one window and no other.
    """

    def __init__(
        self, interrupt: BaseException, at_first_clock_read: BaseException
    ) -> None:
        self._interrupt: BaseException | None = interrupt
        self._pending: BaseException | None = at_first_clock_read

    def sleep(self, seconds: float) -> None:
        pending = self._interrupt
        if pending is not None:
            self._interrupt = None
            raise pending
        time.sleep(seconds)

    def monotonic(self) -> float:
        if self._interrupt is None and self._pending is not None:
            pending = self._pending
            self._pending = None
            raise pending
        return time.monotonic()


def test_a_second_interrupt_building_the_budget_still_chains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 4: the budget was built on the line BEFORE the try, so the
    wind-down's own first instruction sat outside the policy it exists
    to apply. An interrupt there escaped unchained — __cause__ None —
    the same contract break round 3 fixed one statement later."""
    history = tmp_path / "per-job-history"
    history.mkdir()
    tools = _cancellable_tools(
        tmp_path, history=history, removal_writes_classad=False
    )
    executor = CondorLaneExecutor(tools)
    first = KeyboardInterrupt("first")
    second = KeyboardInterrupt("second")
    monkeypatch.setattr(
        lane_executor_module,
        "time",
        _ClockThatFaultsAfterTheInterrupt(first, second),
    )

    work_key = "lifecycle.budgetinterrupt"
    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupted_lane(tools, tmp_path, work_key)

    assert caught.value is second, "the second interrupt did not win"
    assert caught.value.__cause__ is first, (
        "the original ending vanished: the boundary did not cover the "
        "budget construction"
    )
    for directory in Path(tempfile.gettempdir()).glob(f"lane-{work_key}*"):
        shutil.rmtree(directory, ignore_errors=True)


def test_a_system_exit_building_the_budget_is_contained_and_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same window, the other half of the policy: a SystemExit at
    the wind-down's first instruction must not replace the real ending,
    and must not vanish unrecorded."""
    history = tmp_path / "per-job-history"
    history.mkdir()
    tools = _cancellable_tools(
        tmp_path, history=history, removal_writes_classad=False
    )
    executor = CondorLaneExecutor(tools)
    original = KeyboardInterrupt("the real ending")
    monkeypatch.setattr(
        lane_executor_module,
        "time",
        _ClockThatFaultsAfterTheInterrupt(original, SystemExit(17)),
    )

    work_key = "lifecycle.budgetexit"
    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupted_lane(tools, tmp_path, work_key)

    assert caught.value is original, (
        "a SystemExit building the budget rewrote why the lane ended"
    )
    stderr = capsys.readouterr().err
    assert "cancellation cleanup gave up after" in stderr, stderr
    assert "SystemExit" in stderr, stderr
    for directory in Path(tempfile.gettempdir()).glob(f"lane-{work_key}*"):
        shutil.rmtree(directory, ignore_errors=True)


def test_the_cancellation_budget_also_bounds_the_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 3 finding 1: the budget was created inside the accounting
    step, so the two stages before it were outside the clock entirely —
    a slow condor_rm spent the general 30s tool timeout and only THEN
    did a 2s "budget" begin. The wind-down owns the whole operation, so
    the removal draws from the same allowance as everything else."""
    history = tmp_path / "per-job-history"
    history.mkdir()
    tools = _cancellable_tools(
        tmp_path, history=history, removal_writes_classad=False
    )
    tools.remove.write_text(f"#!/bin/sh\nexec sleep {_SLOW_LOOKUP_SECONDS:.0f}\n")
    tools.remove.chmod(0o755)
    work_key = "lifecycle.slowremoval"
    executor = CondorLaneExecutor(tools)
    monkeypatch.setattr(
        lane_executor_module, "time", _WaitsThatRaise(KeyboardInterrupt())
    )

    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        executor.run(
            LaneCommand(
                work_key=LaneWorkKey(work_key),
                arguments=(sys.executable, "-c", "pass"),
                working_directory=tmp_path,
                deadline=LaneDeadline(300.0),
            ),
            LaneResources(request_cpus=1),
        )
    elapsed = time.monotonic() - started

    budget = lane_executor_module._CANCELLED_ACCOUNTING_WAIT_SECONDS
    assert elapsed < budget * 4, (
        "the cancellation budget did not span the job removal: "
        f"{elapsed:.2f}s for a {budget:.0f}s budget"
    )
    for directory in Path(tempfile.gettempdir()).glob(f"lane-{work_key}*"):
        shutil.rmtree(directory, ignore_errors=True)


def test_a_second_interrupt_during_the_removal_still_chains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 3 finding 2: the exception boundary began AFTER the
    removal, so an interrupt during that subprocess wait — the earliest
    and likeliest window — escaped as the newest exception with no
    __cause__, silently breaking the chaining contract round 2 added."""
    history = tmp_path / "per-job-history"
    history.mkdir()
    tools = _cancellable_tools(
        tmp_path, history=history, removal_writes_classad=False
    )
    executor = CondorLaneExecutor(tools)
    first = KeyboardInterrupt("first")
    second = KeyboardInterrupt("second")
    monkeypatch.setattr(lane_executor_module, "time", _WaitsThatRaise(first))
    monkeypatch.setattr(
        tools_module,
        "subprocess",
        _ToolThatRaises("condor_rm", second),
    )

    work_key = "lifecycle.interruptedremoval"
    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupted_lane(tools, tmp_path, work_key)

    assert caught.value is second, "the second interrupt did not win"
    assert caught.value.__cause__ is first, (
        "the original ending vanished: the boundary did not cover the removal"
    )
    for directory in Path(tempfile.gettempdir()).glob(f"lane-{work_key}*"):
        shutil.rmtree(directory, ignore_errors=True)


def test_a_system_exit_during_the_removal_is_contained_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round 3 finding 3: SystemExit behaved differently in the two
    windows — it propagated during the removal (replacing the real
    ending, the exact harm the policy names) and was swallowed in
    silence during the accounting by a leftover bare containment.

    One policy across the whole wind-down: contained, so it cannot
    rewrite why the lane ended, and RECORDED, because a containment that
    reports nothing is indistinguishable from a bug."""
    history = tmp_path / "per-job-history"
    history.mkdir()
    tools = _cancellable_tools(
        tmp_path, history=history, removal_writes_classad=False
    )
    executor = CondorLaneExecutor(tools)
    original = KeyboardInterrupt("the real ending")
    monkeypatch.setattr(lane_executor_module, "time", _WaitsThatRaise(original))
    monkeypatch.setattr(
        tools_module,
        "subprocess",
        _ToolThatRaises("condor_rm", SystemExit("cleanup exit")),
    )

    work_key = "lifecycle.exitduringremoval"
    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupted_lane(tools, tmp_path, work_key)

    assert caught.value is original, (
        "a SystemExit during cleanup rewrote why the lane ended"
    )
    stderr = capsys.readouterr().err
    assert "cancellation cleanup gave up after" in stderr, stderr
    assert "SystemExit" in stderr, stderr
    for directory in Path(tempfile.gettempdir()).glob(f"lane-{work_key}*"):
        shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.parametrize(
    "original",
    [KeyboardInterrupt("first"), asyncio.CancelledError("cancelled")],
    ids=["ctrl-c-then-ctrl-c", "cancellation-then-ctrl-c"],
)
def test_a_second_interrupt_during_cleanup_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, original: BaseException
) -> None:
    """Round 2 finding 2: `except BaseException: pass` around the
    cancellation-path collection ate a SECOND interrupt, so an operator
    hammering Ctrl-C during a slow cleanup kept waiting on the cleanup.

    The teardown policy the sampler already uses applies here too: stop
    signals get out. The first ending is chained rather than discarded,
    so the record of why the lane was ending survives as __cause__.
    """
    history = tmp_path / "per-job-history"
    history.mkdir()
    _unhurried_cancellation(monkeypatch)
    # No ClassAd is ever written, so the collection reaches its own
    # file-wait - exactly where the second interrupt lands.
    tools = _cancellable_tools(
        tmp_path, history=history, removal_writes_classad=False
    )
    work_key = "lifecycle.secondinterrupt"
    executor = CondorLaneExecutor(tools)
    second = KeyboardInterrupt("second")
    monkeypatch.setattr(
        lane_executor_module, "time", _WaitsThatRaise(original, second)
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        executor.run(
            LaneCommand(
                work_key=LaneWorkKey(work_key),
                arguments=(sys.executable, "-c", "pass"),
                working_directory=tmp_path,
                deadline=LaneDeadline(300.0),
            ),
            LaneResources(request_cpus=1),
        )

    assert caught.value is second, (
        "the second interrupt was swallowed and the first propagated"
    )
    assert caught.value.__cause__ is original, (
        "the original ending vanished instead of being chained"
    )
    for directory in Path(tempfile.gettempdir()).glob(f"lane-{work_key}*"):
        shutil.rmtree(directory, ignore_errors=True)


def test_a_clean_lane_collects_nothing_and_keeps_nothing(tmp_path: Path) -> None:
    """The retention decision and the collection decision are one: a
    clean completion leaves no directory, so there is nothing to
    collect into."""
    history = tmp_path / "per-job-history"
    history.mkdir()
    (history / f"history.{_JOB_ID}").write_text("ExitCode = 0\n")
    tools = _completing_tools(tmp_path, history_directory=str(history))
    zero_exit = _TERMINATED_NONZERO_EVENT_LOG.replace("return value 3", "return value 0")
    submit = tools.submit
    submit.write_text(
        "#!/bin/sh\n"
        'log=$(awk -F" = " \'/^log/{print $2}\' "$2")\n'
        f"cat > \"$log\" <<'EVENTS'\n{zero_exit}EVENTS\n"
        f"echo '{_JOB_ID}'\n"
    )
    submit.chmod(0o755)

    outcome, retained = _run_lane(tools, tmp_path, "lifecycle.clean")
    assert outcome.exit_code == 0
    assert not retained, "a clean completion must leave no run directory"
