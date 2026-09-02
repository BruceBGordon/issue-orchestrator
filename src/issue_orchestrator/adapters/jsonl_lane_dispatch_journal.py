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

from ..domain.lane_cpu_request import LaneCpuRequest
from ..domain.lane_execution import LaneWorkKey
from ..infra.machine_state import (
    MachineStateEnvelopeError,
    MachineStateEnvelopeMissing,
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
_DECLARED_CPUS_FIELD = "declared_cpus"
_REQUEST_CPUS_FIELD = "request_cpus"
_LEARNED_BUSY_CORES_FIELD = "learned_busy_cores"
_OBSERVED_BUSY_CORES_FIELD = "observed_busy_cores"


class _RowPredatesSchema(Exception):
    """One row is older than a dimension the record now requires.

    Not corruption and not a fault — it was valid when it was written,
    and the journal is shared by every worktree on the machine, so a
    worktree on older code is appending such rows right now.

    Deliberately ONE signal for every schema epoch. The machine-state
    envelope (#7135) was the first, the cpu request (#7136) the second,
    and each arrives the same way: valid JSON, missing a column that
    :class:`LaneDispatchRecord` cannot do without. Giving each epoch its
    own signal and its own counter would repeat this mechanism through
    the port, the snapshot and the CLI for every dimension ever added,
    to tell the operator something they cannot act on differently.
    """


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
                    # The sizing decision is flattened into sibling
                    # columns so a jq one-liner can compare them;
                    # nesting would make the divergence query the
                    # awkward one. The machine-state envelope below
                    # nests for the opposite reason — it is one unit
                    # shared with the validation timings — so the two
                    # cannot collide.
                    "declared_cpus": record.cpu_request.declared_cpus,
                    "request_cpus": record.cpu_request.request_cpus,
                    "learned_busy_cores": _rounded(
                        record.cpu_request.learned_busy_cores
                    ),
                    "observed_busy_cores": _rounded(record.observed_busy_cores),
                    "cpu_request_capped": record.cpu_request.is_capped,
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
        entries: list[LaneDispatchEntry] = []
        predating = 0
        for number, line in numbered[-limit:]:
            try:
                entries.append(self._parse(number, line))
            except _RowPredatesSchema:
                # Written before some dimension the record now requires,
                # or by a worktree still running code that predates it.
                # Valid when written, so not corruption — but
                # unrepresentable, so counted and reported rather than
                # dropped silently.
                predating += 1
        return LaneDispatchHistory(
            location=str(self._path),
            entries=tuple(entries),
            predating_schema=predating,
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
                # The capacity the lane asked for, restored — not
                # re-decided. Rebuilding it through LaneCpuRequest.resolve
                # would run TODAY's seed-and-ceiling policy over an OLD
                # row and could yield a request the lane never submitted;
                # what was recorded is what is returned. The stored
                # `cpu_request_capped` column is deliberately not read
                # back: it is derived from these three, so reading it
                # would let a hand-edited file disagree with itself.
                cpu_request=self._parse_cpu_request(line_number, fields),
                observed_busy_cores=self._parse_optional_cores(
                    line_number, _OBSERVED_BUSY_CORES_FIELD, fields
                ),
            )
        except MachineStateEnvelopeMissing as missing:
            # Older-schema row, not corruption. Translated into the one
            # signal every epoch shares so the caller counts them
            # together; catching the base class here would instead
            # report every pre-envelope row as garbage.
            raise _RowPredatesSchema(str(missing)) from missing
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

    def _parse_cpu_request(
        self, line_number: int, fields: dict[str, object]
    ) -> LaneCpuRequest:
        """Restore the sizing decision this row recorded.

        Absence of the columns is an epoch, not corruption: rows written
        before #7136 have none. There is no honest value to invent for
        them either — a fabricated ``declared_cpus`` would put a
        scheduling fact into the record that no lane ever declared — so
        the row is skipped and counted, exactly as a pre-envelope row is.
        A row carrying SOME of the columns is a different thing: that is
        a writer bug, and it is corrupt.
        """
        declared = fields.get(_DECLARED_CPUS_FIELD)
        request = fields.get(_REQUEST_CPUS_FIELD)
        learned_present = _LEARNED_BUSY_CORES_FIELD in fields
        if declared is None and request is None and not learned_present:
            raise _RowPredatesSchema(
                f"row has no {_DECLARED_CPUS_FIELD!r}/{_REQUEST_CPUS_FIELD!r}: "
                "written before the cpu request was recorded (#7136)"
            )
        if type(declared) is not int or type(request) is not int:
            raise self._corrupt(
                line_number,
                f"{_DECLARED_CPUS_FIELD!r} and {_REQUEST_CPUS_FIELD!r} must "
                "both be integers",
            )
        try:
            # LaneCpuRequest owns every invariant, the seed-and-ceiling
            # one included: a hand-edited row asking for more than it
            # declared is corrupt, and is refused here rather than
            # returned as a decision no policy could have produced.
            return LaneCpuRequest(
                declared_cpus=declared,
                learned_busy_cores=self._parse_optional_cores(
                    line_number, _LEARNED_BUSY_CORES_FIELD, fields
                ),
                request_cpus=request,
            )
        except ValueError as error:
            raise self._corrupt(line_number, str(error)) from error

    def _parse_optional_cores(
        self, line_number: int, field: str, fields: dict[str, object]
    ) -> float | None:
        """A busy-cores column, where null is a recorded fact.

        ``None`` means the run was not measured — a real observation,
        distinct from a measured zero — so it round-trips as null rather
        than being coerced into a number.
        """
        value = fields.get(field)
        if value is None:
            return None
        # JSON renders 2.0 as 2, so an integer reading is the same fact.
        if type(value) is int:
            return float(value)
        if type(value) is float:
            return value
        raise self._corrupt(line_number, f"{field!r} is not a number or null")

    def _corrupt(self, line_number: int, detail: str) -> LaneDispatchJournalError:
        return LaneDispatchJournalError(
            f"dispatch journal {self._path} is corrupt at line "
            f"{line_number}: {detail}"
        )


def _rounded(value: float | None) -> float | None:
    """Keep an unmeasured dimension null — never round it into a 0.0."""
    if value is None:
        return None
    return round(value, 2)


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
