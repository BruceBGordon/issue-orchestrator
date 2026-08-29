"""The one owner of review-exchange kill evidence.

When a round dies, the artifact that settles what the screen looked like — the
role's terminal recording — lives in a home that deletes itself: a pytest tmp
directory the async E2E runner rotates within the hour, or a worktree the
orchestrator tears down. A pointer into either is a self-deleting record
(#7128). On 2026-08-28 a failed live-codex lane's recording was gone before
anyone could decode it, and two silent stalls were misattributed twice before a
surviving decode showed the prompt stranded in the composer.

A round can die two ways, and this module owns **both** so they cannot drift:

``capture_declared_failure``
    The inner round loop caught a typed round failure and is about to report
    it. Called from ``persistent_session_exchange._send_role_round``.

``capture_abandoned_rounds``
    The round never got to declare anything: a supervisor wall-clock deadline
    or an operator cancellation is tearing the whole exchange down while a
    worker is still wedged inside ``send_round``. Called from
    ``control.review_exchange_lifecycle.cancel_issue_review_exchange``
    *before* the pair is released, because releasing it destroys the very
    recording we came for. This is the case the supervisor exists for and the
    case the real incidents were (#7141 finding 2).

The second entry works because every round registers a typed
:class:`RoundIdentity` with :meth:`round_started` and clears it with
:meth:`round_finished`, so at any instant the owner knows exactly which rounds
are in flight and where their evidence lives.

Each capture writes, under a retained root anchored on the *shared git dir* so
it outlives both the run directory and the worktree::

    <repo>/.issue-orchestrator/diagnostics/exchange-kills/
      index.jsonl
      <ts>__issue-<n>__<role>__round-R-attempt-A-respawn-K/
        terminal-recording.jsonl   copy of the recording (tail-capped)
        idle-trace.json            window config + bytes_drained trajectory
        run-identity.json          branch, HEAD SHA, session, run/exchange dirs

plus a back-pointer next to the turn's ``result.json``, so the cross-reference
runs both ways and correlation never needs mtime archaeology.

**Failure atomicity.** A capture stages into a hidden ``.part`` directory and
renames it to its final name only once every artifact is committed, so a
half-written capture can never masquerade as a complete one. The index line is
appended only after that rename, and a trailing partial line left by a short
write is repaired before appending rather than being followed (#7141 finding 3).

**Deliberate exception to fail-fast.** Neither capture entry raises. They sit
on the failure path of the thing operators actually care about; a diagnostics
bug that propagated from here would replace a real, diagnosable round failure
with a confusing traceback from the evidence collector — destroying the very
signal it exists to preserve. The boundary is the same one ``EventSink`` draws
for fire-and-forget trace events: observability may degrade loudly
(``logger.exception``) but must never change control flow.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Hashable
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..domain.exchange_kill_evidence import (
    ComposerStateVerdict,
    RoundIdleTrace,
    undetermined_composer_state,
)
from ..domain.review_exchange_failures import (
    RoundFailureReason,
    round_failure_reason_value,
)
from .exchange_kill_artifacts import (
    IDLE_TRACE_FILENAME,
    INDEX_FILENAME,
    RECORDING_COPY_FILENAME,
    RUN_IDENTITY_FILENAME,
    STAGING_SUFFIX,
    CaptureBudget,
    CapturedKillEvidence,
    RecordingCopy,
    RoundIdentity,
    RoundKillFacts,
    RoundTicket,
    back_reference_path,
    back_reference_payload,
    discard_empty_claim,
    identity_payload,
    idle_payload,
    record_index_line,
    remove_tree,
    stream_copy,
    write_json,
)
from ..infra.validation_timings import resolve_git_common_dir
from .composer_state import DEFAULT_REPLAY_BYTES, classify_composer_state

logger = logging.getLogger(__name__)


_ORCHESTRATOR_DIRNAME = ".issue-orchestrator"
_DIAGNOSTICS_DIRNAME = "diagnostics"
_KILL_EVIDENCE_DIRNAME = "exchange-kills"

# Copy cap. Past this the *tail* is kept: the failure screen is at the end of
# the file, and an unbounded copy on the failure path could fill the disk the
# orchestrator itself needs.
_DEFAULT_MAX_COPY_BYTES = 64 * 1024 * 1024
# Linear probe depth for the same-second name collision. Generous because
# the probe is a cheap mkdir and the cap has to exceed any plausible burst
# of concurrent captures sharing one timestamp and turn slug.
_MAX_DIRECTORY_ATTEMPTS = 256
# Wall-clock ceiling for one capture. The teardown path runs under the pair
# registry's lock, so this is what stops a stalled disk from wedging the whole
# registry; generous enough that a healthy tail-capped copy never trips it.
_DEFAULT_CAPTURE_BUDGET_SECONDS = 30.0

RetainedRootResolver = Callable[[Path], Path | None]




# ---------------------------------------------------------------------------
# Retained root
# ---------------------------------------------------------------------------


def resolve_retained_diagnostics_root(worktree: Path) -> Path | None:
    """Return the repository-level retained home for kill evidence.

    Anchored on the *shared* git dir rather than the worktree so captures
    survive worktree teardown, and placed under the repository's
    ``.issue-orchestrator/diagnostics/`` convention so existing dirty-tree and
    cleanup filters already treat it as runtime metadata. ``None`` when the
    path is not a git worktree at all — the caller reports that loudly instead
    of writing into the volatile home this module exists to escape.
    """
    common_dir = resolve_git_common_dir(worktree)
    if common_dir is None:
        return None
    anchor = common_dir.parent if common_dir.name == ".git" else common_dir
    return anchor / _ORCHESTRATOR_DIRNAME / _DIAGNOSTICS_DIRNAME / _KILL_EVIDENCE_DIRNAME


# ---------------------------------------------------------------------------
# Typed round records
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Capture owner
# ---------------------------------------------------------------------------


class ExchangeKillEvidenceRecorder:
    """Retains round-failure evidence, from both the inner and outer kill paths.

    One instance per orchestrator process, built at the composition root and
    shared by the round loop and the lifecycle teardown so the two entries
    cannot drift. The in-flight registry is guarded by a lock because the round
    loop runs on a background job thread while teardown runs on the main tick.
    """

    def __init__(
        self,
        *,
        resolve_root: RetainedRootResolver = resolve_retained_diagnostics_root,
        max_copy_bytes: int = _DEFAULT_MAX_COPY_BYTES,
        replay_bytes: int = DEFAULT_REPLAY_BYTES,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        capture_budget_seconds: float = _DEFAULT_CAPTURE_BUDGET_SECONDS,
    ) -> None:
        self._resolve_root = resolve_root
        self._max_copy_bytes = max_copy_bytes
        self._replay_bytes = replay_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._capture_budget_seconds = capture_budget_seconds
        self._lock = threading.RLock()
        self._in_flight: dict[int, RoundTicket] = {}
        self._next_ticket_id = 1
        self._staging_seq = 0

    # -- in-flight registry ------------------------------------------------

    def round_started(self, identity: RoundIdentity) -> RoundTicket:
        """Register a round as in flight. Pair with :meth:`round_finished`."""
        with self._lock:
            ticket = RoundTicket(ticket_id=self._next_ticket_id, identity=identity)
            self._next_ticket_id += 1
            self._in_flight[ticket.ticket_id] = ticket
            return ticket

    def round_finished(self, ticket: RoundTicket) -> None:
        """Clear a registration without capturing. Idempotent."""
        self._claim(ticket)

    def _claim(self, ticket: RoundTicket) -> bool:
        """Take exclusive ownership of a round, once.

        The ticket is the arbitration token shared by both capture paths: the
        first path to claim it captures, the second finds it gone and does
        nothing. Without this the teardown and the unwinding worker both
        captured the same round, and the later (evidence-poor, sources already
        destroyed) copy won the back-reference (#7141 round 2 finding 2).
        """
        with self._lock:
            return self._in_flight.pop(ticket.ticket_id, None) is not None

    def in_flight_for(self, issue_key: Hashable) -> tuple[RoundIdentity, ...]:
        """Snapshot the rounds currently registered for one issue."""
        with self._lock:
            return tuple(
                ticket.identity
                for ticket in self._in_flight.values()
                if ticket.identity.issue_number == issue_key
            )

    # -- capture entries ---------------------------------------------------

    def capture_declared_failure(
        self,
        ticket: RoundTicket,
        *,
        failure_reason: str,
        error_text: str,
        idle_trace: RoundIdleTrace | None,
    ) -> CapturedKillEvidence | None:
        """Retain evidence for a round that declared its own failure.

        Claims ``ticket`` first, so a round the teardown already captured is
        not captured a second time. Returns ``None`` when the round was already
        retained elsewhere or the capture itself failed; the caller keeps
        reporting the real round failure either way.
        """
        if not self._claim(ticket):
            logger.info(
                "[kill-evidence] %s %s was already retained by the teardown path; "
                "skipping the duplicate capture",
                ticket.identity.issue_key,
                ticket.identity.turn_slug,
            )
            return None
        return self._capture_facts(
            RoundKillFacts(
                identity=ticket.identity,
                failure_reason=failure_reason,
                error_text=error_text,
                idle_trace=idle_trace,
            ),
            budget=self._new_budget(),
        )

    def _capture_facts(
        self, facts: RoundKillFacts, *, budget: "CaptureBudget"
    ) -> CapturedKillEvidence | None:
        try:
            return self._capture(facts, budget=budget)
        except Exception:
            # Observability boundary, not a swallowed bug: see module docstring.
            logger.exception(
                "[kill-evidence] capture failed for %s %s %s; the round failure "
                "itself is unaffected but its evidence was not retained",
                facts.identity.issue_key,
                facts.identity.role,
                facts.identity.turn_slug,
            )
            return None

    def capture_abandoned_rounds(
        self, issue_key: Hashable, *, reason: str
    ) -> tuple[Path, ...]:
        """Retain evidence for every round still in flight for one issue.

        The outer kill path: a supervisor deadline or operator cancellation is
        about to tear the exchange down while a worker may still be wedged
        inside ``send_round``. Must run *before* the pair is released, because
        the release destroys the recording. Never raises.
        """
        captured: list[Path] = []
        try:
            tickets = self._take_tickets(issue_key)
        except Exception:
            logger.exception(
                "[kill-evidence] could not read in-flight rounds for %s", issue_key
            )
            return ()
        budget = self._new_budget()
        abandoned: list[str] = []
        for ticket in tickets:
            if budget.expired():
                abandoned.append(ticket.identity.turn_slug)
                continue
            evidence = self._capture_facts(
                _abandoned_facts(ticket, reason=reason, now=self._monotonic),
                budget=budget,
            )
            if evidence is not None:
                captured.append(evidence.directory)
        if abandoned:
            logger.warning(
                "[kill-evidence] capture budget of %.1fs exhausted during teardown "
                "of %s; abandoned %d round(s) without retaining them: %s",
                self._capture_budget_seconds,
                issue_key,
                len(abandoned),
                ", ".join(abandoned),
            )
        if tickets:
            logger.warning(
                "[kill-evidence] exchange teardown abandoned %d in-flight round(s) "
                "for %s (reason=%s); retained %d capture(s)",
                len(tickets),
                issue_key,
                reason,
                len(captured),
            )
        return tuple(captured)

    def _take_tickets(self, issue_key: Hashable) -> tuple[RoundTicket, ...]:
        """Remove and return the in-flight tickets for one issue.

        Removing under the lock makes the capture single-shot: a second
        teardown for the same issue cannot re-capture the same round, and the
        wedged worker's own ``round_finished`` stays a no-op.
        """
        with self._lock:
            matched = [
                ticket
                for ticket in self._in_flight.values()
                if ticket.identity.issue_number == issue_key
            ]
            for ticket in matched:
                self._in_flight.pop(ticket.ticket_id, None)
            return tuple(matched)

    # -- the capture itself ------------------------------------------------

    def _new_budget(self) -> CaptureBudget:
        return CaptureBudget.starting_now(
            seconds=self._capture_budget_seconds, now=self._monotonic
        )

    def _capture(
        self, facts: RoundKillFacts, *, budget: CaptureBudget
    ) -> CapturedKillEvidence:
        identity = facts.identity
        root = self._resolve_root(identity.worktree)
        if root is None:
            raise RuntimeError(
                f"no shared git dir under {identity.worktree}; refusing to write "
                "kill evidence into a volatile home"
            )
        captured_at = self._clock()
        final, staging = self._allocate_directory(root, identity, captured_at)
        try:
            evidence = self._commit(
                facts,
                staging=staging,
                final=final,
                captured_at=captured_at,
                budget=budget,
            )
            # The rename is the commit point. POSIX replaces the empty claimed
            # directory atomically, so the final name goes from empty straight
            # to complete with no partial state in between.
            os.rename(staging, final)
        except BaseException:
            remove_tree(staging)
            discard_empty_claim(final)
            raise
        identity_payload = json.loads(
            (final / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        write_json(
            back_reference_path(identity), back_reference_payload(facts, identity_payload)
        )
        record_index_line(root / INDEX_FILENAME, identity_payload)
        logger.warning(
            "[kill-evidence] retained %s %s %s reason=%s composer_state=%s marker=%s "
            "recording_bytes=%d truncated=%s at %s",
            identity.issue_key,
            identity.role,
            identity.turn_slug,
            facts.failure_reason,
            evidence.composer.state.value,
            evidence.composer.matched_marker,
            evidence.recording_bytes_copied,
            evidence.recording_truncated,
            final,
        )
        return CapturedKillEvidence(
            directory=final,
            composer=evidence.composer,
            recording_bytes_copied=evidence.recording_bytes_copied,
            recording_truncated=evidence.recording_truncated,
        )

    def _commit(
        self,
        facts: RoundKillFacts,
        *,
        staging: Path,
        final: Path,
        captured_at: datetime,
        budget: CaptureBudget,
    ) -> CapturedKillEvidence:
        """Write every artifact into the staging directory.

        A corrupt or vanished recording must not cost us the other two
        artifacts — identity and the idle trace are independently useful — so
        both steps below degrade into a recorded error rather than aborting.
        Anything else raises and the staging directory is discarded whole.
        """
        composer = (
            undetermined_composer_state(
                "capture budget exhausted before the recording could be replayed"
            )
            if budget.expired()
            else self._classify(facts.identity)
        )
        copy = self._copy_recording_or_note_why_not(
            facts.identity.recording_path,
            staging / RECORDING_COPY_FILENAME,
            budget=budget,
        )
        payload = identity_payload(
            facts,
            directory=final,
            captured_at=captured_at,
            composer=composer,
            copy=copy,
        )
        write_json(staging / RUN_IDENTITY_FILENAME, payload)
        write_json(staging / IDLE_TRACE_FILENAME, idle_payload(facts))
        return CapturedKillEvidence(
            directory=staging,
            composer=composer,
            recording_bytes_copied=copy.copied_bytes,
            recording_truncated=copy.truncated,
        )

    def _allocate_directory(
        self, root: Path, identity: RoundIdentity, captured_at: datetime
    ) -> tuple[Path, Path]:
        """Claim a final name, and a staging directory unique to this capture.

        Creating the *final* directory is the claim, because ``mkdir`` is the
        only atomic "this name is mine" operation available: an ``exists()``
        pre-check races, and 64 concurrent captures in the same second proved
        it (two threads picked the same ordinal and the second one's rename hit
        "directory not empty"). Until the staged directory is renamed over it,
        that claim is an *empty* directory — never a partial set of artifacts,
        which is what finding 3 is actually about.

        The staging name carries a per-recorder sequence rather than deriving
        from the final name: a staging name derived from the final name is
        freed again by the rename and can be recycled underneath a slower
        thread that already passed its existence check.
        """
        stamp = captured_at.strftime("%Y%m%dT%H%M%SZ")
        base = f"{stamp}__{identity.issue_key}__{identity.role}__{identity.turn_slug}"
        for ordinal in range(1, _MAX_DIRECTORY_ATTEMPTS + 1):
            suffix = "" if ordinal == 1 else f"-{ordinal}"
            final = root / f"{base}{suffix}"
            try:
                final.mkdir(parents=True)
            except FileExistsError:
                continue
            staging = root / f".{base}{suffix}.{self._next_staging_id()}{STAGING_SUFFIX}"
            try:
                staging.mkdir(parents=True)
            except BaseException:
                # The claim is only ever allowed to outlive this call as a
                # directory we are about to fill; if staging never happened,
                # an empty final-named directory must not be left behind.
                discard_empty_claim(final)
                raise
            return final, staging
        raise RuntimeError(
            f"could not allocate a kill-evidence directory under {root} for "
            f"{base} after {_MAX_DIRECTORY_ATTEMPTS} attempts"
        )

    def _next_staging_id(self) -> int:
        with self._lock:
            self._staging_seq += 1
            return self._staging_seq

    def _classify(self, identity: RoundIdentity) -> ComposerStateVerdict:
        try:
            return classify_composer_state(
                identity.recording_path,
                prompt_marker=identity.prompt_marker,
                replay_bytes=self._replay_bytes,
            )
        except Exception as exc:
            logger.exception(
                "[kill-evidence] composer classification failed for %s",
                identity.recording_path,
            )
            return undetermined_composer_state(f"classification failed: {exc!r}")

    def _copy_recording_or_note_why_not(
        self, source: Path, destination: Path, *, budget: CaptureBudget
    ) -> RecordingCopy:
        if budget.expired():
            return RecordingCopy(
                present=source.exists(),
                source_bytes=0,
                copied_bytes=0,
                truncated=False,
                error="capture budget exhausted before the recording was copied",
            )
        try:
            return self._copy_recording(source, destination, budget=budget)
        except Exception as exc:
            logger.exception("[kill-evidence] could not copy recording %s", source)
            destination.with_name(destination.name + STAGING_SUFFIX).unlink(
                missing_ok=True
            )
            return RecordingCopy(
                present=source.exists(),
                source_bytes=0,
                copied_bytes=0,
                truncated=False,
                error=repr(exc),
            )

    def _copy_recording(
        self, source: Path, destination: Path, *, budget: CaptureBudget
    ) -> RecordingCopy:
        """Snapshot the recording, keeping the tail when it exceeds the cap.

        The source is open for append by a live PTY writer, so the copy is
        bounded to the size observed at entry and any trailing partial row is
        trimmed — the retained file is always valid NDJSON.
        """
        if not source.exists():
            return RecordingCopy(
                present=False, source_bytes=0, copied_bytes=0, truncated=False
            )
        size = source.stat().st_size
        start = max(0, size - self._max_copy_bytes)
        copied, last_newline, ends_clean = stream_copy(
            source, destination, start=start, total=size - start, budget=budget
        )
        if copied and not ends_clean:
            with destination.open("r+b") as handle:
                handle.truncate(last_newline + 1)
            copied = max(0, last_newline + 1)
        cut_short = copied < size - start
        return RecordingCopy(
            present=True,
            source_bytes=size,
            copied_bytes=copied,
            truncated=start > 0 or cut_short,
            error=(
                "capture budget exhausted mid-copy; retained only the leading "
                f"{copied} bytes of the window"
                if cut_short and budget.expired()
                else None
            ),
        )


# ---------------------------------------------------------------------------
# Payloads and IO helpers
# ---------------------------------------------------------------------------


def _abandoned_facts(
    ticket: RoundTicket, *, reason: str, now: Callable[[], float]
) -> RoundKillFacts:
    """Build the facts for a round the teardown is declaring dead for it."""
    detector = ticket.detector
    trace = detector.snapshot(now()) if detector is not None else None
    unavailable = (
        None
        if trace is not None
        else (
            "the round was still in flight and had not reached its poll loop, "
            "so no idle detector existed to snapshot"
        )
    )
    return RoundKillFacts(
        identity=ticket.identity,
        failure_reason=round_failure_reason_value(
            RoundFailureReason.ABANDONED_BY_TEARDOWN
        ),
        error_text=f"exchange torn down while this round was in flight: {reason}",
        idle_trace=trace,
        idle_trace_unavailable=unavailable,
    )


