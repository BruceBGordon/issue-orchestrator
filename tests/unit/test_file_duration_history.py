"""The learned-weight store: what it teaches, pins, and refuses to do."""

from __future__ import annotations

import contextlib
import json
import math
import os
import signal
import time
from pathlib import Path
from typing import Iterator

import pytest

from issue_orchestrator.adapters import json_file_duration_history as adapter
from issue_orchestrator.adapters.json_file_duration_history import (
    FileDurationHistoryError,
    InertFileDurationHistory,
    JsonFileDurationHistory,
    LegacyUnstampedPin,
    PinnedEpochExpiredError,
    StampedPin,
    UndatablePin,
    classify_pin_date,
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


def pin_path(tmp_path: Path, epoch: str) -> Path:
    return store_directory(tmp_path) / f"pinned-{epoch}.json"


def write_pin(
    tmp_path: Path, epoch: str, payload: str, age_seconds: float = 0.0
) -> Path:
    store_directory(tmp_path).mkdir(exist_ok=True)
    path = pin_path(tmp_path, epoch)
    path.write_text(payload, encoding="utf-8")
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


HOUR = 60 * 60
DAY = 24 * HOUR


def redate_pin(tmp_path: Path, epoch: str, seconds_ago: float) -> None:
    """Move an existing pin's recorded date without touching its
    weights — what a forward wall-clock correction looks like from the
    pin's point of view."""
    path = pin_path(tmp_path, epoch)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["published_at"] = time.time() - seconds_ago
    path.write_text(json.dumps(payload), encoding="utf-8")


def published(seconds_ago: float) -> str:
    """A pin payload that dates itself ``seconds_ago`` in the past."""
    return json.dumps({"weights": {}, "published_at": time.time() - seconds_ago})


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


def test_a_delayed_slice_still_gets_its_own_gates_pin(tmp_path: Path) -> None:
    """B1. Retention must not guess liveness. A slice can be admitted
    long after its siblings — while other gates come and go — and it
    must still be answered from ITS gate's pin. Evicting by count lets
    a busy day push a live epoch out; the delayed slice then
    republishes from newer history and its partition disagrees with the
    one its siblings already ran, so the combined gate omits some files
    and runs others twice."""
    history = store(tmp_path)
    history.record_success({"tests/x/test_a.py": 5.0})
    original = history.pinned_weights("gate-delayed")

    # Sixty newer epochs: the round-1 reproduction.
    for index in range(60):
        history.record_success({"tests/x/test_b.py": float(index + 1)})
        history.pinned_weights(f"gate-{index:02d}")

    assert history.pinned_weights("gate-delayed") == original
    assert original == {"tests/x/test_a.py": 5.0}


def test_a_pin_records_when_it_was_published(tmp_path: Path) -> None:
    """Retention dates a pin from its own payload, so the pin has to
    carry the timestamp — mtime is metadata a copy or a restore
    rewrites, and a pin that looked older than it is would be evicted
    while its gate was still running."""
    before = time.time()
    store(tmp_path).pinned_weights(EPOCH)
    payload = json.loads(pin_path(tmp_path, EPOCH).read_text(encoding="utf-8"))
    # The stamp is rounded to milliseconds on the way out, so allow for
    # that rounding rather than asserting a strict half-open window.
    assert before - 0.001 <= payload["published_at"] <= time.time() + 0.001


def test_only_age_decides_and_the_bound_is_a_week(tmp_path: Path) -> None:
    """Pins from one to fourteen days old. Everything past the week
    bound goes; everything inside it stays, however many pins there
    are. Nothing else is consulted — a second signal was the round-3
    hole, not a safeguard."""
    for days in (1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14):
        write_pin(tmp_path, f"gate-{days:02d}", published(days * DAY))
    store(tmp_path).pinned_weights("fresh")

    survivors = {path.name for path in store_directory(tmp_path).glob("pinned-*.json")}
    assert survivors == {"pinned-fresh.json"} | {
        f"pinned-gate-{days:02d}.json" for days in (1, 2, 3, 4, 5, 6)
    }


def test_count_pressure_alone_evicts_nothing(tmp_path: Path) -> None:
    """Round 1's failure mode, at extreme pressure: two hundred newer
    epochs, none of them old. Count is not a liveness signal and is no
    longer consulted, so not one pin may be dropped."""
    history = store(tmp_path)
    for index in range(200):
        history.pinned_weights(f"gate-{index:03d}")
    assert len(list(store_directory(tmp_path).glob("pinned-*.json"))) == 200


def test_age_and_count_pressure_combined_cannot_evict_a_live_pin(
    tmp_path: Path,
) -> None:
    """Round 3's failure mode: the reviewer stopped attacking each
    signal separately and did both at once — aged a live pin thirty
    hours *and* published fifty newer epochs, and the fiftieth publish
    evicted it.

    Kept as a regression on the retention rule that replaced all of
    that: age is the only question asked, and thirty hours is not a
    week. Correctness no longer rests on this test passing — an evicted
    pin is now a loud failure rather than a silent recompute — but
    routine gates should never provoke that failure."""
    history = store(tmp_path)
    history.record_success({"tests/x/test_a.py": 5.0})
    live = history.pinned_weights("live-gate")

    redate_pin(tmp_path, "live-gate", 30 * HOUR)
    for index in range(50):
        history.record_success({"tests/x/test_b.py": float(index + 1)})
        history.pinned_weights(f"gate-{index:02d}")

    assert pin_path(tmp_path, "live-gate").exists()
    assert history.pinned_weights("live-gate") == live == {"tests/x/test_a.py": 5.0}


def test_a_forward_clock_correction_cannot_evict_a_live_pin(
    tmp_path: Path,
) -> None:
    """Round 2's failure mode. A wall-clock correction past the age
    bound makes every pin — including the one a still-running gate is
    about to read — look more than a day old at once. Age alone would
    delete them all, that slice would republish from newer history, and
    its partition would disagree with the one its siblings already ran:
    the same omit-and-duplicate signature as round 1.

    Depth is what survives the jump: a live gate's pin is among the
    newest by recorded time no matter what the clock claims."""
    history = store(tmp_path)
    history.record_success({"tests/x/test_a.py": 5.0})
    live = history.pinned_weights("live-gate")

    # The clock corrects forward by thirty hours: every existing pin
    # now dates itself well past the bound.
    write_pin(tmp_path, "sibling-one", published(30 * HOUR))
    write_pin(tmp_path, "sibling-two", published(30 * HOUR))
    redate_pin(tmp_path, "live-gate", 30 * HOUR)

    history.record_success({"tests/x/test_b.py": 900.0})
    history.pinned_weights("later-gate")

    assert pin_path(tmp_path, "live-gate").exists()
    assert history.pinned_weights("live-gate") == live == {"tests/x/test_a.py": 5.0}


def test_an_absurd_neighbour_date_cannot_fail_a_publish(tmp_path: Path) -> None:
    """A stamp of 10**400 is an int no float can hold; converting it
    raised OverflowError straight out of pruning and failed an
    otherwise-good publish (round 2, finding 3). It is not a date, so
    the pin is undatable — and undatable pins are kept."""
    write_pin(
        tmp_path,
        "absurd",
        json.dumps({"weights": {}, "published_at": 10**400}),
        age_seconds=30 * DAY,
    )
    assert store(tmp_path).pinned_weights("fresh") == {}
    assert pin_path(tmp_path, "absurd").exists()


def test_a_pin_with_an_unusable_date_still_answers(tmp_path: Path) -> None:
    """Dating a pin governs RETENTION only. A gate reading its own pin
    must still get its own partition even if the stamp is damaged —
    refusing to answer would be the same coverage hole by another
    route."""
    write_pin(
        tmp_path,
        "damaged-date",
        json.dumps({"weights": {"tests/x/test_a.py": 7.0}, "published_at": "soon"}),
    )
    assert store(tmp_path).pinned_weights("damaged-date") == {"tests/x/test_a.py": 7.0}


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        '["weights"]',
        '{"weights": {}, "published_at": "yesterday"}',
        '{"weights": {}, "published_at": null}',
        '{"weights": {}, "published_at": NaN}',
        '{"weights": {}, "published_at": Infinity}',
        '{"weights": {}, "published_at": 0}',
        '{"published_at": null}',
    ],
)
def test_an_undatable_pin_is_retained_however_old(
    tmp_path: Path, payload: str
) -> None:
    """Round 2, finding 2: every one of these collapsed to "no
    timestamp" and took the legacy mtime path, which DELETES. A pin
    whose age cannot be established is never evicted, whatever its
    mtime says."""
    write_pin(tmp_path, "damaged", payload, age_seconds=30 * DAY)
    assert store(tmp_path).pinned_weights("fresh") == {}
    assert pin_path(tmp_path, "damaged").exists()


def test_an_unstamped_pin_is_dated_by_its_mtime(tmp_path: Path) -> None:
    """Pins written before the payload carried a timestamp can only be
    dated by mtime, which answers a different question — when the bytes
    were last touched, not when the pin was published — so they get
    their own bound rather than sharing the stamped one."""
    write_pin(tmp_path, "legacy", json.dumps({"weights": {}}), age_seconds=2 * DAY)
    store(tmp_path).pinned_weights("fresh-one")
    assert pin_path(tmp_path, "legacy").exists()

    os.utime(pin_path(tmp_path, "legacy"), (time.time() - 8 * DAY,) * 2)
    store(tmp_path).pinned_weights("fresh-two")
    assert not pin_path(tmp_path, "legacy").exists()


def test_a_reader_never_deletes_a_pin(tmp_path: Path) -> None:
    """Pruning belongs to the publish path. A reader that swept would
    be deleting on behalf of a gate it knows nothing about."""
    store(tmp_path).pinned_weights("mine")
    write_pin(tmp_path, "ancient", published(30 * DAY))
    assert store(tmp_path).pinned_weights("mine") == {}
    assert pin_path(tmp_path, "ancient").exists()


def test_pruning_keeps_a_pin_it_cannot_date(tmp_path: Path) -> None:
    """Never delete what could not be positively dated — and never let
    an unreadable neighbour fail a publish that is otherwise fine."""
    write_pin(tmp_path, "damaged", "{not json")
    assert store(tmp_path).pinned_weights("fresh") == {}
    assert pin_path(tmp_path, "damaged").exists()


# --- the three pin-date states are distinct -----------------------------------


def test_a_well_formed_dated_pin_is_stamped(tmp_path: Path) -> None:
    now = time.time()
    path = write_pin(tmp_path, "good", published(60))
    dated = classify_pin_date(path, now)
    assert type(dated) is StampedPin
    assert now - 61 <= dated.published_at <= now


def test_a_pin_document_without_a_date_is_legacy(tmp_path: Path) -> None:
    path = write_pin(tmp_path, "legacy", json.dumps({"weights": {}}), age_seconds=DAY)
    dated = classify_pin_date(path, time.time())
    assert type(dated) is LegacyUnstampedPin


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("garbled", "{not json"),
        ("not_a_mapping", '["weights"]'),
        ("no_weights", '{"other": 1}'),
        ("text_date", '{"weights": {}, "published_at": "yesterday"}'),
        ("null_date", '{"weights": {}, "published_at": null}'),
        ("nan_date", '{"weights": {}, "published_at": NaN}'),
        ("infinite_date", '{"weights": {}, "published_at": Infinity}'),
        ("prehistoric_date", '{"weights": {}, "published_at": 0}'),
        ("absurd_date", '{"weights": {}, "published_at": ' + str(10**400) + "}"),
    ],
)
def test_anything_that_is_not_a_date_is_undatable(
    tmp_path: Path, name: str, payload: str
) -> None:
    """These are the states round 2 collapsed together. Each one has to
    be distinguishable from "a legacy pin with no date", because that
    one is evictable and these are not."""
    path = write_pin(tmp_path, name, payload)
    dated = classify_pin_date(path, time.time())
    assert type(dated) is UndatablePin
    assert name in dated.reason


def test_a_date_far_in_the_future_is_not_a_date(tmp_path: Path) -> None:
    path = write_pin(tmp_path, "future", published(-30 * DAY))
    assert type(classify_pin_date(path, time.time())) is UndatablePin


def test_a_missing_pin_is_undatable(tmp_path: Path) -> None:
    store_directory(tmp_path).mkdir()
    assert type(classify_pin_date(pin_path(tmp_path, "gone"), time.time())) is (
        UndatablePin
    )


@pytest.mark.parametrize("epoch", ["", "../escape", "with/slash", "a" * 65])
def test_an_unsafe_epoch_is_refused(tmp_path: Path, epoch: str) -> None:
    """The epoch becomes a filename; the grammar is asserted rather
    than trusted."""
    with pytest.raises(ValueError, match="filesystem-safe"):
        store(tmp_path).pinned_weights(epoch)


# --- an evicted pin is loud, never recomputed ---------------------------------


def test_a_reader_whose_pin_was_pruned_fails_loudly(tmp_path: Path) -> None:
    """The defect four rounds of retention tuning were circling.

    A lane's deadline excludes time it spent suspended (#7118) and
    suspension is unbounded, so a frozen slice can legitimately come
    back days later — after its gate's snapshot was reclaimed. Every
    previous version quietly recomputed weights from current history
    and handed back a partition that disagreed with the one its
    siblings already ran, so the gate omitted some files and ran others
    twice while going green.

    No amount of retention tuning can prevent that, because liveness is
    not provable from anything the store can see. What IS provable is
    that recomputing is never right: the store refuses, and says so."""
    history = store(tmp_path)
    history.record_success({"tests/x/test_a.py": 5.0})
    assert history.pinned_weights("suspended-gate") == {"tests/x/test_a.py": 5.0}

    pin_path(tmp_path, "suspended-gate").unlink()
    history.record_success({"tests/x/test_b.py": 900.0})

    with pytest.raises(PinnedEpochExpiredError) as raised:
        history.pinned_weights("suspended-gate")

    message = str(raised.value)
    assert "suspended-gate" in message, "the failure must name the epoch"
    assert "7 days" in message, "the failure must state the retention policy"
    assert "Re-run the gate" in message, "the failure must state the remedy"
    assert not pin_path(tmp_path, "suspended-gate").exists(), (
        "a refused epoch must not be quietly republished"
    )


def test_an_expired_pin_is_a_store_fault(tmp_path: Path) -> None:
    """It travels the path already wired for a broken store, so the
    slicer turns it into a failed lane rather than a partition."""
    history = store(tmp_path)
    history.pinned_weights("gone")
    pin_path(tmp_path, "gone").unlink()
    with pytest.raises(FileDurationHistoryError):
        history.pinned_weights("gone")


def test_an_epoch_never_pinned_is_published_not_refused(tmp_path: Path) -> None:
    """The refusal must not swallow the ordinary first ask — that would
    fail every gate instead of every stale slice."""
    history = store(tmp_path)
    history.record_success({"tests/x/test_a.py": 5.0})
    assert history.pinned_weights("brand-new") == {"tests/x/test_a.py": 5.0}


def test_the_ledger_outlives_the_process_that_wrote_it(tmp_path: Path) -> None:
    """Each slice is its own process, so the record of what was pinned
    has to be on disk, not in an instance."""
    store(tmp_path).pinned_weights("earlier-gate")
    pin_path(tmp_path, "earlier-gate").unlink()
    with pytest.raises(PinnedEpochExpiredError):
        store(tmp_path).pinned_weights("earlier-gate")


def test_recording_durations_preserves_the_ledger(tmp_path: Path) -> None:
    """Losing the ledger would turn every evicted pin straight back
    into a silent recompute, so the write path that does not care about
    it must still carry it through."""
    history = store(tmp_path)
    history.pinned_weights("earlier-gate")
    pin_path(tmp_path, "earlier-gate").unlink()
    for index in range(10):
        history.record_success({"tests/x/test_a.py": float(index)})
    with pytest.raises(PinnedEpochExpiredError):
        history.pinned_weights("earlier-gate")


def test_a_corrupt_ledger_fails_loudly(tmp_path: Path) -> None:
    write_history(
        tmp_path, json.dumps({"durations": {}, "pinned_epochs": "everything"})
    )
    with pytest.raises(FileDurationHistoryError, match="delete the file"):
        store(tmp_path).pinned_weights("any")


def test_a_store_written_before_the_ledger_existed_still_works(
    tmp_path: Path,
) -> None:
    """An absent ledger is not corruption — it is a store from before
    the field existed, and the first pin starts one."""
    write_history(tmp_path, json.dumps({"durations": {"tests/x/test_a.py": [4.0]}}))
    assert store(tmp_path).pinned_weights("first") == {"tests/x/test_a.py": 4.0}


# --- the two writes of a publish, and dying between them ----------------------


def replace_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the order in which files become observable."""
    order: list[str] = []
    real = adapter.os.replace

    def spy(source: object, destination: object) -> None:
        order.append(Path(str(destination)).name)
        real(source, destination)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(adapter.os, "replace", spy)
    return order


def test_the_ledger_lands_before_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering IS the safety property. Publishing the snapshot
    first opens a window in which it exists and no ledger records it —
    a consumer adopts it, retention later reclaims it, and the reader
    after that recomputes silently."""
    order = replace_spy(monkeypatch)
    store(tmp_path).pinned_weights("ordered-gate")
    assert order.index("history.json") < order.index("pinned-ordered-gate.json"), (
        f"ledger must be durable before the snapshot is observable: {order}"
    )


def test_a_publish_that_dies_before_its_snapshot_refuses_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fault injection at the second write. What survives is a
    remembered epoch with no snapshot, and the next reader must treat
    that as the loud failure — never as a fresh epoch to recompute."""
    history = store(tmp_path)
    history.record_success({"tests/x/test_a.py": 5.0})
    real = adapter.os.replace

    def die_on_the_snapshot(source: object, destination: object) -> None:
        if Path(str(destination)).name.startswith("pinned-"):
            raise OSError("simulated death before the snapshot landed")
        real(source, destination)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(adapter.os, "replace", die_on_the_snapshot)
    with pytest.raises(FileDurationHistoryError):
        history.pinned_weights("dying-gate")
    monkeypatch.setattr(adapter.os, "replace", real)

    assert not pin_path(tmp_path, "dying-gate").exists()
    history.record_success({"tests/x/test_b.py": 900.0})
    with pytest.raises(PinnedEpochExpiredError):
        history.pinned_weights("dying-gate")


def test_an_unledgered_snapshot_cannot_decay_into_a_silent_recompute(
    tmp_path: Path,
) -> None:
    """The artifact the old ordering left behind — and the one a store
    written before the ledger existed is full of: a snapshot no ledger
    records. Consuming it must repair the ledger, so that when
    retention later reclaims the snapshot the epoch is still refused
    rather than quietly recomputed."""
    history = store(tmp_path)
    history.record_success({"tests/x/test_a.py": 5.0})
    write_pin(
        tmp_path,
        "crashed-gate",
        json.dumps(
            {"weights": {"tests/x/test_a.py": 5.0}, "published_at": time.time()}
        ),
    )

    assert history.pinned_weights("crashed-gate") == {"tests/x/test_a.py": 5.0}

    pin_path(tmp_path, "crashed-gate").unlink()
    history.record_success({"tests/x/test_b.py": 900.0})
    with pytest.raises(PinnedEpochExpiredError):
        history.pinned_weights("crashed-gate")


def test_repairing_the_ledger_needs_no_second_visit(tmp_path: Path) -> None:
    """The repair is durable, so a snapshot adopted by one process is
    protected for every later one — each slice is its own process."""
    write_pin(tmp_path, "legacy-gate", json.dumps({"weights": {}}))
    store(tmp_path).pinned_weights("legacy-gate")
    pin_path(tmp_path, "legacy-gate").unlink()
    with pytest.raises(PinnedEpochExpiredError):
        store(tmp_path).pinned_weights("legacy-gate")


# --- who holds the lock when repair runs --------------------------------------


class WatchdogExpired(Exception):
    """Deliberately not an OSError.

    TimeoutError is one, so a watchdog raised as TimeoutError gets
    caught by the store's own ``except OSError`` and re-reported as a
    lock failure — the deadlock would still fail the test, but under a
    misleading name.
    """


@contextlib.contextmanager
def fails_instead_of_hanging(seconds: float = 5.0) -> Iterator[None]:
    """Bound a call a regression would block on forever.

    This is not timing-based coordination — nothing waits on the alarm
    when the code is correct. It exists because the failure it guards
    is an invisible stall: a nested lock acquisition never returns, so
    without a bound a regression would hang the suite (and, in a gate,
    burn the whole lane deadline in silence) instead of failing.
    """

    def expire(signum: int, frame: object) -> None:
        del signum, frame
        raise WatchdogExpired(
            "call never returned - a nested directory-lock acquisition deadlocks"
        )

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_a_sibling_publishing_while_we_wait_for_the_lock_does_not_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repair path reached from INSIDE the lock.

    A sibling slice publishes between this process's miss and its turn
    at the lock, so the locked re-read finds a snapshot — possibly one
    no ledger records — and must repair it while already holding the
    lock. Repairing through the unlocked entry point instead asks for
    the same lock a second time, and flock belongs to the open file
    description rather than the process, so the second acquisition
    waits on the first and never wakes.

    The earlier repair tests only ever discovered a snapshot on the
    unlocked fast path, which is why they never reached this.
    """
    history = store(tmp_path)
    history.record_success({"tests/x/test_a.py": 5.0})

    real_flock = adapter.fcntl.flock
    published: list[str] = []

    def a_sibling_publishes_just_before_we_get_the_lock(
        handle: object, operation: int
    ) -> None:
        real_flock(handle, operation)  # pyright: ignore[reportArgumentType]
        if published:
            return
        published.append("raced-gate")
        write_pin(
            tmp_path,
            "raced-gate",
            json.dumps(
                {"weights": {"tests/x/test_a.py": 5.0}, "published_at": time.time()}
            ),
        )

    monkeypatch.setattr(
        adapter.fcntl, "flock", a_sibling_publishes_just_before_we_get_the_lock
    )

    with fails_instead_of_hanging():
        weights = history.pinned_weights("raced-gate")

    assert weights == {"tests/x/test_a.py": 5.0}
    assert published == ["raced-gate"], "the race never happened - probe broken"

    # The repair was durable before the snapshot was handed back.
    document = json.loads(
        (store_directory(tmp_path) / "history.json").read_text(encoding="utf-8")
    )
    assert "raced-gate" in document["pinned_epochs"]

    # And it holds: once retention reclaims that snapshot the epoch is
    # refused, not silently recomputed.
    pin_path(tmp_path, "raced-gate").unlink()
    history.record_success({"tests/x/test_b.py": 900.0})
    with fails_instead_of_hanging():
        with pytest.raises(PinnedEpochExpiredError):
            history.pinned_weights("raced-gate")


def test_a_nested_lock_acquisition_is_refused_not_hung(tmp_path: Path) -> None:
    """One owner, one discipline, and a named error the moment anyone
    breaks it — rather than the stall that hid this defect."""
    lock_path = tmp_path / "guard.lock"
    with fails_instead_of_hanging():
        with adapter._DirectoryLock(lock_path):  # noqa: SLF001
            with pytest.raises(FileDurationHistoryError, match="already held"):
                with adapter._DirectoryLock(lock_path):  # noqa: SLF001
                    pass


def test_the_lock_is_reusable_after_release(tmp_path: Path) -> None:
    """The nesting guard must not leak: a released lock is takeable
    again, or the second gate on this store would fail spuriously."""
    lock_path = tmp_path / "guard.lock"
    with fails_instead_of_hanging():
        for _ in range(3):
            with adapter._DirectoryLock(lock_path):  # noqa: SLF001
                pass


def test_a_failed_acquisition_does_not_mark_the_lock_held(tmp_path: Path) -> None:
    """A lock that could not be taken must not look taken afterwards."""
    missing = tmp_path / "no-such-directory" / "guard.lock"
    with pytest.raises(FileDurationHistoryError, match="cannot lock"):
        with adapter._DirectoryLock(missing):  # noqa: SLF001
            pass
    (tmp_path / "no-such-directory").mkdir()
    with fails_instead_of_hanging():
        with adapter._DirectoryLock(missing):  # noqa: SLF001
            pass


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
