# pyright: strict
"""Per-lane gate verdict policy, consulted by the Makefile's TIMED_RUN.

TIMED_RUN is the one wrapper every gate lane — scheduler-backed and
host-side alike — already runs through, so it is the one honest seam
for per-lane caching. The Makefile contributes two shell lines; ALL
policy lives here:

- ``check --target X``: exit 0 = cached green at this gate's SHA, skip
  the lane (a loud line says so); exit 3 = run it; anything else = a
  real error that must fail the lane (corruption is never green).
- ``record --target X --exit-status N``: records green only when N == 0
  and the target is a declared gate lane; failures are never cached.

The gate enables the layer by exporting, once per gate:

- ``LANE_VERDICT_SHA``: the tree SHA, read exactly once at gate start.
  Every invocation re-verifies the worktree HEAD still matches — a
  mid-gate commit turns into a loud error instead of a verdict keyed
  to a tree nobody validated (that incident has happened).
- ``LANE_VERDICT_LANES``: the gate's complete lane set, derived from
  the same Makefile variable the fan executes — membership is what
  keeps phase aggregates and non-gate targets out of the cache.

Exit codes: 0 skip/recorded/no-op; 3 run; 70 store corruption or
mid-gate tree movement; 78 configuration (bad environment).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from ...execution import GitWorkingCopy
from ...infra.lane_verdicts import (
    LaneVerdictError,
    read_green,
    record_green,
)

SHA_ENVIRONMENT_VARIABLE = "LANE_VERDICT_SHA"
LANES_ENVIRONMENT_VARIABLE = "LANE_VERDICT_LANES"

_RUN_EXIT_CODE = 3
_FAULT_EXIT_CODE = 70
_CONFIGURATION_EXIT_CODE = 78


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    tree_sha = os.environ.get(SHA_ENVIRONMENT_VARIABLE, "")
    lanes_raw = os.environ.get(LANES_ENVIRONMENT_VARIABLE, "")
    if not tree_sha or not lanes_raw.split():
        print(
            "lane-verdict: requires LANE_VERDICT_SHA and LANE_VERDICT_LANES "
            "in the environment (the gate phase exports both)",
            file=sys.stderr,
        )
        return _CONFIGURATION_EXIT_CODE
    lanes = set(lanes_raw.split())
    target = str(arguments.target)
    worktree = Path.cwd()
    if target not in lanes:
        # Phase aggregates and non-gate targets pass through untouched:
        # membership in the gate's own lane list is what makes a target
        # cacheable at all.
        return _RUN_EXIT_CODE if arguments.command == "check" else 0
    moved = _tree_moved(worktree, tree_sha)
    if moved is not None:
        print(f"lane-verdict: {moved}", file=sys.stderr)
        return _FAULT_EXIT_CODE
    try:
        if arguments.command == "check":
            verdict = read_green(worktree, tree_sha, target)
            if verdict is None:
                return _RUN_EXIT_CODE
            print(
                f"[lane-verdict] {target} cached-green-at-{tree_sha[:12]} "
                f"(recorded {verdict.recorded_at}) — skipping"
            )
            return 0
        if int(arguments.exit_status) != 0:
            # Failures are never cached: a red lane re-runs next gate.
            return 0
        record_green(worktree, tree_sha, target)
        print(f"[lane-verdict] {target} recorded-green-at-{tree_sha[:12]}")
        return 0
    except LaneVerdictError as error:
        print(f"lane-verdict: {error}", file=sys.stderr)
        return _FAULT_EXIT_CODE


def _tree_moved(worktree: Path, tree_sha: str) -> str | None:
    """A message when HEAD no longer matches the gate's SHA (test seam).

    The gate reads the SHA once at its start; if the worktree HEAD has
    moved since, any verdict written or trusted now would describe a
    tree the gate is not actually validating.
    """
    head = _current_head(worktree)
    if head != tree_sha:
        return (
            "tree moved mid-gate: gate started at "
            f"{tree_sha[:12]} but HEAD is now {head[:12]} — refusing to "
            "trust or record lane verdicts for a tree the gate is not "
            "validating"
        )
    return None


def _current_head(worktree: Path) -> str:
    head = GitWorkingCopy().get_head_sha(worktree)
    if head is None:
        return "<unresolvable: not a git worktree>"
    return head


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lane-verdict",
        description="Per-lane gate verdict cache (policy owner).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("--target", required=True)
    record = commands.add_parser("record")
    record.add_argument("--target", required=True)
    record.add_argument("--exit-status", type=int, required=True)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
