"""On-disk shape of one retained kill-evidence capture.

The typed records a capture is built from, the JSON payloads it writes, and the
atomic file operations that put them on disk. Split out of
``exchange_kill_evidence`` so that module keeps only the decision of *when* to
capture and *which* round; this one owns *what lands where*.

Every write here is crash-shaped on purpose: JSON artifacts land on a
per-call-unique temp and are renamed, the index append repairs a torn tail
before writing and truncates its own short write, and the helpers that discard
a claim or a staging tree never raise.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..domain.exchange_kill_evidence import (
    ComposerStateVerdict,
    RoundIdleDetector,
    RoundIdleTrace,
)
from ..infra.validation_timings import read_branch_name, read_head_sha

logger = logging.getLogger(__name__)

KILL_EVIDENCE_SCHEMA_VERSION = 2
RECORDING_COPY_FILENAME = "terminal-recording.jsonl"
IDLE_TRACE_FILENAME = "idle-trace.json"
RUN_IDENTITY_FILENAME = "run-identity.json"
INDEX_FILENAME = "index.jsonl"
BACK_REFERENCE_SUFFIX = ".kill-evidence.json"
STAGING_SUFFIX = ".part"

_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_ERROR_CHARS = 2_000

#: Process-wide sequence for unique temp-file names (see ``write_json``).
_TEMP_SEQ = itertools.count()

#: Serialises the read-repair-then-append sequence on the index file.
_INDEX_LOCK = threading.Lock()


@dataclass(frozen=True)
class CaptureBudget:
    """Wall-clock ceiling for one capture attempt.

    The teardown capture runs while the pair registry holds its lock, so a
    stalled filesystem (NFS hang, dying disk) would otherwise block every pair
    operation in the process indefinitely. Size caps bound the *volume* of the
    work; this bounds its *duration*.

    It bounds work not yet started, which is the honest guarantee available in
    userspace: a syscall already blocked in the kernel cannot be preempted from
    here. Every stage checks it, so a stall shows up as an abandoned stage with
    a recorded reason instead of a teardown that never returns.
    """

    deadline: float
    now: Callable[[], float]

    @classmethod
    def starting_now(
        cls, *, seconds: float, now: Callable[[], float]
    ) -> "CaptureBudget":
        return cls(deadline=now() + seconds, now=now)

    def expired(self) -> bool:
        return self.now() >= self.deadline


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
class RecordingCopy:
    present: bool
    source_bytes: int
    copied_bytes: int
    truncated: bool
    error: str | None = None



def stream_copy(
    source: Path,
    destination: Path,
    *,
    start: int,
    total: int,
    budget: CaptureBudget,
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
            if budget.expired():
                break
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


def back_reference_path(identity: RoundIdentity) -> Path:
    name = (
        f"round-{identity.round_index}-{identity.role}"
        f"-attempt-{identity.attempt_index}-respawn-{identity.respawn_retries}"
        f"{BACK_REFERENCE_SUFFIX}"
    )
    return identity.exchange_dir / name


def back_reference_payload(
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


def identity_payload(
    facts: RoundKillFacts,
    *,
    directory: Path,
    captured_at: datetime,
    composer: ComposerStateVerdict,
    copy: RecordingCopy,
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
        "back_reference": str(back_reference_path(identity)),
        "composer_state": composer.to_dict(),
    }


def idle_payload(facts: RoundKillFacts) -> dict[str, Any]:
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


def write_json(path: Path, payload: dict[str, Any]) -> None:
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


def record_index_line(path: Path, payload: dict[str, Any]) -> None:
    """Append the index row without ever failing an already-committed capture.

    The artifacts are renamed into place before this runs, so the evidence
    exists whatever happens here; the index is a convenience for grepping. A
    failure is loud but never turns a retained capture into a reported miss.
    """
    try:
        _append_index_line(path, payload)
    except Exception:
        logger.exception(
            "[kill-evidence] capture at %s is retained but its index row could "
            "not be appended to %s",
            payload.get("retained_dir"),
            path,
        )


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
            before = os.lseek(fd, 0, os.SEEK_END)
            written = os.write(fd, line)
            if written != len(line):
                # Repair now, not on some future append: a committed capture
                # must never leave a torn row behind it for the next writer to
                # concatenate onto (#7141 round 2 finding 3b). Only when our
                # short write is still the tail — another process appending
                # between the write and here owns those bytes, and losing
                # someone else's row would be worse than the torn tail their
                # next append repairs.
                if os.lseek(fd, 0, os.SEEK_END) == before + written:
                    os.ftruncate(fd, before)
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


def discard_empty_claim(final: Path) -> None:
    """Drop the claimed-but-never-filled final directory. Never raises."""
    try:
        final.rmdir()
    except OSError:
        logger.exception(
            "[kill-evidence] could not discard the claimed directory %s", final
        )


def remove_tree(path: Path) -> None:
    """Best-effort removal of a staging directory; never raises."""
    try:
        for child in sorted(path.rglob("*"), reverse=True):
            child.rmdir() if child.is_dir() else child.unlink(missing_ok=True)
        path.rmdir()
    except OSError:
        logger.exception("[kill-evidence] could not discard staging directory %s", path)
