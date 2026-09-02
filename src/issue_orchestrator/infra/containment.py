# pyright: strict
"""What every containment boundary in this repository needs.

Several places deliberately contain failure so that a best-effort side
activity — sampling host contention, winding down a cancelled lane — can
never replace the outcome of the work it observes. Each such boundary
has to answer the same three questions, and answering them differently
in different places is how the boundaries leak. They are answered once,
here.

**What must still get out?** Only the signals whose meaning is *"stop,
the caller is going away"*. Containing one of those does not protect an
outcome, it defeats a teardown:

- ``KeyboardInterrupt`` — the operator's Ctrl-C must win, always, and a
  SECOND one during cleanup must win over the first (#7135 round 2).
- ``GeneratorExit`` — swallowing it breaks the generator protocol for a
  caller that is being closed.
- ``asyncio.CancelledError`` — swallowing a cancellation is the classic
  way to make an async caller un-cancellable.

**What about SystemExit?** Deliberately ABSENT from that list. It is the
one stop-shaped exception a best-effort activity may contain, because
letting it out means a probe or a diagnostic silently substituting its
own exit status for the real one — the precise harm these boundaries
exist to prevent.

**How is a contained failure reported?** With ``describe_exception`` or
``describe_value``, and never silently: a containment that records
nothing is indistinguishable from a bug, which is why the one bare
``except BaseException: pass`` left in the lane executor came back as a
finding of its own (#7135 round 3). Rendering is the boundary's last
line and must not itself raise, so it does not trust what it renders.

**The renderers obey the same teardown policy as their callers.** They
used to catch every ``BaseException``, teardown signals included — so a
``__repr__`` or a hostile metaclass ``__name__`` that raised
``KeyboardInterrupt`` was swallowed one layer BELOW the boundary that
had just re-raised the same signal one layer above. The module
contradicted itself: every consumer here calls a renderer from inside
its own ``except BaseException`` handler, so a renderer that eats a
Ctrl-C eats it for good. They now re-raise ``TEARDOWN_SIGNALS`` and
contain everything else, which is what this module always claimed.

**Every rendering is capped**, not just the exception one. A hostile
metaclass can return a 100,000-character ``__name__`` and a caller can
hand over an equally long value; a cap that only covers ``repr`` is not
a cap. ``MAX_RENDERED_CHARS`` bounds all three.
"""

from __future__ import annotations

import asyncio

TEARDOWN_SIGNALS: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,
    GeneratorExit,
    asyncio.CancelledError,
)

UNRENDERABLE = "<unrenderable exception>"
# A hostile or merely enormous exception must not bloat a log line or a
# JSONL row.
MAX_RENDERED_CHARS = 500


def is_teardown_signal(error: BaseException) -> bool:
    """Whether this exception means the caller is being torn down."""
    return isinstance(error, TEARDOWN_SIGNALS)


def safe_type_name(value: object) -> str:
    """The type's name, without trusting a hostile metaclass for it.

    A teardown signal raised by that metaclass still gets out: this is
    called from inside boundaries that have just re-raised the same
    signals themselves, so containing one here would defeat the very
    teardown they let through.
    """
    try:
        name = type(value).__name__
    except TEARDOWN_SIGNALS:
        raise
    except BaseException:
        return UNRENDERABLE
    if type(name) is not str or not name:
        return UNRENDERABLE
    # A metaclass is free to return 100,000 characters; a cap that only
    # covers ``repr`` is not a cap.
    return name[:MAX_RENDERED_CHARS]


def describe_exception(error: BaseException) -> str:
    """Render an exception for a record WITHOUT trusting the exception.

    An exception is user code: ``__str__`` and ``__repr__`` can raise,
    return a non-string, or return megabytes. Rendering one inside a
    containment boundary and letting that rendering raise is how the
    boundary leaks (#7135 round 1) — so every attempt is itself
    contained, the last resort is a constant, and the result is capped.

    Contained, except for :data:`TEARDOWN_SIGNALS`: see the module
    docstring. A ``__repr__`` that raises ``KeyboardInterrupt`` is still
    the operator saying stop.
    """
    try:
        rendered = repr(error)
    except TEARDOWN_SIGNALS:
        raise
    except BaseException:
        rendered = ""
    if type(rendered) is not str or not rendered:
        rendered = safe_type_name(error)
    return rendered[:MAX_RENDERED_CHARS]


def describe_value(value: object) -> str:
    """Render an arbitrary untrusted value for a record.

    The sibling of :func:`describe_exception` for the things that are not
    exceptions: a status string a caller handed back, a reason a
    predicate returned. Same three properties — contained, degrading to
    the type name and then to a constant, and capped — because a value
    from caller code is exactly as untrustworthy as an exception from
    it, and interpolating one into a message is how a boundary that
    contains everything else still dies.

    ``str`` rather than ``repr``: this renders values that are meant to
    READ as part of a message, where an exception is being identified.
    """
    try:
        rendered = str(value)
    except TEARDOWN_SIGNALS:
        raise
    except BaseException:
        rendered = ""
    if type(rendered) is not str or not rendered:
        rendered = safe_type_name(value)
    return rendered[:MAX_RENDERED_CHARS]
