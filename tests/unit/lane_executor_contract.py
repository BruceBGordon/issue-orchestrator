"""Backend-agnostic contract assertions for the LaneExecutor port.

Every backend adapter must pass these unchanged: they define what
"callers cannot tell backends apart" means. Backend-specific test
modules instantiate :class:`LaneExecutorContract` with a factory for
their adapter and inherit the whole suite.

Waiting here is always waiting on an EVENT the system emits — a pid file
the fixture wrote, a marker on the lane's stdout, a pid the kernel has
stopped knowing about. Every ``*_BACKSTOP_SECONDS`` below exists only to
turn a hang into a failure that NAMES the event that never happened; none
of them is a coordination device, and no assertion is true because a
sleep was long enough (#7148).
"""

from __future__ import annotations

import _thread
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from issue_orchestrator.domain.lane_execution import (
    LANE_TIMEOUT_EXIT_CODE,
    LaneCommand,
    LaneCompleted,
    LaneDeadline,
    LaneOutcome,
    LaneResources,
    LaneTimedOut,
    LaneWorkKey,
)
from issue_orchestrator.ports.lane_executor import LaneExecutor
from tests.load_fixture import reap_marked_processes

# --------------------------------------------------------------------------
# Backstops. Each one names the event whose absence it reports.
# --------------------------------------------------------------------------

# How long the tree fixture may take to reach its first instruction and
# announce itself. Deliberately generous, because this is precisely the
# window #7148 was about: on a slim CI container the interpreter's own
# start-up outran a 5s lane deadline, the fixture was killed before it
# wrote its pid file, and the test failed with a FileNotFoundError that
# named nothing. Startup speed is now a precondition, never a race.
_READINESS_BACKSTOP_SECONDS = 60.0

# How long a cancelled lane's process tree may take to disappear. Must
# exceed the scheduler backend's soft-to-hard kill grace
# (``job_max_vacate_time``, 10s) plus its polling latency.
_TREE_REAP_BACKSTOP_SECONDS = 60.0

# The deadline the deadline test submits. Nothing has to have happened by
# the time it fires, so it can be short.
_DEADLINE_UNDER_TEST_SECONDS = 5.0

# How long the streaming lane's first output may take to appear while the
# lane is provably still running, and how long the lane may then take to
# conclude once the handshake releases it.
_STREAM_MARKER_BACKSTOP_SECONDS = 60.0
_LANE_CONCLUSION_BACKSTOP_SECONDS = 60.0

# Poll gap while waiting for an event. Granularity, not coordination:
# there is no ack channel from the kernel for "this pid is gone" or from
# the filesystem for "this file appeared".
_POLL_SECONDS = 0.05

# --------------------------------------------------------------------------
# Fixture processes. Every one of them dies of its own clock (#7142): a
# ``finally`` protects only a harness that gets to run it, and the machine
# pays for the fixtures of harnesses that did not.
# --------------------------------------------------------------------------

# 300s is chosen against the observation windows above, not against comfort:
# every wait in this module is bounded well inside it, so this clock cannot
# end a fixture before the backend's duty has been observed — it can only
# stop one outliving the machine's next few gates.
_TREE_LIFETIME_SECONDS = 300.0
_SLEEPER_LIFETIME_SECONDS = 300.0
# The streaming lane waits for a handshake this test writes; the clock is
# what makes it safe when the handshake never comes. Declared as a constant
# and threaded through argv so the lifetime scan can see it — spelled as
# ``time.time() + 90`` inside the script it was invisible to the scan.
_STREAMING_LANE_LIFETIME_SECONDS = 90.0

# A process tree that must be KILLED, never asked: both processes ignore
# SIGTERM, so only an escalation to SIGKILL (or a scheduler's hard kill of
# the job family) removes them. Ignoring SIGTERM is the point of this
# fixture; being immortal is not, so both hold one absolute expiry taken
# before the fork.
#
# Its FIRST act after forking is to announce itself: the child writes both
# pids and renames the record into place, so the file's existence is the
# readiness event and a reader can never see half of it. Everything that
# waits on this tree waits on that event.
_TREE_SCRIPT = """
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
deadline = time.monotonic() + float(sys.argv[2])
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    from pathlib import Path
    ready = Path(sys.argv[1])
    staged = ready.with_name(ready.name + '.staged')
    staged.write_text(str(os.getppid()) + '\\n' + str(os.getpid()) + '\\n')
    staged.replace(ready)
    while time.monotonic() < deadline:
        time.sleep(0.5)
    os._exit(0)
while time.monotonic() < deadline:
    time.sleep(0.5)
"""

# Prints one marker, then refuses to conclude until the test releases it —
# so the marker can only be observed while the lane is provably running.
_STREAMING_SCRIPT = """
import sys, time, pathlib
print('STREAM-MARKER', flush=True)
deadline = time.monotonic() + float(sys.argv[2])
while not pathlib.Path(sys.argv[1]).exists():
    if time.monotonic() > deadline:
        raise SystemExit(9)
    time.sleep(0.1)
"""

# A lane with nothing to establish: it is asleep from its first instruction,
# so how long it took to get there cannot change what the deadline does to
# it. Cooperative with SIGTERM on purpose — this fixture is for classifying
# an overrun, not for proving anything about kill topology.
_SLEEPER_SCRIPT = """
import sys, time
time.sleep(float(sys.argv[1]))
"""


@dataclass(frozen=True, slots=True)
class TreePids:
    """The tree fixture's own report of the tree it started."""

    parent: int
    grandchild: int


def read_tree_pids(readiness_path: Path) -> TreePids:
    """Read the readiness record :data:`_TREE_SCRIPT` writes.

    The one owner of that file's format, so the tests that wait on the
    event and the guardrail that re-asserts the fixture's TERM-immunity
    cannot drift apart.

    Raises:
        FileNotFoundError: the tree has not announced itself yet.
        ValueError: the content is not the two pids the fixture writes.
            Callers polling for the event retry on both.
    """
    fields = readiness_path.read_text().split()
    if len(fields) != 2:
        raise ValueError(
            f"{readiness_path} is not a tree readiness record: {fields!r}"
        )
    return TreePids(parent=int(fields[0]), grandchild=int(fields[1]))


def _command(
    work_key: str,
    arguments: tuple[str, ...],
    working_directory: Path,
    timeout_seconds: float,
) -> LaneCommand:
    return LaneCommand(
        work_key=LaneWorkKey(work_key),
        arguments=arguments,
        working_directory=working_directory.resolve(),
        deadline=LaneDeadline(timeout_seconds),
    )


def _await_pid_gone(pid: int, deadline_seconds: float) -> bool:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(_POLL_SECONDS)
    return False


@dataclass(frozen=True, slots=True)
class _CancellationAttempt:
    """What happened when a running lane's caller was cancelled."""

    outcome: LaneOutcome | None
    cancelled: BaseException | None
    pids: TreePids | None
    readiness_timed_out: bool


class _CancelWhenTreeIsReady:
    """Cancel the lane when its process tree announces itself — or when
    the backstop says it never will.

    The backends' cancellation path is entered by an exception raised in
    the thread that called ``run()`` — an operator's Ctrl-C, a dying
    supervisor. There is no out-of-band cancel API to call, so this
    reproduces the real trigger: a watcher waits for the readiness event
    and then raises ``KeyboardInterrupt`` in the main thread with
    ``_thread.interrupt_main``. No signal is delivered to any process, and
    none to any other thread.

    A backstop that only stops WATCHING would leave the run to finish on
    somebody else's clock (round 1, #7148). A lane that is admitted and
    then never dispatched does not reach its own deadline — that clock
    starts at dispatch — so it runs until the backend's admission
    watchdog, ten minutes away, and the enclosing pytest timeout gets
    there first. The operator would then read a generic "test exceeded
    600s" for precisely the failure this suite exists to name. So the
    expiry cancels too: the run ends here, the named readiness assertion
    is what fails, and the lane's job is removed instead of sitting in a
    queue nobody is watching any more.

    Armed only while ``run()`` is in progress. If the lane instead
    concludes on its own — a regression, or the deadline backstop — the
    interrupt is never sent, so a failing test fails on its own assertions
    rather than throwing a KeyboardInterrupt into pytest.
    """

    def __init__(self, readiness_path: Path) -> None:
        self._readiness_path = readiness_path
        self._lock = threading.Lock()
        self._armed = False
        self._observed_pids: TreePids | None = None
        self._readiness_timed_out = False
        self._thread = threading.Thread(
            target=self._watch, name="lane-cancel-when-ready", daemon=True
        )

    @property
    def observed_pids(self) -> TreePids | None:
        """The tree the watcher saw, or None if readiness never arrived."""
        return self._observed_pids

    @property
    def readiness_timed_out(self) -> bool:
        """Whether the run was cancelled for never announcing a tree."""
        return self._readiness_timed_out

    def arm(self) -> None:
        self._armed = True
        self._thread.start()

    def disarm(self) -> None:
        with self._lock:
            self._armed = False
        self._thread.join(timeout=_READINESS_BACKSTOP_SECONDS)
        if self._thread.is_alive():
            raise AssertionError(
                "the cancellation watcher outlived its own readiness "
                f"backstop of {_READINESS_BACKSTOP_SECONDS:.0f}s"
            )

    def _watch(self) -> None:
        deadline = time.monotonic() + _READINESS_BACKSTOP_SECONDS
        while time.monotonic() < deadline:
            with self._lock:
                if not self._armed:
                    return
                pids = self._read_pids()
                if pids is not None:
                    self._observed_pids = pids
                    _thread.interrupt_main()
                    return
            time.sleep(_POLL_SECONDS)
        # Nothing is ever going to announce itself. End the run on THIS
        # clock so the test's own assertion is the failure the operator
        # reads, and so the lane does not outlive the watcher.
        with self._lock:
            if not self._armed:
                return
            self._readiness_timed_out = True
            _thread.interrupt_main()

    def _read_pids(self) -> TreePids | None:
        try:
            return read_tree_pids(self._readiness_path)
        except (FileNotFoundError, ValueError):
            return None


def _cancel_when_ready(
    executor: LaneExecutor,
    command: LaneCommand,
    resources: LaneResources,
    readiness_path: Path,
) -> _CancellationAttempt:
    """Run the lane and cancel its caller once the tree is up."""
    trigger = _CancelWhenTreeIsReady(readiness_path)
    outcome: LaneOutcome | None = None
    cancelled: BaseException | None = None
    trigger.arm()
    try:
        outcome = executor.run(command, resources)
    except KeyboardInterrupt as interrupt:
        cancelled = interrupt
    finally:
        try:
            trigger.disarm()
        except KeyboardInterrupt as late:
            # Delivered in the microseconds between the watcher's armed
            # check and this disarm. It belongs to this attempt, not to
            # whatever pytest would have run next.
            cancelled = late if cancelled is None else cancelled
    return _CancellationAttempt(
        outcome, cancelled, trigger.observed_pids, trigger.readiness_timed_out
    )


class LaneExecutorContract:
    """Inherit and implement :meth:`build_executor` to adopt the suite."""

    # Generous machinery allowance: scheduling/startup overhead must
    # never be billed against the behavior under test.
    completion_timeout_seconds = 120.0

    def build_executor(self) -> LaneExecutor:
        raise NotImplementedError

    def resources(self) -> LaneResources:
        return LaneResources(request_cpus=1)

    def test_completes_in_working_directory_with_environment(
        self, tmp_path: Path
    ) -> None:
        marker_variable = "LANE_CONTRACT_TOKEN"
        os.environ[marker_variable] = "lane-contract-proof"
        try:
            outcome = self.build_executor().run(
                _command(
                    "contract.completes",
                    (
                        sys.executable,
                        "-c",
                        "import os, pathlib; "
                        "pathlib.Path('lane-proof.txt').write_text("
                        f"os.environ['{marker_variable}'])",
                    ),
                    tmp_path,
                    self.completion_timeout_seconds,
                ),
                self.resources(),
            )
        finally:
            del os.environ[marker_variable]
        assert type(outcome) is LaneCompleted
        assert outcome.exit_code == 0
        assert (tmp_path / "lane-proof.txt").read_text() == "lane-contract-proof"

    def test_nonzero_exit_code_propagates_exactly(self, tmp_path: Path) -> None:
        outcome = self.build_executor().run(
            _command(
                "contract.exit-code",
                (sys.executable, "-c", "raise SystemExit(17)"),
                tmp_path,
                self.completion_timeout_seconds,
            ),
            self.resources(),
        )
        assert type(outcome) is LaneCompleted
        assert outcome.exit_code == 17

    def test_observed_runtime_reflects_actual_execution(
        self, tmp_path: Path
    ) -> None:
        """Completed lanes report how long they actually executed.

        The lower bound proves the value tracks real execution; the
        upper bound is the machinery allowance, deliberately loose —
        precision belongs to the backends, plausibility to the
        contract. Queue-wait exclusion is proven where queues exist
        (the scheduler backend's integration suite)."""
        outcome = self.build_executor().run(
            _command(
                "contract.runtime",
                (sys.executable, "-c", "import time; time.sleep(2)"),
                tmp_path,
                self.completion_timeout_seconds,
            ),
            self.resources(),
        )
        assert type(outcome) is LaneCompleted
        assert outcome.exit_code == 0
        assert 1.5 <= outcome.observed_runtime_seconds <= (
            self.completion_timeout_seconds
        )

    def test_queue_wait_is_reported_and_plausible(self, tmp_path: Path) -> None:
        """Completed lanes price their scheduling wait separately.

        No upper bound on purpose (B3, #7122 review): queue wait is
        explicitly excluded from the lane's deadline and may
        legitimately exceed it under pool contention — capping it by
        the runtime allowance would fail this shared contract for
        correct behavior. The contract asks only that the field is
        reported non-negative; the direct backend's exact zero and the
        scheduler backend's real waits are backend-suite facts."""
        outcome = self.build_executor().run(
            _command(
                "contract.queue-wait",
                (sys.executable, "-c", "pass"),
                tmp_path,
                self.completion_timeout_seconds,
            ),
            self.resources(),
        )
        assert type(outcome) is LaneCompleted
        assert outcome.queue_wait_seconds >= 0.0


    def test_output_streams_before_the_lane_completes(
        self, tmp_path: Path, capfd: "pytest.CaptureFixture[str]"
    ) -> None:
        """The port promises STREAMED output, not buffered-until-done.

        The lane prints a marker and then refuses to exit until this
        test creates a handshake file. Observing the marker while the
        lane is provably still running is the streaming proof; a
        backend that buffers until completion deadlocks here and fails
        by timeout instead of passing dishonestly.
        """
        handshake = tmp_path / "proceed"
        outcomes: list[object] = []

        def run_lane() -> None:
            outcomes.append(
                self.build_executor().run(
                    _command(
                        "contract.streaming",
                        (
                            sys.executable,
                            "-c",
                            _STREAMING_SCRIPT,
                            str(handshake),
                            str(_STREAMING_LANE_LIFETIME_SECONDS),
                        ),
                        tmp_path,
                        self.completion_timeout_seconds,
                    ),
                    self.resources(),
                )
            )

        thread = threading.Thread(target=run_lane)
        thread.start()
        observed = ""
        deadline = time.monotonic() + _STREAM_MARKER_BACKSTOP_SECONDS
        while time.monotonic() < deadline and "STREAM-MARKER" not in observed:
            captured = capfd.readouterr()
            observed += captured.out + captured.err
            time.sleep(_POLL_SECONDS)
        marker_seen_while_running = (
            "STREAM-MARKER" in observed and thread.is_alive()
        )
        handshake.write_text("go")
        thread.join(timeout=_LANE_CONCLUSION_BACKSTOP_SECONDS)
        assert not thread.is_alive(), (
            "the lane never concluded within "
            f"{_LANE_CONCLUSION_BACKSTOP_SECONDS:.0f}s of being released by "
            "the handshake"
        )
        assert marker_seen_while_running, (
            "output was not observable before completion - the backend "
            "buffers instead of streaming"
        )
        assert outcomes and type(outcomes[0]) is LaneCompleted
        assert outcomes[0].exit_code == 0

    def test_signal_death_reports_as_128_plus_signal(self, tmp_path: Path) -> None:
        outcome = self.build_executor().run(
            _command(
                "contract.signal-death",
                (
                    sys.executable,
                    "-c",
                    "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
                ),
                tmp_path,
                self.completion_timeout_seconds,
            ),
            self.resources(),
        )
        assert type(outcome) is LaneCompleted
        assert outcome.exit_code == 137

    def test_deadline_overrun_is_reported_as_timed_out(
        self, tmp_path: Path
    ) -> None:
        """What the deadline uniquely owns here: the classification.

        The workload has nothing to establish before it can be killed — it
        is asleep from its first instruction — so how long it took to get
        there cannot change the outcome, only when it happens. That is the
        whole point of splitting this test (#7148): the previous version
        proved the classification AND the reaping of a process tree in one
        run, which made it a race between the lane's 5s deadline and the
        job's own start-up. It lost that race twice in CI.

        Reaping descendants is proven by the cancellation test below,
        against the same kill mechanism this deadline reaches (the direct
        backend calls one containment routine from both paths; the
        scheduler backend removes the job either way — a periodic
        expression and ``condor_rm`` produce the same abort event, and the
        classifier tells them apart only by the reason text).
        """
        outcome = self.build_executor().run(
            _command(
                "contract.deadline",
                (
                    sys.executable,
                    "-c",
                    _SLEEPER_SCRIPT,
                    str(_SLEEPER_LIFETIME_SECONDS),
                ),
                tmp_path,
                _DEADLINE_UNDER_TEST_SECONDS,
            ),
            self.resources(),
        )
        assert type(outcome) is LaneTimedOut, (
            "a lane still sleeping past its "
            f"{_DEADLINE_UNDER_TEST_SECONDS:.0f}s deadline ended as "
            f"{outcome!r}"
        )
        assert outcome.exit_code == LANE_TIMEOUT_EXIT_CODE

    def test_cancelling_a_running_lane_reaps_its_whole_process_tree(
        self, tmp_path: Path
    ) -> None:
        """A cancelled lane takes its descendants with it — on purpose.

        Three separate things, in order, each waited on as an event:

        1. The tree announces itself (both pids, one atomic record). Until
           that has happened there is nothing to kill, so the lane's own
           deadline is a generous backstop rather than the trigger.
        2. The kill is triggered deliberately, through the cancellation
           path the backends actually implement: an exception in the
           thread that called ``run()``.
        3. Both processes — the parent and its TERM-immune grandchild —
           stop existing. Neither cooperates with SIGTERM, so nothing here
           passes because a process was polite.
        """
        # Both preconditions of the trigger, asserted rather than assumed:
        # the interrupt is raised in the main thread and handled by the
        # default handler. If either changed, the cancellation would never
        # reach ``run()`` and this test would report the wrong culprit.
        assert threading.current_thread() is threading.main_thread(), (
            "this test cancels by interrupting the main thread, so it must "
            "be the thread that called into the backend"
        )
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, (
            "something in this run owns SIGINT, so interrupting the main "
            "thread would run that handler instead of cancelling the lane"
        )
        readiness_path = tmp_path / "tree-ready.pids"
        try:
            attempt = _cancel_when_ready(
                self.build_executor(),
                _command(
                    "contract.cancel-tree",
                    (
                        sys.executable,
                        "-c",
                        _TREE_SCRIPT,
                        str(readiness_path),
                        str(_TREE_LIFETIME_SECONDS),
                    ),
                    tmp_path,
                    self.completion_timeout_seconds,
                ),
                self.resources(),
                readiness_path,
            )
            assert attempt.pids is not None, (
                "the lane never announced a running process tree at "
                f"{readiness_path} within {_READINESS_BACKSTOP_SECONDS:.0f}s. "
                + (
                    "The run was cancelled at that backstop so THIS is the "
                    "failure you are reading, rather than the enclosing test "
                    "timeout: the lane was still running (queued, or started "
                    "and mute) with nothing to kill."
                    if attempt.readiness_timed_out
                    else "The lane concluded on its own first, as "
                    f"{attempt.outcome!r}."
                )
            )
            assert attempt.cancelled is not None, (
                "the backend swallowed its caller's cancellation and "
                f"returned {attempt.outcome!r} instead of re-raising"
            )
            for role, pid in (
                ("parent", attempt.pids.parent),
                ("grandchild", attempt.pids.grandchild),
            ):
                assert _await_pid_gone(pid, _TREE_REAP_BACKSTOP_SECONDS), (
                    f"a TERM-immune {role} survived the lane's cancellation "
                    f"by {_TREE_REAP_BACKSTOP_SECONDS:.0f}s: pid={pid}"
                )
        finally:
            # #7142: ``_TREE_SCRIPT`` ignores SIGTERM, so exactly when the
            # assertions above are doing their job — a backend regressed,
            # or the tree never announced itself — this test is the thing
            # leaving signal-resistant processes on the machine. Five of
            # them, up to twelve hours old, were found here. The pgid
            # belongs to the backend, so identity comes from the argv.
            reap_marked_processes(str(readiness_path))
