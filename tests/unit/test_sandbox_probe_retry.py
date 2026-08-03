"""Deterministic coverage for the live sandbox probe retry policy.

The probes themselves need a real agent CLI, but their retry/timeout policy
is the part that can silently turn a half-executed security probe into a pass.
That policy lives in ``tests/sandbox_probe_retry`` precisely so it can be
exercised here with no subprocess at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.sandbox_probe_retry import (
    TIMEOUT_RETURNCODE,
    ProbeTimeout,
    run_until_paths_created,
)


def _completed(stdout: str = "ok") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["sandbox-probe"], 0, stdout=stdout, stderr="")


def _timeout(*, output: bytes = b"partial", stderr: bytes = b"err") -> Exception:
    return subprocess.TimeoutExpired(
        cmd=["sandbox-probe"], timeout=1, output=output, stderr=stderr
    )


def test_snapshots_preserve_first_attempt_breach_before_later_overwrite(
    tmp_path: Path,
) -> None:
    """A breach seen only on attempt 1 must survive attempt 2 overwriting it."""
    network_status = tmp_path / "network-status.txt"
    completed = tmp_path / "completed.txt"
    attempts = 0

    def run_attempt() -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        network_status.write_text(
            "OPENED" if attempts == 1 else "CLOSED", encoding="utf-8"
        )
        if attempts == 2:
            completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_paths_created(
        run_attempt,
        expected_paths=(completed,),
        observed_paths=(network_status,),
    )

    assert [snapshot[network_status] for snapshot in probe.snapshots] == [
        b"OPENED",
        b"CLOSED",
    ]
    probe.require_completed()


def test_timeout_then_success_retries_and_completes(tmp_path: Path) -> None:
    """A first-attempt timeout is retried, and the completed retry wins."""
    completed = tmp_path / "completed.txt"
    attempts = 0

    def run_attempt() -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _timeout()
        completed.write_text("done", encoding="utf-8")
        return _completed("second attempt ok")

    probe = run_until_paths_created(
        run_attempt,
        expected_paths=(completed,),
        observed_paths=(completed,),
    )

    assert attempts == 2
    assert not probe.timed_out
    assert probe.result.stdout == "second attempt ok"
    probe.require_completed()  # must not raise
    # The timed-out attempt's evidence is still reported.
    assert "probe timed out after 1s" in probe.combined_output


def test_timed_out_attempt_with_all_paths_present_is_not_success(
    tmp_path: Path,
) -> None:
    """A killed attempt that already created every path must not stop the retry.

    This is the false-pass the retry originally introduced: the probe was
    killed mid-run, so its side effects prove nothing about the boundary even
    though every expected file exists.
    """
    completed = tmp_path / "completed.txt"
    attempts = 0

    def run_attempt() -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        completed.write_text("done", encoding="utf-8")
        if attempts == 1:
            raise _timeout()
        return _completed()

    probe = run_until_paths_created(
        run_attempt,
        expected_paths=(completed,),
        observed_paths=(completed,),
    )

    assert attempts == 2, "a timed-out attempt must never satisfy the success check"
    assert not probe.timed_out
    probe.require_completed()


def test_two_timeouts_exhaust_and_fail_loudly(tmp_path: Path) -> None:
    """Exhausting the retries on timeouts fails, even with every path present."""
    completed = tmp_path / "completed.txt"
    attempts = 0

    def run_attempt() -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        completed.write_text("done", encoding="utf-8")
        raise _timeout()

    probe = run_until_paths_created(
        run_attempt,
        expected_paths=(completed,),
        observed_paths=(completed,),
    )

    assert attempts == 2
    assert probe.timed_out
    assert probe.result.returncode == TIMEOUT_RETURNCODE

    with pytest.raises(ProbeTimeout) as excinfo:
        probe.require_completed()

    message = str(excinfo.value)
    assert "timed out on all 2 attempt(s)" in message
    # The captured evidence must survive into the failure report.
    assert "probe timed out after 1s" in message
    assert "partial" in message


def test_exhausted_run_still_exposes_every_attempt_snapshot(tmp_path: Path) -> None:
    """Breach evidence from timed-out attempts is still available to assert on."""
    escaped = tmp_path / "escaped.txt"
    completed = tmp_path / "completed.txt"

    def run_attempt() -> subprocess.CompletedProcess[str]:
        escaped.write_text("ESCAPED", encoding="utf-8")
        raise _timeout()

    probe = run_until_paths_created(
        run_attempt,
        expected_paths=(completed,),
        observed_paths=(escaped,),
    )

    assert [snapshot[escaped] for snapshot in probe.snapshots] == [
        b"ESCAPED",
        b"ESCAPED",
    ]


def test_missing_expected_paths_without_timeout_does_not_raise(tmp_path: Path) -> None:
    """A completed-but-incomplete run is the caller's assertion to make.

    ``require_completed`` only guards the timeout case; "the probe ran but did
    not produce its files" is reported by the caller's own positive-control
    assertion, which carries a far more specific message.
    """
    completed = tmp_path / "completed.txt"

    probe = run_until_paths_created(
        lambda: _completed(),
        expected_paths=(completed,),
        observed_paths=(completed,),
    )

    assert not probe.timed_out
    assert probe.snapshots == ({completed: None}, {completed: None})
    probe.require_completed()
