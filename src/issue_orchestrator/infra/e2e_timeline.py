"""E2E timeline helpers for reading orchestrator events from worktree databases."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ports.timeline_store import TimelineStore
    from .e2e_db import E2ERun

logger = logging.getLogger(__name__)


def read_orchestrator_events_by_window(
    timeline_db_path: Path,
    started_at: str,
    finished_at: str | None,
) -> list[dict]:
    """Read orchestrator events from a timeline DB by time window.

    Used for reading agent events from the E2E worktree's timeline.
    The E2E worktree is isolated, so time-window filtering is sufficient
    (no instance_id needed). Only returns issue-keyed events (issue_number > 0).
    """
    try:
        uri = f"file:{timeline_db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row

        end_ts = finished_at or "9999-12-31T23:59:59Z"

        rows = conn.execute(
            """
            SELECT issue_number, event_id, source_event, timestamp, event, data_json
            FROM timeline_events
            WHERE issue_number > 0
              AND timestamp >= ? AND timestamp <= ?
            ORDER BY sequence ASC
            """,
            (started_at, end_ts),
        ).fetchall()

        conn.close()
    except Exception:
        logger.debug("Could not read timeline from %s", timeline_db_path, exc_info=True)
        return []

    from collections import defaultdict

    from ..ports.timeline_store import TimelineRecord
    from ..timeline import TimelineStream

    # Group records by issue_number so each event keeps its real identity.
    # Passing a single placeholder issue_number to TimelineStream.from_records
    # would lose per-issue identity and break downstream window matching.
    by_issue: dict[int, list[TimelineRecord]] = defaultdict(list)
    for row in rows:
        data_json = row["data_json"] or "{}"
        try:
            data = json.loads(data_json)
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        issue_num = int(row["issue_number"])
        by_issue[issue_num].append(
            TimelineRecord(
                event_id=str(row["event_id"]),
                timestamp=str(row["timestamp"]),
                event=str(row["event"]),
                data=data,
                source_event=str(row["source_event"] or ""),
            )
        )

    if not by_issue:
        return []

    all_events: list[dict] = []
    for issue_num, records in by_issue.items():
        stream = TimelineStream.from_records(issue_num, records)
        events = [evt.to_dict() for evt in stream.events]
        # Promote ``branch_name`` from the raw data blob onto the
        # serialized event dict. TimelineEvent's dataclass doesn't
        # carry it as a first-class field, so ``to_dict()`` would
        # drop it — but the matcher needs it to build compact
        # affordance labels ("concurrent-1-pipeline (5713)" instead
        # of bare "#5713"). Matching is positional because
        # TimelineStream.from_records preserves record order.
        for evt, rec in zip(events, records):
            data = rec.data if isinstance(rec.data, dict) else {}
            branch = data.get("branch_name")
            if isinstance(branch, str) and branch:
                evt["branch_name"] = branch
        all_events.extend(events)

    # Restore chronological order across issues
    all_events.sort(key=lambda e: e.get("timestamp", ""))
    return all_events


def snapshot_agent_events(
    run: "E2ERun",
    *,
    repo_root: Path,
    timeline_store: "TimelineStore",
) -> None:
    """Copy agent timeline events from the E2E worktree into the base repo.

    Agent sessions run in the E2E worktree, so their timeline events live in
    that worktree's ``timeline.sqlite`` — which is wiped on refresh. They are
    snapshotted into the base repo's timeline under the same E2E run key (a
    negative int) so the nesting endpoints can split them from the pytest
    events by event-name prefix (``e2e.*`` vs ``session.*``).

    Lives beside :func:`read_orchestrator_events_by_window` rather than on the
    orchestrator facade: it is the write half of the same worktree-timeline
    concern, and needs nothing from the facade but a repo root and the store.
    """
    from ..domain.timeline_key import TimelineKey
    from ..ports.timeline_store import TimelineRecord
    from .e2e_worktree import get_e2e_worktree_path

    try:
        wt_timeline = (
            get_e2e_worktree_path(repo_root)
            / ".issue-orchestrator" / "state" / "timeline.sqlite"
        )
        if not wt_timeline.exists():
            return

        agent_events = read_orchestrator_events_by_window(
            wt_timeline, started_at=run.started_at, finished_at=run.finished_at
        )
        if not agent_events:
            return

        store_key = TimelineKey.for_e2e_run(run.id).to_store_key()
        for evt in agent_events:
            # Stored as ``e2e.agent_snapshot`` to avoid the run-scoped CHECK
            # constraint. The data blob carries the complete pre-rendered event
            # dict; the read path returns it directly rather than re-deriving
            # it through TimelineStream.
            timeline_store.append(
                store_key,
                TimelineRecord(
                    event_id=f"snap-{evt.get('event_id', '')}",
                    timestamp=evt.get("timestamp", ""),
                    event="e2e.agent_snapshot",
                    data=evt,
                    source_event="e2e.agent_snapshot",
                ),
            )
        logger.info(
            "Snapshot %d agent events for E2E run %d", len(agent_events), run.id
        )
    except Exception:
        logger.debug(
            "Could not snapshot E2E agent events for run %d", run.id, exc_info=True
        )
