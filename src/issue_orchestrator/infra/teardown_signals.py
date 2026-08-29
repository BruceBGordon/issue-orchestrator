# pyright: strict
"""The exceptions a containment boundary may never swallow.

Several places in this repository deliberately contain failure so that a
best-effort side activity — sampling host contention, collecting a
failed lane's accounting — can never replace the outcome of the work it
observes. Each of those boundaries has to catch ``BaseException``,
because the leaks they exist to stop are not all ``Exception``\\ s (a
sampler raising ``SystemExit`` walked straight through an
``except Exception`` in round 1 of #7135).

Catching ``BaseException`` immediately raises the opposite question:
what must still get out? Exactly the signals whose meaning is *"stop,
the caller is going away"*. Containing one of those does not protect a
record, it defeats a teardown:

- ``KeyboardInterrupt`` — the operator's Ctrl-C must win, always, and a
  SECOND one during cleanup must win over the first (round 2 finding 2).
- ``GeneratorExit`` — swallowing it breaks the generator protocol for a
  caller that is being closed.
- ``asyncio.CancelledError`` — swallowing a cancellation is the classic
  way to make an async caller un-cancellable.

``SystemExit`` is deliberately ABSENT. It is the one stop-shaped
exception a best-effort activity may contain, because letting it out
means a probe or a diagnostic silently substituting its own exit status
for the real one — the precise harm these boundaries exist to prevent.
Every boundary that contains it records that it did.

One tuple, one rationale, so the two boundaries cannot drift apart.
"""

from __future__ import annotations

import asyncio

TEARDOWN_SIGNALS: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,
    GeneratorExit,
    asyncio.CancelledError,
)


def is_teardown_signal(error: BaseException) -> bool:
    """Whether this exception means the caller is being torn down."""
    return isinstance(error, TEARDOWN_SIGNALS)
