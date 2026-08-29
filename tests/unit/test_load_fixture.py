"""The load fixture must leave nothing behind, especially when tests fail.

These spawn real processes — that is the point. A fake cannot demonstrate that
a TERM-immune child is dead afterwards, and "dead afterwards" is the entire
guarantee (#7142). Every process spawned here is a session leader with its own
hard deadline, so a failure in this file caps its own blast radius instead of
seeding the next one.

Two traps these tests are written around, both of which produced a green suite
that proved nothing:

* ``Popen`` returns once the child has *exec'd*, not once its script has run.
  Signalling at that point kills a child whose handler is not installed yet, so
  the fixture under test is not TERM-immune and the escalation is never
  exercised. Readiness is therefore an explicit file the fixture writes.
* ``os.kill(pid, 0)`` succeeds for a zombie. For our own children, "still
  running" is ``poll() is None``; anything else counts a corpse as alive.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.load_fixture import (
    LeakedProcessError,
    cpu_load,
    reap_marked_processes,
    reap_process_groups,
)

# Hard ceiling on anything this file spawns, if every cleanup path fails.
FIXTURE_LIFETIME_SECONDS = 30
# Bounded waits on the kernel, which has no ack channel for "this pid is gone".
OBSERVATION_TIMEOUT_SECONDS = 10.0
# How long a fixture is given to prove it did NOT die. Paid in full on the
# happy path, so it stays small.
IMMUNITY_WINDOW_SECONDS = 1.0
_POLL_SECONDS = 0.02

# A faithful copy of what the sweep found: a waiter that handles SIGTERM and
# carries on. ``signal.alarm`` is the self-limit — SIGALRM's default action
# still terminates, and nothing here installs a handler for it. The ready file
# is written last, so its existence proves the handler is already in place.
_TERM_IMMUNE_WAITER = (
    "import signal, sys\n"
    "signal.signal(signal.SIGTERM, lambda *_: None)\n"
    "signal.alarm(int(sys.argv[1]))\n"
    "open(sys.argv[2], 'w').close()\n"
    "while True:\n"
    "    signal.pause()\n"
)

# Same immunity, plus a fork: the shape a system under test leaves behind when
# its own cleanup fails. Neither process is a child of the test.
_TERM_IMMUNE_TREE = (
    "import os, signal, sys\n"
    "signal.signal(signal.SIGTERM, lambda *_: None)\n"
    "signal.alarm(int(sys.argv[1]))\n"
    "if os.fork() == 0:\n"
    "    signal.signal(signal.SIGTERM, lambda *_: None)\n"
    "    signal.alarm(int(sys.argv[1]))\n"
    "    open(sys.argv[2] + '.child', 'w').close()\n"
    "    while True:\n"
    "        signal.pause()\n"
    "open(sys.argv[2] + '.parent', 'w').close()\n"
    "while True:\n"
    "    signal.pause()\n"
)


def _spawn_leader(script: str, ready: Path) -> subprocess.Popen[bytes]:
    """Spawn a session leader; its pid is its pgid."""
    return subprocess.Popen(
        [sys.executable, "-c", script, str(FIXTURE_LIFETIME_SECONDS), str(ready)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _await_paths(*paths: Path) -> bool:
    deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if all(path.exists() for path in paths):
            return True
        time.sleep(_POLL_SECONDS)
    return False


def _exited_within(process: subprocess.Popen[bytes], seconds: float) -> bool:
    """Whether our own child exits inside the window. Reaps it if it does."""
    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _await_gone(pid: int) -> bool:
    deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return True
        time.sleep(_POLL_SECONDS)
    return False


def _backstop(*pids: int) -> None:
    """Last-resort kill, so a failure in this file cannot leak what it spawned."""
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue


class TestCpuLoad:
    def test_burners_run_during_the_body_and_are_gone_after_it(self) -> None:
        with cpu_load(workers=2, max_lifetime_seconds=FIXTURE_LIFETIME_SECONDS) as pids:
            assert len(pids) == 2
            assert all(_is_alive(pid) for pid in pids)
        assert all(_await_gone(pid) for pid in pids)

    def test_burners_are_reaped_when_the_body_raises(self) -> None:
        """The failing assert is exactly when the load is still running."""
        spawned: tuple[int, ...] = ()
        with pytest.raises(RuntimeError, match="the test failed"):
            with cpu_load(
                workers=2, max_lifetime_seconds=FIXTURE_LIFETIME_SECONDS
            ) as pids:
                spawned = pids
                raise RuntimeError("the test failed")
        assert spawned and all(_await_gone(pid) for pid in spawned)

    def test_each_burner_leads_its_own_process_group(self) -> None:
        with cpu_load(workers=2, max_lifetime_seconds=FIXTURE_LIFETIME_SECONDS) as pids:
            assert [os.getpgid(pid) for pid in pids] == list(pids)
            assert os.getpgid(pids[0]) != os.getpgrp()

    @pytest.mark.parametrize(
        ("workers", "lifetime"), [(0, 5.0), (-1, 5.0), (1, 0.0), (1, -5.0)]
    )
    def test_refuses_incoherent_load(self, workers: int, lifetime: float) -> None:
        with pytest.raises(ValueError):
            with cpu_load(workers=workers, max_lifetime_seconds=lifetime):
                pytest.fail("load must not be spawned at all")

    @pytest.mark.parametrize(
        "lifetime",
        [
            pytest.param(float("inf"), id="inf"),
            pytest.param(float("-inf"), id="-inf"),
            pytest.param(float("nan"), id="nan"),
        ],
    )
    def test_refuses_a_lifetime_that_is_not_a_number_of_seconds(
        self, lifetime: float
    ) -> None:
        """``inf`` and ``nan`` both slip past ``<= 0``.

        An inf-lifetime burner is a `while True: pass` with extra steps: it
        outlives a SIGKILLed harness, which is the whole failure mode.
        """
        with pytest.raises(ValueError, match="positive finite"):
            with cpu_load(workers=1, max_lifetime_seconds=lifetime):
                pytest.fail("load must not be spawned at all")


class TestReapProcessGroups:
    def test_a_term_immune_child_is_dead_after_the_helper_returns(
        self, tmp_path: Path
    ) -> None:
        """SIGTERM alone leaves this process running; the escalation is the point."""
        process = _spawn_leader(_TERM_IMMUNE_WAITER, tmp_path / "ready")
        try:
            assert _await_paths(tmp_path / "ready"), (
                "fixture never installed its SIGTERM handler"
            )
            os.killpg(process.pid, signal.SIGTERM)
            assert not _exited_within(process, IMMUNITY_WINDOW_SECONDS), (
                "fixture died on SIGTERM, so this test would pass with no "
                "SIGKILL escalation at all"
            )

            reap_process_groups(
                [process], terminate_grace_seconds=0.2, kill_grace_seconds=5.0
            )
        finally:
            _backstop(process.pid)

        assert process.returncode == -signal.SIGKILL, (
            f"the child ended as {process.returncode}, not by the group SIGKILL"
        )
        assert _await_gone(process.pid)

    def test_a_leader_that_cannot_be_killed_is_reported_as_a_leak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silence would hand the next gate load it cannot attribute."""
        process = _spawn_leader(_TERM_IMMUNE_WAITER, tmp_path / "ready")
        assert _await_paths(tmp_path / "ready")
        monkeypatch.setattr("tests.load_fixture.signal_group", lambda pgid, sig: None)
        try:
            with pytest.raises(LeakedProcessError, match="survived SIGKILL"):
                reap_process_groups(
                    [process], terminate_grace_seconds=0.05, kill_grace_seconds=0.5
                )
        finally:
            _backstop(process.pid)
            process.wait(timeout=OBSERVATION_TIMEOUT_SECONDS)


class TestReapMarkedProcesses:
    def test_kills_a_term_immune_tree_the_caller_never_spawned(
        self, tmp_path: Path
    ) -> None:
        marker = tmp_path / "lane-marker"
        process = _spawn_leader(_TERM_IMMUNE_TREE, marker)
        try:
            assert _await_paths(
                Path(f"{marker}.parent"), Path(f"{marker}.child")
            ), "the fixture tree never came up"

            killed = reap_marked_processes(
                str(marker), terminate_grace_seconds=0.2, kill_grace_seconds=5.0
            )

            assert process.pid in killed
            assert len(killed) == 2, f"the forked child was missed: {killed}"
        finally:
            _backstop(process.pid)
            process.wait(timeout=OBSERVATION_TIMEOUT_SECONDS)
        assert _await_gone(process.pid)

    def test_nothing_to_sweep_is_not_an_error(self, tmp_path: Path) -> None:
        assert reap_marked_processes(str(tmp_path / "never-spawned")) == ()

    def test_refuses_a_marker_that_would_match_this_process(self) -> None:
        with pytest.raises(ValueError, match="own argv"):
            reap_marked_processes(sys.argv[0])
