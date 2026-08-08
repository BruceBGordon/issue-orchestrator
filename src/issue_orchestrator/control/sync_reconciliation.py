"""The soft reconciliation check a collection sync runs before it writes.

Extracted from :mod:`.action_applier` (a long-standing line-budget hotspot)
because it depends on nothing of the applier's: given an issue's current labels
- or ``None`` when they could not be observed - and what a sync intends to do,
it reports whether to proceed and emits the two observation events the UI reads.

Deliberately SOFT. A label that is already gone is not a failure: something
removed it, which is exactly what the sync was going to do. The check exists so
that divergence is visible, not so that it blocks.
"""

from __future__ import annotations

import logging

from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import EventSink, make_trace_event

logger = logging.getLogger(__name__)


def check_sync_reconciliation(
    events: EventSink,
    issue_number: int,
    add_labels: tuple[str, ...],
    remove_labels: tuple[str, ...],
    current: set[str] | None,
) -> tuple[bool, str, set[str]]:
    """Whether a sync should proceed, why not, and the labels it observed.

    ``current`` is ``None`` when the labels could not be OBSERVED, which is not
    the same as "no labels": the sync proceeds with a warning rather than
    treating an unreadable board as an empty one.
    """
    if current is None:
        logger.warning(
            issue_log(issue_number, "Reconciliation enabled but cannot fetch labels"),
        )
        return True, "Cannot fetch current labels", set()

    missing_to_remove = set(remove_labels) - current
    if missing_to_remove:
        msg = f"Labels to remove not present: {missing_to_remove}"
        logger.warning(issue_log(issue_number, "Reconciliation: %s"), msg)
        events.publish(make_trace_event(
            EventName.RECONCILIATION_WARNING,
            {
                "issue_number": issue_number,
                "message": msg,
                "missing_labels": list(missing_to_remove),
            },
        ))

    events.publish(make_trace_event(
        EventName.RECONCILIATION_CHECKED,
        {
            "issue_number": issue_number,
            "current_labels": list(current),
            "add_labels": list(add_labels),
            "remove_labels": list(remove_labels),
        },
    ))
    return True, "", current


__all__ = ["check_sync_reconciliation"]
