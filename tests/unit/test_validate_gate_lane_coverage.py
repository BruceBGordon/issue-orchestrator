"""Every lane appears exactly once per gate mode — no double-runs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _dry_run(target: str, *variables: str) -> str:
    return subprocess.run(
        ["make", "-n", target, *variables],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    ).stdout


def test_condor_mode_runs_vscode_only_inside_the_flat_fan() -> None:
    tail = _dry_run("validate-pr-raw", "LANE_EXECUTOR=condor")
    assert "test-vscode" not in tail, (
        "condor mode must not also run the sequential vscode tail"
    )
    # -n does not recurse through sub-makes, so ask the flat target
    # itself; the vscode lane's recipe (npm test) must appear exactly once.
    fan = _dry_run("_validate-pr-flat-impl", "LANE_EXECUTOR=condor")
    assert fan.count("npm test") == 1, fan


def test_direct_mode_keeps_the_sequential_vscode_tail() -> None:
    tail = _dry_run("validate-pr-raw", "LANE_EXECUTOR=direct")
    assert tail.count("test-vscode") == 1, tail
