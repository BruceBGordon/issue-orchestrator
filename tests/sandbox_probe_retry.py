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

BREACHES ARE DECLARED TO THE OWNER, NOT CHECKED BY THE CALLER. Retrying is the
one thing in this module that can destroy evidence: the reset clears the
previous attempt's output, and a probe's result sinks are frequently the very
files that *record* a breach — a secret-read sink holds the secret when the
read was allowed, a status sink holds ``READABLE`` or ``OPENED``. If such a path
is reset without having been captured, a leaking attempt that then times out
is erased and a clean retry reports a pass over the top of it.

Two structural rules close that hole, and both live here rather than at the call
sites, because the call sites are where it was got wrong:

- :class:`BreachCheck` names the paths it inspects, and
  :func:`run_until_evidence` snapshots exactly those paths on every attempt.
  A breach path therefore cannot be left out of the capture set — declaring the
  check IS declaring the capture.
- :meth:`ProbeRun.require_intact` evaluates every attempt's snapshot BEFORE the
  completion requirement. A breach recorded by an attempt that later timed out
  is reported as a breach, never masked by the timeout failure. The ordering is
  not a caller's choice; there is one entry point and it owns the order.
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


class ProbeBreach(AssertionError):
    """Raised when any attempt's snapshot shows the sandbox boundary gave way.

    Subclasses ``AssertionError`` for the same reason as :class:`ProbeTimeout`.
    Raised in preference to a timeout: a run that both leaked and timed out
    leaked, and that is the finding worth reporting.
    """


@runtime_checkable
class BreachCheck(Protocol):
    """One thing that must never be true of an attempt's on-disk evidence.

    ``paths`` is what the check needs captured. :func:`run_until_evidence`
    snapshots exactly these, so a breach path cannot be silently left out of
    the capture set the way it can when the two are declared separately.
    """

    @property
    def paths(self) -> tuple[Path, ...]:
        """Paths this check inspects, snapshotted after every attempt."""

    def violated_by(self, snapshot: Mapping[Path, bytes | None]) -> str | None:
        """Describe the breach this snapshot shows, or ``None`` if intact."""


@dataclass(frozen=True)
class AbsentPath:
    """Breach: ``path`` exists at all. Its absence IS the security property."""

    path: Path
    detail: str

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.path,)

    def violated_by(self, snapshot: Mapping[Path, bytes | None]) -> str | None:
        if snapshot[self.path] is None:
            return None
        return f"{self.detail} ({self.path})"


@dataclass(frozen=True)
class AbsentContent:
    """Breach: ``marker`` appears in ``path``.

    A missing file cannot leak, so absence satisfies this check — presence of
    the sink is the caller's evidence condition, not a breach.
    """

    path: Path
    marker: bytes
    detail: str

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.path,)

    def violated_by(self, snapshot: Mapping[Path, bytes | None]) -> str | None:
        if self.marker not in (snapshot[self.path] or b""):
            return None
        return f"{self.detail} ({self.path})"


@dataclass(frozen=True)
class PresentContent:
    """Breach: ``marker`` is missing from ``path``.

    For planted fixture content whose survival is the property — a policy-file
    marker the agent must not have been able to overwrite. A deleted file is a
    violation: the marker is gone either way.
    """

    path: Path
    marker: bytes
    detail: str

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.path,)

    def violated_by(self, snapshot: Mapping[Path, bytes | None]) -> str | None:
        if self.marker in (snapshot[self.path] or b""):
            return None
        return f"{self.detail} ({self.path})"


@dataclass(frozen=True)
class UnchangedBytes:
    """Breach: ``path`` differs from the bytes captured before the run.

    Stronger than :class:`PresentContent` where the whole file must survive
    untouched — shared Git config and a base branch ref are modified by
    appending as readily as by overwriting.
    """

    path: Path
    expected: bytes
    detail: str

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.path,)

    def violated_by(self, snapshot: Mapping[Path, bytes | None]) -> str | None:
        if snapshot[self.path] == self.expected:
            return None
        return f"{self.detail} ({self.path})"


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

    def __post_init__(self) -> None:
        # An empty requirement is satisfied by every attempt, including one that
        # did nothing — the same ``all([])`` hole this module exists to close,
        # one refactor away. There is no probe that legitimately requires
        # nothing, so refuse at construction rather than accept vacuously.
        if not self.paths:
            raise ValueError(
                "CreatedPaths requires at least one path; an empty requirement "
                "accepts an attempt that produced nothing"
            )

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

    def __post_init__(self) -> None:
        # Same vacuity guard as CreatedPaths: a conjunction of nothing is true.
        if not self.parts:
            raise ValueError(
                "AllEvidence requires at least one part; a conjunction of no "
                "parts accepts an attempt that produced nothing"
            )

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
    breach_checks: tuple[BreachCheck, ...] = ()

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

    def require_intact(self) -> None:
        """Report any attempt's breach, THEN require the run to have completed.

        This is the entry point a live probe calls. It exists so the ordering
        is the owner's and not the caller's: two call sites had it backwards
        and a run that leaked on its final attempt and then timed out reported
        the timeout, burying the leak. Snapshots are evaluated oldest-first, so
        evidence from an attempt whose output was later reset — or which was
        killed mid-run — is still what gets reported.

        A probe with no on-disk breach surface declares that by passing no
        breach checks; the absence is then a visible decision in the call
        rather than a line someone forgot to write.
        """
        for attempt in self.attempts:
            for check in self.breach_checks:
                reason = check.violated_by(attempt.snapshot)
                if reason is not None:
                    raise ProbeBreach(
                        f"SANDBOX BREACH on attempt {attempt.number} of "
                        f"{len(self.attempts)}: {reason}"
                    )
        self.require_completed()

    def require_completed(self) -> None:
        """Fail loudly if the run never produced a non-timed-out attempt.

        Prefer :meth:`require_intact`, which runs this after the breach checks.
        Calling it directly is correct only for a probe that declares no breach
        checks at all; with checks declared it would report a timeout ahead of
        a leak.

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
    breach_checks: Sequence[BreachCheck] = (),
    observed_paths: Sequence[Path] = (),
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
        breach_checks: What must never be true of an attempt's on-disk
            evidence. Their paths are snapshotted automatically, which is the
            point: a breach path cannot be omitted from the capture set while
            the check that reads it is still declared. Evaluated by
            :meth:`ProbeRun.require_intact`.
        observed_paths: Extra paths to snapshot for diagnostics, beyond what
            the breach checks already cover. Snapshotting never clears; a path
            being cleared by ``evidence.reset()`` is exactly why the capture
            has to happen first.
        max_attempts: How many invocations to allow.

    Returns:
        A :class:`ProbeRun`. Callers must call
        :meth:`ProbeRun.require_intact` before treating the run as evidence.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    # The union, deduplicated and order-stable: every breach path is captured
    # whether or not the caller also listed it, so the two declarations cannot
    # drift apart.
    captured = tuple(
        dict.fromkeys(
            [path for check in breach_checks for path in check.paths]
            + list(observed_paths)
        )
    )
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
        # Snapshot BEFORE anything can be reset. The next iteration's
        # ``evidence.reset()`` may delete a path that recorded a breach on this
        # attempt; capturing here is what keeps that evidence recoverable.
        #
        # A timed-out attempt never counts, even when its evidence is present:
        # it was killed mid-run, so its side effects prove nothing.
        attempt = ProbeAttempt(
            number=number,
            result=result,
            timed_out=timed_out,
            snapshot=_snapshot(captured),
            missing_evidence=evidence.missing_from(result),
        )
        attempts.append(attempt)
        if attempt.is_complete_evidence:
            break
    return ProbeRun(attempts=tuple(attempts), breach_checks=tuple(breach_checks))
