"""Backend-agnostic contract assertions for the LaneExecutor port.

Every backend adapter must pass these unchanged: they define what
"callers cannot tell backends apart" means. Backend-specific test
modules instantiate :class:`LaneExecutorContract` with a factory for
their adapter and inherit the whole suite.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from issue_orchestrator.domain.lane_execution import (
    LANE_TIMEOUT_EXIT_CODE,
    LaneCommand,
    LaneCompleted,
    LaneDeadline,
    LaneResources,
    LaneTimedOut,
    LaneWorkKey,
)
from issue_orchestrator.ports.lane_executor import LaneExecutor

_TREE_SCRIPT = """
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    from pathlib import Path
    Path(sys.argv[1]).write_text(str(os.getpid()))
    while True:
        time.sleep(0.5)
while True:
    time.sleep(0.5)
"""


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
        time.sleep(0.05)
    return False


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
        import threading

        handshake = tmp_path / "proceed"
        script = (
            "import sys, time, pathlib\n"
            "print('STREAM-MARKER', flush=True)\n"
            "deadline = time.time() + 90\n"
            "while not pathlib.Path(sys.argv[1]).exists():\n"
            "    if time.time() > deadline:\n"
            "        raise SystemExit(9)\n"
            "    time.sleep(0.1)\n"
        )
        outcomes: list[object] = []

        def run_lane() -> None:
            outcomes.append(
                self.build_executor().run(
                    _command(
                        "contract.streaming",
                        (sys.executable, "-c", script, str(handshake)),
                        tmp_path,
                        self.completion_timeout_seconds,
                    ),
                    self.resources(),
                )
            )

        thread = threading.Thread(target=run_lane)
        thread.start()
        observed = ""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and "STREAM-MARKER" not in observed:
            captured = capfd.readouterr()
            observed += captured.out + captured.err
            time.sleep(0.1)
        marker_seen_while_running = (
            "STREAM-MARKER" in observed and thread.is_alive()
        )
        handshake.write_text("go")
        thread.join(timeout=60)
        assert not thread.is_alive(), "lane never concluded"
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

    def test_deadline_terminates_the_entire_process_tree(
        self, tmp_path: Path
    ) -> None:
        grandchild_pid_path = tmp_path / "grandchild.pid"
        outcome = self.build_executor().run(
            _command(
                "contract.deadline",
                (sys.executable, "-c", _TREE_SCRIPT, str(grandchild_pid_path)),
                tmp_path,
                5.0,
            ),
            self.resources(),
        )
        assert type(outcome) is LaneTimedOut
        assert outcome.exit_code == LANE_TIMEOUT_EXIT_CODE
        grandchild_pid = int(grandchild_pid_path.read_text())
        assert _await_pid_gone(grandchild_pid, 30.0), (
            "a TERM-immune grandchild survived the lane deadline: "
            f"pid={grandchild_pid}"
        )
