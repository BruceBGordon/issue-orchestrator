"""Tech Lead board publisher: the projection sink for tech_lead facts (#6781).

Runs at fact-gathering time, whenever the tech_lead anchor scan produced facts
(the health-review-relevant cadence). Two fire-and-forget effects, in the
same spirit as the fact gatherer's event sink — projections of observed
facts, never decisions:

1. retain the latest open case-file facts behind a read-only injected reader
   so the board snapshot builder projects the ledger into later snapshots;
2. render the operator-facing tech_lead board markdown — including the
   approval backlog, which unions the durable op ledger with the gate labels
   this tick observed, so a gated proposal with no ledger row is still
   visible to the operator who has to approve it (#7014);
   (:mod:`..view_models.tech_lead_board`) and write it to
   ``.issue-orchestrator/state/tech-lead-board.md``, throttled by content
   comparison — the render is deterministic and hour-granular, so unchanged
   ledgers cost zero writes.

Publish failures are logged, never raised: a projection write must not fail
the planning tick (the ledgers themselves stay authoritative in SQLite).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ..infra.repo_identity import state_dir
from ..view_models.tech_lead_board import build_tech_lead_board_view, render_tech_lead_board_md
from .tech_lead_case_files import case_file_area_counts

if TYPE_CHECKING:
    from ..domain.models import TechLeadFacts
    from ..domain.tech_lead_session import TechLeadCaseFileSummary, TechLeadShippedFixSummary
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from ..view_models.tech_lead_board import TechLeadBoardView

logger = logging.getLogger(__name__)

TECH_LEAD_BOARD_FILENAME = "tech-lead-board.md"


def tech_lead_board_path(repo_root: Path | str) -> Path:
    """Canonical on-disk home of the rendered tech_lead board (single owner)."""
    return state_dir(repo_root) / TECH_LEAD_BOARD_FILENAME


class TechLeadBoardPublisher:
    """Projects each tech-lead-armed fact scan onto state + the board file."""

    def __init__(
        self,
        *,
        board_path: Path,
        authority: "Optional[TechLeadAuthorityStore]",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._board_path = board_path
        self._authority = authority
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_rendered: str | None = None
        self._case_files: tuple[TechLeadCaseFileSummary, ...] = ()

    def case_files(self) -> tuple["TechLeadCaseFileSummary", ...]:
        """Return the latest immutable anchor-scan case-file projection."""
        return self._case_files

    def shipped_fixes(self, limit: int) -> tuple["TechLeadShippedFixSummary", ...]:
        """Return restart-safe merged-fix facts from the durable ledger."""
        if self._authority is None:
            return ()
        return self._authority.list_recent_shipped_fixes(limit=limit)

    def publish(self, facts: "TechLeadFacts", *, last_health_review_at: float) -> None:
        """Retain the scan facts and refresh the local board projection.

        The retained case-file projection is replaced ONLY when this tick
        actually ran the anchor scan (``facts.case_files_scanned``). An
        API-frugal tick that skipped the scan carries ``open_case_files=()``
        meaning "not observed", not "observed empty"; overwriting the
        projection with that empty tuple would erase durable case-file
        evidence between scans and leave the board snapshot blind to
        accumulating pattern evidence (#6781 R2). A scan that genuinely
        observed no open case files still clears the projection — there the
        empty tuple is a real observation. ``self._case_files`` is the single
        source of truth for both the injected reader and the rendered board,
        so ``_build_view`` reads it rather than the raw facts.
        """
        if facts.case_files_scanned:
            self._case_files = facts.open_case_files
        try:
            rendered = render_tech_lead_board_md(
                self._build_view(facts, last_health_review_at)
            )
        except Exception:
            logger.warning("[tech-lead-board] Failed to render board", exc_info=True)
            return
        if rendered == self._last_rendered:
            return
        try:
            self._board_path.parent.mkdir(parents=True, exist_ok=True)
            self._board_path.write_text(rendered, encoding="utf-8")
        except OSError:
            logger.warning(
                "[tech-lead-board] Failed to write %s", self._board_path, exc_info=True
            )
            return
        self._last_rendered = rendered
        logger.debug("[tech-lead-board] Board written: %s", self._board_path)

    def _build_view(
        self, facts: "TechLeadFacts", last_health_review_at: float
    ) -> "TechLeadBoardView":
        """Union the durable op ledger with THIS tick's observed gate labels.

        The gated proposals are taken straight from the facts rather than
        retained across ticks like the case-file projection: they are observed
        from the board the tick always fetches, so every publish carries a
        complete, current backlog — and a retained one would keep advertising a
        proposal the operator has already approved (#7014).
        """
        return build_tech_lead_board_view(
            ops=self._authority.list_ops() if self._authority is not None else (),
            gated_proposals=facts.gated_proposals,
            case_files=self._case_files,
            area_counts=case_file_area_counts(self._case_files),
            last_health_review_at=last_health_review_at,
            now=self._clock(),
        )
