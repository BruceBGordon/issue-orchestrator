# pyright: strict
"""Prove the selected lane backend still carries the policy lanes need.

The gate proves lanes *complete*; nothing else proves the backend still
enforces the scheduling contract those lanes were written against. A
backend edited by hand, reinstalled, or reverted keeps accepting work
and quietly degrades every lane that follows — exclusives that no
longer exclude, deadlines that overrun into "backend unresponsive"
faults, lanes held on their own working directory.

Run ONCE per gate, ahead of dispatch. Once, because a gate fans out
ten-plus lanes and none of them may pay for a check that answers the
same question; ahead, because a drifted backend must be fixed before
work is submitted, not diagnosed from the wreckage afterwards.

Exit codes mirror lane-run's: 0 when policy holds; 78 (configuration)
when a required setting has drifted or the backend is unavailable; 70
(software) when the backend itself faults while being read. There is no
warn-and-continue path — a degraded backend is a stop.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

from ...domain.lane_execution import (
    LaneExecutorError,
    LaneExecutorUnavailableError,
    LanePolicyReport,
)
from ...execution.lane_backends import (
    BACKEND_ENVIRONMENT_VARIABLE,
    BACKEND_NAMES,
    DIRECT_BACKEND,
    build_lane_policy_check,
)

_UNAVAILABLE_EXIT_CODE = 78
_BACKEND_FAULT_EXIT_CODE = 70


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    backend = str(arguments.backend)
    try:
        report = build_lane_policy_check(backend).inspect()
    except LaneExecutorUnavailableError as error:
        print(f"lane-preflight: {error}", file=sys.stderr)
        return _UNAVAILABLE_EXIT_CODE
    except LaneExecutorError as error:
        print(f"lane-preflight: backend fault: {error}", file=sys.stderr)
        return _BACKEND_FAULT_EXIT_CODE
    return _report(backend, report)


def _report(backend: str, report: LanePolicyReport) -> int:
    """Put the whole finding in the gate log, then decide."""
    for observation in report.observations:
        print(
            f"[lane-preflight] {observation.name}: {observation.detail}",
            file=sys.stderr,
        )
    drifted = report.drifted
    if not drifted:
        print(
            f"[lane-preflight] {backend}: {len(report.invariants)} required "
            f"setting(s) hold — {report.source}",
            file=sys.stderr,
        )
        return 0
    print(
        f"lane-preflight: the {backend} backend's policy has drifted "
        f"({report.source}); lanes would run degraded, so no lane was "
        "dispatched:",
        file=sys.stderr,
    )
    for invariant in drifted:
        print(f"  {invariant.describe()}", file=sys.stderr)
    print(f"  fix: {report.remedy}", file=sys.stderr)
    return _UNAVAILABLE_EXIT_CODE


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lane-preflight",
        description=(
            "Assert the selected lane backend still carries its designed "
            "policy, before any lane is dispatched."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=BACKEND_NAMES,
        default=os.environ.get(BACKEND_ENVIRONMENT_VARIABLE, DIRECT_BACKEND),
        help=(
            "Execution backend; defaults to "
            f"${BACKEND_ENVIRONMENT_VARIABLE} or '{DIRECT_BACKEND}'."
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
