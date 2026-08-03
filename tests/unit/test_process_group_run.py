"""Process-tree cleanup for the live-agent probe runner.

These spawn real ``/bin/sh`` processes — that is the point. The property under
test is that a timed-out agent CLI cannot leave descendants behind that keep
writing after the harness has moved on. A fake cannot demonstrate that.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.process_group_run import ProcessGroupCleanupError, run_in_process_group
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
    its PID synchronously so the test can prove it really existed. This one
    takes SIGTERM's default disposition, so it dies in the first cleanup phase.
    """
    return (
        f"sh -c 'sleep {_BLOCK_SECONDS}; echo LATE > {evidence}' & "
        f"echo $! > {pid_file}; "
        f"sleep {_BLOCK_SECONDS}"
    )


def _term_resistant_grandchild_script(*, pid_file: Path, evidence: Path) -> str:
    """A descendant that survives SIGTERM *and* lets go of the inherited pipes.

    This is the case that separates "the pipes closed" from "the group is
    empty". The descendant ignores SIGTERM and redirects stdout and stderr away
    from the captured pipes, so once the leader dies the pipes reach EOF while
    the descendant is still alive and still able to write ``evidence``. Cleanup
    that concludes from EOF alone returns here without ever sending SIGKILL.

    It waits in a loop of short sleeps rather than one long sleep: a signal
    delivered to the group kills the current ``sleep`` child, and a single long
    sleep would let the script fall straight through to writing ``evidence``.
    """
    iterations = int(_BLOCK_SECONDS / 0.1)
    return (
        "sh -c '"
        'trap "" TERM; '
        "exec >/dev/null 2>&1; "
        "i=0; "
        f"while [ $i -lt {iterations} ]; do sleep 0.1; i=$((i+1)); done; "
        f"echo LATE > {evidence}"
        "' & "
        f"echo $! > {pid_file}; "
        f"sleep {_BLOCK_SECONDS}"
    )


# Both descendant shapes must be cleaned up. The TERM-resistant one is the
# regression: it is alive at the moment the pipes go quiet.
_GRANDCHILD_SCRIPTS = pytest.mark.parametrize(
    "build_script",
    [
        pytest.param(_grandchild_script, id="term-cooperative"),
        pytest.param(_term_resistant_grandchild_script, id="term-resistant"),
    ],
)

# Short enough to keep the SIGKILL escalation quick; the TERM-resistant
# descendant only dies once that escalation actually happens.
_TERMINATE_GRACE = 0.5


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


@_GRANDCHILD_SCRIPTS
def test_a_grandchild_cannot_outlive_the_timeout_cleanup(
    tmp_path: Path, build_script: Callable[..., str]
) -> None:
    """The finding: killing only the session leader leaves the tool running.

    ``subprocess.run`` signals just the process object, so the backgrounded
    descendant here would survive and write ``evidence`` long after the caller
    had snapshotted and reset the attempt. The TERM-resistant variant covers the
    follow-on: cleanup that stops at pipe EOF also leaves it running, because
    the descendant has already dropped the pipes it was judged by.
    """
    pid_file = tmp_path / "grandchild.pid"
    evidence = tmp_path / "completed.txt"

    with pytest.raises(subprocess.TimeoutExpired):
        run_in_process_group(
            ["/bin/sh", "-c", build_script(pid_file=pid_file, evidence=evidence)],
            cwd=tmp_path,
            timeout=3,
            terminate_grace_seconds=_TERMINATE_GRACE,
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


@_GRANDCHILD_SCRIPTS
def test_a_surviving_grandchild_cannot_supply_the_next_attempt_s_evidence(
    tmp_path: Path, build_script: Callable[..., str]
) -> None:
    """End-to-end: the retry owner over the real runner rejects the stale path.

    Attempt 1 spawns a descendant that would create the expected path, then
    times out. Attempt 2 returns immediately without doing any work. With the
    process tree provably dead and the attempt-owned outputs reset, nothing can
    present that run as complete evidence.
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
                    build_script(pid_file=pid_file, evidence=expected),
                ],
                cwd=tmp_path,
                timeout=3,
                terminate_grace_seconds=_TERMINATE_GRACE,
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


def test_sigkill_is_sent_even_when_the_pipes_have_already_gone_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escalation is unconditional, not conditional on the group looking alive.

    Here the descendant dies on SIGTERM, so the pipes reach EOF during the
    courtesy window and cleanup *could* return without escalating. It must not:
    quiet pipes are not evidence of an empty group, and the check that would
    tell them apart is not available once the leader has been reaped. Deleting
    the SIGKILL must fail a test, and this is that test.
    """
    real_killpg = os.killpg
    sent: list[int] = []

    def record(pgid: int, sig: int) -> None:
        sent.append(sig)
        real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", record)

    with pytest.raises(subprocess.TimeoutExpired):
        run_in_process_group(
            [
                "/bin/sh",
                "-c",
                _grandchild_script(
                    pid_file=tmp_path / "grandchild.pid",
                    evidence=tmp_path / "completed.txt",
                ),
            ],
            cwd=tmp_path,
            timeout=2,
            terminate_grace_seconds=_TERMINATE_GRACE,
        )

    assert signal.SIGKILL in sent, (
        "cleanup stopped at SIGTERM once the pipes closed; a descendant that "
        "ignores SIGTERM and drops the pipes would have survived"
    )


def test_a_descendant_that_survives_sigkill_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup that cannot finish must raise, not hand back a live filesystem.

    SIGKILL is undeliverable-proof in reality, so the only way to reach this
    branch is to simulate a kill that does not take. The descendant keeps the
    pipes open, so the drain cannot complete and the run is rejected rather than
    reported as a cleanly killed attempt.
    """
    real_killpg = os.killpg
    signalled: list[int] = []

    def drop_sigkill(pgid: int, sig: int) -> None:
        signalled.append(pgid)
        if sig == signal.SIGKILL:
            return
        real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", drop_sigkill)

    # Ignores SIGTERM and holds the inherited pipes, so nothing reaches EOF.
    script = (
        f'sh -c \'trap "" TERM; i=0; while [ $i -lt {int(_BLOCK_SECONDS / 0.1)} ]; '
        "do sleep 0.1; i=$((i+1)); done' & "
        f"sleep {_BLOCK_SECONDS}"
    )

    with pytest.raises(ProcessGroupCleanupError) as excinfo:
        run_in_process_group(
            ["/bin/sh", "-c", script],
            cwd=tmp_path,
            timeout=1,
            terminate_grace_seconds=_TERMINATE_GRACE,
            kill_grace_seconds=0.5,
        )

    assert "cannot be trusted" in str(excinfo.value)

    # The kill was simulated away, so clean up for real.
    monkeypatch.undo()
    for pgid in signalled:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
