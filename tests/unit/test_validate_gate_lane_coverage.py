"""Every lane appears exactly once per gate mode — and cannot be
vacuously skipped by files named after targets.

Collision planting happens in an isolated temporary fixture (`make -C`
into a temp dir against the repo's Makefile), never in the repo tree:
an earlier version of this test planted collisions in the repo root
and six of them escaped into a commit. The debris guard at the bottom
exists so that class of escape fails loudly forever.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

FLAT_FAN_LANES = (
    "typecheck",
    "lint-arch",
    "lint-complexity",
    "test-unit",
    "test-simulated-core",
    "test-simulated-agent",
    "test-integration-core-slice-1",
    "test-integration-core-slice-2",
    "test-integration-core-slice-3",
    "test-integration-agent-claude",
    "test-integration-agent-codex",
    "test-integration-agent-chain",
    "test-integration-core-live-codex",
    "test-web",
    "test-vscode",
)

# Names whose collision-sensitivity has already bitten once.
COLLISION_SENSITIVE_NAMES = (*FLAT_FAN_LANES, "FORCE", "ensure-uv")


def _scrubbed_environment() -> dict[str, str]:
    # Hermetic against the hosting gate: when this test runs INSIDE a
    # make-driven lane, MAKEFLAGS carries the recursion's own
    # LANE_EXECUTOR and the publish command exports another — both
    # would leak into the subprocess and poison the mode under test.
    return {
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


def _dry_run(working_directory: Path, target: str, *variables: str) -> str:
    make = shutil.which("gmake") or "make"
    completed = subprocess.run(
        [
            make,
            "-C",
            str(working_directory),
            "-f",
            str(REPO_ROOT / "Makefile"),
            "-n",
            target,
            *variables,
        ],
        capture_output=True,
        text=True,
        env=_scrubbed_environment(),
    )
    return completed.stdout


def _plant_collisions(directory: Path) -> None:
    for name in COLLISION_SENSITIVE_NAMES:
        (directory / name).write_text("")


def test_condor_fan_runs_every_lane_exactly_once_despite_collisions(
    tmp_path: Path,
) -> None:
    _plant_collisions(tmp_path)
    fan = _dry_run(tmp_path, "_validate-pr-flat-impl", "LANE_EXECUTOR=condor")
    for lane in FLAT_FAN_LANES:
        assert fan.count(f'target="{lane}"') == 1, (
            f"lane {lane!r} must appear exactly once in the condor fan "
            f"(collision files planted); found "
            f"{fan.count(chr(116))} occurrences\n{fan[:2000]}"
        )


def test_condor_mode_has_no_sequential_vscode_tail(tmp_path: Path) -> None:
    _plant_collisions(tmp_path)
    tail = _dry_run(tmp_path, "validate-pr-raw", "LANE_EXECUTOR=condor")
    assert "test-vscode" not in tail, (
        "condor mode must not also run the sequential vscode tail"
    )


def test_direct_mode_keeps_the_sequential_vscode_tail(tmp_path: Path) -> None:
    _plant_collisions(tmp_path)
    tail = _dry_run(tmp_path, "validate-pr-raw", "LANE_EXECUTOR=direct")
    assert tail.count("test-vscode") == 1, tail


def test_repo_tree_contains_no_target_named_debris() -> None:
    """Files named after make targets silently delete lanes from the
    gate (and six such files once escaped into a commit). The repo tree
    must never contain them."""
    offenders = [
        name for name in COLLISION_SENSITIVE_NAMES if (REPO_ROOT / name).is_file()
    ]
    assert not offenders, (
        f"target-named files present in the repo tree: {offenders} — "
        "these vacuously skip gate lanes; delete them and find what "
        "created them"
    )


def test_worker_counts_are_declared_once_and_mode_consistent(
    tmp_path: Path,
) -> None:
    """B1 (#7122 review): the measured CPU requests in lanes.yaml were
    taken at specific worker counts, so a mode running a different
    count invalidates them silently. Two guards: no literal -n in any
    pytest lane recipe (worker counts are declared variables), and the
    two modes provably run the unit suite at the same width."""
    import re

    literal_lines = [
        line.strip()
        for line in (REPO_ROOT / "Makefile").read_text().splitlines()
        if "PYTEST)" in line and re.search(r"-n [0-9]", line)
    ]
    assert not literal_lines, (
        "lane recipes with literal worker counts (declare a "
        f"LANE_WORKERS_* variable instead): {literal_lines}"
    )

    direct = _dry_run(tmp_path, "test-unit", "LANE_EXECUTOR=direct")
    condor = _dry_run(tmp_path, "test-unit", "LANE_EXECUTOR=condor")
    direct_workers = re.search(r"-n (\w+)", direct)
    condor_workers = re.search(r"UNIT_PARALLEL=(\w+)", condor)
    assert direct_workers and condor_workers, "probe broken"
    assert direct_workers.group(1) == condor_workers.group(1), (
        f"unit suite width drifts by mode: direct -n "
        f"{direct_workers.group(1)} vs condor "
        f"UNIT_PARALLEL={condor_workers.group(1)}"
    )


def test_every_condor_lane_resolves_declared_scheduling_facts(
    tmp_path: Path,
) -> None:
    """The invariants formerly enforced as recipe flags — every lane
    declares a memory budget (a lane without one inherits the tiny
    wrapper's image size and its workload is OOM-killed at a ~259MB
    ceiling, proven live) and classifies suspendability explicitly
    (A1, #7118 review) — kept, with a new owner: both are
    schema-required fields in .issue-orchestrator/lanes.yaml, resolved
    per work key. Proven end-to-end here: every work key the flat fan
    actually submits resolves to a declaration carrying both facts.
    Lanes outside the flat fan are held to the same bar by the
    bidirectional drift test in test_lane_declarations.py."""
    import re

    from issue_orchestrator.infra.lane_declarations import (
        load_lane_declaration,
    )

    fan = _dry_run(tmp_path, "_validate-pr-flat-impl", "LANE_EXECUTOR=condor")
    work_keys = re.findall(r"--work-key (\S+)", fan)
    assert work_keys, "flat fan submitted no lanes - probe broken"
    for work_key in work_keys:
        declaration = load_lane_declaration(REPO_ROOT, work_key)
        assert declaration.memory_mb >= 1
        assert isinstance(declaration.suspendable, bool)
