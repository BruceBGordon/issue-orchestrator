"""Retry policy for the live sandbox boundary probes.

The OS-boundary probes in ``tests/integration/test_sandbox_os_boundary.py``
drive a real agent CLI, so under a loaded parallel run an attempt can blow its
deadline without the sandbox having misbehaved. Retrying once is legitimate;
*forgetting that an attempt timed out* is not — a timed-out attempt may have
created some or all of the expected result files before it was killed, and
treating "the files exist" as success would let a half-executed security probe
report a pass.

This module owns that distinction so no probe can accidentally re-derive it:

- A timed-out attempt **never** satisfies the success condition, even when
  every expected path is already present on disk.
- Every attempt's output and on-disk evidence is captured, so the caller's
  breach assertions still run against a run that ultimately timed out.
- Exhausting the retries on a timeout fails loudly via
  :meth:`ProbeRun.require_completed`, restoring the pre-retry behaviour where
  a ``TimeoutExpired`` aborted the test.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# Conventional shell exit code for "killed by a timeout". Synthesised for a
# timed-out attempt so callers can print a returncode without pretending the
# process exited on its own.
TIMEOUT_RETURNCODE = 124


class ProbeTimeout(AssertionError):
    """Raised when every probe attempt timed out.

    Subclasses ``AssertionError`` so pytest reports it as a test failure with
    the captured evidence rather than an infrastructure error.
    """


def decode_stream(stream: str | bytes | None) -> str:
    """Normalise a subprocess stream (``str``, ``bytes``, or ``None``) to text."""
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


@dataclass(frozen=True)
class ProbeAttempt:
    """One probe invocation: what it printed, whether it timed out, what it left."""

    number: int
    result: subprocess.CompletedProcess[str]
    timed_out: bool
    snapshot: Mapping[Path, bytes | None]

    @property
    def combined_output(self) -> str:
        return (self.result.stdout or "") + (self.result.stderr or "")


@dataclass(frozen=True)
class ProbeRun:
    """The outcome of retrying a probe until it completed or ran out of tries."""

    attempts: tuple[ProbeAttempt, ...]

    @property
    def result(self) -> subprocess.CompletedProcess[str]:
        """The final attempt's process result."""
        return self.attempts[-1].result

    @property
    def timed_out(self) -> bool:
        """Whether the run ended on a timeout (so its evidence is incomplete)."""
        return self.attempts[-1].timed_out

    @property
    def snapshots(self) -> tuple[Mapping[Path, bytes | None], ...]:
        """Per-attempt on-disk evidence, oldest first."""
        return tuple(attempt.snapshot for attempt in self.attempts)

    @property
    def combined_output(self) -> str:
        return "\n".join(
            f"attempt {attempt.number}:\n{attempt.combined_output}"
            for attempt in self.attempts
        )

    def require_completed(self) -> None:
        """Fail loudly if the run never produced a non-timed-out attempt.

        Call this *after* asserting on :attr:`snapshots`, so a breach captured
        by a timed-out attempt is still reported as a breach rather than being
        masked by the timeout failure.
        """
        if not self.timed_out:
            return
        raise ProbeTimeout(
            f"the sandbox boundary probe timed out on all {len(self.attempts)} "
            "attempt(s); the boundary was never fully exercised, so this run "
            f"proves nothing.\noutput:\n{self.combined_output[:2000]}"
        )


def _snapshot(observed_paths: Sequence[Path]) -> dict[Path, bytes | None]:
    return {
        path: path.read_bytes() if path.exists() else None for path in observed_paths
    }


def _timed_out_result(
    exc: subprocess.TimeoutExpired,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=exc.cmd,
        returncode=TIMEOUT_RETURNCODE,
        stdout=decode_stream(exc.stdout),
        stderr=f"probe timed out after {exc.timeout}s\n{decode_stream(exc.stderr)}",
    )


def run_until_paths_created(
    run_attempt: Callable[[], subprocess.CompletedProcess[str]],
    *,
    expected_paths: Sequence[Path],
    observed_paths: Sequence[Path],
    max_attempts: int = 2,
) -> ProbeRun:
    """Retry ``run_attempt`` until it completes and created ``expected_paths``.

    Args:
        run_attempt: Runs one probe invocation. It may raise
            ``subprocess.TimeoutExpired``; that is recorded as a timed-out
            attempt rather than aborting the retry.
        expected_paths: Paths a *completed* attempt must have created.
        observed_paths: Paths to snapshot after every attempt, so breach
            assertions can inspect what each attempt left behind.
        max_attempts: How many invocations to allow.

    Returns:
        A :class:`ProbeRun`. Callers must call
        :meth:`ProbeRun.require_completed` before treating the run as evidence.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    attempts: list[ProbeAttempt] = []
    for number in range(1, max_attempts + 1):
        timed_out = False
        try:
            result = run_attempt()
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            result = _timed_out_result(exc)
        attempts.append(
            ProbeAttempt(
                number=number,
                result=result,
                timed_out=timed_out,
                snapshot=_snapshot(observed_paths),
            )
        )
        # A timed-out attempt can leave every expected path behind — it was
        # killed mid-run, not finished — so it never counts as success.
        if not timed_out and all(path.exists() for path in expected_paths):
            break
    return ProbeRun(attempts=tuple(attempts))
