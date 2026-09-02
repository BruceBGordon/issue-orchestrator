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
from issue_orchestrator.domain.lane_cpu_request import LaneCpuRequest
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


def _record(
    exit_code: int = 0,
    cpu_request: LaneCpuRequest | None = None,
    observed_busy_cores: float | None = 7.5,
) -> LaneDispatchRecord:
    return LaneDispatchRecord(
        work_key=LaneWorkKey("test-unit"),
        backend="condor",
        priority=45,
        queue_wait_seconds=12.0,
        observed_runtime_seconds=45.0,
        exit_code=exit_code,
        machine_state=_MACHINE_STATE,
        cpu_request=cpu_request or LaneCpuRequest.resolve(8, 6.2),
        observed_busy_cores=observed_busy_cores,
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
    # The sizing decision lands as flat sibling columns so the
    # measured-vs-declared divergence is one jq expression away.
    assert first["declared_cpus"] == 8
    assert first["request_cpus"] == 7
    assert first["learned_busy_cores"] == 6.2
    assert first["observed_busy_cores"] == 7.5
    assert first["cpu_request_capped"] is False


def test_unmeasured_dimensions_stay_null_never_zero(tmp_path: Path) -> None:
    """A run nobody measured must not serialize as 0.0 cores: a reader
    aggregating the column would treat the lane as free."""
    journal = JsonlLaneDispatchJournal(tmp_path)
    journal.record(
        _record(
            cpu_request=LaneCpuRequest.resolve(4, None),
            observed_busy_cores=None,
        )
    )
    row = json.loads((tmp_path / "lane-dispatch.jsonl").read_text().strip())
    assert row["learned_busy_cores"] is None
    assert row["observed_busy_cores"] is None
    assert row["request_cpus"] == 4


def test_capped_evidence_is_visible_in_the_record(tmp_path: Path) -> None:
    """A lane whose evidence exceeded its declaration was REFUSED that
    capacity. The refusal has to be greppable, or a real change in
    demand looks identical to agreement."""
    journal = JsonlLaneDispatchJournal(tmp_path)
    journal.record(_record(cpu_request=LaneCpuRequest.resolve(2, 16.0)))
    row = json.loads((tmp_path / "lane-dispatch.jsonl").read_text().strip())
    assert row["declared_cpus"] == 2
    assert row["request_cpus"] == 2
    assert row["learned_busy_cores"] == 16.0
    assert row["cpu_request_capped"] is True


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
            cpu_request=LaneCpuRequest.resolve(1, None),
            observed_busy_cores=None,
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
    # Both widenings survive the round trip, and neither substitutes
    # for the other: the contention the lane MET and the capacity it
    # ASKED FOR are separate answers about the same run.
    assert first.record.machine_state == _MACHINE_STATE
    assert first.record.cpu_request == LaneCpuRequest.resolve(8, 6.2)
    assert first.record.observed_busy_cores == 7.5


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
            cpu_request=LaneCpuRequest.resolve(1, None),
            observed_busy_cores=None,
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
            cpu_request=LaneCpuRequest.resolve(1, None),
            observed_busy_cores=None,
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
            cpu_request=LaneCpuRequest.resolve(1, None),
            observed_busy_cores=None,
        )
    with pytest.raises(ValueError, match="observed_busy_cores"):
        LaneDispatchRecord(
            work_key=LaneWorkKey("test-unit"),
            backend="condor",
            priority=0,
            queue_wait_seconds=0.0,
            observed_runtime_seconds=1.0,
            exit_code=0,
            machine_state=_MACHINE_STATE,
            cpu_request=LaneCpuRequest.resolve(1, None),
            observed_busy_cores=float("inf"),
        )
    with pytest.raises(ValueError, match="cpu_request"):
        LaneDispatchRecord(
            work_key=LaneWorkKey("test-unit"),
            backend="condor",
            priority=0,
            queue_wait_seconds=0.0,
            observed_runtime_seconds=1.0,
            exit_code=0,
            machine_state=_MACHINE_STATE,
            cpu_request=4,  # type: ignore[arg-type]
            observed_busy_cores=None,
        )


def test_reading_never_blocks_a_concurrent_writer_and_never_tears(
    tmp_path: Path,
) -> None:
    """A gate must not stall because someone ran executor-status.

    The reader takes no lock, and the writer's single O_APPEND write is
    what keeps a concurrently-read line whole: every record the reader
    sees parses, and the writer is never made to wait.
    """
    import threading

    journal = JsonlLaneDispatchJournal(tmp_path)
    writes = 200
    done = threading.Event()

    def write_many() -> None:
        try:
            for exit_code in range(writes):
                journal.record(_record(exit_code=exit_code))
        finally:
            done.set()

    writer = threading.Thread(target=write_many)
    writer.start()
    # Read repeatedly *while* the writer runs: a torn line would raise
    # the journal's corruption error, and a lock would deadlock or
    # serialize the writer behind us.
    reads = 0
    while not done.is_set():
        journal.read_recent(50)
        reads += 1
    writer.join(timeout=30)
    assert not writer.is_alive(), "reading must not block the writer"
    assert reads > 0, "probe never actually read during the writes"

    final = journal.read_recent(writes)
    assert [entry.record.exit_code for entry in final.entries] == list(range(writes))


# --- rows older than the machine-state envelope (#7135) --------------


def test_rows_written_before_the_envelope_are_skipped_and_counted(
    tmp_path: Path,
) -> None:
    """They were valid when written, so they are not corruption.

    The journal is append-only and shared by every worktree on the
    machine, so rows predating the envelope are interleaved with new
    ones and a worktree on older code keeps adding them. Refusing the
    whole window would make the reader useless on any real journal;
    hiding them would overstate the sample behind every runtime read
    from it. So: skipped, and counted.
    """
    journal = JsonlLaneDispatchJournal(tmp_path)
    journal.record(_record(exit_code=0))
    path = tmp_path / "lane-dispatch.jsonl"
    modern = json.loads(path.read_text().splitlines()[0])
    legacy = {key: value for key, value in modern.items() if key != "machine_state"}
    path.write_text(
        json.dumps(legacy) + "\n" + json.dumps(modern) + "\n"
        + json.dumps(legacy) + "\n"
    )

    history = journal.read_recent(10)

    assert len(history.entries) == 1, "the readable row must survive"
    assert history.predating_schema == 2
    assert history.entries[0].record.machine_state == _MACHINE_STATE


def test_a_malformed_envelope_is_still_corruption(tmp_path: Path) -> None:
    """The distinction is the point: absent is a schema version, but
    present-and-wrong is a writer getting it wrong, and that stays
    loud."""
    journal = JsonlLaneDispatchJournal(tmp_path)
    journal.record(_record())
    path = tmp_path / "lane-dispatch.jsonl"
    row = json.loads(path.read_text().splitlines()[0])
    row["machine_state"] = {"sampled_at": "2026-08-29T00:00:00+00:00"}
    path.write_text(json.dumps(row) + "\n")

    with pytest.raises(LaneDispatchJournalError, match="missing"):
        journal.read_recent(10)


def test_an_envelope_that_is_not_a_mapping_is_corruption(tmp_path: Path) -> None:
    journal = JsonlLaneDispatchJournal(tmp_path)
    journal.record(_record())
    path = tmp_path / "lane-dispatch.jsonl"
    row = json.loads(path.read_text().splitlines()[0])
    row["machine_state"] = "not an envelope"
    path.write_text(json.dumps(row) + "\n")

    with pytest.raises(LaneDispatchJournalError, match="not an envelope"):
        journal.read_recent(10)


def test_a_window_of_only_legacy_rows_is_empty_not_broken(tmp_path: Path) -> None:
    journal = JsonlLaneDispatchJournal(tmp_path)
    journal.record(_record())
    path = tmp_path / "lane-dispatch.jsonl"
    modern = json.loads(path.read_text().splitlines()[0])
    legacy = {key: value for key, value in modern.items() if key != "machine_state"}
    path.write_text("".join(json.dumps(legacy) + "\n" for _ in range(3)))

    history = journal.read_recent(10)

    assert history.entries == ()
    assert history.predating_schema == 3


def _rewrite(path: Path, *rows: dict[str, object]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _stored_row(journal: JsonlLaneDispatchJournal, path: Path) -> dict[str, object]:
    journal.record(_record())
    return json.loads(path.read_text().splitlines()[0])


_CPU_COLUMNS = (
    "declared_cpus",
    "request_cpus",
    "learned_busy_cores",
    "observed_busy_cores",
    "cpu_request_capped",
)


def test_rows_written_before_the_cpu_request_are_older_not_corrupt(
    tmp_path: Path,
) -> None:
    """The SECOND instance of the rule #7138 established for the
    machine-state envelope, and the reason that rule is not named after
    its first instance.

    A row written between #7135 and #7136 carries a perfectly good
    envelope and no cpu columns. It was valid when written, and the
    journal is shared by every worktree on the machine, so a worktree on
    older code is appending such rows right now. Reading it as garbage
    would fail the whole snapshot over history that is merely old."""
    path = tmp_path / "lane-dispatch.jsonl"
    journal = JsonlLaneDispatchJournal(tmp_path)
    modern = _stored_row(journal, path)
    pre_cpu = {k: v for k, v in modern.items() if k not in _CPU_COLUMNS}
    assert "machine_state" in pre_cpu, "this row must still carry the envelope"
    _rewrite(path, pre_cpu, modern, pre_cpu)

    history = journal.read_recent(10)

    assert len(history.entries) == 1, "the readable row must survive"
    assert history.predating_schema == 2
    assert history.entries[0].record.cpu_request.declared_cpus == 8


def test_a_row_predating_both_epochs_is_counted_once(tmp_path: Path) -> None:
    """One count across every epoch: the operator asked how much of the
    window was too old to read, and a row missing two dimensions is
    still one row."""
    path = tmp_path / "lane-dispatch.jsonl"
    journal = JsonlLaneDispatchJournal(tmp_path)
    modern = _stored_row(journal, path)
    ancient = {
        k: v
        for k, v in modern.items()
        if k not in _CPU_COLUMNS and k != "machine_state"
    }
    _rewrite(path, ancient)

    history = journal.read_recent(10)

    assert history.entries == ()
    assert history.predating_schema == 1


def test_a_half_written_cpu_request_is_corruption(tmp_path: Path) -> None:
    """Absent is an epoch; half-present is a writer bug. Guessing the
    missing half would put a scheduling fact into the record that no
    lane ever declared."""
    path = tmp_path / "lane-dispatch.jsonl"
    journal = JsonlLaneDispatchJournal(tmp_path)
    modern = _stored_row(journal, path)
    half = {k: v for k, v in modern.items() if k != "request_cpus"}
    _rewrite(path, half)

    with pytest.raises(LaneDispatchJournalError, match="must\n?\\s*both be integers"):
        journal.read_recent(10)


def test_a_request_above_its_declaration_is_corruption(tmp_path: Path) -> None:
    """The seed-and-ceiling invariant is enforced on the way IN too: a
    hand-edited row asking for more than it declared is a decision no
    policy could have produced, and must not read back as one."""
    path = tmp_path / "lane-dispatch.jsonl"
    journal = JsonlLaneDispatchJournal(tmp_path)
    modern = _stored_row(journal, path)
    modern["request_cpus"] = 99
    _rewrite(path, modern)

    with pytest.raises(LaneDispatchJournalError, match="never exceed"):
        journal.read_recent(10)


def test_an_unmeasured_run_reads_back_as_unmeasured_never_zero(
    tmp_path: Path,
) -> None:
    """Null is a recorded fact — the run was not measured — and must
    stay distinguishable from a measured 0.0 through the round trip, or
    a reader aggregating the column treats the lane as free."""
    journal = JsonlLaneDispatchJournal(tmp_path)
    journal.record(
        _record(
            cpu_request=LaneCpuRequest.resolve(4, None),
            observed_busy_cores=None,
        )
    )
    (entry,) = journal.read_recent(10).entries
    assert entry.record.observed_busy_cores is None
    assert entry.record.cpu_request.learned_busy_cores is None
    assert entry.record.cpu_request.request_cpus == 4


def test_a_measured_zero_survives_as_a_measurement(tmp_path: Path) -> None:
    journal = JsonlLaneDispatchJournal(tmp_path)
    journal.record(_record(observed_busy_cores=0.0))
    (entry,) = journal.read_recent(10).entries
    assert entry.record.observed_busy_cores == 0.0


def test_the_derived_capped_column_is_not_read_back(tmp_path: Path) -> None:
    """`cpu_request_capped` is a jq convenience derived from the other
    three, so the record recomputes it rather than trusting the file: a
    row whose stored flag disagrees with its own numbers must not be
    able to assert the disagreement into the object."""
    path = tmp_path / "lane-dispatch.jsonl"
    journal = JsonlLaneDispatchJournal(tmp_path)
    modern = _stored_row(journal, path)
    assert modern["cpu_request_capped"] is False
    modern["cpu_request_capped"] = True
    _rewrite(path, modern)

    (entry,) = journal.read_recent(10).entries
    assert entry.record.cpu_request.is_capped is False


def test_integer_busy_cores_are_the_same_fact_as_floats(tmp_path: Path) -> None:
    """JSON renders 2.0 as 2, so a whole-number reading must not read
    back as corruption."""
    path = tmp_path / "lane-dispatch.jsonl"
    journal = JsonlLaneDispatchJournal(tmp_path)
    modern = _stored_row(journal, path)
    modern["observed_busy_cores"] = 2
    modern["learned_busy_cores"] = 6
    _rewrite(path, modern)

    (entry,) = journal.read_recent(10).entries
    assert entry.record.observed_busy_cores == 2.0
    assert entry.record.cpu_request.learned_busy_cores == 6.0
