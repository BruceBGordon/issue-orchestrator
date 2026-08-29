"""The shared containment policy, pinned so its two users cannot drift.

Both boundaries that catch ``BaseException`` — the machine-state sampler
and the lane executor's cancellation path — decide what gets out by
consulting this one tuple. Its membership is a deliberate decision, and
the surprising half (``SystemExit`` is contained, not re-raised) is the
half most likely to be "fixed" by someone who has not read why.
"""

from __future__ import annotations

import asyncio

from issue_orchestrator.infra.teardown_signals import (
    TEARDOWN_SIGNALS,
    is_teardown_signal,
)


def test_the_policy_is_exactly_the_teardown_signals() -> None:
    assert set(TEARDOWN_SIGNALS) == {
        KeyboardInterrupt,
        GeneratorExit,
        asyncio.CancelledError,
    }


def test_system_exit_is_deliberately_not_a_teardown_signal() -> None:
    """Letting SystemExit out of a best-effort boundary means a probe or
    a diagnostic silently substituting its own exit status for the real
    one — the precise harm those boundaries exist to prevent. It is
    contained AND recorded, never re-raised."""
    assert not is_teardown_signal(SystemExit(3))
    assert SystemExit not in TEARDOWN_SIGNALS


def test_ordinary_failures_are_not_teardown_signals() -> None:
    assert not is_teardown_signal(RuntimeError("probe host melted"))
    assert not is_teardown_signal(OSError("disk gone"))


def test_every_teardown_signal_is_recognised_including_subclasses() -> None:
    class _DerivedInterrupt(KeyboardInterrupt):
        pass

    for signal_type in TEARDOWN_SIGNALS:
        assert is_teardown_signal(signal_type())
    assert is_teardown_signal(_DerivedInterrupt())
