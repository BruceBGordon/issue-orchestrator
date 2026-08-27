"""Partition properties of the lane slicer: coverage, disjointness, determinism."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "lane_slices.py"

_spec = importlib.util.spec_from_file_location("lane_slices", SCRIPT)
assert _spec is not None and _spec.loader is not None
lane_slices = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lane_slices)


def test_partition_covers_every_file_exactly_once() -> None:
    files = sorted(f"tests/x/test_{index}.py" for index in range(11))
    groups = lane_slices.partition(files, 3)
    lane_slices.verify(files, groups)
    flattened = [path for group in groups for path in group]
    assert sorted(flattened) == files


def test_unknown_files_still_land_in_a_slice() -> None:
    files = ["tests/x/test_never_measured.py"]
    groups = lane_slices.partition(files, 3)
    assert any("test_never_measured" in path for group in groups for path in group)


def test_partition_is_deterministic() -> None:
    files = sorted(f"tests/x/test_{index}.py" for index in range(9))
    assert lane_slices.partition(files, 3) == lane_slices.partition(files, 3)


def test_heavy_files_spread_across_slices() -> None:
    files = [
        "tests/integration/test_ai_gate_hooks.py",  # 32.6s measured
        "tests/integration/test_persistent_review_exchange_integration.py",  # 23.5s
        "tests/x/test_light_a.py",
        "tests/x/test_light_b.py",
    ]
    groups = lane_slices.partition(sorted(files), 2)
    heavy = {
        "tests/integration/test_ai_gate_hooks.py",
        "tests/integration/test_persistent_review_exchange_integration.py",
    }
    in_first = heavy & set(groups[0])
    in_second = heavy & set(groups[1])
    assert in_first and in_second, "both heavy files ended up in one slice"


def test_node_split_file_joins_every_slice() -> None:
    files = sorted(
        [
            "tests/integration/test_sandbox_os_boundary.py",
            "tests/x/test_light.py",
        ]
    )
    groups = lane_slices.partition(files, 3)
    for group in groups:
        assert "tests/integration/test_sandbox_os_boundary.py" in group
    lane_slices.verify(files, groups)


def test_check_mode_passes_on_the_live_integration_glob() -> None:
    files = sorted(str(p) for p in (REPO_ROOT / "tests/integration").glob("test_*.py"))
    completed = subprocess.run(
        (sys.executable, str(SCRIPT), "--group", "1", "--of", "3", "--check", *files),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
