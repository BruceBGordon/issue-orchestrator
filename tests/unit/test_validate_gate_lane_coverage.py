"""Every lane appears exactly once per gate mode — no double-runs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _dry_run(target: str, *variables: str) -> str:
    # Hermetic against the hosting gate: when this test runs INSIDE a
    # make-driven lane, MAKEFLAGS carries the recursion's own
    # LANE_EXECUTOR and the publish command exports another — both
    # would leak into the subprocess and poison the mode under test.
    import os
    import shutil

    scrubbed = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "MAKEFLAGS",
            "MFLAGS",
            "MAKELEVEL",
            "LANE_EXECUTOR",
            "ISSUE_ORCHESTRATOR_LANE_EXECUTOR",
        }
    }
    make = shutil.which("gmake") or "make"
    return subprocess.run(
        [make, "-n", target, *variables],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=scrubbed,
    ).stdout


def test_condor_mode_runs_vscode_only_inside_the_flat_fan() -> None:
    tail = _dry_run("validate-pr-raw", "LANE_EXECUTOR=condor")
    assert "test-vscode" not in tail, (
        "condor mode must not also run the sequential vscode tail"
    )
    # -n does not recurse through sub-makes, so ask the flat target
    # itself; the vscode lane's recipe (npm test) must appear exactly once.
    fan = _dry_run("_validate-pr-flat-impl", "LANE_EXECUTOR=condor")
    assert fan.count('target="test-vscode"') == 1, fan


def test_direct_mode_keeps_the_sequential_vscode_tail() -> None:
    tail = _dry_run("validate-pr-raw", "LANE_EXECUTOR=direct")
    assert tail.count("test-vscode") == 1, tail  # the tail's sub-make line
