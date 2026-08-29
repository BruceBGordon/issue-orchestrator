"""The shared containment primitives, pinned so their users cannot drift.

Both boundaries that catch ``BaseException`` — the machine-state sampler
and the lane executor's cancellation wind-down — decide what gets out by
consulting this one tuple, and report what they contained with this one
renderer. The tuple's membership is a deliberate decision, and the
surprising half (``SystemExit`` is contained, not re-raised) is the half
most likely to be "fixed" by someone who has not read why.
"""

from __future__ import annotations

import asyncio

from issue_orchestrator.infra.containment import (
    MAX_RENDERED_CHARS,
    TEARDOWN_SIGNALS,
    describe_exception,
    is_teardown_signal,
    safe_type_name,
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


class TestRenderingNeverRaises:
    """The renderer is every boundary's last line: it is handed user
    code (an exception) at the moment things are already going wrong,
    so it must degrade rather than throw."""

    def test_an_ordinary_error_keeps_its_message(self) -> None:
        assert "boom" in describe_exception(ValueError("boom"))
        assert describe_exception(KeyboardInterrupt()) == "KeyboardInterrupt()"

    def test_a_hostile_repr_degrades_to_the_type_name(self) -> None:
        class _Hostile(Exception):
            def __repr__(self) -> str:
                raise ValueError("no repr")

        assert describe_exception(_Hostile()) == "_Hostile"

    def test_a_hostile_str_is_irrelevant_because_repr_is_asked_first(
        self,
    ) -> None:
        class _NoStr(Exception):
            def __str__(self) -> str:
                raise ValueError("no str")

        assert "_NoStr" in describe_exception(_NoStr())

    def test_an_enormous_rendering_is_capped(self) -> None:
        class _Verbose(Exception):
            def __repr__(self) -> str:
                return "x" * 10_000_000

        assert len(describe_exception(_Verbose())) == MAX_RENDERED_CHARS

    def test_a_type_name_is_obtained_without_trusting_a_metaclass(self) -> None:
        class _HostileMeta(type):
            @property
            def __name__(cls) -> str:
                raise ValueError("no name")

        class _Unnameable(Exception, metaclass=_HostileMeta):
            def __repr__(self) -> str:
                raise ValueError("no repr")

        assert safe_type_name(_Unnameable()) == "<unrenderable exception>"
        assert describe_exception(_Unnameable()) == "<unrenderable exception>"
