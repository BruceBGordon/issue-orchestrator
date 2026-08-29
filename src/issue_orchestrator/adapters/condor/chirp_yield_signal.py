# pyright: strict
"""Outbound yield-signal adapter: advertisements → the job ClassAd.

``condor_chirp set_job_attr SafeToSuspend …`` is how a running job
updates its own ad; the pool's suspension policy gates cooperative
lanes on exactly that attribute (see the load-backoff config in
scripts/condor-personal.sh). Everything scheduler-flavored about the
mechanism — the binary, the starter-provided ``_CONDOR_CHIRP_CONFIG``
environment marker, the attribute name — lives here and nowhere else.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Written into the job environment by the starter when the IO proxy is
# enabled (the submit description sets +WantIOProxy for cooperative
# lanes); its presence is the "inside a condor job, chirp reachable"
# marker.
CHIRP_CONFIG_ENVIRONMENT_VARIABLE = "_CONDOR_CHIRP_CONFIG"
_SAFE_TO_SUSPEND_ATTRIBUTE = "SafeToSuspend"
_CHIRP_TIMEOUT_SECONDS = 10.0


class InertLaneYieldSignal:
    """No consumer for advertisements here; saying nothing is correct."""

    def advertise(self, safe: bool) -> None:
        del safe


class ChirpLaneYieldSignal:
    """Advertise via ``condor_chirp`` — degrade to inert on failure.

    A failed advertisement must not fail the lane (the consumer's
    ``=?=`` semantics already treat silence as not-safe), but it must
    not fail silently either: the first failure is reported to stderr
    and the signal goes inert, so a broken chirp path is one loud line
    in the lane log, not a hazard and not a mystery. The residual risk
    is a stale ``True`` published before the failure — bounded by the
    owner-load window and accepted in the design record (#7124).
    """

    def __init__(self, chirp: Path) -> None:
        if not chirp.is_absolute():
            raise ValueError("ChirpLaneYieldSignal.chirp must be an absolute Path")
        self._chirp = chirp
        self._broken = False

    def advertise(self, safe: bool) -> None:
        if self._broken:
            return
        try:
            completed = subprocess.run(
                (
                    str(self._chirp),
                    "set_job_attr",
                    _SAFE_TO_SUSPEND_ATTRIBUTE,
                    "True" if safe else "False",
                ),
                capture_output=True,
                text=True,
                check=False,
                timeout=_CHIRP_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self._go_inert(repr(error))
            return
        if completed.returncode != 0:
            self._go_inert(
                completed.stderr.strip() or completed.stdout.strip() or "exit "
                f"{completed.returncode}"
            )

    def _go_inert(self, detail: str) -> None:
        self._broken = True
        print(
            "lane-yield: chirp advertisement failed; going inert for the "
            f"rest of this lane (the pool will not freeze it): {detail}",
            file=sys.stderr,
        )


def resolve_lane_yield_signal() -> InertLaneYieldSignal | ChirpLaneYieldSignal:
    """The chirp signal inside a chirp-capable job; inert anywhere else.

    Inert is the correct resolution, not a fallback: outside a
    scheduler job there is no consumer, and inside one without a chirp
    path the consumer's own semantics already keep the lane unfrozen —
    but that second case gets one loud stderr line, because a
    cooperative lane that cannot advertise runs with `never` semantics
    the operator should know about.
    """
    if CHIRP_CONFIG_ENVIRONMENT_VARIABLE not in os.environ:
        return InertLaneYieldSignal()
    located = shutil.which("condor_chirp")
    if located is None:
        print(
            "lane-yield: inside a scheduler job but condor_chirp is not on "
            "PATH; cooperative yielding is inert (the lane will not be "
            "frozen)",
            file=sys.stderr,
        )
        return InertLaneYieldSignal()
    return ChirpLaneYieldSignal(Path(located).resolve())
