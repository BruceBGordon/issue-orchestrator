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

**How is a contained failure reported?** With ``describe_exception``,
and never silently: a containment that records nothing is
indistinguishable from a bug, which is why the one bare
``except BaseException: pass`` left in the lane executor came back as a
finding of its own (#7135 round 3). Rendering is the boundary's last
line and must not itself raise, so it does not trust the exception.
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
    """The type's name, without trusting a hostile metaclass for it."""
    try:
        name = type(value).__name__
    except BaseException:
        return UNRENDERABLE
    if type(name) is not str or not name:
        return UNRENDERABLE
    return name


def describe_exception(error: BaseException) -> str:
    """Render an exception for a record WITHOUT trusting the exception.

    An exception is user code: ``__str__`` and ``__repr__`` can raise,
    return a non-string, or return megabytes. Rendering one inside a
    containment boundary and letting that rendering raise is how the
    boundary leaks (#7135 round 1) — so every attempt is itself
    contained, the last resort is a constant, and the result is capped.
    """
    try:
        rendered = repr(error)
    except BaseException:
        rendered = ""
    if type(rendered) is not str or not rendered:
        rendered = safe_type_name(error)
    return rendered[:MAX_RENDERED_CHARS]
