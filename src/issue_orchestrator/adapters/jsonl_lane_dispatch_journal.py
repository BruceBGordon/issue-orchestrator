# pyright: strict
"""JSONL adapter for the lane dispatch journal.

Owns the storage decisions the port hides: the journal lives beside
the runtime history in the repository's git common dir (shared across
worktrees, like the validation timings), one JSON object per line via
a single O_APPEND write so concurrent gates cannot interleave rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from ..infra.machine_state import machine_state_fields
from ..infra.validation_timings import append_jsonl
from ..ports.lane_dispatch_journal import (
    LaneDispatchJournalError,
    LaneDispatchRecord,
)


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
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "worktree": Path.cwd().name,
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


class InertLaneDispatchJournal:
    """No-repo stand-in: records nowhere, like the inert history."""

    def record(self, record: LaneDispatchRecord) -> None:
        del record
