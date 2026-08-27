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
        store.record_success(KEY, runtime)
    assert store.learned_priority(KEY) == 60


def test_window_forgets_old_runs(tmp_path: Path) -> None:
    """A lane whose cost drifts re-converges with no invalidation step:
    the window is the adaptivity."""
    store = _store(tmp_path, window=3)
    for runtime in (600.0, 600.0, 600.0):
        store.record_success(KEY, runtime)
    for runtime in (10.0, 12.0, 11.0):
        store.record_success(KEY, runtime)
    assert store.learned_priority(KEY) == 11


def test_keys_do_not_interfere(tmp_path: Path) -> None:
    store = _store(tmp_path)
    other = LaneWorkKey("test-web")
    store.record_success(KEY, 30.0)
    store.record_success(other, 70.0)
    assert store.learned_priority(KEY) == 30
    assert store.learned_priority(other) == 70


def test_no_temporary_files_survive_a_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_success(KEY, 30.0)
    survivors = [
        path.name
        for path in (tmp_path / "history").iterdir()
        if path.name != f"{KEY.value}.json"
    ]
    assert not survivors, survivors


def test_corrupt_file_raises_and_names_the_remedy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_success(KEY, 30.0)
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
    with pytest.raises(LaneRuntimeHistoryError, match="non-runtime"):
        store.learned_priority(KEY)


def test_record_rejects_nonsense_runtimes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for bad in (float("nan"), float("inf"), -1.0):
        with pytest.raises(ValueError):
            store.record_success(KEY, bad)
    with pytest.raises(ValueError):
        store.record_success(KEY, 5)  # type: ignore[arg-type]
