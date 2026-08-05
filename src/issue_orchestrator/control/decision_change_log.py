"""On-change logging of a keyed control decision.

Coarse control decisions (launch / skip / defer / gate outcomes) belong in the
human-readable log, not only in the machine-consumable event stream — a queued
session that keeps being deferred should explain itself in the per-issue trace
log instead of going silent. The problem with logging such a decision every tick
is spam: a paused orchestrator would emit "skip: paused" ~60x/hour. This owner
solves that WITHOUT eliding the decision: it logs at INFO only when the decision
*changes* for a given key (a state transition), so the first skip and the later
resume are both visible while the steady state stays quiet.

Events remain the sole machine contract (UI/tests never parse log text); this is
the additive human-readable "what did I decide, and why" (see the Events vs Logs
section of the repo guide).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable


class DecisionChangeLog:
    """Log a per-key decision at INFO only when its fingerprint changes."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._last: dict[int, str] = {}

    def note(
        self, key: int, fingerprint: str, message: str, *args: object
    ) -> bool:
        """Emit ``message % args`` at INFO iff ``fingerprint`` changed for ``key``.

        ``fingerprint`` is the change-detection identity (encode decision + reason
        so a reason flip re-logs); ``message``/``args`` are the human line. Returns
        True when it logged, so callers can assert the transition in tests.
        """
        if self._last.get(key) == fingerprint:
            return False
        self._last[key] = fingerprint
        self._logger.info(message, *args)
        return True

    def retain(self, live_keys: Iterable[int]) -> None:
        """Forget keys no longer present, so a later reappearance logs fresh."""
        live = set(live_keys)
        for key in [k for k in self._last if k not in live]:
            del self._last[key]
