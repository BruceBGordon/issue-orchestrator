"""The settle before the Enter: waits on quiet, bounded by a real backstop.

Guards #7104. The prompt text and the Enter that submits it are two separate
PTY writes, and between them the drain waits for the TUI to stop painting. If
that wait gives up early the Enter lands in a TUI that is still busy, the
submission is dropped, and the turn text strands in the composer until the
round times out — ten minutes later, reported as a provider failure.

The bug was that the backstop was computed as ``max(quiet_seconds, 1.0)``,
which for the 0.3s settle is a flat one second: a cap SHORTER than the event
it bounds. Codex 0.153.4 boots an MCP server and animates a spinner while it
does, comfortably outlasting a second.

No real clock and no real PTY here — the clock, the sleep, the readiness check
and the read are all injected, so these assert the decision the code makes
rather than how fast the machine is.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from issue_orchestrator.execution import persistent_round_io
from issue_orchestrator.execution.persistent_round_io import (
    drain_pty_output_until_quiet,
)


class _Clock:
    """A clock that moves only when the code under test spends time."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class _FakeSession:
    master_fd = 7
    closed = False
    log_writer = None

    def __init__(self) -> None:
        self.observed: list[bytes] = []
        self.output_observer: Callable[[bytes], None] | None = self.observed.append


def _install_pty(
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    *,
    busy_until: float,
    chunk: bytes = b"spinner",
) -> dict[str, int]:
    """A PTY that streams until ``busy_until``, then falls silent forever."""
    counts = {"reads": 0}

    def _select(rlist, _w, _x, _timeout):  # type: ignore[no-untyped-def]
        return ([rlist[0]], [], []) if clock.now() < busy_until else ([], [], [])

    def _read(_fd: int) -> bytes:
        counts["reads"] += 1
        # Reading costs time. The drain resets its deadline on every chunk and
        # does NOT sleep on that path, so a clock that only advanced on sleep
        # would sit in this branch forever — a fake that cannot represent the
        # real system, rather than a finding about it.
        clock.t += 0.05
        return chunk

    monkeypatch.setattr(persistent_round_io.select, "select", _select)
    monkeypatch.setattr(persistent_round_io, "os_read", _read)
    return counts


def test_the_settle_waits_out_a_boot_that_outlasts_the_old_one_second_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression itself, stated as the decision rather than a duration.

    A TUI that paints for eight seconds is a codex MCP boot. The old implicit
    cap abandoned the settle at one second and the caller submitted into it.
    """
    clock = _Clock()
    _install_pty(monkeypatch, clock, busy_until=8.0)

    settled = drain_pty_output_until_quiet(
        _FakeSession(),
        quiet_seconds=0.3,
        max_wait_seconds=60.0,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert settled is True, "the settle gave up while the TUI was still painting"
    assert clock.now() >= 8.0, (
        "the settle returned before the output stopped, so the Enter would "
        f"have been written into a busy TUI at t={clock.now():.2f}s"
    )


def test_the_backstop_reports_that_it_fired_rather_than_claiming_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TUI that never stops painting must be distinguishable from a quiet one.

    The caller submits anyway either way — failing the round outright would be
    worse — so the return value is the only thing that lets it say so, and
    saying so at this moment is what stops the failure being diagnosed from a
    screen replay ten minutes later.
    """
    clock = _Clock()
    _install_pty(monkeypatch, clock, busy_until=float("inf"))

    settled = drain_pty_output_until_quiet(
        _FakeSession(),
        quiet_seconds=0.3,
        max_wait_seconds=5.0,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert settled is False
    assert clock.now() >= 5.0, "the backstop fired before its own deadline"


def test_quiet_output_settles_without_waiting_for_the_backstop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common case must stay fast: an idle TUI costs the quiet window."""
    clock = _Clock()
    counts = _install_pty(monkeypatch, clock, busy_until=0.0)

    settled = drain_pty_output_until_quiet(
        _FakeSession(),
        quiet_seconds=0.3,
        max_wait_seconds=60.0,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert settled is True
    assert counts["reads"] == 0
    assert clock.now() < 1.0, (
        f"an already-quiet TUI cost {clock.now():.2f}s; the backstop is a "
        "backstop, not a delay every round pays"
    )


def test_the_default_backstop_preserves_the_response_drain_call_sites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting the backstop keeps the historical `max(quiet_seconds, 1.0)`.

    The two response-drain callers pass no backstop and are not part of #7104;
    pinning the default keeps this fix to the settle it is about.
    """
    clock = _Clock()
    _install_pty(monkeypatch, clock, busy_until=float("inf"))

    settled = drain_pty_output_until_quiet(
        _FakeSession(), quiet_seconds=0.3, now=clock.now, sleep=clock.sleep
    )

    assert settled is False
    assert 1.0 <= clock.now() < 1.5, (
        f"default backstop moved: gave up at {clock.now():.2f}s, expected ~1.0s"
    )


def test_a_closed_session_is_settled_not_a_backstop_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session that closed under us did not fail to settle.

    Reporting False here would make every closed session look like a stranded
    composer and bury the signal this return value exists to carry.
    """
    clock = _Clock()
    _install_pty(monkeypatch, clock, busy_until=float("inf"))
    session = _FakeSession()
    session.closed = True

    assert (
        drain_pty_output_until_quiet(
            session,
            quiet_seconds=0.3,
            max_wait_seconds=60.0,
            now=clock.now,
            sleep=clock.sleep,
        )
        is True
    )
