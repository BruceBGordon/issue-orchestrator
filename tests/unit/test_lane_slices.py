"""Partition properties of the lane slicer, and the weights it learns.

Coverage is the property that must hold no matter what the duration
store contains: staleness, absence, and drift may only cost speed. The
balancing tests therefore pass weights explicitly rather than reading
the machine's real store, and the two tests that exercise the real read
path point the slicer at a fixture repository.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from issue_orchestrator.infra.file_duration_store import open_file_duration_history

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "lane_slices.py"
EPOCH = "20260828T120000Z"

_spec = importlib.util.spec_from_file_location("lane_slices", SCRIPT)
assert _spec is not None and _spec.loader is not None
lane_slices = importlib.util.module_from_spec(_spec)
# Registered before execution: @dataclass resolves annotations through
# sys.modules[cls.__module__], which is None for an unregistered module.
sys.modules["lane_slices"] = lane_slices
_spec.loader.exec_module(lane_slices)


def files(count: int) -> list[str]:
    return sorted(f"tests/x/test_{index}.py" for index in range(count))


def slice_of(groups: tuple[tuple[str, ...], ...], path: str) -> int:
    return next(index for index, group in enumerate(groups) if path in group)


# --- coverage by construction -------------------------------------------------


def test_partition_covers_every_file_exactly_once_without_history() -> None:
    live = files(11)
    plan = lane_slices.build_plan(live, 3, {})
    plan.verify(live)
    assert sorted(path for group in plan.groups for path in group) == live


def test_partition_covers_every_file_exactly_once_with_history() -> None:
    live = files(11)
    weights = {path: float(index) * 7.0 for index, path in enumerate(live)}
    plan = lane_slices.build_plan(live, 3, weights)
    plan.verify(live)
    solo = sorted(
        path for group in plan.groups for path in group if path not in plan.node_split
    )
    assert solo + sorted(plan.node_split) == live


def test_a_file_the_store_has_never_heard_of_still_lands_in_a_slice() -> None:
    live = ["tests/x/test_brand_new.py"]
    plan = lane_slices.build_plan(live, 3, {"tests/x/test_other.py": 900.0})
    assert any("test_brand_new" in path for group in plan.groups for path in group)


def test_coverage_survives_a_store_that_only_knows_dead_files() -> None:
    """Weights for files that no longer exist are inert, never a
    reason for a live file to go unrun."""
    live = files(5)
    weights = {f"tests/x/test_deleted_{index}.py": 500.0 for index in range(4)}
    plan = lane_slices.build_plan(live, 3, weights)
    plan.verify(live)
    assert sorted(path for group in plan.groups for path in group) == live


def test_partition_is_deterministic() -> None:
    live = files(9)
    weights = {live[0]: 12.5, live[4]: 3.0}
    assert lane_slices.build_plan(live, 3, weights) == lane_slices.build_plan(
        live, 3, weights
    )


# --- naive first --------------------------------------------------------------


def test_an_empty_store_is_exactly_an_equal_split() -> None:
    """Run one is naive by design: with nothing learned every file
    weighs the same, so LPT deals them round-robin by name."""
    live = files(11)
    plan = lane_slices.build_plan(live, 3, {})
    assert plan.groups == tuple(tuple(live[index::3]) for index in range(3))
    assert plan.node_split == frozenset()


def test_an_empty_store_never_node_splits_even_with_fewer_files_than_slices() -> None:
    """A file with no history is never *assumed* fat — otherwise the
    naive first run would pay for live collection subprocesses it has
    no evidence it needs."""
    live = files(2)
    plan = lane_slices.build_plan(live, 3, {})
    assert plan.node_split == frozenset()
    assert plan.groups == (("tests/x/test_0.py",), ("tests/x/test_1.py",), ())


def test_a_file_cheaper_than_an_unknown_file_is_never_node_split() -> None:
    """An all-but-free suite (every test deselected by the lane's
    marker expression, say) must not split everything: below the naive
    default weight a collection subprocess cannot pay for itself."""
    live = files(4)
    plan = lane_slices.build_plan(live, 3, {path: 0.01 for path in live})
    assert plan.node_split == frozenset()


# --- balance ------------------------------------------------------------------


def test_heavy_files_are_spread_across_slices() -> None:
    live = sorted(["tests/x/test_heavy_a.py", "tests/x/test_heavy_b.py", *files(2)])
    plan = lane_slices.build_plan(
        live, 2, {"tests/x/test_heavy_a.py": 15.0, "tests/x/test_heavy_b.py": 14.0}
    )
    assert plan.node_split == frozenset(), "fixture must exercise file-level balance"
    assert slice_of(plan.groups, "tests/x/test_heavy_a.py") != slice_of(
        plan.groups, "tests/x/test_heavy_b.py"
    ), "both heavy files ended up in one slice"


def test_a_learned_fat_file_joins_every_slice() -> None:
    live = sorted(["tests/x/test_fat.py", *files(3)])
    plan = lane_slices.build_plan(live, 3, {"tests/x/test_fat.py": 800.0})
    assert plan.node_split == frozenset({"tests/x/test_fat.py"})
    for group in plan.groups:
        assert "tests/x/test_fat.py" in group
    plan.verify(live)


def test_a_fat_file_that_stops_being_fat_is_balanced_as_a_whole_file() -> None:
    """Nothing is pinned: when the suite grows around a big file, its
    weight no longer exceeds a fair share and it goes back to riding in
    one slice — and to teaching the store again."""
    live = sorted(["tests/x/test_fat.py", *files(3)])
    weights = {"tests/x/test_fat.py": 800.0}
    assert lane_slices.build_plan(live, 3, weights).node_split
    weights.update({path: 900.0 for path in files(3)})
    assert not lane_slices.build_plan(live, 3, weights).node_split


def test_a_slice_with_no_files_selects_nothing(tmp_path: Path) -> None:
    """Fewer files than slices leaves a slice empty. It must select
    *nothing* — the Makefile guards on that, because a pytest invoked
    with no arguments collects the whole repository."""
    live = files(2)
    plan = lane_slices.build_plan(live, 3, {})
    assert lane_slices.slice_targets(plan, 3, 3) == []


def test_node_split_targets_replace_the_file_with_its_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = sorted(["tests/x/test_fat.py", *files(3)])
    plan = lane_slices.build_plan(live, 3, {"tests/x/test_fat.py": 800.0})
    monkeypatch.setattr(
        lane_slices,
        "collect_node_share",
        lambda path, group, of: [f"{path}::test_{group}"],
    )
    targets = lane_slices.slice_targets(plan, 2, 3)
    assert "tests/x/test_fat.py::test_2" in targets
    assert "tests/x/test_fat.py" not in targets


def test_a_file_with_fewer_tests_than_slices_rides_whole_with_slice_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It cannot be spread, so do not pretend to: a node-id selection
    would put it in one slice anyway *and* silence its teaching, since
    only whole-file selections record. This is the shape a fat file
    holding a single long journey test actually has."""
    fat = tmp_path / "test_fat.py"
    fat.write_text("def test_only() -> None:\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert lane_slices.collect_node_share("test_fat.py", 1, 3) == ["test_fat.py"]
    assert lane_slices.collect_node_share("test_fat.py", 2, 3) == []
    assert lane_slices.collect_node_share("test_fat.py", 3, 3) == []


def test_a_file_with_enough_tests_is_dealt_across_every_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fat = tmp_path / "test_fat.py"
    fat.write_text(
        "".join(
            f"def test_{index}() -> None:\n    assert True\n" for index in range(6)
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    shares = [lane_slices.collect_node_share("test_fat.py", g, 3) for g in (1, 2, 3)]
    assert [len(share) for share in shares] == [2, 2, 2]
    assert len({node for share in shares for node in share}) == 6


# --- the learning loop closes -------------------------------------------------


def test_recorded_durations_move_a_file_between_slices(tmp_path: Path) -> None:
    """The acceptance property of the whole change: what a green run
    recorded is what the next partition balances on. Same live file
    list, same slice count — only the store changed."""
    (tmp_path / ".git").mkdir()
    history = open_file_duration_history(tmp_path)
    live = sorted(
        [
            "tests/x/test_a.py",
            "tests/x/test_b.py",
            "tests/x/test_c.py",
            "tests/x/test_d.py",
        ]
    )
    history.record_success(
        {
            "tests/x/test_a.py": 10.0,
            "tests/x/test_b.py": 1.0,
            "tests/x/test_c.py": 5.0,
            "tests/x/test_d.py": 5.0,
        }
    )
    before = lane_slices.build_plan(live, 2, dict(history.pinned_weights("gate-one")))

    # test_b turns out to be expensive; five green runs carry the
    # rolling median over.
    for _ in range(5):
        history.record_success({"tests/x/test_b.py": 9.0})
    after = lane_slices.build_plan(live, 2, dict(history.pinned_weights("gate-two")))

    assert slice_of(before.groups, "tests/x/test_b.py") != slice_of(
        after.groups, "tests/x/test_b.py"
    )
    before.verify(live)
    after.verify(live)


def test_the_slicer_reads_the_repository_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read half of the loop end to end: store on disk in a
    repository's common dir, weights in the slicer."""
    (tmp_path / ".git").mkdir()
    open_file_duration_history(tmp_path).record_success({"tests/x/test_a.py": 42.0})
    monkeypatch.setattr(lane_slices, "REPO_ROOT", tmp_path)
    assert lane_slices.pinned_weights(EPOCH) == {"tests/x/test_a.py": 42.0}


def test_the_slicer_fails_the_lane_when_its_pin_has_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slice that outlived its gate's snapshot must take the lane
    down, not partition on freshly recomputed weights. The Makefile
    recipe aborts on a non-zero slicer, so SystemExit here is a red
    lane — the same route a corrupt store already takes."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(lane_slices, "REPO_ROOT", tmp_path)
    history = open_file_duration_history(tmp_path)
    history.record_success({"tests/x/test_a.py": 5.0})
    lane_slices.pinned_weights("suspended-gate")

    store = tmp_path / ".git" / "issue-orchestrator" / "file-durations"
    (store / "pinned-suspended-gate.json").unlink()

    with pytest.raises(SystemExit) as raised:
        lane_slices.pinned_weights("suspended-gate")
    assert "suspended-gate" in str(raised.value)
    assert "Re-run the gate" in str(raised.value)


def test_the_slicer_is_naive_outside_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lane_slices, "REPO_ROOT", tmp_path)
    assert lane_slices.pinned_weights(EPOCH) == {}


# --- the invariant check is not vacuous ---------------------------------------


def test_verify_rejects_a_lost_file() -> None:
    live = files(4)
    plan = lane_slices.build_plan(live, 2, {})
    damaged = lane_slices.SlicePlan(
        (plan.groups[0][:-1], plan.groups[1]), plan.node_split
    )
    with pytest.raises(SystemExit, match="lost or duplicated"):
        damaged.verify(live)


def test_verify_rejects_a_duplicated_file() -> None:
    live = files(4)
    plan = lane_slices.build_plan(live, 2, {})
    damaged = lane_slices.SlicePlan(
        (plan.groups[0], (*plan.groups[1], plan.groups[0][0])), plan.node_split
    )
    with pytest.raises(SystemExit, match="lost or duplicated"):
        damaged.verify(live)


def test_verify_rejects_a_node_split_file_missing_from_a_slice() -> None:
    live = sorted(["tests/x/test_fat.py", *files(3)])
    plan = lane_slices.build_plan(live, 3, {"tests/x/test_fat.py": 800.0})
    damaged = lane_slices.SlicePlan(
        (
            plan.groups[0],
            tuple(p for p in plan.groups[1] if p != "tests/x/test_fat.py"),
            plan.groups[2],
        ),
        plan.node_split,
    )
    with pytest.raises(SystemExit, match="missing from a slice"):
        damaged.verify(live)


# --- the live gate ------------------------------------------------------------


def test_check_mode_passes_on_the_live_integration_glob(tmp_path: Path) -> None:
    """End to end on the REAL file list, as a script, through the real
    store path — but against an isolated repository, so a unit-suite
    run neither pins into the shared store nor inherits a pin that
    could later age out and refuse itself."""
    isolated = tmp_path / "repo"
    (isolated / "scripts").mkdir(parents=True)
    (isolated / ".git").mkdir()
    script = isolated / "scripts" / "lane_slices.py"
    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    live = sorted(str(p) for p in (REPO_ROOT / "tests/integration").glob("test_*.py"))
    completed = subprocess.run(
        (
            sys.executable,
            str(script),
            "--group",
            "1",
            "--of",
            "3",
            "--epoch",
            "live-glob-check",
            "--check",
            *live,
        ),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_check_mode_goes_through_the_real_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verification must exercise the same owner path as a real run,
    weights included. A --check that partitioned on stand-in weights
    would be verifying a partition no gate ever gets, so it pins its
    epoch exactly as slice one does."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(lane_slices, "REPO_ROOT", tmp_path)
    open_file_duration_history(tmp_path).record_success({"tests/x/test_a.py": 9.0})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lane_slices.py", "--group", "1", "--of", "2",
            "--epoch", "accepted-check", "--check",
            "tests/x/test_a.py", "tests/x/test_b.py",
        ],
    )
    assert lane_slices.main() == 0
    pins = list(tmp_path.rglob("pinned-accepted-check.json"))
    assert pins, "check mode must pin through the real store, not bypass it"
    assert json.loads(pins[0].read_text(encoding="utf-8"))["weights"] == {
        "tests/x/test_a.py": 9.0
    }


def test_the_baked_constants_are_gone() -> None:
    """Deleted, not deprecated: a constant left behind is a second
    source of truth waiting to be read again."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "MEASURED_WEIGHTS" not in source
    assert "NODE_SPLIT_FILES" not in source


def test_every_slice_of_one_gate_partitions_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coverage invariant across processes, not just within one.

    Slice 1 asks for the weights, runs, and teaches the store what it
    measured; slice 3 is admitted minutes later and asks. Pinned to one
    gate epoch they get the same weights and therefore the same
    partition — unpinned, slice 3 would compute a different one, and
    two different partitions of one file list can leave a file unrun in
    a gate that goes green.
    """
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(lane_slices, "REPO_ROOT", tmp_path)
    history = open_file_duration_history(tmp_path)
    live = files(9)
    history.record_success({path: 2.0 for path in live})

    first = lane_slices.build_plan(live, 3, lane_slices.pinned_weights(EPOCH))
    for _ in range(5):
        history.record_success({live[0]: 400.0, live[8]: 300.0})
    last = lane_slices.build_plan(live, 3, lane_slices.pinned_weights(EPOCH))

    assert first == last
    union = sorted(path for group in last.groups for path in group)
    assert union == live
