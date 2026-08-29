# pyright: strict
"""Outbound yield transport: acknowledged publications → the job ClassAd.

``condor_chirp set_job_attr SafeToSuspend …`` is how a running job
updates its own ad; the pool's suspension policy gates cooperative
lanes on exactly that attribute. Everything scheduler-flavored — the
binary, its LIBEXEC home, the starter's ``_CONDOR_CHIRP_CONFIG``
marker, the attribute name — lives here. The publication POLICY (when
a failure is a hard error vs a loud degradation) is owned by
``execution/lane_yield.py``; this module only publishes and reports
acknowledgment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ...domain.lane_execution import LaneExecutorUnavailableError
from .lane_executor import CondorTools

# Written into the job environment by the starter when the IO proxy is
# enabled (the submit description sets +WantIOProxy for cooperative
# lanes); its presence is the "inside a condor job, chirp reachable"
# marker.
CHIRP_CONFIG_ENVIRONMENT_VARIABLE = "_CONDOR_CHIRP_CONFIG"
_SAFE_TO_SUSPEND_ATTRIBUTE = "SafeToSuspend"
_CHIRP_TIMEOUT_SECONDS = 10.0


class ChirpYieldTransport:
    """Publish via ``condor_chirp``; acknowledged = clean exit."""

    def __init__(self, chirp: Path) -> None:
        if not chirp.is_absolute():
            raise ValueError("ChirpYieldTransport.chirp must be an absolute Path")
        self._chirp = chirp

    def publish(self, safe: bool) -> bool:
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
            print(f"lane-yield: chirp invocation failed: {error!r}", file=sys.stderr)
            return False
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            print(
                "lane-yield: chirp rejected the publication: "
                f"{detail or f'exit {completed.returncode}'}",
                file=sys.stderr,
            )
            return False
        return True


def _locate_chirp() -> Path | None:
    """The tarball ships condor_chirp in LIBEXEC, not bin (B1, #7134):
    ask the pool's own configuration where that is, through the same
    tool boundary lanes submit through; PATH is only a courtesy first
    try for system installs that expose it there."""
    located = shutil.which("condor_chirp")
    if located is not None:
        return Path(located).resolve()
    try:
        tools = CondorTools.resolve()
    except LaneExecutorUnavailableError:
        return None
    config_val = tools.query.parent / "condor_config_val"
    if not config_val.is_file():
        return None
    environment = dict(os.environ)
    if tools.pool_config is not None:
        environment["CONDOR_CONFIG"] = str(tools.pool_config)
    try:
        completed = subprocess.run(
            (str(config_val), "LIBEXEC"),
            capture_output=True,
            text=True,
            check=False,
            timeout=_CHIRP_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    candidate = Path(completed.stdout.strip()) / "condor_chirp"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate.resolve()
    return None


def inside_scheduler_job() -> bool:
    """Whether this process runs inside a chirp-capable scheduler job.

    The distinction matters at composition (A3, #7134 round two):
    outside a job, an absent transport is ordinary and inert is
    correct; INSIDE a job, an unresolvable transport must be fatal —
    a successor process cannot perform the acknowledged opening False
    that a predecessor's possible True demands, so it must not run.
    """
    return CHIRP_CONFIG_ENVIRONMENT_VARIABLE in os.environ


def resolve_lane_yield_transport() -> ChirpYieldTransport | None:
    """The chirp transport inside a chirp-capable job; None elsewhere.

    None outside a job is silent and ordinary (there is no consumer
    to publish to, and composition stays inert). None INSIDE a job is
    a refusal, not a degradation: the composition owner escalates it
    to :class:`~issue_orchestrator.ports.lane_yield_signal.LaneYieldError`
    at lane startup (pytest_configure), because a lane that cannot
    publish cannot perform the acknowledged opening False that the
    submit description's possible True demands — it must not run.
    The stderr line below is diagnosis for that refusal, not a
    license to continue.
    """
    if not inside_scheduler_job():
        return None
    chirp = _locate_chirp()
    if chirp is None:
        print(
            "lane-yield: inside a scheduler job but condor_chirp was not "
            "found on PATH or in the pool's LIBEXEC; cooperative yielding "
            "is unavailable (the lane will not be frozen)",
            file=sys.stderr,
        )
        return None
    return ChirpYieldTransport(chirp)
