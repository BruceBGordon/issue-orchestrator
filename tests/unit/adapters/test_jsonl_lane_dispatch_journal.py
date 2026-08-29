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
