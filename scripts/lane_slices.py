#!/usr/bin/env python3
"""Partition a test-file list into balanced lane slices.

Prints the pytest arguments for one slice of a suite so the Makefile
can run each slice as its own validation lane. Two properties matter
more than balance:

- **Coverage by construction**: the partition is computed from the file
  list passed on the command line (the Makefile's live wildcard), every
  input appears in exactly one slice, and a file this script has never
  heard of still lands in a slice with a default weight. A new test
  file cannot silently fall out of the gate.
- **Determinism**: identical inputs and identical history produce
  identical slices.

Balance uses LPT (longest processing time first) over the per-file
durations the slice lanes *learned* from their own successful runs
(``infra/pytest_file_durations.py`` captures them; the store lives in
the git common dir). A file with no history weighs ``DEFAULT_WEIGHT``,
so an empty store is exactly an unweighted equal split: the first run
is naive by design and every run after is sharper. Nothing is baked, so
there is nothing to regenerate and nothing to invalidate — staleness
may only cost speed, never coverage.

A file whose learned weight exceeds one slice's fair share is too fat
for file-level balance, so it is split at test-node granularity via
live pytest collection — also drift-proof, since the node list is
collected fresh. A file with no history is never *assumed* fat: unknown
means naive, not special.

The weights are read *pinned to an epoch*, not live. The slices of one
gate ask minutes apart and each teaches the store as it finishes, so an
unpinned read would hand slice 3 a different partition than slice 1 —
and two different partitions of one file list can drop a file between
them. The Makefile stamps one epoch per gate and every slice of that
gate is answered with the snapshot the first of them pinned.

Usage:
    lane_slices.py --group I --of N --epoch E [--check] FILE...

`--check` prints nothing and exits 0 after verifying the partition
properties (used by the unit tests and available to guardrails). It
does not consult the weight store: coverage holds for any weights, and
a verification run should not pin an epoch it has no gate for.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]

# What an unmeasured file is assumed to cost. Its only job is to keep
# unknown files comparable to each other, which is why an all-unknown
# suite partitions exactly like an unweighted equal split.
DEFAULT_WEIGHT = 1.0


@dataclass(frozen=True)
class SlicePlan:
    """A computed partition and the files it spreads at node level.

    The plan owns its own invariant check: whoever builds one can ask
    it to prove coverage without knowing how it was balanced.
    """

    groups: tuple[tuple[str, ...], ...]
    node_split: frozenset[str]

    def verify(self, files: Sequence[str]) -> None:
        """Fail loudly unless this plan is a partition of ``files``."""
        unknown = self.node_split - set(files)
        if unknown:
            raise SystemExit(f"lane_slices: node-split file not in the input: {unknown}")
        placed = [path for group in self.groups for path in group]
        solo_placed = sorted(path for path in placed if path not in self.node_split)
        solo_expected = sorted(path for path in files if path not in self.node_split)
        if solo_placed != solo_expected:
            raise SystemExit("lane_slices: partition lost or duplicated a file")
        for path in self.node_split:
            if not all(path in group for group in self.groups):
                raise SystemExit("lane_slices: node-split file missing from a slice")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", type=int, required=True)
    parser.add_argument("--of", type=int, required=True)
    parser.add_argument(
        "--epoch",
        help=(
            "Gate-run stamp; every slice of one gate must pass the same one. "
            "Required unless --check."
        ),
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("files", nargs="+")
    arguments = parser.parse_args()
    if arguments.of < 1 or not (1 <= arguments.group <= arguments.of):
        parser.error("--group must be within 1..--of")
    files = sorted(dict.fromkeys(arguments.files))
    if arguments.check:
        # Coverage is a structural property of the partition and holds
        # for ANY weights, so verification neither reads nor publishes
        # them. Keeping --check off the shared store matters: pinning
        # an epoch is a durable act, and a verification run has no gate
        # behind it to justify one.
        build_plan(files, arguments.of, {}).verify(files)
        return 0
    if not arguments.epoch:
        parser.error("--epoch is required unless --check")
    plan = build_plan(files, arguments.of, pinned_weights(arguments.epoch))
    print(" ".join(slice_targets(plan, arguments.group, arguments.of)))
    return 0


def pinned_weights(epoch: str) -> dict[str, float]:
    """Per-file seconds learned from this repository's own green runs.

    Pinned to the gate epoch, so every slice of one gate balances on
    identical weights however far apart they are dispatched. A store
    this cannot read is a bug in whatever wrote it: fail with the
    message that names the file, never guess a partition from garbage.
    """
    from issue_orchestrator.adapters.json_file_duration_history import (
        FileDurationHistoryError,
    )
    from issue_orchestrator.infra.file_duration_store import (
        open_file_duration_history,
    )

    try:
        return dict(open_file_duration_history(REPO_ROOT).pinned_weights(epoch))
    except FileDurationHistoryError as error:
        raise SystemExit(f"lane_slices: {error}") from error


def build_plan(
    files: Sequence[str], slice_count: int, weights: Mapping[str, float]
) -> SlicePlan:
    """LPT partition; files too fat to balance join every slice."""
    node_split = fat_files(files, slice_count, weights)
    loads = [0.0] * slice_count
    groups: list[list[str]] = [sorted(node_split) for _ in range(slice_count)]
    for path in sorted(node_split):
        for index in range(slice_count):
            loads[index] += weight_of(path, weights) / slice_count
    solo = [path for path in files if path not in node_split]
    for path in sorted(solo, key=lambda p: (-weight_of(p, weights), p)):
        index = loads.index(min(loads))
        groups[index].append(path)
        loads[index] += weight_of(path, weights)
    return SlicePlan(tuple(tuple(group) for group in groups), node_split)


def fat_files(
    files: Sequence[str], slice_count: int, weights: Mapping[str, float]
) -> frozenset[str]:
    """Files whose learned weight exceeds one slice's fair share.

    Two guards keep this naive-first. A file with no history is never
    assumed fat, so an empty store never node-splits anything. And a
    file cheaper than an *unknown* file is never split either: node
    splitting costs a live collection subprocess, which cannot pay for
    itself below the default weight (it also keeps a suite whose
    measured costs are all near zero from splitting everything).
    """
    fair_share = sum(weight_of(path, weights) for path in files) / slice_count
    threshold = max(fair_share, DEFAULT_WEIGHT)
    return frozenset(
        path for path in files if path in weights and weights[path] > threshold
    )


def weight_of(path: str, weights: Mapping[str, float]) -> float:
    return weights.get(path, DEFAULT_WEIGHT)


def slice_targets(plan: SlicePlan, group: int, of: int) -> list[str]:
    """The pytest arguments for one slice of a computed plan."""
    targets: list[str] = []
    for path in plan.groups[group - 1]:
        if path in plan.node_split:
            targets.extend(collect_node_share(path, group, of))
        else:
            targets.append(path)
    return targets


def collect_node_share(path: str, group: int, of: int) -> list[str]:
    """Collect the file's test ids live and take this slice's share."""
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            path,
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    nodes = [
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip().startswith(f"{path}::")
    ]
    if completed.returncode != 0 or not nodes:
        raise SystemExit(
            f"lane_slices: could not collect test ids from {path}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    if len(nodes) < of:
        # Fewer tests than slices: the file cannot actually be spread,
        # so do not pretend to. It rides WHOLE with slice 1 — coverage
        # beats balance, and a whole-file selection is also the only
        # kind that teaches, so a fat file that happens to hold one
        # test keeps its weight current instead of freezing at the
        # measurement that made it fat.
        return [path] if group == 1 else []
    return [node for index, node in enumerate(sorted(nodes)) if index % of == group - 1]


if __name__ == "__main__":
    raise SystemExit(main())
