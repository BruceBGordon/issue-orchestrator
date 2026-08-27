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


def test_every_condor_lane_declares_a_memory_budget(tmp_path: Path) -> None:
    """The scheduler sizes slots from request_memory; a lane submitted
    without one inherits the tiny wrapper's image size and its real
    workload is OOM-killed (proven live by pyright at a ~259MB heap
    ceiling). Every wired lane must declare its budget."""
    fan = _dry_run(tmp_path, "_validate-pr-flat-impl", "LANE_EXECUTOR=condor")
    import re

    for line in fan.splitlines():
        for match in re.finditer(r"lane_run [^;]*?--work-key (\S+)", line):
            segment = line[match.start() :]
            assert "--request-memory-mb" in segment.split(";")[0], (
                f"condor lane {match.group(1)!r} declares no memory budget"
            )


def test_no_lane_run_declaration_anywhere_lacks_a_memory_budget() -> None:
    """Complete-owner-surface guard: the dry-run test above sees only
    the flat fan's consumer graph, and two supported monolith lanes
    (test-integration-core-local, test-integration-agent) drifted onto
    the silent CLI default unseen. Every LANE_RUN invocation in the
    Makefile - regardless of which gate path consumes it - must declare
    its budget explicitly."""
    offenders = [
        line.strip()
        for line in (REPO_ROOT / "Makefile").read_text().splitlines()
        if "$(LANE_RUN)" in line and "--request-memory-mb" not in line
    ]
    assert not offenders, (
        "condor lane declarations without a memory budget:\n"
        + "\n".join(offenders)
    )
