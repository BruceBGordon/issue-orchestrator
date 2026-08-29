"""Retained kill-evidence capture for review-exchange round failures.

When a persistent-PTY round dies, the artifact that settles *what the screen
looked like* is the role's terminal recording — and it lives in a volatile
home. Under the async E2E runner that home is a pytest tmp directory, which
rotation deletes within the hour; under normal operation it is a worktree the
orchestrator tears down. A pointer into either is a self-deleting record
(#7128): on 2026-08-28 a failed live-codex lane's recording was gone before
anyone could decode it, and two silent stalls were misattributed twice before
a surviving decode showed the prompt stranded in the composer.

This module is the one owner of that capture. At round-failure declaration the
round loop hands it a :class:`RoundKillFacts` and it writes, under a retained
root that outlives both the run directory and the worktree:

``<root>/<timestamp>__issue-<n>__<role>__round-R-attempt-A-respawn-K/``
    ``terminal-recording.jsonl``  copy of the recording (tail-capped)
    ``idle-trace.json``          idle-detector window, trajectory, idle_for
    ``run-identity.json``        branch, HEAD SHA, session, run/exchange dirs

plus a back-reference file next to the turn's ``result.json`` in the exchange
directory and one line in the root's ``index.jsonl``. The cross-reference runs
both ways on purpose: from a retained capture you can find the run, and from a
run directory you can find the capture, without sorting anything by mtime.

**Deliberate exception to the repo's fail-fast stance.** :meth:`capture` never
raises. It sits on the failure path of the thing operators actually care
about; a diagnostics bug that propagated from here would replace a real,
diagnosable round failure with a confusing traceback from the evidence
collector — destroying the very signal it exists to preserve. The boundary is
the same one ``EventSink`` draws for fire-and-forget trace events: observability
may degrade loudly (``logger.exception``) but must never change control flow.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..domain.exchange_kill_evidence import (
    ComposerState,
    ComposerStateVerdict,
    RoundIdleTrace,
    undetermined_composer_state,
)
from ..infra.validation_timings import (
    append_jsonl,
    read_branch_name,
    read_head_sha,
    resolve_git_common_dir,
)
from .session_interactions import normalize_terminal_text

logger = logging.getLogger(__name__)

KILL_EVIDENCE_SCHEMA_VERSION = 1
RECORDING_COPY_FILENAME = "terminal-recording.jsonl"
IDLE_TRACE_FILENAME = "idle-trace.json"
RUN_IDENTITY_FILENAME = "run-identity.json"
INDEX_FILENAME = "index.jsonl"
BACK_REFERENCE_SUFFIX = ".kill-evidence.json"

_ORCHESTRATOR_DIRNAME = ".issue-orchestrator"
_DIAGNOSTICS_DIRNAME = "diagnostics"
_KILL_EVIDENCE_DIRNAME = "exchange-kills"

# Decode budget. A wedged agent can leave a multi-hundred-megabyte recording;
# the discriminator only needs the final screen, so it reads a bounded byte
# window from the end and looks at the last N events inside it.
_DEFAULT_TAIL_BYTES = 256 * 1024
_DEFAULT_TAIL_EVENTS = 400
_DEFAULT_SCAN_CHARS = 8_000
_EVIDENCE_LEAD_CHARS = 80
_EVIDENCE_TRAIL_CHARS = 200
_COPY_CHUNK_BYTES = 1024 * 1024
# Copy cap. Past this the *tail* is kept: the failure screen is at the end of
# the file, and an unbounded copy on the failure path could fill the disk that
# the orchestrator itself needs.
_DEFAULT_MAX_COPY_BYTES = 64 * 1024 * 1024
_MAX_ERROR_CHARS = 2_000
_MAX_DIRECTORY_ATTEMPTS = 50


@dataclass(frozen=True)
class ComposerMarker:
    """One TUI affordance whose presence implies a composer state.

    Matched against :func:`normalize_terminal_text` output, so entries are
    lowercase with collapsed whitespace and no escape sequences.
    """

    name: str
    text: str
    state: ComposerState


# Heuristics, keyed to observed evidence and deliberately small. Precedence is
# positional rather than list order: terminal output is chronological, so the
# marker appearing *last* in the scanned tail describes the most recent render
# and wins. Nothing matched at all stays UNDETERMINED — this classifier never
# guesses, and every verdict carries the snippet that produced it.
COMPOSER_MARKERS: tuple[ComposerMarker, ...] = (
    # Claude-shaped TUIs show this only while the composer holds text the
    # agent has not taken. This is the exact footer visible in the 2026-08-28
    # recording tail where the injected prompt never submitted.
    ComposerMarker(
        name="queue_message_footer",
        text="tab to queue message",
        state=ComposerState.COMPOSER_STRANDED,
    ),
    # Generic "you have typed something, press Enter" affordance.
    ComposerMarker(
        name="send_hint_footer",
        text="enter to send",
        state=ComposerState.COMPOSER_STRANDED,
    ),
    # The agent is working on a submitted turn: the composer took the message.
    ComposerMarker(
        name="interrupt_footer",
        text="to interrupt",
        state=ComposerState.COMPOSER_EMPTIED,
    ),
    # Idle footer shown with an empty composer.
    ComposerMarker(
        name="shortcuts_footer",
        text="? for shortcuts",
        state=ComposerState.COMPOSER_EMPTIED,
    ),
)


def resolve_retained_diagnostics_root(worktree: Path) -> Path | None:
    """Return the repository-level retained home for kill evidence.

    Anchored on the *shared* git dir rather than the worktree so the captures
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


def build_exchange_kill_evidence_recorder(
    worktree: Path,
) -> "ExchangeKillEvidenceRecorder | None":
    """Build the recorder for a repository worktree, or ``None`` with a warning."""
    root = resolve_retained_diagnostics_root(worktree)
    if root is None:
        logger.warning(
            "[kill-evidence] no shared git dir under %s; round-failure evidence "
            "will not be retained for this exchange",
            worktree,
        )
        return None
    return ExchangeKillEvidenceRecorder(root)


# ---------------------------------------------------------------------------
# Recording tail decode + composer-state discriminator
# ---------------------------------------------------------------------------


def read_recording_tail_text(
    recording_path: Path,
    *,
    tail_bytes: int = _DEFAULT_TAIL_BYTES,
    tail_events: int = _DEFAULT_TAIL_EVENTS,
) -> tuple[str, int]:
    """Decode the end of a terminal recording into normalized screen text.

    Returns ``(normalized_text, events_decoded)``. Bounded twice over: at most
    ``tail_bytes`` are read from the end of the file, and at most
    ``tail_events`` output events inside that window are decoded. Rows that do
    not parse, carry no payload, or hold invalid base64 are skipped — a
    half-written final line is normal for a recording still open for append.
    Non-UTF-8 PTY bytes decode with replacement rather than failing.
    """
    lines = _read_tail_lines(recording_path, max_bytes=tail_bytes)
    payloads: list[bytes] = []
    decoded = 0
    for raw_line in lines[-tail_events:]:
        chunk = _output_event_bytes(raw_line)
        if chunk is None:
            continue
        payloads.append(chunk)
        decoded += 1
    text = b"".join(payloads).decode("utf-8", errors="replace")
    return normalize_terminal_text(text), decoded


def classify_composer_state(
    recording_path: Path,
    *,
    prompt_marker: str = "",
    tail_bytes: int = _DEFAULT_TAIL_BYTES,
    tail_events: int = _DEFAULT_TAIL_EVENTS,
    scan_chars: int = _DEFAULT_SCAN_CHARS,
) -> ComposerStateVerdict:
    """Classify whether the injected prompt stranded in the composer.

    ``prompt_marker`` is a short, per-turn token expected to echo into the
    composer (the round/attempt tag). Its presence is recorded as supporting
    evidence only, never as the decision: a submitted prompt is *also* rendered
    in the transcript, and long text wraps, so echo presence is neither
    necessary nor sufficient. The decision comes from the marker table.
    """
    if not recording_path.exists():
        return undetermined_composer_state(f"recording is missing at {recording_path}")
    text, events = read_recording_tail_text(
        recording_path, tail_bytes=tail_bytes, tail_events=tail_events
    )
    window = text[-scan_chars:]
    if not window:
        return undetermined_composer_state("recording tail decoded to no screen text")
    marker, position = _last_marker(window)
    normalized_prompt = normalize_terminal_text(prompt_marker)
    echoed = bool(normalized_prompt) and normalized_prompt in window
    if marker is None:
        return ComposerStateVerdict(
            state=ComposerState.UNDETERMINED,
            matched_marker=None,
            evidence_snippet=window[-_EVIDENCE_TRAIL_CHARS:],
            scanned_events=events,
            scanned_chars=len(window),
            prompt_marker_present=echoed,
        )
    return ComposerStateVerdict(
        state=marker.state,
        matched_marker=marker.name,
        evidence_snippet=_snippet_around(window, position),
        scanned_events=events,
        scanned_chars=len(window),
        prompt_marker_present=echoed,
    )


def _last_marker(window: str) -> tuple[ComposerMarker | None, int]:
    found: ComposerMarker | None = None
    position = -1
    for marker in COMPOSER_MARKERS:
        index = window.rfind(marker.text)
        if index > position:
            position = index
            found = marker
    return found, position


def _snippet_around(window: str, position: int) -> str:
    start = max(0, position - _EVIDENCE_LEAD_CHARS)
    return window[start : position + _EVIDENCE_TRAIL_CHARS]


def _output_event_bytes(raw_line: str) -> bytes | None:
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict) or event.get("event_type") != "output":
        return None
    encoded = event.get("data_b64")
    if not isinstance(encoded, str):
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None


def _read_tail_lines(recording_path: Path, *, max_bytes: int) -> list[str]:
    with recording_path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        start = max(0, size - max_bytes)
        handle.seek(start)
        blob = handle.read(size - start)
    rows = blob.split(b"\n")
    if start > 0:
        # The window almost certainly begins mid-row; drop that fragment.
        rows = rows[1:]
    return [row.decode("utf-8", errors="replace") for row in rows if row.strip()]


# ---------------------------------------------------------------------------
# Capture owner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundKillFacts:
    """Everything the recorder needs about one declared round failure."""

    issue_number: int
    role: str
    round_index: int
    attempt_index: int
    respawn_retries: int
    failure_reason: str
    error_text: str
    session_name: str
    exchange_run_id: str
    agent_pid: int
    recording_path: Path
    run_dir: Path
    exchange_dir: Path
    worktree: Path
    response_file: Path
    prompt_marker: str
    idle_trace: RoundIdleTrace | None

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
class CapturedKillEvidence:
    """What one successful capture produced."""

    directory: Path
    composer: ComposerStateVerdict
    recording_bytes_copied: int
    recording_truncated: bool


@dataclass(frozen=True)
class _RecordingCopy:
    present: bool
    source_bytes: int
    copied_bytes: int
    truncated: bool
    error: str | None = None


class ExchangeKillEvidenceRecorder:
    """Copies round-failure evidence out of its volatile home, once, in order.

    One instance per exchange. ``capture`` is the only entry point and never
    raises — see the module docstring for why that exception to fail-fast is
    deliberate.
    """

    def __init__(
        self,
        retained_root: Path,
        *,
        max_copy_bytes: int = _DEFAULT_MAX_COPY_BYTES,
        tail_bytes: int = _DEFAULT_TAIL_BYTES,
        tail_events: int = _DEFAULT_TAIL_EVENTS,
        scan_chars: int = _DEFAULT_SCAN_CHARS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.retained_root = retained_root
        self._max_copy_bytes = max_copy_bytes
        self._tail_bytes = tail_bytes
        self._tail_events = tail_events
        self._scan_chars = scan_chars
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def capture(self, facts: RoundKillFacts) -> CapturedKillEvidence | None:
        """Retain the evidence for one declared round failure.

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
                facts.issue_key,
                facts.role,
                facts.turn_slug,
            )
            return None

    def _capture(self, facts: RoundKillFacts) -> CapturedKillEvidence:
        captured_at = self._clock()
        directory = self._allocate_directory(facts, captured_at)
        # A corrupt or vanished recording must not cost us the other two
        # artifacts: identity and the idle trace are independently useful, and
        # a half-retained capture beats none. Both steps below degrade into a
        # recorded error instead of aborting the capture.
        composer = self._classify(facts)
        copy = self._copy_recording_or_note_why_not(
            facts.recording_path, directory / RECORDING_COPY_FILENAME
        )
        identity = _identity_payload(
            facts,
            directory=directory,
            captured_at=captured_at,
            composer=composer,
            copy=copy,
        )
        _write_json(directory / RUN_IDENTITY_FILENAME, identity)
        _write_json(directory / IDLE_TRACE_FILENAME, _idle_payload(facts))
        _write_json(_back_reference_path(facts), _back_reference_payload(facts, identity))
        append_jsonl(self.retained_root / INDEX_FILENAME, identity)
        logger.warning(
            "[kill-evidence] retained %s %s %s composer_state=%s marker=%s "
            "recording_bytes=%d truncated=%s at %s",
            facts.issue_key,
            facts.role,
            facts.turn_slug,
            composer.state.value,
            composer.matched_marker,
            copy.copied_bytes,
            copy.truncated,
            directory,
        )
        return CapturedKillEvidence(
            directory=directory,
            composer=composer,
            recording_bytes_copied=copy.copied_bytes,
            recording_truncated=copy.truncated,
        )

    def _allocate_directory(self, facts: RoundKillFacts, captured_at: datetime) -> Path:
        stamp = captured_at.strftime("%Y%m%dT%H%M%SZ")
        base = f"{stamp}__{facts.issue_key}__{facts.role}__{facts.turn_slug}"
        for ordinal in range(1, _MAX_DIRECTORY_ATTEMPTS + 1):
            suffix = "" if ordinal == 1 else f"-{ordinal}"
            candidate = self.retained_root / f"{base}{suffix}"
            try:
                # ``exist_ok=False`` is the whole point: creation is the claim,
                # so two captures in the same second cannot land in one
                # directory and overwrite each other's evidence.
                candidate.mkdir(parents=True)
            except FileExistsError:
                continue
            return candidate
        raise RuntimeError(
            f"could not allocate a kill-evidence directory under {self.retained_root} "
            f"for {base} after {_MAX_DIRECTORY_ATTEMPTS} attempts"
        )

    def _classify(self, facts: RoundKillFacts) -> ComposerStateVerdict:
        try:
            return classify_composer_state(
                facts.recording_path,
                prompt_marker=facts.prompt_marker,
                tail_bytes=self._tail_bytes,
                tail_events=self._tail_events,
                scan_chars=self._scan_chars,
            )
        except Exception as exc:
            logger.exception(
                "[kill-evidence] composer classification failed for %s",
                facts.recording_path,
            )
            return undetermined_composer_state(f"classification failed: {exc!r}")

    def _copy_recording_or_note_why_not(
        self, source: Path, destination: Path
    ) -> _RecordingCopy:
        try:
            return self._copy_recording(source, destination)
        except Exception as exc:
            logger.exception("[kill-evidence] could not copy recording %s", source)
            destination.with_name(destination.name + ".part").unlink(missing_ok=True)
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
        trimmed — the retained file is always valid NDJSON. The write lands on
        a ``.part`` sibling and is renamed, so an interrupted copy can never
        leave a half-written artifact under the retained name.
        """
        if not source.exists():
            return _RecordingCopy(
                present=False, source_bytes=0, copied_bytes=0, truncated=False
            )
        size = source.stat().st_size
        start = max(0, size - self._max_copy_bytes)
        staging = destination.with_name(destination.name + ".part")
        copied, last_newline, ends_clean = _stream_copy(
            source, staging, start=start, total=size - start
        )
        if copied and not ends_clean:
            with staging.open("r+b") as handle:
                handle.truncate(last_newline + 1)
            copied = max(0, last_newline + 1)
        staging.replace(destination)
        return _RecordingCopy(
            present=True,
            source_bytes=size,
            copied_bytes=copied,
            truncated=start > 0,
        )


def _stream_copy(
    source: Path, staging: Path, *, start: int, total: int
) -> tuple[int, int, bool]:
    """Copy ``total`` bytes from ``start``; report newline framing as we go."""
    copied = 0
    last_newline = -1
    ends_clean = True
    with source.open("rb") as reader, staging.open("wb") as writer:
        reader.seek(start)
        if start > 0:
            # The window opens mid-row; skip to the first complete row so the
            # retained copy parses as NDJSON from its first line.
            first = reader.readline()
            total -= len(first)
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


def _back_reference_path(facts: RoundKillFacts) -> Path:
    name = (
        f"round-{facts.round_index}-{facts.role}"
        f"-attempt-{facts.attempt_index}-respawn-{facts.respawn_retries}"
        f"{BACK_REFERENCE_SUFFIX}"
    )
    return facts.exchange_dir / name


def _back_reference_payload(
    facts: RoundKillFacts, identity: dict[str, Any]
) -> dict[str, Any]:
    """The volatile-side pointer at the retained capture.

    Deliberately small: it exists so someone holding a run directory can jump
    to the retained evidence, which carries the full identity.
    """
    return {
        "schema_version": KILL_EVIDENCE_SCHEMA_VERSION,
        "kind": "exchange_kill_evidence_reference",
        "retained_dir": identity["retained_dir"],
        "retained_recording": identity["retained_recording"],
        "captured_at": identity["captured_at"],
        "failure_reason": facts.failure_reason,
        "composer_state": identity["composer_state"]["state"],
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
    degrades into sorting directories by mtime.
    """
    return {
        "schema_version": KILL_EVIDENCE_SCHEMA_VERSION,
        "kind": "exchange_kill_evidence",
        "captured_at": captured_at.isoformat(),
        "issue_key": facts.issue_key,
        "issue_number": facts.issue_number,
        "role": facts.role,
        "round_index": facts.round_index,
        "attempt_index": facts.attempt_index,
        "respawn_retries": facts.respawn_retries,
        "failure_reason": facts.failure_reason,
        "error": facts.error_text[:_MAX_ERROR_CHARS],
        "session_name": facts.session_name,
        "exchange_run_id": facts.exchange_run_id,
        "agent_pid": facts.agent_pid,
        "branch": read_branch_name(facts.worktree),
        "head_sha": read_head_sha(facts.worktree),
        "worktree": str(facts.worktree),
        "run_dir": str(facts.run_dir),
        "exchange_dir": str(facts.exchange_dir),
        "response_file": str(facts.response_file),
        "original_recording": str(facts.recording_path),
        "recording_present": copy.present,
        "recording_source_bytes": copy.source_bytes,
        "recording_bytes_copied": copy.copied_bytes,
        "recording_truncated": copy.truncated,
        "recording_copy_error": copy.error,
        "retained_dir": str(directory),
        "retained_recording": str(directory / RECORDING_COPY_FILENAME),
        "back_reference": str(_back_reference_path(facts)),
        "composer_state": composer.to_dict(),
    }


def _idle_payload(facts: RoundKillFacts) -> dict[str, Any]:
    trace = facts.idle_trace
    return {
        "schema_version": KILL_EVIDENCE_SCHEMA_VERSION,
        "kind": "exchange_kill_idle_trace",
        "issue_key": facts.issue_key,
        "role": facts.role,
        "round_index": facts.round_index,
        "attempt_index": facts.attempt_index,
        "respawn_retries": facts.respawn_retries,
        "failure_reason": facts.failure_reason,
        "idle_trace": None if trace is None else trace.to_dict(),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + ".part")
    staging.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    staging.replace(path)
