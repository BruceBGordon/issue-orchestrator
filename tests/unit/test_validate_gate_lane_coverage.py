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
COLLISION_SENSITIVE_NAMES = (*FLAT_FAN_LANES, "FORCE", "ensure-uv", "lane-preflight")

# How the gate's pool-policy self-check appears in a dry run: the make
# target the gate invokes, and the module that target runs. The target
# name alone - never "<make binary> lane-preflight", which differs
# between gmake and make hosts.
PREFLIGHT_TARGET = "lane-preflight"
PREFLIGHT_MODULE = "cli_tools.lane_preflight"


def _scrubbed_environment() -> dict[str, str]:
    # Hermetic against the hosting gate: when this test runs INSIDE a
    # make-driven lane, MAKEFLAGS carries the recursion's own
    # LANE_EXECUTOR and the publish command exports another — both
    # would leak into the subprocess and poison the mode under test.
    # The worker-width family leaks the same way: the condor wrapper's
    # command-line UNIT_PARALLEL=12 is exported into the lane's
    # environment, and environment-origin variables beat ?= defaults,
    # so the override-matrix dry-runs would test the hosting lane's
    # width instead of the Makefile's (failed live in the gate's own
    # unit lane while passing on a clean host shell).
    blocked = {
        "MAKEFLAGS",
        "MFLAGS",
        "MAKELEVEL",
        "LANE_EXECUTOR",
        "ISSUE_ORCHESTRATOR_LANE_EXECUTOR",
        "PARALLEL",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key not in blocked
        and not key.endswith("_PARALLEL")
        and not key.startswith("LANE_WORKERS_")
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


def test_condor_gate_preflights_pool_policy_exactly_once(tmp_path: Path) -> None:
    """Once per GATE, never once per lane.

    The fan dispatches fifteen lanes; a per-lane check would ask the
    same question fifteen times and still not stop the gate any
    earlier. One invocation at the gate's head is the whole mechanism —
    if this count ever rises, the check has leaked into the lanes."""
    _plant_collisions(tmp_path)
    gate = _dry_run(tmp_path, "_validate-pr-impl", "LANE_EXECUTOR=condor")
    assert gate.count(PREFLIGHT_TARGET) == 1, (
        "the condor gate must preflight pool policy exactly once\n"
        f"{gate[:2000]}"
    )


def test_preflight_precedes_the_lane_fan(tmp_path: Path) -> None:
    """A drifted pool must stop the gate BEFORE work is dispatched;
    diagnosing it from fifteen degraded lanes afterwards is the failure
    this check exists to prevent."""
    _plant_collisions(tmp_path)
    gate = _dry_run(tmp_path, "_validate-pr-impl", "LANE_EXECUTOR=condor")
    assert PREFLIGHT_TARGET in gate, gate[:2000]
    assert "_validate-pr-flat-impl" in gate, "dry run has no fan - probe broken"
    assert gate.index(PREFLIGHT_TARGET) < gate.index("_validate-pr-flat-impl"), (
        gate[:2000]
    )


def test_a_lane_never_runs_its_own_preflight(tmp_path: Path) -> None:
    """The lane recipes are identical in both modes and know nothing
    about pool policy; only the gate does."""
    _plant_collisions(tmp_path)
    for lane in (*FLAT_FAN_LANES, "_validate-pr-flat-impl"):
        expansion = _dry_run(tmp_path, lane, "LANE_EXECUTOR=condor")
        assert PREFLIGHT_MODULE not in expansion, (
            f"lane {lane!r} pays for the pool-policy check itself"
        )
        assert PREFLIGHT_TARGET not in expansion, (
            f"lane {lane!r} pays for the pool-policy check itself"
        )


def test_direct_mode_pays_nothing_for_the_pool_preflight(tmp_path: Path) -> None:
    _plant_collisions(tmp_path)
    gate = _dry_run(tmp_path, "_validate-pr-impl", "LANE_EXECUTOR=direct")
    assert PREFLIGHT_TARGET not in gate, gate[:2000]


def test_preflight_target_selects_the_backend_the_lanes_will_use(
    tmp_path: Path,
) -> None:
    """One owner for the check, and it cannot preflight one backend
    while the lanes run another."""
    _plant_collisions(tmp_path)
    for mode in ("direct", "condor"):
        expansion = _dry_run(tmp_path, "lane-preflight", f"LANE_EXECUTOR={mode}")
        assert expansion.count(PREFLIGHT_MODULE) == 1, expansion[:2000]
        assert f"--backend {mode}" in expansion, expansion[:2000]


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


def test_no_worker_count_literal_anywhere_in_the_makefile() -> None:
    """B1 round two (#7122 review): the first guard examined only
    single source lines containing both PYTEST) and the literal, so a
    multi-line recipe with `-n 4` on a continuation line — exactly the
    prior integration-slice shape — passed unnoticed. The whole file
    is clean of `-n <number>` today, so the strongest guard is a
    whole-file ban: any future literal (lane recipe or otherwise)
    must become a declared variable."""
    import re

    literal_lines = [
        line.strip()
        for line in (REPO_ROOT / "Makefile").read_text().splitlines()
        if re.search(r"-n [0-9]", line)
    ]
    assert not literal_lines, (
        "literal worker counts in the Makefile (declare a "
        f"LANE_WORKERS_* variable instead): {literal_lines}"
    )


def test_unit_worker_width_is_mode_consistent_including_overrides(
    tmp_path: Path,
) -> None:
    """B1 round two (#7122 review): the measured CPU requests were
    taken at declared worker widths, and the documented overrides must
    flow identically through both modes — the first fix defaulted the
    width correctly but let the condor wrapper clobber an explicit
    UNIT_PARALLEL and broke PARALLEL=0's disable semantics."""
    import re

    def direct_width(*variables: str) -> str:
        expansion = _dry_run(
            tmp_path, "test-unit", "LANE_EXECUTOR=direct", *variables
        )
        found = re.search(r"-n (\w+)", expansion)
        if found is None:
            assert "--dist=loadgroup" not in expansion
            return "disabled"
        return found.group(1)

    def condor_width(*variables: str) -> str:
        expansion = _dry_run(
            tmp_path, "test-unit", "LANE_EXECUTOR=condor", *variables
        )
        found = re.search(r"UNIT_PARALLEL=(\w+)", expansion)
        assert found, "condor wrapper did not forward a unit width"
        return found.group(1)

    # Default: both modes run the declared width.
    assert direct_width() == condor_width() == "12"
    # Explicit UNIT_PARALLEL flows through both modes unchanged.
    assert direct_width("UNIT_PARALLEL=6") == "6"
    assert condor_width("UNIT_PARALLEL=6") == "6"
    # Documented disable: PARALLEL=0 turns xdist off in direct mode
    # and forwards 0 through the condor wrapper.
    assert direct_width("PARALLEL=0") == "disabled"
    assert condor_width("PARALLEL=0") == "0"


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


def test_slice_recipe_captures_durations_and_guards_an_empty_selection(
    tmp_path: Path,
) -> None:
    """The consume/capture pair, checked on the recipe that wires it.

    The empty guard is not cosmetic: the previous shape spliced the
    slicer's stdout straight into the pytest argument list, so a slicer
    that exited non-zero (a corrupt weight store now makes it do
    exactly that) left pytest with no arguments — which collects the
    WHOLE repository instead of failing the lane.
    """
    _plant_collisions(tmp_path)
    recipe = _dry_run(
        tmp_path, "test-integration-core-slice-2", "LANE_EXECUTOR=direct"
    )
    assert "-p issue_orchestrator.infra.pytest_file_durations" in recipe
    assert "--epoch " in recipe
    assert "targets=$(" in recipe
    assert '-z "$targets"' in recipe


def test_the_duration_capture_point_is_backend_neutral(tmp_path: Path) -> None:
    """A scheduler backend submits a wrapper that re-invokes the very
    same direct recipe inside its job, so the durations are captured
    identically in both modes. Nothing about capture lives in the
    backend branch."""
    _plant_collisions(tmp_path)
    condor = _dry_run(
        tmp_path, "test-integration-core-slice-2", "LANE_EXECUTOR=condor"
    )
    assert "test-integration-core-slice-2 LANE_EXECUTOR=direct" in condor
    assert "issue_orchestrator.infra.pytest_file_durations" not in condor


def test_every_slice_of_one_gate_shares_one_weight_epoch(tmp_path: Path) -> None:
    """Coverage across processes. The slice lanes read the learned
    weights minutes apart and each teaches the store as it finishes, so
    two slicers reading live would balance on different numbers — and
    two different partitions of one file list can leave a file unrun in
    a gate that still goes green. One stamp per gate is what makes the
    three partitions the same partition."""
    import re

    _plant_collisions(tmp_path)
    fan = _dry_run(tmp_path, "_validate-pr-flat-impl", "LANE_EXECUTOR=condor")
    stamps = re.findall(r"SLICE_WEIGHTS_EPOCH=(\S+)", fan)
    slice_lanes = [lane for lane in FLAT_FAN_LANES if "core-slice-" in lane]
    assert len(stamps) == len(slice_lanes), (
        f"every slice lane must carry the gate stamp: {stamps}"
    )
    assert len(set(stamps)) == 1, f"slices of one gate disagree on weights: {stamps}"
