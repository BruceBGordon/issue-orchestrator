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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from ..domain.lane_cpu_request import LaneCpuRequest
from ..domain.lane_execution import LaneWorkKey
from ..infra.machine_state import (
    MachineState,
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
_CAPPED_FIELD = "cpu_request_capped"


#: Every column the cpu-request dimension (#7136) writes, as ONE unit.
#: Epoch detection asks about the whole set, never a sample of it:
#: checking part of it read a half-written row as merely old, and then
#: filled the absent columns with None — inventing the very facts this
#: reader refuses to invent (F1, #7136 journal review).
_CPU_REQUEST_COLUMNS = (
    _DECLARED_CPUS_FIELD,
    _REQUEST_CPUS_FIELD,
    _LEARNED_BUSY_CORES_FIELD,
    _OBSERVED_BUSY_CORES_FIELD,
    _CAPPED_FIELD,
)
#: Of those, the ones whose stored value may legitimately be null.
#: ``learned_busy_cores`` is null when nothing has been learned yet and
#: ``observed_busy_cores`` when the run was not measured — both are
#: recorded observations, not absences. The others are never null: a
#: null ``declared_cpus`` is a writer bug, because no lane ran without a
#: declaration.
_NULLABLE_CPU_COLUMNS = frozenset(
    {_LEARNED_BUSY_CORES_FIELD, _OBSERVED_BUSY_CORES_FIELD}
)


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


@dataclass(frozen=True, slots=True)
class _DimensionAbsent:
    """Every column of one dimension is missing: a row from an older epoch."""

    detail: str


@dataclass(frozen=True, slots=True)
class _DimensionCorrupt:
    """One dimension is present and wrong: a writer bug, at any epoch."""

    detail: str


@dataclass(frozen=True, slots=True)
class _CpuFacts:
    """The cpu-request dimension, read back whole.

    ``observed_busy_cores`` rides here rather than being read separately
    because it belongs to the same epoch: the writer emits it with the
    request columns or not at all, so its presence is part of the same
    question.
    """

    cpu_request: LaneCpuRequest
    observed_busy_cores: float | None


class _ColumnError(Exception):
    """One column of a present dimension violates its own rule."""


_MachineStateDimension = MachineState | _DimensionAbsent | _DimensionCorrupt
_CpuRequestDimension = _CpuFacts | _DimensionAbsent | _DimensionCorrupt


def _adjudicate(
    dimensions: tuple[
        _MachineStateDimension | _CpuRequestDimension, ...
    ],
) -> _DimensionCorrupt | _DimensionAbsent | None:
    """Decide one row's fate from EVERY dimension, never from the first.

    The order dimensions happen to be parsed in must not decide what a
    row is. Parsing the envelope first meant an absent envelope
    short-circuited before a present-and-contradictory cpu request was
    ever looked at, so a row that violated an invariant the write path
    enforces was reported as merely old (F2, #7136 journal review).

    The rule, in precedence order:

    - **Any dimension corrupt makes the row corrupt.** Corruption is a
      claim about something that IS there, so it outranks absence:
      a row that is old in one dimension and wrong in another is wrong.
    - **Otherwise any absent dimension makes the row an epoch skip**,
      counted once however many dimensions are absent — the operator's
      question is how much of the window was unreadable, and a row
      missing two dimensions is still one row.
    - **Otherwise the row is readable.**

    Returning the verdict instead of raising is what makes "once per
    row" structural rather than a property of where the raises sit.
    """
    corrupt = [
        dimension.detail
        for dimension in dimensions
        if type(dimension) is _DimensionCorrupt
    ]
    if corrupt:
        return _DimensionCorrupt("; ".join(corrupt))
    absent = [
        dimension.detail
        for dimension in dimensions
        if type(dimension) is _DimensionAbsent
    ]
    if absent:
        return _DimensionAbsent("; ".join(absent))
    return None


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
                    _CAPPED_FIELD: record.cpu_request.is_capped,
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
        # EVERY dimension is evaluated before anything is decided, so
        # the order they are read in cannot decide what the row is.
        dimensions = (
            _read_machine_state(fields),
            self._read_cpu_request(fields),
        )
        verdict = _adjudicate(dimensions)
        if type(verdict) is _DimensionCorrupt:
            raise self._corrupt(line_number, verdict.detail)
        if type(verdict) is _DimensionAbsent:
            raise _RowPredatesSchema(verdict.detail)
        envelope, cpu = dimensions
        if type(envelope) is not MachineState or type(cpu) is not _CpuFacts:
            raise AssertionError("a dimension outcome is a closed union")
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
                machine_state=envelope,
                # The capacity the lane asked for, restored — not
                # re-decided. Rebuilding it through LaneCpuRequest.resolve
                # would run TODAY's seed-and-ceiling policy over an OLD
                # row and could yield a request the lane never submitted;
                # what was recorded is what is returned. The stored
                # `cpu_request_capped` column is read for PRESENCE but
                # never for its value: it is derived from the other
                # three, so trusting it would let a hand-edited file
                # disagree with itself.
                cpu_request=cpu.cpu_request,
                observed_busy_cores=cpu.observed_busy_cores,
            )
        except ValueError as error:
            # LaneWorkKey and LaneDispatchRecord already own every field
            # invariant; reuse them rather than restating them here.
            raise self._corrupt(line_number, str(error)) from error
        return LaneDispatchEntry(
            recorded_at=recorded_at, worktree=worktree, record=record
        )

    def _read_cpu_request(self, fields: dict[str, object]) -> _CpuRequestDimension:
        """Evaluate the cpu-request dimension as ONE unit.

        Three outcomes, and the boundaries between them are the whole
        point (F1, #7136 journal review):

        - **Every column absent** is the epoch: rows written before
          #7136 have none of them. Nothing is invented for such a row —
          a fabricated ``declared_cpus`` would put a scheduling fact
          into the record that no lane ever declared — so it is skipped
          and counted.
        - **Some present, some absent** is a writer bug. Filling the
          gap with ``None`` would be that same fabrication wearing a
          nullable type, and it silently accepted half-written rows.
        - **All present** must each satisfy their own column's rule.
          Absent and null are different facts and are read as such:
          only the two busy-cores columns may be null, because null
          there is a recorded observation (nothing learned yet; not
          measured). A null anywhere else is corruption.
        """
        present = [name for name in _CPU_REQUEST_COLUMNS if name in fields]
        if not present:
            return _DimensionAbsent(
                "row has none of "
                f"{', '.join(repr(name) for name in _CPU_REQUEST_COLUMNS)}: "
                "written before the cpu request was recorded (#7136)"
            )
        missing = [name for name in _CPU_REQUEST_COLUMNS if name not in fields]
        if missing:
            return _DimensionCorrupt(
                "the cpu request is half written: "
                f"{', '.join(repr(name) for name in missing)} absent while "
                f"{', '.join(repr(name) for name in present)} present"
            )
        try:
            # LaneCpuRequest owns every invariant, the seed-and-ceiling
            # one included: a hand-edited row asking for more than it
            # declared is corrupt, and is refused here rather than
            # returned as a decision no policy could have produced.
            return _CpuFacts(
                cpu_request=LaneCpuRequest(
                    declared_cpus=_required_int(fields, _DECLARED_CPUS_FIELD),
                    learned_busy_cores=_nullable_cores(
                        fields, _LEARNED_BUSY_CORES_FIELD
                    ),
                    request_cpus=_required_int(fields, _REQUEST_CPUS_FIELD),
                ),
                observed_busy_cores=_nullable_cores(
                    fields, _OBSERVED_BUSY_CORES_FIELD
                ),
            )
        except (_ColumnError, ValueError) as error:
            return _DimensionCorrupt(str(error))
        except KeyError as error:
            # Unreachable while the presence check above owns absence,
            # and deliberately kept anyway: the column readers subscript
            # rather than `.get()` so a missing column can never become
            # a fabricated None, and this keeps that strictness inside
            # the port's typed error instead of letting a KeyError
            # escape read_recent untyped if the two ever disagree.
            return _DimensionCorrupt(f"column {error} is absent")

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


def _read_machine_state(fields: dict[str, object]) -> _MachineStateDimension:
    """Evaluate the machine-state dimension without deciding anything.

    The envelope's own owner (#7135) still answers every question about
    it; this only turns its two signals into verdicts the adjudicator
    can weigh against the other dimensions instead of letting whichever
    was parsed first win.
    """
    try:
        return machine_state_from_fields(fields)
    except MachineStateEnvelopeMissing as missing:
        return _DimensionAbsent(str(missing))
    except MachineStateEnvelopeError as error:
        return _DimensionCorrupt(str(error))


def _required_int(fields: dict[str, object], name: str) -> int:
    """A column that must be present and hold a non-null integer."""
    value = fields[name]
    # bool is an int subclass; a JSON true here is not a cpu count.
    if type(value) is not int:
        raise _ColumnError(f"{name!r} is not an integer: {value!r}")
    return value


def _nullable_cores(fields: dict[str, object], name: str) -> float | None:
    """A busy-cores column, where null is a recorded fact.

    ``None`` means the run taught nothing in this dimension — not
    measured, or nothing learned yet — which is a real observation and
    stays distinct from a measured zero. The column must still be
    PRESENT: its absence is the caller's business, not a null.
    """
    if name not in _NULLABLE_CPU_COLUMNS:
        raise AssertionError(f"{name!r} is not a nullable column")
    value = fields[name]
    if value is None:
        return None
    # JSON renders 2.0 as 2, so an integer reading is the same fact.
    if type(value) is int:
        return float(value)
    if type(value) is float:
        return value
    raise _ColumnError(f"{name!r} is not a number or null: {value!r}")


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
