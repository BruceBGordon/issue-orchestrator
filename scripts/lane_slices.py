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
- **Determinism**: identical inputs produce identical slices.

Balance uses LPT (longest processing time first) over measured per-file
durations captured from real gate runs; unknown files weigh 1s. One
file is too fat for file-level balance (its weight exceeds a whole
slice budget), so it is split at test-node granularity via live pytest
collection — also drift-proof, since the node list is collected fresh.

Usage:
    lane_slices.py --group I --of N [--check] FILE...

`--check` prints nothing and exits 0 after verifying the partition
properties (used by the unit tests and available to guardrails).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Measured per-file seconds from full gate runs (2026-08-26, M5 Pro).
# Balance data only: staleness degrades speed, never coverage.
MEASURED_WEIGHTS: dict[str, float] = {
    "tests/integration/test_sandbox_os_boundary.py": 80.0,
    "tests/integration/test_ai_gate_hooks.py": 32.6,
    "tests/integration/test_persistent_review_exchange_integration.py": 23.5,
    "tests/integration/test_onboarding_journey.py": 10.9,
    "tests/integration/test_completion_command_contracts.py": 8.8,
    "tests/integration/test_e2e_runner.py": 5.5,
    "tests/integration/test_claude_execution.py": 35.0,
    "tests/integration/test_codex_execution.py": 65.0,
    "tests/integration/test_live_agent_chain.py": 30.0,
}
DEFAULT_WEIGHT = 1.0

# Files whose measured weight dominates a slice are split at node
# granularity: their collected test ids are dealt round-robin across
# ALL slices, so each slice carries an even share of the fat file.
NODE_SPLIT_FILES = frozenset({"tests/integration/test_sandbox_os_boundary.py"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", type=int, required=True)
    parser.add_argument("--of", type=int, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("files", nargs="+")
    arguments = parser.parse_args()
    if arguments.of < 1 or not (1 <= arguments.group <= arguments.of):
        parser.error("--group must be within 1..--of")
    files = sorted(dict.fromkeys(arguments.files))
    groups = partition(files, arguments.of)
    if arguments.check:
        verify(files, groups)
        return 0
    slice_files = groups[arguments.group - 1]
    targets: list[str] = []
    for path in slice_files:
        if path in NODE_SPLIT_FILES:
            targets.extend(
                collect_node_share(path, arguments.group, arguments.of)
            )
        else:
            targets.append(path)
    print(" ".join(targets))
    return 0


def partition(files: list[str], slice_count: int) -> list[list[str]]:
    """LPT partition; node-split files join every slice."""
    shared = [path for path in files if path in NODE_SPLIT_FILES]
    loads = [0.0] * slice_count
    groups: list[list[str]] = [list(shared) for _ in range(slice_count)]
    for path in shared:
        for index in range(slice_count):
            loads[index] += weight_of(path) / slice_count
    solo = [path for path in files if path not in NODE_SPLIT_FILES]
    for path in sorted(solo, key=lambda p: (-weight_of(p), p)):
        index = loads.index(min(loads))
        groups[index].append(path)
        loads[index] += weight_of(path)
    return groups


def weight_of(path: str) -> float:
    return MEASURED_WEIGHTS.get(path, DEFAULT_WEIGHT)


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
    share = [node for index, node in enumerate(sorted(nodes)) if index % of == group - 1]
    if not share:
        # Fewer tests than slices: coverage beats balance — the whole
        # file rides with slice 1.
        return [path] if group == 1 else []
    return share


def verify(files: list[str], groups: list[list[str]]) -> None:
    seen: list[str] = [path for group in groups for path in group]
    solo_seen = [path for path in seen if path not in NODE_SPLIT_FILES]
    if sorted(set(solo_seen)) != sorted(
        path for path in files if path not in NODE_SPLIT_FILES
    ):
        raise SystemExit("lane_slices: partition lost or duplicated a file")
    if len(solo_seen) != len(set(solo_seen)):
        raise SystemExit("lane_slices: a file appears in more than one slice")
    for path in files:
        if path in NODE_SPLIT_FILES and not all(path in group for group in groups):
            raise SystemExit("lane_slices: node-split file missing from a slice")


if __name__ == "__main__":
    raise SystemExit(main())
