"""Per-target read budget for finding-promotion loop closure (#6957).

``tech_lead.findings.max_open_promoted`` is the lane's promise about API cost —
"loop closure polls at most that many issues per target" — but the promotion
ledger is DURABLE and outlives the setting. Lower the cap, or restart after a
larger cohort was filed, and the ledger still holds more open rows than the
current window; an unbudgeted reader would poll every one of them, every tick,
forever (#6957 review F5).

This module owns that budget, separately from the promotion policy owner
because it is the lane's only piece of cross-tick MUTABLE state: everything in
``tech_lead_finding_promotion`` is a pure function of the durable ledgers, while
this carries a rotation cursor so rows beyond the current window are polled on a
later tick instead of starving behind a fixed prefix.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from ..domain.tech_lead_findings import PromotedFinding

logger = logging.getLogger(__name__)


class PromotionReadBudget:
    """Bounds loop-closure reads per target repo, with fair rotation (#6957 F5).

    ``max_open_promoted`` is the lane's promise about API cost — "at most that
    many reads per target per tick" — but the durable promotion ledger outlives
    the setting. Lower the cap (or restart after a larger cohort was filed) and
    the ledger still holds more open rows than the current window, so an
    unbudgeted reader would poll every one of them on every tick, forever.

    This is the budget owner: it hands out at most ``per_target_limit`` open
    promotions per target on each call, and it ROTATES — the next call resumes
    after the last signature it handed out, wrapping around — so rows beyond
    the current window are polled on a later tick instead of starving behind a
    fixed prefix. ``ceil(open_rows / limit)`` ticks cover every row.

    The cursor is per-process: a restart simply resumes rotation from the
    lowest signature, which costs nothing but re-reading rows already covered.
    """

    def __init__(self) -> None:
        # target repo (casefolded) -> the last signature handed out for it.
        self._cursor: dict[str, str] = {}

    def select(
        self, promotions: Iterable["PromotedFinding"], *, per_target_limit: int
    ) -> tuple["PromotedFinding", ...]:
        """The open promotions to poll this tick, capped per target repo."""
        if per_target_limit < 1:
            raise ValueError(
                "the promotion read budget needs a positive per-target limit;"
                f" got {per_target_limit}"
            )
        by_target: dict[str, list["PromotedFinding"]] = {}
        for promotion in promotions:
            if not promotion.is_open:
                continue
            by_target.setdefault(promotion.target_repo.casefold(), []).append(promotion)
        selected: list["PromotedFinding"] = []
        for target_key in sorted(by_target):
            rows = sorted(by_target[target_key], key=lambda row: row.signature)
            window = self._window(target_key, rows, per_target_limit)
            if window:
                self._cursor[target_key] = window[-1].signature
            if len(rows) > len(window):
                logger.info(
                    "[tech_lead] Loop-closure reads for %s bounded to %d of %d"
                    " in-flight promotion(s) this tick"
                    " (tech_lead.findings.max_open_promoted); the remainder are"
                    " polled on following ticks",
                    window[0].target_repo if window else target_key,
                    len(window),
                    len(rows),
                )
            selected.extend(window)
        return tuple(selected)

    def _window(
        self, target_key: str, rows: list["PromotedFinding"], limit: int
    ) -> list["PromotedFinding"]:
        """``limit`` rows starting after the cursor, wrapping around."""
        cursor = self._cursor.get(target_key, "")
        start = 0
        if cursor:
            start = next(
                (i for i, row in enumerate(rows) if row.signature > cursor),
                0,
            )
        rotated = rows[start:] + rows[:start]
        return rotated[:limit]
