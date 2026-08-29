"""Every fixture script this repo spawns must die of its own clock.

Cleanup in a ``finally`` protects a harness that gets to run its ``finally``.
It does nothing for a harness that is SIGKILLed, loses power, or is torn down
by a pytest timeout — and the 2026-08-29 leak (#7142) is exactly what the
machine looks like afterwards. So each fixture carries an independent deadline,
and this module reproduces the escape to prove it: start the fixture, kill its
supervisor outright, and require the orphan to be gone on time.

Two separate properties, deliberately not merged:

* the *mechanism* — the script honours the deadline it is handed, tested with a
  short one so the suite stays fast;
* the *policy* — the lifetimes those scripts are actually spawned with are
  finite and measured in minutes.

TERM-immunity is re-asserted alongside, because a fixture that quietly started
cooperating with SIGTERM would make the contract tests that use it vacuous.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.integration.test_condor_lane_executor import (
    _ESCAPE_LIFETIME_SECONDS,
    _ESCAPE_SCRIPT,
    _LOAD_SPIKE_MAX_SECONDS,
)
from tests.unit.lane_executor_contract import _TREE_LIFETIME_SECONDS, _TREE_SCRIPT
from tests.unit.test_load_fixture import FIXTURE_LIFETIME_SECONDS

# Short enough to keep this suite fast, long enough that the fixture is
# provably alive while the harness is killed.
SHORT_LIFETIME_SECONDS = 3.0
# Slack over the fixture's own deadline before we call it a leak.
EXPIRY_SLACK_SECONDS = 20.0
# "Minutes, not hours." A fixture that outlives a quarter hour outlives most of
# the gates that would then be blamed for its load.
MAX_FIXTURE_LIFETIME_SECONDS = 900.0

_MARKER_TIMEOUT_SECONDS = 15.0
_POLL_SECONDS = 0.02


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _await_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return True
        time.sleep(_POLL_SECONDS)
    return False


def _await_recorded_pid(marker: Path) -> int:
    """The pid the fixture wrote, once it is fully written."""
    deadline = time.monotonic() + _MARKER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            return int(marker.read_text())
        except (FileNotFoundError, ValueError):
            time.sleep(_POLL_SECONDS)
    raise AssertionError(f"fixture never recorded a pid at {marker}")


# A harness that takes cpu_load's guarantee and then dies without running it.
# `cpu_load` reaps in a finally; a SIGKILLed process has no finally, so what is
# left is the burner's own deadline and nothing else.
_CPU_LOAD_HARNESS = (
    "import sys, time\n"
    "sys.path.insert(0, sys.argv[3])\n"
    "from tests.load_fixture import cpu_load\n"
    "lifetime = float(sys.argv[2])\n"
    "with cpu_load(workers=1, max_lifetime_seconds=lifetime) as pids:\n"
    "    open(sys.argv[1], 'w').write(str(pids[0]))\n"
    "    time.sleep(lifetime + 30)\n"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _spawn(script: str, marker: Path, *extra: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(marker),
            str(SHORT_LIFETIME_SECONDS),
            *extra,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _force_kill(*pids: int) -> None:
    """Backstop, so a failure here cannot leak what it spawned."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue


@pytest.mark.parametrize(
    ("script", "term_immune"),
    [
        pytest.param(_TREE_SCRIPT, True, id="lane-contract-tree"),
        pytest.param(_ESCAPE_SCRIPT, False, id="condor-setsid-escapee"),
    ],
)
def test_an_orphaned_fixture_expires_without_anyone_reaping_it(
    tmp_path: Path, script: str, term_immune: bool
) -> None:
    """Kill the supervisor outright; the orphan must still go away."""
    marker = tmp_path / "grandchild.pid"
    supervisor = _spawn(script, marker)
    orphan = _await_recorded_pid(marker)
    try:
        if term_immune:
            os.kill(orphan, signal.SIGTERM)
            time.sleep(0.5)
            assert _is_alive(orphan), (
                "fixture now dies on SIGTERM, so the contract test using it no "
                "longer proves the backend killed anything"
            )

        # The reviewer's repro: nothing gets to run its cleanup.
        os.kill(supervisor.pid, signal.SIGKILL)
        supervisor.wait(timeout=EXPIRY_SLACK_SECONDS)
        assert orphan != supervisor.pid
        assert _await_gone(orphan, SHORT_LIFETIME_SECONDS + EXPIRY_SLACK_SECONDS), (
            f"orphaned fixture {orphan} outlived its own "
            f"{SHORT_LIFETIME_SECONDS}s deadline with no supervisor left to "
            "reap it; this is the shape that poisoned nine hours of gates"
        )
    finally:
        _force_kill(orphan, supervisor.pid)


def test_a_cpu_load_burner_expires_when_its_harness_is_sigkilled(
    tmp_path: Path,
) -> None:
    """``cpu_load``'s finally is not the last line of defence; the clock is."""
    marker = tmp_path / "burner.pid"
    harness = _spawn(_CPU_LOAD_HARNESS, marker, str(_REPO_ROOT))
    burner = _await_recorded_pid(marker)
    try:
        os.kill(harness.pid, signal.SIGKILL)
        harness.wait(timeout=EXPIRY_SLACK_SECONDS)
        assert _await_gone(burner, SHORT_LIFETIME_SECONDS + EXPIRY_SLACK_SECONDS), (
            f"burner {burner} outlived the harness that was going to reap it; "
            "this is the nine-hour incident exactly"
        )
    finally:
        _force_kill(burner, harness.pid)


@pytest.mark.parametrize(
    ("fixture", "seconds"),
    [
        ("lane contract TERM-immune tree", _TREE_LIFETIME_SECONDS),
        ("condor setsid escapee", _ESCAPE_LIFETIME_SECONDS),
        ("condor owner-load spike", _LOAD_SPIKE_MAX_SECONDS),
        ("load fixture unit tests", float(FIXTURE_LIFETIME_SECONDS)),
    ],
)
def test_declared_fixture_lifetimes_are_finite_and_measured_in_minutes(
    fixture: str, seconds: float
) -> None:
    assert math.isfinite(seconds), f"{fixture} has a non-finite lifetime"
    assert 0.0 < seconds <= MAX_FIXTURE_LIFETIME_SECONDS, (
        f"{fixture} may hold this machine for {seconds}s; the budget is "
        f"{MAX_FIXTURE_LIFETIME_SECONDS}s"
    )
