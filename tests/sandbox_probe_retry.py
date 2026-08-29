"""Retry policy for the live sandbox boundary probes.

The OS-boundary probes in ``tests/integration/test_sandbox_os_boundary.py``
drive a real agent CLI, so an attempt can end without having exercised the
boundary: it can blow its deadline under a loaded parallel run, or it can
return cleanly having declined to issue the tool call the probe is built
around. Retrying such an attempt is legitimate; *forgetting that the attempt
proved nothing* is not — a killed or short-circuited attempt may leave every
expected result file in place, and treating "the files exist" as success would
let a half-executed security probe report a pass.

This module owns that distinction so no probe can accidentally re-derive it:

- An attempt counts only when it ran to completion **and** produced the
  evidence the caller declared. A timed-out attempt **never** counts, even when
  every expected path is already present on disk.
- Every attempt's output and on-disk evidence is captured, so the caller's
  breach assertions still run against a run that ultimately failed.
- Exhausting the retries on a timeout fails loudly via
  :meth:`ProbeRun.require_completed`, restoring the pre-retry behaviour where
  a ``TimeoutExpired`` aborted the test.

WHAT COUNTS AS EVIDENCE IS THE CALLER'S DECLARATION, NOT A FIXED RULE. Probes
differ in where their evidence lands: the multi-command probe writes result
files, while the native-tool probes prove themselves through the CLI's
``--output-format stream-json`` event stream and write nothing at all. Both are
"did this attempt actually exercise the boundary?", so both belong to the same
retry owner; expressing the condition as :class:`ProbeEvidence` is what lets
them share it. Before that, the condition was hardcoded to path existence and
the native-tool probes could not use this module — so they ran with no retry at
all, and a single declined tool call failed the gate.

EVIDENCE IS NOT THE SECURITY ASSERTION. ``ProbeEvidence`` answers only "is this
attempt usable as evidence" — e.g. "the agent did attempt the write". Whether
the sandbox then *denied* that write is the caller's assertion, and it must stay
a hard failure: an attempt that was made and NOT denied is a breach, never a
retry.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

# Conventional shell exit code for "killed by a timeout". Synthesised for a
# timed-out attempt so callers can print a returncode without pretending the
# process exited on its own.
TIMEOUT_RETURNCODE = 124


class ProbeTimeout(AssertionError):
    """Raised when every probe attempt timed out.

    Subclasses ``AssertionError`` so pytest reports it as a test failure with
    the captured evidence rather than an infrastructure error.
    """


@runtime_checkable
class ProbeEvidence(Protocol):
    """What one attempt must produce before its result may be trusted."""

    def reset(self) -> None:
        """Discard evidence a previous attempt left, before a retry runs.

        Implementations must clear ONLY what the probe itself produces. Planted
        fixture files and breach markers must survive: the caller asserts on
        their final state after the run, and clearing a breach marker would
        erase the very thing the run exists to detect.
        """

    def missing_from(self, result: subprocess.CompletedProcess[str]) -> str | None:
        """Say why this attempt is unusable, or ``None`` when it is complete."""


@dataclass(frozen=True)
class CreatedPaths:
    """Evidence: the probe's own result sinks all exist.

    ``paths`` are attempt-owned outputs, deleted before each retry so an
    accepted attempt must have created every one of them itself. Without that,
    a killed attempt's leftovers would satisfy the check for a retry that exited
    normally without redoing the work.
    """

    paths: tuple[Path, ...]

    def reset(self) -> None:
        for path in self.paths:
            path.unlink(missing_ok=True)

    def missing_from(self, result: subprocess.CompletedProcess[str]) -> str | None:
        absent = [str(path) for path in self.paths if not path.exists()]
        if not absent:
            return None
        return "the probe did not create: " + ", ".join(absent)


@dataclass(frozen=True)
class AllEvidence:
    """Evidence: every part holds. Reports the first unmet part."""

    parts: tuple[ProbeEvidence, ...]

    def reset(self) -> None:
        for part in self.parts:
            part.reset()

    def missing_from(self, result: subprocess.CompletedProcess[str]) -> str | None:
        for part in self.parts:
            reason = part.missing_from(result)
            if reason is not None:
                return reason
        return None


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
    missing_evidence: str | None

    @property
    def combined_output(self) -> str:
        return (self.result.stdout or "") + (self.result.stderr or "")

    @property
    def produced_required_evidence(self) -> bool:
        """Whether this attempt produced everything the caller declared."""
        return self.missing_evidence is None

    @property
    def is_complete_evidence(self) -> bool:
        """Whether this attempt alone ran to completion and produced its evidence."""
        return not self.timed_out and self.produced_required_evidence


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

    @property
    def missing_evidence(self) -> str | None:
        """Why the final attempt is unusable, or ``None`` when it is complete."""
        return self.attempts[-1].missing_evidence

    @property
    def completed_attempt(self) -> ProbeAttempt | None:
        """The single attempt whose own evidence the caller may rely on.

        ``None`` when no attempt both ran to completion and produced its
        evidence. Because evidence is reset before each retry, this is never
        satisfied by what a killed attempt left behind.
        """
        for attempt in self.attempts:
            if attempt.is_complete_evidence:
                return attempt
        return None

    def require_completed(self) -> None:
        """Fail loudly if the run never produced a non-timed-out attempt.

        Call this *after* asserting on :attr:`snapshots`, so a breach captured
        by a timed-out attempt is still reported as a breach rather than being
        masked by the timeout failure.

        This guards the timeout case only. "The probe completed but produced
        nothing" is left to the caller's own positive-control assertion, which
        carries a far more specific message — and which cannot be satisfied by
        a stale artifact, since the evidence is reset before each retry.
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


def run_until_evidence(
    run_attempt: Callable[[], subprocess.CompletedProcess[str]],
    *,
    evidence: ProbeEvidence,
    observed_paths: Sequence[Path],
    max_attempts: int = 2,
) -> ProbeRun:
    """Retry ``run_attempt`` until one attempt completes and produces ``evidence``.

    Success evidence is isolated per attempt: ``evidence.reset()`` runs before
    each retry, so the accepted attempt must have produced it itself. Without
    that, a killed attempt's leftovers would satisfy the check for a retry that
    exited normally without redoing the work.

    Args:
        run_attempt: Runs one probe invocation. It may raise
            ``subprocess.TimeoutExpired``; that is recorded as a timed-out
            attempt rather than aborting the retry.
        evidence: What a *completed* attempt must have produced. Declares both
            how to recognise it and how to clear the previous attempt's copy.
        observed_paths: Paths to snapshot after every attempt, so breach
            assertions can inspect what each attempt left behind. These are
            never cleared.
        max_attempts: How many invocations to allow.

    Returns:
        A :class:`ProbeRun`. Callers must call
        :meth:`ProbeRun.require_completed` before treating the run as evidence.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    attempts: list[ProbeAttempt] = []
    for number in range(1, max_attempts + 1):
        if number > 1:
            # Clear before the retry, after the previous attempt's snapshot was
            # taken — the breach evidence is preserved, the success evidence is
            # not inherited.
            evidence.reset()
        timed_out = False
        try:
            result = run_attempt()
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            result = _timed_out_result(exc)
        # A timed-out attempt never counts, even when its evidence is present:
        # it was killed mid-run, so its side effects prove nothing.
        attempt = ProbeAttempt(
            number=number,
            result=result,
            timed_out=timed_out,
            snapshot=_snapshot(observed_paths),
            missing_evidence=evidence.missing_from(result),
        )
        attempts.append(attempt)
        if attempt.is_complete_evidence:
            break
    return ProbeRun(attempts=tuple(attempts))
