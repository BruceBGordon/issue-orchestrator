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

import itertools
import json
import logging
import os
import threading
import time
from collections.abc import Hashable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..domain.exchange_kill_evidence import (
    ComposerStateVerdict,
    RoundIdleDetector,
    RoundIdleTrace,
    undetermined_composer_state,
)
from ..domain.review_exchange_failures import (
    RoundFailureReason,
    round_failure_reason_value,
)
from ..infra.validation_timings import read_branch_name, read_head_sha, resolve_git_common_dir
from .composer_state import DEFAULT_REPLAY_BYTES, classify_composer_state

logger = logging.getLogger(__name__)

KILL_EVIDENCE_SCHEMA_VERSION = 2
RECORDING_COPY_FILENAME = "terminal-recording.jsonl"
IDLE_TRACE_FILENAME = "idle-trace.json"
RUN_IDENTITY_FILENAME = "run-identity.json"
INDEX_FILENAME = "index.jsonl"
BACK_REFERENCE_SUFFIX = ".kill-evidence.json"
STAGING_SUFFIX = ".part"

_ORCHESTRATOR_DIRNAME = ".issue-orchestrator"
_DIAGNOSTICS_DIRNAME = "diagnostics"
_KILL_EVIDENCE_DIRNAME = "exchange-kills"

# Copy cap. Past this the *tail* is kept: the failure screen is at the end of
# the file, and an unbounded copy on the failure path could fill the disk the
# orchestrator itself needs.
_DEFAULT_MAX_COPY_BYTES = 64 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_ERROR_CHARS = 2_000
# Linear probe depth for the same-second name collision. Generous because
# the probe is a cheap mkdir and the cap has to exceed any plausible burst
# of concurrent captures sharing one timestamp and turn slug.
_MAX_DIRECTORY_ATTEMPTS = 256

RetainedRootResolver = Callable[[Path], Path | None]

#: Process-wide sequence for unique temp-file names (see ``_write_json``).
_TEMP_SEQ = itertools.count()

#: Serialises the read-repair-then-append sequence on the index file.
_INDEX_LOCK = threading.Lock()


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


@dataclass(frozen=True)
class RoundIdentity:
    """Where one in-flight round lives and what it is.

    Registered when the round *starts*, so a teardown that arrives while the
    round is still wedged knows exactly which evidence to retain.
    """

    issue_number: int
    role: str
    round_index: int
    attempt_index: int
    respawn_retries: int
    session_name: str
    exchange_run_id: str
    agent_pid: int
    recording_path: Path
    run_dir: Path
    exchange_dir: Path
    worktree: Path
    response_file: Path
    prompt_marker: str

    @property
    def issue_key(self) -> str:
        return f"issue-{self.issue_number}"

    @property
    def turn_slug(self) -> str:
        return (
            f"round-{self.round_index}"
            f"-attempt-{self.attempt_index}"
            f"-respawn-{self.respawn_retries}"
        )


@dataclass(frozen=True)
class RoundKillFacts:
    """One round's identity plus how it died."""

    identity: RoundIdentity
    failure_reason: str
    error_text: str
    idle_trace: RoundIdleTrace | None
    idle_trace_unavailable: str | None = None


@dataclass(frozen=True)
class CapturedKillEvidence:
    """What one successful capture produced."""

    directory: Path
    composer: ComposerStateVerdict
    recording_bytes_copied: int
    recording_truncated: bool


@dataclass
class RoundTicket:
    """Handle for one registered in-flight round.

    Carries an optional live idle detector so a teardown can retain the
    trajectory of a round that is *still running* — for a wedged worker that
    frozen ``bytes_drained`` series is the whole diagnosis.
    """

    ticket_id: int
    identity: RoundIdentity
    detector: RoundIdleDetector | None = field(default=None)

    def attach_detector(self, detector: RoundIdleDetector) -> None:
        self.detector = detector


@dataclass(frozen=True)
class _RecordingCopy:
    present: bool
    source_bytes: int
    copied_bytes: int
    truncated: bool
    error: str | None = None


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
    ) -> None:
        self._resolve_root = resolve_root
        self._max_copy_bytes = max_copy_bytes
        self._replay_bytes = replay_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
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
        """Clear a registration. Idempotent so callers can put it in a finally."""
        with self._lock:
            self._in_flight.pop(ticket.ticket_id, None)

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
        self, facts: RoundKillFacts
    ) -> CapturedKillEvidence | None:
        """Retain evidence for a round that declared its own failure.

        Returns ``None`` when the capture itself failed; the caller keeps
        reporting the real round failure either way.
        """
        try:
            return self._capture(facts)
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
        for ticket in tickets:
            evidence = self.capture_declared_failure(
                _abandoned_facts(ticket, reason=reason, now=self._monotonic)
            )
            if evidence is not None:
                captured.append(evidence.directory)
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

    def _capture(self, facts: RoundKillFacts) -> CapturedKillEvidence:
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
                facts, staging=staging, final=final, captured_at=captured_at
            )
            # The rename is the commit point. POSIX replaces the empty claimed
            # directory atomically, so the final name goes from empty straight
            # to complete with no partial state in between.
            os.rename(staging, final)
        except BaseException:
            _remove_tree(staging)
            _discard_empty_claim(final)
            raise
        identity_payload = json.loads(
            (final / RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        _write_json(
            _back_reference_path(identity), _back_reference_payload(facts, identity_payload)
        )
        _append_index_line(root / INDEX_FILENAME, identity_payload)
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
    ) -> CapturedKillEvidence:
        """Write every artifact into the staging directory.

        A corrupt or vanished recording must not cost us the other two
        artifacts — identity and the idle trace are independently useful — so
        both steps below degrade into a recorded error rather than aborting.
        Anything else raises and the staging directory is discarded whole.
        """
        composer = self._classify(facts.identity)
        copy = self._copy_recording_or_note_why_not(
            facts.identity.recording_path, staging / RECORDING_COPY_FILENAME
        )
        payload = _identity_payload(
            facts,
            directory=final,
            captured_at=captured_at,
            composer=composer,
            copy=copy,
        )
        _write_json(staging / RUN_IDENTITY_FILENAME, payload)
        _write_json(staging / IDLE_TRACE_FILENAME, _idle_payload(facts))
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
            staging.mkdir(parents=True)
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
        self, source: Path, destination: Path
    ) -> _RecordingCopy:
        try:
            return self._copy_recording(source, destination)
        except Exception as exc:
            logger.exception("[kill-evidence] could not copy recording %s", source)
            destination.with_name(destination.name + STAGING_SUFFIX).unlink(
                missing_ok=True
            )
            return _RecordingCopy(
                present=source.exists(),
                source_bytes=0,
                copied_bytes=0,
                truncated=False,
                error=repr(exc),
            )

    def _copy_recording(self, source: Path, destination: Path) -> _RecordingCopy:
        """Snapshot the recording, keeping the tail when it exceeds the cap.

        The source is open for append by a live PTY writer, so the copy is
        bounded to the size observed at entry and any trailing partial row is
        trimmed — the retained file is always valid NDJSON.
        """
        if not source.exists():
            return _RecordingCopy(
                present=False, source_bytes=0, copied_bytes=0, truncated=False
            )
        size = source.stat().st_size
        start = max(0, size - self._max_copy_bytes)
        copied, last_newline, ends_clean = _stream_copy(
            source, destination, start=start, total=size - start
        )
        if copied and not ends_clean:
            with destination.open("r+b") as handle:
                handle.truncate(last_newline + 1)
            copied = max(0, last_newline + 1)
        return _RecordingCopy(
            present=True,
            source_bytes=size,
            copied_bytes=copied,
            truncated=start > 0,
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


def _stream_copy(
    source: Path, destination: Path, *, start: int, total: int
) -> tuple[int, int, bool]:
    """Copy ``total`` bytes from ``start``; report newline framing as we go."""
    copied = 0
    last_newline = -1
    ends_clean = True
    with source.open("rb") as reader, destination.open("wb") as writer:
        reader.seek(start)
        if start > 0:
            # The window opens mid-row; skip to the first complete row so the
            # retained copy parses as NDJSON from its first line.
            total -= len(reader.readline())
        while copied < total:
            chunk = reader.read(min(_COPY_CHUNK_BYTES, total - copied))
            if not chunk:
                break
            index = chunk.rfind(b"\n")
            if index >= 0:
                last_newline = copied + index
            writer.write(chunk)
            copied += len(chunk)
            ends_clean = chunk.endswith(b"\n")
    return copied, last_newline, ends_clean


def _back_reference_path(identity: RoundIdentity) -> Path:
    name = (
        f"round-{identity.round_index}-{identity.role}"
        f"-attempt-{identity.attempt_index}-respawn-{identity.respawn_retries}"
        f"{BACK_REFERENCE_SUFFIX}"
    )
    return identity.exchange_dir / name


def _back_reference_payload(
    facts: RoundKillFacts, identity_payload: dict[str, Any]
) -> dict[str, Any]:
    """The volatile-side pointer at the retained capture.

    Deliberately small: it exists so someone holding a run directory can jump
    to the retained evidence, which carries the full identity.
    """
    return {
        "schema_version": KILL_EVIDENCE_SCHEMA_VERSION,
        "kind": "exchange_kill_evidence_reference",
        "retained_dir": identity_payload["retained_dir"],
        "retained_recording": identity_payload["retained_recording"],
        "captured_at": identity_payload["captured_at"],
        "failure_reason": facts.failure_reason,
        "composer_state": identity_payload["composer_state"]["state"],
    }


def _identity_payload(
    facts: RoundKillFacts,
    *,
    directory: Path,
    captured_at: datetime,
    composer: ComposerStateVerdict,
    copy: _RecordingCopy,
) -> dict[str, Any]:
    """Run identity for one capture — the correlation record.

    Every key a human or a grep needs to tie a retained capture back to the
    gate run, the branch, and the session is here, so correlation never
    degrades into sorting directories by mtime. Paths point at the *final*
    directory even while staging, because that is where they will live.
    """
    identity = facts.identity
    final = directory
    return {
        "schema_version": KILL_EVIDENCE_SCHEMA_VERSION,
        "kind": "exchange_kill_evidence",
        "captured_at": captured_at.isoformat(),
        "issue_key": identity.issue_key,
        "issue_number": identity.issue_number,
        "role": identity.role,
        "round_index": identity.round_index,
        "attempt_index": identity.attempt_index,
        "respawn_retries": identity.respawn_retries,
        "failure_reason": facts.failure_reason,
        "error": facts.error_text[:_MAX_ERROR_CHARS],
        "session_name": identity.session_name,
        "exchange_run_id": identity.exchange_run_id,
        "agent_pid": identity.agent_pid,
        "branch": read_branch_name(identity.worktree),
        "head_sha": read_head_sha(identity.worktree),
        "worktree": str(identity.worktree),
        "run_dir": str(identity.run_dir),
        "exchange_dir": str(identity.exchange_dir),
        "response_file": str(identity.response_file),
        "original_recording": str(identity.recording_path),
        "recording_present": copy.present,
        "recording_source_bytes": copy.source_bytes,
        "recording_bytes_copied": copy.copied_bytes,
        "recording_truncated": copy.truncated,
        "recording_copy_error": copy.error,
        "retained_dir": str(final),
        "retained_recording": str(final / RECORDING_COPY_FILENAME),
        "back_reference": str(_back_reference_path(identity)),
        "composer_state": composer.to_dict(),
    }


def _idle_payload(facts: RoundKillFacts) -> dict[str, Any]:
    identity = facts.identity
    trace = facts.idle_trace
    return {
        "schema_version": KILL_EVIDENCE_SCHEMA_VERSION,
        "kind": "exchange_kill_idle_trace",
        "issue_key": identity.issue_key,
        "role": identity.role,
        "round_index": identity.round_index,
        "attempt_index": identity.attempt_index,
        "respawn_retries": identity.respawn_retries,
        "failure_reason": facts.failure_reason,
        "idle_trace": None if trace is None else trace.to_dict(),
        "idle_trace_unavailable": facts.idle_trace_unavailable,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write one JSON artifact atomically.

    The temp name is unique per call, not derived from the destination: two
    captures of the same turn share a back-reference path, and a shared temp
    name makes them clobber each other's half-written file mid-rename. Unique
    temps make the destination a clean last-writer-wins, which is the right
    answer for a pointer that should name the newest capture.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f"{path.name}.{os.getpid()}-{next(_TEMP_SEQ)}{STAGING_SUFFIX}")
    try:
        staging.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.rename(staging, path)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise


def _append_index_line(path: Path, payload: dict[str, Any]) -> None:
    """Append one index row, repairing a trailing partial line first.

    Pinned choice: **self-healing on append**. ``os.write`` can return short,
    and a previous short write would otherwise leave a JSON fragment that every
    later append silently concatenates onto, corrupting the row that follows
    it. Rather than assume single-write atomicity we check the framing and
    truncate back to the last newline before writing.

    The write itself goes through ``O_APPEND`` so concurrent writers cannot
    land on the same offset — a plain seek-to-end would let two captures
    compute the same position and overwrite each other, which 64 concurrent
    captures reproduce immediately. The repair is not part of that atomicity,
    so it is serialised in-process by ``_INDEX_LOCK``; across processes it is
    idempotent and only ever triggered by the rare short write it repairs.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    with _INDEX_LOCK:
        _repair_trailing_fragment(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            written = os.write(fd, line)
            if written != len(line):
                raise OSError(
                    f"short index write to {path}: {written} of {len(line)} bytes"
                )
        finally:
            os.close(fd)


def _repair_trailing_fragment(path: Path) -> None:
    """Truncate a trailing partial line so the next append starts framed."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
        if size == 0:
            return
        os.lseek(fd, size - 1, os.SEEK_SET)
        if os.read(fd, 1) == b"\n":
            return
        window = min(size, _COPY_CHUNK_BYTES)
        os.lseek(fd, size - window, os.SEEK_SET)
        tail = os.read(fd, window)
        cut = tail.rfind(b"\n")
        os.ftruncate(fd, size - window + cut + 1 if cut >= 0 else 0)
    finally:
        os.close(fd)


def _discard_empty_claim(final: Path) -> None:
    """Drop the claimed-but-never-filled final directory. Never raises."""
    try:
        final.rmdir()
    except OSError:
        logger.exception(
            "[kill-evidence] could not discard the claimed directory %s", final
        )


def _remove_tree(path: Path) -> None:
    """Best-effort removal of a staging directory; never raises."""
    try:
        for child in sorted(path.rglob("*"), reverse=True):
            child.rmdir() if child.is_dir() else child.unlink(missing_ok=True)
        path.rmdir()
    except OSError:
        logger.exception("[kill-evidence] could not discard staging directory %s", path)
