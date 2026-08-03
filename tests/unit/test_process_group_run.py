"""Process-tree cleanup for the live-agent probe runner.

These spawn real ``/bin/sh`` processes — that is the point. The property under
test is that a timed-out agent CLI cannot leave descendants behind that keep
writing after the harness has moved on. A fake cannot demonstrate that.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.process_group_run import run_in_process_group
from tests.sandbox_probe_retry import decode_stream, run_until_paths_created

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="process-group signalling (os.killpg) is POSIX-only",
)

# Long enough that the probe is unambiguously killed mid-flight rather than
# finishing on its own.
_BLOCK_SECONDS = 300


def _pid_has_exited(pid: int, *, deadline_seconds: float = 10.0) -> bool:
    """Bounded wait for ``pid`` to disappear.

    Reaping a reparented grandchild is done by init, so it is observable but
    not synchronous with our kill. ``tests/AGENTS.md`` permits a bounded wait
    on a real external system; there is no ack channel from init to poll.
    """
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        time.sleep(0.05)
    return False


def _grandchild_script(*, pid_file: Path, evidence: Path) -> str:
    """A probe that spawns a descendant which would later write ``evidence``.

    The descendant is exactly the shape that breaks naive cleanup: an agent
    CLI's Bash tool, still running when the CLI itself is killed. It records
    its PID synchronously so the test can prove it really existed.
    """
    return (
        f"sh -c 'sleep {_BLOCK_SECONDS}; echo LATE > {evidence}' & "
        f"echo $! > {pid_file}; "
        f"sleep {_BLOCK_SECONDS}"
    )


def test_returns_the_completed_process_for_a_normal_command(tmp_path: Path) -> None:
    result = run_in_process_group(
        ["/bin/sh", "-c", "echo hello; echo oops >&2"], cwd=tmp_path, timeout=30
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "hello"
    assert result.stderr.strip() == "oops"


def test_propagates_a_non_zero_exit_without_raising(tmp_path: Path) -> None:
    result = run_in_process_group(["/bin/sh", "-c", "exit 3"], cwd=tmp_path, timeout=30)

    assert result.returncode == 3


def test_passes_the_environment_through(tmp_path: Path) -> None:
    result = run_in_process_group(
        ["/bin/sh", "-c", "echo $PROBE_MARKER"],
        cwd=tmp_path,
        timeout=30,
        env={"PROBE_MARKER": "MARKER_5f2a", "PATH": os.environ.get("PATH", "")},
    )

    assert result.stdout.strip() == "MARKER_5f2a"


def test_timeout_raises_with_the_captured_output(tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        run_in_process_group(
            ["/bin/sh", "-c", f"echo BEFORE_STALL; sleep {_BLOCK_SECONDS}"],
            cwd=tmp_path,
            timeout=2,
        )

    assert "BEFORE_STALL" in decode_stream(excinfo.value.stdout)


def test_a_grandchild_cannot_outlive_the_timeout_cleanup(tmp_path: Path) -> None:
    """The finding: killing only the session leader leaves the tool running.

    ``subprocess.run`` signals just the process object, so the backgrounded
    descendant here would survive and write ``evidence`` long after the caller
    had snapshotted and reset the attempt.
    """
    pid_file = tmp_path / "grandchild.pid"
    evidence = tmp_path / "completed.txt"

    with pytest.raises(subprocess.TimeoutExpired):
        run_in_process_group(
            ["/bin/sh", "-c", _grandchild_script(pid_file=pid_file, evidence=evidence)],
            cwd=tmp_path,
            timeout=3,
        )

    # Non-vacuity: the descendant really was spawned, so the kill below is a
    # real observation rather than an empty one.
    assert pid_file.exists(), "the probe never spawned its descendant"
    grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())

    assert _pid_has_exited(grandchild_pid), (
        f"grandchild {grandchild_pid} survived the timeout cleanup; it can still "
        "write result files after the harness resets the attempt"
    )
    assert not evidence.exists()


def test_a_surviving_grandchild_cannot_supply_the_next_attempt_s_evidence(
    tmp_path: Path,
) -> None:
    """End-to-end: the retry owner over the real runner rejects the stale path.

    Attempt 1 spawns a descendant that would create the expected path, then
    times out. Attempt 2 returns immediately without doing any work. With the
    process tree properly killed and the attempt-owned outputs reset, nothing
    can present that run as complete evidence.
    """
    pid_file = tmp_path / "grandchild.pid"
    expected = tmp_path / "completed.txt"
    attempts = 0

    def run_attempt() -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return run_in_process_group(
                [
                    "/bin/sh",
                    "-c",
                    _grandchild_script(pid_file=pid_file, evidence=expected),
                ],
                cwd=tmp_path,
                timeout=3,
            )
        return run_in_process_group(["/bin/sh", "-c", "true"], cwd=tmp_path, timeout=30)

    probe = run_until_paths_created(
        run_attempt,
        expected_paths=(expected,),
        observed_paths=(expected,),
    )

    assert attempts == 2
    assert pid_file.exists(), "the first attempt never spawned its descendant"
    assert _pid_has_exited(int(pid_file.read_text(encoding="utf-8").strip()))
    assert probe.completed_attempt is None, (
        "a run whose only evidence came from a killed attempt must not be accepted"
    )
    assert not expected.exists()
