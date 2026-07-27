"""On-change logging of the per-issue *queue* decision (launch vs. skip + why).

Extracted from the planner: it is the same "log a coarse control decision only
when it changes" concern as the tech_lead launch log, so it delegates the
keyed on-change cache and stale-key pruning to the shared ``DecisionChangeLog``
owner and keeps only what is queue-specific — the message formatting and the
periodic summary. Behavior is unchanged: the ``trace-queue-decision`` /
``trace-queue-summary`` lines and their ``issue=%d`` keying (so
``issue-orchestrator trace <n>`` surfaces them) are preserved verbatim. Events
remain the machine contract; this is the additive human line.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .decision_change_log import DecisionChangeLog


class QueueDecisionLog:
    """Log each issue's queue decision at INFO on change, plus a periodic summary."""

    def __init__(
        self,
        logger: logging.Logger,
        *,
        summary_interval_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger
        self._changes = DecisionChangeLog(logger)  # owns the on-change cache + prune
        self._last_summary_at: float = 0.0
        self._summary_interval_seconds = summary_interval_seconds
        self._clock = clock

    def record(
        self,
        decision_by_issue: dict[int, str],
        detail_by_issue: dict[int, str],
    ) -> None:
        """Emit queue decision traces only when they change, plus periodic summary."""
        for issue_number, decision in decision_by_issue.items():
            self._log_decision(
                issue_number,
                decision,
                detail_by_issue.get(issue_number),
            )
        # Stale issues no longer in this snapshot are pruned by the shared owner.
        self._changes.retain(decision_by_issue.keys())

        now = self._clock()
        if (now - self._last_summary_at) < self._summary_interval_seconds:
            return
        self._last_summary_at = now

        launch_count = 0
        reason_counts: dict[str, int] = {}
        for decision in decision_by_issue.values():
            kind, reason = decision.split(":", 1)
            if kind == "launch":
                launch_count += 1
                continue
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        reason_summary = ", ".join(
            f"{reason}:{count}" for reason, count in sorted(reason_counts.items())
        )
        self._logger.info(
            "trace-queue-summary total=%d launch=%d skip=%d reasons=%s",
            len(decision_by_issue),
            launch_count,
            len(decision_by_issue) - launch_count,
            reason_summary or "none",
        )

    def _log_decision(
        self,
        issue_number: int,
        decision: str,
        detail: str | None,
    ) -> None:
        """Render one queue decision and hand it to the on-change owner, which
        emits it only when the fingerprint changes for this issue."""
        fingerprint = f"{decision}|{detail}" if detail else decision
        reason = decision.split(":", 1)[1]
        if decision.startswith("launch:"):
            self._changes.note(
                issue_number,
                fingerprint,
                "trace-queue-decision issue=%d decision=launch reason=%s",
                issue_number,
                reason,
            )
            return
        if reason == "dependency_blocked":
            self._changes.note(
                issue_number,
                fingerprint,
                "trace-queue-decision issue=%d decision=skip reason=dependency_blocked detail=%s",
                issue_number,
                detail or "dependency blocked",
            )
            return
        if detail:
            self._changes.note(
                issue_number,
                fingerprint,
                "trace-queue-decision issue=%d decision=skip reason=%s detail=%s",
                issue_number,
                reason,
                detail,
            )
            return
        self._changes.note(
            issue_number,
            fingerprint,
            "trace-queue-decision issue=%d decision=skip reason=%s",
            issue_number,
            reason,
        )
