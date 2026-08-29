"""Rolling-median behavior and failure honesty of the history store."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.adapters.json_lane_runtime_history import (
    JsonLaneRuntimeHistory,
    LaneRuntimeHistoryError,
)
from issue_orchestrator.domain.lane_execution import LaneWorkKey

KEY = LaneWorkKey("test-unit")


def _store(tmp_path: Path, window: int = 5) -> JsonLaneRuntimeHistory:
    return JsonLaneRuntimeHistory(tmp_path / "history", window=window)


def test_absence_is_naive_not_an_error(tmp_path: Path) -> None:
    assert _store(tmp_path).learned_priority(KEY) == 0


def test_priority_is_the_rolling_median(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for runtime in (30.0, 90.0, 60.0):
        store.record_success(KEY, runtime, None)
    assert store.learned_priority(KEY) == 60


def test_window_forgets_old_runs(tmp_path: Path) -> None:
    """A lane whose cost drifts re-converges with no invalidation step:
    the window is the adaptivity."""
    store = _store(tmp_path, window=3)
    for runtime in (600.0, 600.0, 600.0):
        store.record_success(KEY, runtime, None)
    for runtime in (10.0, 12.0, 11.0):
        store.record_success(KEY, runtime, None)
    assert store.learned_priority(KEY) == 11


def test_keys_do_not_interfere(tmp_path: Path) -> None:
    store = _store(tmp_path)
    other = LaneWorkKey("test-web")
    store.record_success(KEY, 30.0, None)
    store.record_success(other, 70.0, None)
    assert store.learned_priority(KEY) == 30
    assert store.learned_priority(other) == 70


def test_no_temporary_files_survive_a_record(tmp_path: Path) -> None:
    """Atomic-replace temporaries must not accumulate; the per-key
    .lock sibling is the one deliberate persistent artifact."""
    store = _store(tmp_path)
    store.record_success(KEY, 30.0, None)
    survivors = [
        path.name
        for path in (tmp_path / "history").iterdir()
        if path.name not in (f"{KEY.value}.json", f"{KEY.value}.lock")
    ]
    assert not survivors, survivors


def test_an_unmeasured_lane_creates_no_sibling_file(tmp_path: Path) -> None:
    """The CPU file appears only for lanes that were measured, so the
    sibling directory stays a census of what is actually known."""
    store = _store(tmp_path)
    store.record_success(KEY, 30.0, None)
    assert not (tmp_path / "history" / "busy-cores").exists()


def test_no_temporary_files_survive_a_measured_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_success(KEY, 30.0, 4.0)
    survivors = [
        path.name
        for path in (tmp_path / "history" / "busy-cores").iterdir()
        if path.name != f"{KEY.value}.json"
    ]
    assert not survivors, survivors


def test_corrupt_file_raises_and_names_the_remedy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_success(KEY, 30.0, None)
    path = tmp_path / "history" / f"{KEY.value}.json"
    path.write_text("{definitely not json", encoding="utf-8")
    with pytest.raises(LaneRuntimeHistoryError, match="delete the file"):
        store.learned_priority(KEY)


def test_wrong_shape_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = tmp_path / "history" / f"{KEY.value}.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"runtimes": "fast"}', encoding="utf-8")
    with pytest.raises(LaneRuntimeHistoryError, match="unexpected shape"):
        store.learned_priority(KEY)


def test_non_runtime_entry_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = tmp_path / "history" / f"{KEY.value}.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"runtimes": [30.0, "NaN?", 60.0]}', encoding="utf-8")
    with pytest.raises(LaneRuntimeHistoryError, match="non-measurement runtimes"):
        store.learned_priority(KEY)


def test_record_rejects_nonsense_runtimes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for bad in (float("nan"), float("inf"), -1.0):
        with pytest.raises(ValueError):
            store.record_success(KEY, bad, None)
    with pytest.raises(ValueError):
        # The annotation's numeric tower accepts an int; the runtime
        # guard does not — recorded runtimes are always measured floats.
        store.record_success(KEY, 5, None)


def test_concurrent_records_both_persist(tmp_path: Path) -> None:
    """B2 (#7117 review): the store is shared across worktrees, so two
    gates can record the same lane simultaneously. An unlocked
    read-modify-replace loses one observation; the per-key lock must
    keep both."""
    import threading

    store = _store(tmp_path)
    barrier = threading.Barrier(2)

    def record(value: float) -> None:
        barrier.wait()
        store.record_success(KEY, value, None)

    threads = [
        threading.Thread(target=record, args=(value,))
        for value in (10.0, 20.0)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    import json

    persisted = json.loads(
        (tmp_path / "history" / f"{KEY.value}.json").read_text()
    )["runtimes"]
    assert sorted(persisted) == [10.0, 20.0], persisted


def test_negative_integer_entry_is_corrupt(tmp_path: Path) -> None:
    """B3 (#7117 review): a negative int is as corrupt as a NaN — the
    fail-loud contract covers both representations."""
    store = _store(tmp_path)
    path = tmp_path / "history" / f"{KEY.value}.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"runtimes": [-1]}', encoding="utf-8")
    with pytest.raises(LaneRuntimeHistoryError, match="non-measurement runtimes"):
        store.learned_priority(KEY)


def test_boolean_entry_is_corrupt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = tmp_path / "history" / f"{KEY.value}.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"runtimes": [true]}', encoding="utf-8")
    with pytest.raises(LaneRuntimeHistoryError, match="non-measurement runtimes"):
        store.learned_priority(KEY)


def test_short_write_never_replaces_valid_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B5 (#7117 review): POSIX os.write may persist fewer bytes than
    asked without raising (storage exhausted). That must fail loudly,
    keep the previous valid history intact, and leave no temporary —
    never install a truncated file and report success."""
    import os as _os

    store = _store(tmp_path)
    store.record_success(KEY, 30.0, None)
    path = tmp_path / "history" / f"{KEY.value}.json"
    intact = path.read_text()

    real_write = _os.write

    def half_write(fd: int, data: bytes) -> int:
        half = data[: len(data) // 2]
        real_write(fd, half)
        return len(half)

    monkeypatch.setattr(
        "issue_orchestrator.adapters.json_lane_runtime_history.os.write",
        half_write,
    )
    with pytest.raises(LaneRuntimeHistoryError, match="short write"):
        store.record_success(KEY, 60.0, None)
    monkeypatch.undo()

    assert path.read_text() == intact, "a short write replaced valid history"
    survivors = [
        name.name
        for name in (tmp_path / "history").iterdir()
        if name.name not in (f"{KEY.value}.json", f"{KEY.value}.lock")
    ]
    assert not survivors, f"short write leaked temporaries: {survivors}"
    assert store.learned_priority(KEY) == 30


def test_never_measured_cpu_is_unknown_not_zero(tmp_path: Path) -> None:
    """Absence must stay distinguishable from a measured 0.0: returning
    zero here would floor every lane's request at one core on the
    strength of a measurement nobody took."""
    store = _store(tmp_path)
    store.record_success(KEY, 30.0, None)
    assert store.learned_busy_cores(KEY) is None
    assert store.learned_priority(KEY) == 30


def test_cpu_demand_is_the_rolling_median(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for cores in (6.0, 8.0, 7.0):
        store.record_success(KEY, 30.0, cores)
    assert store.learned_busy_cores(KEY) == 7.0


def test_a_measured_zero_is_a_measurement(tmp_path: Path) -> None:
    """A provider-wait lane genuinely burns ~0 cores. That is evidence,
    and it must not read back as 'unmeasured'."""
    store = _store(tmp_path)
    store.record_success(KEY, 30.0, 0.0)
    assert store.learned_busy_cores(KEY) == 0.0


def test_the_two_dimensions_roll_independently(tmp_path: Path) -> None:
    """Runtimes and busy cores are NOT parallel arrays: every backend
    reports a runtime, only a measuring backend reports cores. Pairing
    them positionally would attribute one run's CPU to another run's
    duration — here the window drops old runtimes while the two
    measurements, taken first, must survive."""
    store = _store(tmp_path, window=3)
    store.record_success(KEY, 100.0, 4.0)
    store.record_success(KEY, 100.0, 6.0)
    for runtime in (10.0, 12.0, 11.0):
        store.record_success(KEY, runtime, None)
    assert store.learned_priority(KEY) == 11
    assert store.learned_busy_cores(KEY) == 5.0


def test_cpu_window_forgets_old_measurements(tmp_path: Path) -> None:
    store = _store(tmp_path, window=3)
    for cores in (12.0, 12.0, 12.0):
        store.record_success(KEY, 30.0, cores)
    for cores in (2.0, 2.0, 2.0):
        store.record_success(KEY, 30.0, cores)
    assert store.learned_busy_cores(KEY) == 2.0


def test_history_written_before_the_cpu_dimension_reads_as_unmeasured(
    tmp_path: Path,
) -> None:
    """A pre-existing store has no busy_cores key at all. Absence of a
    dimension is the same naive state as an absent file — it must not
    be mistaken for corruption and blow up the gate."""
    store = _store(tmp_path)
    path = tmp_path / "history" / f"{KEY.value}.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"runtimes": [30.0, 60.0]}', encoding="utf-8")
    assert store.learned_busy_cores(KEY) is None
    assert store.learned_priority(KEY) == 45
    # ...and the next measured run upgrades it in place.
    store.record_success(KEY, 30.0, 3.0)
    assert store.learned_busy_cores(KEY) == 3.0


def test_corrupt_cpu_dimension_is_as_loud_as_a_corrupt_runtime(
    tmp_path: Path,
) -> None:
    """A sibling file that holds garbage is a writer bug, and gets the
    same fail-loud treatment as the runtime list."""
    store = _store(tmp_path)
    path = tmp_path / "history" / "busy-cores" / f"{KEY.value}.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"busy_cores": "eight"}', encoding="utf-8")
    with pytest.raises(LaneRuntimeHistoryError, match="unexpected shape"):
        store.learned_busy_cores(KEY)

    path.write_text('{"busy_cores": [8.0, -1.0]}', encoding="utf-8")
    # The message names WHICH dimension is corrupt: a reader who is
    # told "runtimes" while the busy_cores list is the broken one
    # deletes the wrong evidence.
    with pytest.raises(
        LaneRuntimeHistoryError, match="non-measurement busy_cores"
    ):
        store.learned_busy_cores(KEY)


def test_record_rejects_nonsense_cpu_measurements(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for bad in (float("nan"), float("inf"), -1.0):
        with pytest.raises(ValueError, match="busy_cores"):
            store.record_success(KEY, 30.0, bad)
    with pytest.raises(ValueError, match="busy_cores"):
        # The annotation's numeric tower accepts an int; the runtime
        # guard does not — measured busy cores are always floats.
        store.record_success(KEY, 30.0, 8)  # type: ignore[arg-type]


def test_a_rejected_measurement_records_nothing_at_all(tmp_path: Path) -> None:
    """Validation happens before the lock: a bad CPU figure must not
    slip its run's runtime into the store as a side effect."""
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.record_success(KEY, 30.0, float("nan"))
    assert store.learned_priority(KEY) == 0
    assert store.learned_busy_cores(KEY) is None


def _legacy_write(directory: Path, work_key: LaneWorkKey, runtimes: list[float]) -> None:
    """Rewrite the shared file exactly as the pre-#7131 writer does.

    Mirrors ``JsonLaneRuntimeHistory._write`` as of c94da53 (#7122):
    the same single-key sorted payload, installed by the same
    ``mkstemp`` + ``os.replace``, under the same per-key lock. This is
    not a hypothetical — the store is shared by every worktree of the
    repository, and a worktree checked out before this change runs
    gates against it.
    """
    import fcntl
    import json
    import os
    import tempfile

    path = directory / f"{work_key.value}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        payload = json.dumps({"runtimes": runtimes}, sort_keys=True).encode("utf-8")
        handle, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        os.write(handle, payload)
        os.close(handle)
        os.replace(temporary, path)


def test_a_legacy_writer_cannot_erase_the_cpu_dimension(tmp_path: Path) -> None:
    """A (#7136 review), reproduced: with both dimensions inside one
    file, a gate from ANY worktree on older code rewrote it with
    runtimes only and the learned CPU evidence was gone. The sibling
    file is the fix — older code does not know the path, so it cannot
    replace it."""
    directory = tmp_path / "history"
    store = _store(tmp_path)
    for cores in (7.0, 8.0, 9.0):
        store.record_success(KEY, 30.0, cores)
    assert store.learned_busy_cores(KEY) == 8.0

    _legacy_write(directory, KEY, [11.0, 12.0, 13.0])

    assert store.learned_busy_cores(KEY) == 8.0, (
        "an older worktree's writer erased the CPU dimension"
    )
    # ...and the legacy writer's own dimension is read normally.
    assert store.learned_priority(KEY) == 12


def test_the_shared_file_keeps_exactly_the_legacy_payload(tmp_path: Path) -> None:
    """The other half of the interop contract: this writer must leave
    the shared file byte-identical to what older code writes, so an
    older reader never meets a shape it does not expect — and so the
    legacy simulation above cannot silently drift from reality."""
    import json

    store = _store(tmp_path)
    store.record_success(KEY, 30.0, 4.0)
    written = (tmp_path / "history" / f"{KEY.value}.json").read_text()
    assert written == json.dumps({"runtimes": [30.0]}, sort_keys=True)
    assert "busy_cores" not in written


def test_a_legacy_reader_still_reads_a_measured_lane(tmp_path: Path) -> None:
    """Interop in the other direction: whatever this store writes, the
    pre-#7131 reader's one expectation — a dict with a runtimes list —
    must still hold."""
    import json

    store = _store(tmp_path)
    store.record_success(KEY, 42.0, 4.0)
    payload = json.loads((tmp_path / "history" / f"{KEY.value}.json").read_text())
    assert list(payload) == ["runtimes"]
    assert payload["runtimes"] == [42.0]


def test_the_cpu_dimension_lives_beside_not_inside(tmp_path: Path) -> None:
    """A subdirectory, not a `<key>.cpu.json` suffix: work keys may
    contain dots, so a lane named `test-unit.cpu` would otherwise own
    `test-unit`'s CPU file."""
    store = _store(tmp_path)
    dotted = LaneWorkKey("execenv.memory-ok")
    colliding = LaneWorkKey("execenv.memory-ok.cpu")
    store.record_success(dotted, 30.0, 2.0)
    store.record_success(colliding, 30.0, 6.0)
    assert store.learned_busy_cores(dotted) == 2.0
    assert store.learned_busy_cores(colliding) == 6.0


def test_concurrent_measured_records_both_persist(tmp_path: Path) -> None:
    """The legacy lock still serializes writers, and it now covers the
    sibling file too — an interleave there would lose a measurement
    exactly as it once lost a runtime (B2, #7117 review)."""
    import json
    import threading

    store = _store(tmp_path)
    barrier = threading.Barrier(2)

    def record(value: float) -> None:
        barrier.wait()
        store.record_success(KEY, 30.0, value)

    threads = [
        threading.Thread(target=record, args=(value,)) for value in (3.0, 5.0)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    persisted = json.loads(
        (tmp_path / "history" / "busy-cores" / f"{KEY.value}.json").read_text()
    )["busy_cores"]
    assert sorted(persisted) == [3.0, 5.0], persisted
