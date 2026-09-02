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

import pytest

from issue_orchestrator.infra.containment import (
    MAX_RENDERED_CHARS,
    TEARDOWN_SIGNALS,
    UNRENDERABLE,
    describe_exception,
    describe_value,
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
    so it must degrade rather than throw — for every failure EXCEPT a
    teardown signal, which this module's whole policy says must get
    out (see TestRenderingObeysTheTeardownPolicy)."""

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


def _raising(signal_type: type[BaseException]) -> type[BaseException]:
    """An exception whose ``__repr__`` and ``__str__`` raise ``signal_type``."""

    class _Raises(Exception):
        def __repr__(self) -> str:
            raise signal_type()

        def __str__(self) -> str:
            raise signal_type()

    return _Raises


def _unnameable(signal_type: type[BaseException]) -> type[BaseException]:
    """An exception whose metaclass raises ``signal_type`` for ``__name__``."""

    class _HostileMeta(type):
        @property
        def __name__(cls) -> str:
            raise signal_type()

    class _Unnameable(Exception, metaclass=_HostileMeta):
        def __repr__(self) -> str:
            raise signal_type()

    return _Unnameable


class TestRenderingObeysTheTeardownPolicy:
    """A renderer that eats a Ctrl-C eats it for good.

    Every consumer calls these from INSIDE its own ``except BaseException``
    handler, one layer below the ``except TEARDOWN_SIGNALS: raise`` that was
    supposed to let the signal through. While the renderers caught every
    ``BaseException``, that upper guard never saw an interrupt raised by a
    ``__repr__`` or a hostile metaclass ``__name__`` — the module contradicted
    its own documented policy.

    Parameterised over renderer x signal because the first fix covered only
    one renderer and only ``KeyboardInterrupt``.
    """

    @pytest.mark.parametrize("signal_type", TEARDOWN_SIGNALS)
    def test_describe_exception_lets_a_repr_raised_signal_out(
        self, signal_type: type[BaseException]
    ) -> None:
        with pytest.raises(signal_type):
            describe_exception(_raising(signal_type)())

    @pytest.mark.parametrize("signal_type", TEARDOWN_SIGNALS)
    def test_describe_value_lets_a_str_raised_signal_out(
        self, signal_type: type[BaseException]
    ) -> None:
        with pytest.raises(signal_type):
            describe_value(_raising(signal_type)())

    @pytest.mark.parametrize("signal_type", TEARDOWN_SIGNALS)
    def test_safe_type_name_lets_a_metaclass_raised_signal_out(
        self, signal_type: type[BaseException]
    ) -> None:
        with pytest.raises(signal_type):
            safe_type_name(_unnameable(signal_type)())

    @pytest.mark.parametrize("signal_type", TEARDOWN_SIGNALS)
    def test_the_type_name_fallback_does_not_swallow_it_either(
        self, signal_type: type[BaseException]
    ) -> None:
        """describe_exception falls back to safe_type_name; that path counts too."""
        with pytest.raises(signal_type):
            describe_exception(_unnameable(signal_type)())

    def test_system_exit_is_still_contained_by_every_renderer(self) -> None:
        """The deliberate exception to the rule, pinned so it is not "fixed"."""

        class _Exits(Exception):
            def __repr__(self) -> str:
                raise SystemExit(91)

            def __str__(self) -> str:
                raise SystemExit(91)

        assert describe_exception(_Exits()) == "_Exits"
        assert describe_value(_Exits()) == "_Exits"
        assert safe_type_name(_Exits()) == "_Exits"


class TestEveryRenderingIsCapped:
    """A cap that only covers ``repr`` is not a cap.

    A hostile metaclass can hand back a 100,000-character ``__name__``, and a
    caller can hand over an equally long value; both reached a log line and a
    failure message uncapped.
    """

    def test_an_enormous_type_name_is_capped(self) -> None:
        class _VerboseMeta(type):
            @property
            def __name__(cls) -> str:
                return "n" * 100_000

        class _Verbose(Exception, metaclass=_VerboseMeta):
            def __repr__(self) -> str:
                raise ValueError("no repr")

        assert len(safe_type_name(_Verbose())) == MAX_RENDERED_CHARS
        # ...including when it is reached through the fallback.
        assert len(describe_exception(_Verbose())) == MAX_RENDERED_CHARS

    def test_an_enormous_value_is_capped(self) -> None:
        assert len(describe_value("x" * 100_000)) == MAX_RENDERED_CHARS

    def test_an_ordinary_value_is_untouched(self) -> None:
        assert describe_value("the secret was read") == "the secret was read"
        assert describe_value(17) == "17"

    def test_a_value_with_no_usable_str_degrades_to_its_type(self) -> None:
        class _NoStr:
            def __str__(self) -> str:
                raise ValueError("no str")

            __repr__ = __str__

        assert describe_value(_NoStr()) == "_NoStr"

    def test_a_value_whose_str_returns_a_non_string_degrades(self) -> None:
        class _WrongType:
            def __str__(self) -> str:
                return 17  # type: ignore[return-value]

        # Python's str() itself rejects a non-str __str__, so this lands in the
        # contained path and degrades rather than propagating a TypeError.
        assert describe_value(_WrongType()) == "_WrongType"

    def test_an_empty_rendering_degrades_rather_than_vanishing(self) -> None:
        class _Empty:
            def __str__(self) -> str:
                return ""

        assert describe_value(_Empty()) == "_Empty"

    def test_a_wholly_unrenderable_value_reaches_the_constant(self) -> None:
        class _HostileMeta(type):
            @property
            def __name__(cls) -> str:
                raise ValueError("no name")

        class _Hopeless(metaclass=_HostileMeta):
            def __str__(self) -> str:
                raise ValueError("no str")

        assert describe_value(_Hopeless()) == UNRENDERABLE
