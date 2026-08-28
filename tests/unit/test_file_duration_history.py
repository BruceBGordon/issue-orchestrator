"""The learned-weight store: what it teaches, pins, and refuses to do."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from issue_orchestrator.adapters.json_file_duration_history import (
    FileDurationHistoryError,
    InertFileDurationHistory,
    JsonFileDurationHistory,
)
from issue_orchestrator.infra.file_duration_store import (
    STORE_DIRNAME,
    open_file_duration_history,
)
from issue_orchestrator.ports.file_duration_history import FileDurationHistory

EPOCH = "20260828T120000Z"


def store(tmp_path: Path, window: int = 5) -> JsonFileDurationHistory:
    # A private subdirectory: tmp_path itself carries an autouse
    # fixture's scratch directory, and these tests assert on what the
    # store does and does not leave behind.
    return JsonFileDurationHistory(store_directory(tmp_path), window=window)


def store_directory(tmp_path: Path) -> Path:
    return tmp_path / "store"


def write_history(tmp_path: Path, payload: str) -> None:
    store_directory(tmp_path).mkdir(exist_ok=True)
    (store_directory(tmp_path) / "history.json").write_text(payload, encoding="utf-8")


# --- what a green run teaches -------------------------------------------------


def test_absent_store_is_empty_not_an_error(tmp_path: Path) -> None:
    assert store(tmp_path).pinned_weights(EPOCH) == {}


def test_recorded_durations_come_back_as_their_median(tmp_path: Path) -> None:
    history = store(tmp_path)
    for seconds in (10.0, 30.0, 20.0):
        history.record_success({"tests/x/test_a.py": seconds})
    assert history.pinned_weights(EPOCH) == {"tests/x/test_a.py": 20.0}


def test_rolling_window_forgets_the_oldest_runs(tmp_path: Path) -> None:
    history = store(tmp_path, window=3)
    for seconds in (100.0, 1.0, 1.0, 1.0):
        history.record_success({"tests/x/test_a.py": seconds})
    # The 100s outlier has rolled out of the window entirely; nothing
    # had to be invalidated for the store to forget it.
    assert history.pinned_weights(EPOCH) == {"tests/x/test_a.py": 1.0}


def test_disjoint_writers_both_survive(tmp_path: Path) -> None:
    """Slice lanes write disjoint key sets into one document; a
    read-modify-replace that dropped the other writer's keys would
    silently un-learn two thirds of the suite every run."""
    first = store(tmp_path)
    second = store(tmp_path)
    first.record_success({"tests/x/test_a.py": 5.0})
    second.record_success({"tests/x/test_b.py": 7.0})
    assert first.pinned_weights(EPOCH) == {
        "tests/x/test_a.py": 5.0,
        "tests/x/test_b.py": 7.0,
    }


def test_a_zero_duration_is_a_fact_not_an_absence(tmp_path: Path) -> None:
    """A file whose every test is deselected by the lane's marker
    expression truly costs nothing there; consumers must see 0.0 and
    not fall back to their naive default."""
    history = store(tmp_path)
    history.record_success({"tests/x/test_a.py": 0.0})
    assert history.pinned_weights(EPOCH) == {"tests/x/test_a.py": 0.0}


def test_recording_nothing_creates_nothing(tmp_path: Path) -> None:
    store(tmp_path).record_success({})
    assert not store_directory(tmp_path).exists()


def test_writes_leave_no_temporary_debris(tmp_path: Path) -> None:
    history = store(tmp_path)
    for seconds in (1.0, 2.0, 3.0):
        history.record_success({"tests/x/test_a.py": seconds})
    assert sorted(path.name for path in store_directory(tmp_path).iterdir()) == [
        "history.json",
        "history.lock",
    ]


# --- one gate, one set of weights ---------------------------------------------


def test_a_pinned_epoch_ignores_everything_recorded_after_it(tmp_path: Path) -> None:
    """The invariant the whole pin exists for: slice 1 asks, then
    finishes and teaches the store, then slice 3 asks. Both must be
    answered identically, or their two partitions of one file list can
    drop a file between them."""
    history = store(tmp_path)
    history.record_success({"tests/x/test_a.py": 5.0})
    slice_one = history.pinned_weights(EPOCH)
    history.record_success({"tests/x/test_b.py": 900.0})
    assert history.pinned_weights(EPOCH) == slice_one == {"tests/x/test_a.py": 5.0}


def test_a_separate_process_reads_the_same_pin(tmp_path: Path) -> None:
    """Each slice runs in its own process (its own job, even), so the
    pin has to live on disk, not in an instance."""
    store(tmp_path).record_success({"tests/x/test_a.py": 5.0})
    store(tmp_path).pinned_weights(EPOCH)
    store(tmp_path).record_success({"tests/x/test_a.py": 500.0})
    store(tmp_path).record_success({"tests/x/test_a.py": 500.0})
    store(tmp_path).record_success({"tests/x/test_a.py": 500.0})
    assert store(tmp_path).pinned_weights(EPOCH) == {"tests/x/test_a.py": 5.0}


def test_the_next_gate_sees_what_the_last_one_taught(tmp_path: Path) -> None:
    history = store(tmp_path)
    history.record_success({"tests/x/test_a.py": 5.0})
    history.pinned_weights(EPOCH)
    history.record_success({"tests/x/test_b.py": 9.0})
    assert history.pinned_weights("20260828T130000Z") == {
        "tests/x/test_a.py": 5.0,
        "tests/x/test_b.py": 9.0,
    }


def test_old_pins_are_pruned(tmp_path: Path) -> None:
    history = store(tmp_path)
    history.record_success({"tests/x/test_a.py": 1.0})
    for index in range(14):
        history.pinned_weights(f"epoch-{index:02d}")
    pins = sorted(path.name for path in store_directory(tmp_path).glob("pinned-*.json"))
    assert len(pins) == 10
    assert pins[0] == "pinned-epoch-04.json"


@pytest.mark.parametrize("epoch", ["", "../escape", "with/slash", "a" * 65])
def test_an_unsafe_epoch_is_refused(tmp_path: Path, epoch: str) -> None:
    """The epoch becomes a filename; the grammar is asserted rather
    than trusted."""
    with pytest.raises(ValueError, match="filesystem-safe"):
        store(tmp_path).pinned_weights(epoch)


# --- corrupt state is loud ----------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        '["durations"]',
        '{"other": {}}',
        '{"durations": []}',
        '{"durations": {"tests/x/test_a.py": 3.0}}',
        '{"durations": {"tests/x/test_a.py": [-1]}}',
        '{"durations": {"tests/x/test_a.py": ["3.0"]}}',
        '{"durations": {"tests/x/test_a.py": [null]}}',
        '{"durations": {"tests/x/test_a.py": [NaN]}}',
    ],
)
def test_a_corrupt_history_fails_loudly(tmp_path: Path, payload: str) -> None:
    """Corrupt state is a writer's bug. Degrading to naive behavior
    would hide it and stop the loop learning forever, so the read
    raises and names the file to delete."""
    write_history(tmp_path, payload)
    with pytest.raises(FileDurationHistoryError, match="delete the file"):
        store(tmp_path).pinned_weights(EPOCH)


def test_a_corrupt_pin_fails_loudly(tmp_path: Path) -> None:
    store_directory(tmp_path).mkdir()
    (store_directory(tmp_path) / f"pinned-{EPOCH}.json").write_text(
        '{"weights": {"tests/x/test_a.py": "fast"}}', encoding="utf-8"
    )
    with pytest.raises(FileDurationHistoryError, match="delete the file"):
        store(tmp_path).pinned_weights(EPOCH)


@pytest.mark.parametrize(
    "durations",
    [
        {"tests/x/test_a.py": -1.0},
        {"tests/x/test_a.py": math.nan},
        {"tests/x/test_a.py": math.inf},
        {"tests/x/test_a.py": 3},
        {"": 3.0},
    ],
)
def test_garbage_is_rejected_at_the_boundary(
    tmp_path: Path, durations: dict[str, float]
) -> None:
    """Rejected before it is persisted: a store is forever, and a NaN
    written today is a corrupt-store failure on every later run."""
    with pytest.raises(ValueError):
        store(tmp_path).record_success(durations)
    assert not store_directory(tmp_path).exists()


def test_a_long_window_is_trimmed_on_read_too(tmp_path: Path) -> None:
    """A window written by a wider-window build must not skew this
    build's median; the window bound is enforced on both sides."""
    write_history(
        tmp_path,
        json.dumps({"durations": {"tests/x/test_a.py": [100.0, 100.0, 1.0, 1.0]}}),
    )
    assert store(tmp_path, window=2).pinned_weights(EPOCH) == {"tests/x/test_a.py": 1.0}


def test_a_relative_store_directory_is_refused() -> None:
    with pytest.raises(ValueError):
        JsonFileDurationHistory(Path("durations"))


def test_a_non_positive_window_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        JsonFileDurationHistory(tmp_path, window=0)


# --- where the store lives ----------------------------------------------------


def test_both_implementations_satisfy_the_port(tmp_path: Path) -> None:
    """A renamed method on either side would otherwise only surface as
    an AttributeError inside a gate lane."""
    assert isinstance(store(tmp_path), FileDurationHistory)
    assert isinstance(InertFileDurationHistory(), FileDurationHistory)


def test_the_inert_history_is_always_naive() -> None:
    inert = InertFileDurationHistory()
    inert.record_success({"tests/x/test_a.py": 9.0})
    assert inert.pinned_weights(EPOCH) == {}


def test_the_store_lives_in_the_repository_common_dir(tmp_path: Path) -> None:
    """Shared across every worktree of a repository, beside the lane
    runtime history and the validation timings."""
    (tmp_path / ".git").mkdir()
    open_file_duration_history(tmp_path).record_success({"tests/x/test_a.py": 4.0})
    assert (
        tmp_path / ".git" / "issue-orchestrator" / STORE_DIRNAME / "history.json"
    ).is_file()


def test_a_linked_worktree_learns_from_the_shared_store(tmp_path: Path) -> None:
    """A git worktree's .git is a file pointing at the common dir; both
    it and the main checkout must reach the same weights."""
    common = tmp_path / "main" / ".git"
    (common / "worktrees" / "wt").mkdir(parents=True)
    (common / "worktrees" / "wt" / "commondir").write_text("../..", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(
        f"gitdir: {common / 'worktrees' / 'wt'}", encoding="utf-8"
    )
    open_file_duration_history(tmp_path / "main").record_success(
        {"tests/x/test_a.py": 4.0}
    )
    assert open_file_duration_history(worktree).pinned_weights(EPOCH) == {
        "tests/x/test_a.py": 4.0
    }


def test_outside_a_repository_the_loop_is_inert(tmp_path: Path) -> None:
    history = open_file_duration_history(tmp_path)
    history.record_success({"tests/x/test_a.py": 4.0})
    assert history.pinned_weights(EPOCH) == {}
    assert not list(tmp_path.rglob(STORE_DIRNAME))
