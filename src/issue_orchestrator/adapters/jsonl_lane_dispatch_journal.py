# pyright: strict
"""JSONL adapter for the lane dispatch journal.

Owns the storage decisions the port hides: the journal lives beside
the runtime history in the repository's git common dir (shared across
worktrees, like the validation timings), one JSON object per line via
a single O_APPEND write so concurrent gates cannot interleave rows.

The same class owns reading the file back, because the line format is
one decision and splitting it across a writer and a separate reader is
how the two drift apart.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from ..domain.lane_execution import LaneWorkKey
from ..infra.machine_state import (
    MachineStateEnvelopeError,
    machine_state_fields,
    machine_state_from_fields,
)
from ..infra.validation_timings import append_jsonl
from ..ports.lane_dispatch_journal import (
    LaneDispatchEntry,
    LaneDispatchHistory,
    LaneDispatchJournalError,
    LaneDispatchRecord,
)

_RECORDED_AT_FIELD = "recorded_at"
_WORKTREE_FIELD = "worktree"


class JsonlLaneDispatchJournal:
    """Append records to ``<directory>/lane-dispatch.jsonl``."""

    def __init__(self, directory: Path) -> None:
        if (
            not isinstance(cast(object, directory), Path)
            or not directory.is_absolute()
        ):
            raise ValueError(
                "JsonlLaneDispatchJournal.directory must be an absolute Path"
            )
        self._path = directory / "lane-dispatch.jsonl"

    def record(self, record: LaneDispatchRecord) -> None:
        if type(record) is not LaneDispatchRecord:
            raise ValueError(
                "JsonlLaneDispatchJournal.record requires a LaneDispatchRecord"
            )
        try:
            append_jsonl(
                self._path,
                {
                    _RECORDED_AT_FIELD: datetime.now(timezone.utc).isoformat(),
                    _WORKTREE_FIELD: Path.cwd().name,
                    "backend": record.backend,
                    "work_key": record.work_key.value,
                    "priority": record.priority,
                    "queue_wait_seconds": round(record.queue_wait_seconds, 1),
                    "observed_runtime_seconds": round(
                        record.observed_runtime_seconds, 1
                    ),
                    "exit_code": record.exit_code,
                    # Same envelope shape and same owner as the
                    # validation timings beside it, so one query reads
                    # host contention across both files (#7127).
                    **machine_state_fields(record.machine_state),
                },
            )
        except OSError as error:
            raise LaneDispatchJournalError(
                f"could not persist dispatch record to {self._path}: {error}"
            ) from error

    def read_recent(self, limit: int) -> LaneDispatchHistory:
        if type(limit) is not int or limit < 1:
            raise ValueError("read_recent limit must be a positive integer")
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Absence is the first run, never an error.
            return LaneDispatchHistory(location=str(self._path), entries=())
        except OSError as error:
            raise LaneDispatchJournalError(
                f"could not read the dispatch journal at {self._path}: {error}"
            ) from error
        # Number the lines before windowing so a parse failure names the
        # line as it appears in the file, not as it appears in the tail.
        numbered = [
            (number, line)
            for number, line in enumerate(raw.splitlines(), start=1)
            if line.strip()
        ]
        return LaneDispatchHistory(
            location=str(self._path),
            entries=tuple(
                self._parse(number, line) for number, line in numbered[-limit:]
            ),
        )

    def _parse(self, line_number: int, line: str) -> LaneDispatchEntry:
        """Translate one stored line, refusing anything malformed.

        Every field is validated: a journal that cannot be read back
        means something wrote garbage, and guessing past it would hide
        that writer's bug behind plausible-looking numbers.
        """
        try:
            payload = cast(object, json.loads(line))
        except json.JSONDecodeError as error:
            raise self._corrupt(line_number, f"line is not JSON: {error}") from error
        if not isinstance(payload, dict):
            raise self._corrupt(line_number, "line is not a JSON object")
        fields = cast(dict[str, object], payload)
        recorded_at = self._parse_timestamp(line_number, fields.get(_RECORDED_AT_FIELD))
        worktree = fields.get(_WORKTREE_FIELD)
        if type(worktree) is not str or not worktree:
            raise self._corrupt(line_number, f"{_WORKTREE_FIELD!r} is not a name")
        try:
            record = LaneDispatchRecord(
                work_key=LaneWorkKey(cast(str, fields.get("work_key"))),
                backend=cast(str, fields.get("backend")),
                priority=cast(int, fields.get("priority")),
                queue_wait_seconds=self._parse_seconds(
                    line_number, "queue_wait_seconds", fields.get("queue_wait_seconds")
                ),
                observed_runtime_seconds=self._parse_seconds(
                    line_number,
                    "observed_runtime_seconds",
                    fields.get("observed_runtime_seconds"),
                ),
                exit_code=cast(int, fields.get("exit_code")),
                # The contention the lane ran under, read back through
                # the same owner that wrote it. Required, not optional:
                # a runtime without it is exactly the ambiguity the
                # envelope exists to end (#7127).
                machine_state=machine_state_from_fields(fields),
            )
        except MachineStateEnvelopeError as error:
            raise self._corrupt(line_number, str(error)) from error
        except ValueError as error:
            # LaneWorkKey and LaneDispatchRecord already own every field
            # invariant; reuse them rather than restating them here.
            raise self._corrupt(line_number, str(error)) from error
        return LaneDispatchEntry(
            recorded_at=recorded_at, worktree=worktree, record=record
        )

    def _parse_timestamp(self, line_number: int, value: object) -> datetime:
        if type(value) is not str:
            raise self._corrupt(line_number, f"{_RECORDED_AT_FIELD!r} is not a string")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise self._corrupt(
                line_number, f"{_RECORDED_AT_FIELD!r} is not a timestamp: {value!r}"
            ) from error
        if parsed.tzinfo is None:
            raise self._corrupt(
                line_number, f"{_RECORDED_AT_FIELD!r} has no timezone: {value!r}"
            )
        return parsed.astimezone(timezone.utc)

    def _parse_seconds(self, line_number: int, field: str, value: object) -> float:
        # JSON renders 0.0 as 0, so an integer duration is the same fact
        # as a float one; anything else is not a duration at all.
        if type(value) is int:
            return float(value)
        if type(value) is float:
            return value
        raise self._corrupt(line_number, f"{field!r} is not a number")

    def _corrupt(self, line_number: int, detail: str) -> LaneDispatchJournalError:
        return LaneDispatchJournalError(
            f"dispatch journal {self._path} is corrupt at line "
            f"{line_number}: {detail}"
        )


class InertLaneDispatchJournal:
    """No-repo stand-in: records nowhere, like the inert history."""

    def record(self, record: LaneDispatchRecord) -> None:
        del record

    def read_recent(self, limit: int) -> LaneDispatchHistory:
        if type(limit) is not int or limit < 1:
            raise ValueError("read_recent limit must be a positive integer")
        return LaneDispatchHistory(
            location=(
                "not persisted: dispatch records are kept per repository and "
                "this is not a repository"
            ),
            entries=(),
        )
