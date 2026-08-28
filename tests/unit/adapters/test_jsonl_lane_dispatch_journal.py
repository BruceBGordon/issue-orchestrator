"""The JSONL journal adapter owns storage; the port owns the contract."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_orchestrator.adapters.jsonl_lane_dispatch_journal import (
    InertLaneDispatchJournal,
    JsonlLaneDispatchJournal,
)
from issue_orchestrator.domain.lane_execution import LaneWorkKey
from issue_orchestrator.ports.lane_dispatch_journal import (
    LaneDispatchJournalError,
    LaneDispatchRecord,
)
from issue_orchestrator.ports.machine_state import MachineState

_MACHINE_STATE = MachineState(
    sampled_at=datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc),
    loadavg_1m=7.91,
    loadavg_5m=12.51,
    loadavg_15m=9.0,
    cpu_idle_percent=85.68,
    cpu_idle_source="top -l 1 -n 0",
    physical_cores=18,
    probe_error=None,
)


def _record(exit_code: int = 0) -> LaneDispatchRecord:
    return LaneDispatchRecord(
        work_key=LaneWorkKey("test-unit"),
        backend="condor",
        priority=45,
        queue_wait_seconds=12.0,
        observed_runtime_seconds=45.0,
        exit_code=exit_code,
        machine_state=_MACHINE_STATE,
    )


def test_records_append_as_one_json_row_each(tmp_path: Path) -> None:
    journal = JsonlLaneDispatchJournal(tmp_path)
    journal.record(_record(exit_code=0))
    journal.record(_record(exit_code=1))
    rows = [
        json.loads(line)
        for line in (tmp_path / "lane-dispatch.jsonl").read_text().splitlines()
    ]
    assert [row["exit_code"] for row in rows] == [0, 1]
    first = rows[0]
    assert first["work_key"] == "test-unit"
    assert first["backend"] == "condor"
    assert first["priority"] == 45
    assert first["queue_wait_seconds"] == 12.0
    assert first["observed_runtime_seconds"] == 45.0
    assert first["recorded_at"]


def test_every_row_carries_the_machine_state_envelope(tmp_path: Path) -> None:
    """Acceptance (#7127): a runtime is only interpretable next to the
    contention it ran under, so the envelope rides EVERY row — the
    faked sampler's reading, verbatim, under one key."""
    journal = JsonlLaneDispatchJournal(tmp_path)
    journal.record(_record(exit_code=0))
    journal.record(_record(exit_code=1))
    rows = [
        json.loads(line)
        for line in (tmp_path / "lane-dispatch.jsonl").read_text().splitlines()
    ]
    for row in rows:
        assert row["machine_state"] == {
            "sampled_at": "2026-08-29T12:00:00+00:00",
            "loadavg_1m": 7.91,
            "loadavg_5m": 12.51,
            "loadavg_15m": 9.0,
            "cpu_idle_percent": 85.68,
            "cpu_idle_source": "top -l 1 -n 0",
            "physical_cores": 18,
            "probe_error": None,
        }


def test_a_record_without_a_reading_is_rejected() -> None:
    """Required, not optional: a row that may omit the covariate
    re-creates the ambiguity the envelope exists to end."""
    with pytest.raises(ValueError, match="machine_state"):
        LaneDispatchRecord(
            work_key=LaneWorkKey("test-unit"),
            backend="direct",
            priority=0,
            queue_wait_seconds=0.0,
            observed_runtime_seconds=1.0,
            exit_code=0,
            machine_state=None,  # type: ignore[arg-type]
        )


def test_unwritable_destination_raises_the_journal_error(
    tmp_path: Path,
) -> None:
    """Persistence failure surfaces as the port's typed error — never a
    raw OSError leaking transport details upward."""
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o555)
    journal = JsonlLaneDispatchJournal(blocked / "nested")
    with pytest.raises(LaneDispatchJournalError, match="lane-dispatch.jsonl"):
        journal.record(_record())


def test_relative_directory_is_rejected() -> None:
    with pytest.raises(ValueError, match="absolute"):
        JsonlLaneDispatchJournal(Path("relative/dir"))


def test_inert_journal_records_nowhere(tmp_path: Path) -> None:
    # A dedicated probe dir: conftest fixtures plant their own entries
    # in tmp_path itself, so watching it wholesale is a false signal.
    probe = tmp_path / "probe"
    probe.mkdir()
    InertLaneDispatchJournal().record(_record())
    assert not list(probe.iterdir())


def test_written_records_read_back_with_their_observation_facts(
    tmp_path: Path,
) -> None:
    """Round trip: what the writer stored is what the reader returns."""
    journal = JsonlLaneDispatchJournal(tmp_path)
    journal.record(_record(exit_code=0))
    journal.record(_record(exit_code=124))

    history = journal.read_recent(10)

    assert history.location == str(tmp_path / "lane-dispatch.jsonl")
    assert [entry.record.exit_code for entry in history.entries] == [0, 124]
    first = history.entries[0]
    assert first.record.work_key == LaneWorkKey("test-unit")
    assert first.record.backend == "condor"
    assert first.record.priority == 45
    assert first.record.queue_wait_seconds == 12.0
    assert first.record.observed_runtime_seconds == 45.0
    # The worktree that submitted is an observation of the write, not
    # part of what the lane reported, so it lives on the entry.
    assert first.worktree == Path.cwd().name
    assert first.recorded_at.tzinfo is not None


def test_absent_journal_is_an_empty_history_not_an_error(tmp_path: Path) -> None:
    """A repository that has never run a lane is the first run, not a fault."""
    history = JsonlLaneDispatchJournal(tmp_path).read_recent(10)
    assert history.entries == ()
    assert "lane-dispatch.jsonl" in history.location


def test_only_the_most_recent_window_is_returned(tmp_path: Path) -> None:
    journal = JsonlLaneDispatchJournal(tmp_path)
    for exit_code in range(5):
        journal.record(_record(exit_code=exit_code))

    history = journal.read_recent(2)

    assert [entry.record.exit_code for entry in history.entries] == [3, 4]


def test_corruption_outside_the_window_does_not_block_the_snapshot(
    tmp_path: Path,
) -> None:
    """Only what is actually read is parsed.

    Ancient garbage must not permanently break a status command that is
    reporting on today's runs — but see the next test: garbage inside
    the window is never skipped past.
    """
    path = tmp_path / "lane-dispatch.jsonl"
    journal = JsonlLaneDispatchJournal(tmp_path)
    path.write_text("not json at all\n")
    journal.record(_record(exit_code=7))

    history = journal.read_recent(1)

    assert [entry.record.exit_code for entry in history.entries] == [7]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("{not json", "not JSON"),
        ('["a list"]', "not a JSON object"),
        ('{"recorded_at": 5}', "not a string"),
        ('{"recorded_at": "yesterday"}', "not a timestamp"),
        ('{"recorded_at": "2026-08-28T10:00:00"}', "no timezone"),
        ('{"recorded_at": "2026-08-28T10:00:00+00:00"}', "worktree"),
    ],
)
def test_corrupt_records_fail_loudly_naming_the_line(
    tmp_path: Path, line: str, expected: str
) -> None:
    """A journal that cannot be read back means something wrote garbage.

    Guessing past it would hide the writer's bug behind numbers that
    look plausible, so every malformed line raises and says where.
    """
    (tmp_path / "lane-dispatch.jsonl").write_text(f"{line}\n")
    with pytest.raises(LaneDispatchJournalError) as caught:
        JsonlLaneDispatchJournal(tmp_path).read_recent(10)
    assert "line 1" in str(caught.value)
    assert expected in str(caught.value)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"work_key": "Not A Work Key"}, "LaneWorkKey"),
        ({"work_key": None}, "LaneWorkKey"),
        ({"backend": ""}, "backend"),
        ({"priority": -3}, "priority"),
        ({"priority": "high"}, "priority"),
        ({"exit_code": "zero"}, "exit_code"),
        ({"queue_wait_seconds": "soon"}, "not a number"),
        ({"observed_runtime_seconds": None}, "not a number"),
    ],
)
def test_field_invariants_are_enforced_by_the_record_itself(
    tmp_path: Path, mutation: dict[str, object], expected: str
) -> None:
    """The stored row must satisfy the same contract a live record does."""
    journal = JsonlLaneDispatchJournal(tmp_path)
    journal.record(_record())
    path = tmp_path / "lane-dispatch.jsonl"
    row = json.loads(path.read_text().splitlines()[0])
    row.update(mutation)
    path.write_text(json.dumps(row) + "\n")

    with pytest.raises(LaneDispatchJournalError) as caught:
        journal.read_recent(10)
    assert expected in str(caught.value)


def test_blank_lines_are_not_records(tmp_path: Path) -> None:
    journal = JsonlLaneDispatchJournal(tmp_path)
    journal.record(_record())
    path = tmp_path / "lane-dispatch.jsonl"
    path.write_text(f"\n{path.read_text()}\n\n")

    assert len(journal.read_recent(10).entries) == 1


def test_reader_rejects_a_meaningless_window(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        JsonlLaneDispatchJournal(tmp_path).read_recent(0)
    with pytest.raises(ValueError, match="positive"):
        InertLaneDispatchJournal().read_recent(0)


def test_inert_journal_reads_back_nothing_and_says_why() -> None:
    history = InertLaneDispatchJournal().read_recent(10)
    assert history.entries == ()
    assert "not a repository" in history.location


def test_record_validation_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="priority"):
        LaneDispatchRecord(
            work_key=LaneWorkKey("test-unit"),
            backend="condor",
            priority=-1,
            queue_wait_seconds=0.0,
            observed_runtime_seconds=1.0,
            exit_code=0,
            machine_state=_MACHINE_STATE,
        )
    with pytest.raises(ValueError, match="queue_wait_seconds"):
        LaneDispatchRecord(
            work_key=LaneWorkKey("test-unit"),
            backend="condor",
            priority=0,
            queue_wait_seconds=float("nan"),
            observed_runtime_seconds=1.0,
            exit_code=0,
            machine_state=_MACHINE_STATE,
        )
    with pytest.raises(ValueError, match="backend"):
        LaneDispatchRecord(
            work_key=LaneWorkKey("test-unit"),
            backend="",
            priority=0,
            queue_wait_seconds=0.0,
            observed_runtime_seconds=1.0,
            exit_code=0,
            machine_state=_MACHINE_STATE,
        )
