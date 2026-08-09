"""Composing the tech-lead run history honours its best-effort contract (#6858 F2).

The port says plainly that an unreachable history backing store must never fail a
tech-lead run. The SQLite store cannot honour that itself — it cannot know whether
losing durability is acceptable — so it fails loudly on an unusable database and
the COMPOSITION ROOT makes the call: log the loss of durability and select the
in-memory implementation.

Before this, production eagerly constructed the store, whose ``__init__`` created
directories, opened a connection, ran the schema and committed. A read-only state
directory or a corrupt file therefore raised out of ``build_orchestrator`` and the
Repository Engine did not start at all — because a receipt could not be filed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from issue_orchestrator.domain.models import SessionStatus
from issue_orchestrator.domain.tech_lead_run_record import TechLeadRunPhase
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
)
from issue_orchestrator.entrypoints.bootstrap_tech_lead import (
    create_tech_lead_run_activity,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.repo_identity import state_dir

STARTED = datetime(2026, 8, 9, 9, 0, 0)


def _config(repo_root: Path) -> Config:
    config = Config()
    config.repo_root = repo_root
    return config


def _session(run_dir: Path) -> SimpleNamespace:
    run_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        issue=SimpleNamespace(number=900, title="Board anchor", agent_type="agent:tl"),
        terminal_id="tech-lead-900",
        started_at=STARTED,
        run_dir=run_dir,
        run_assets=SimpleNamespace(run_id="run-900", session_name="tech-lead-900"),
        tech_lead_scope=TechLeadLaunchScope(
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW
        ),
    )


def _records_a_run(activity, run_dir: Path) -> TechLeadRunPhase:
    session = _session(run_dir)
    activity.note_started(session)
    activity.note_concluded(session, SessionStatus.COMPLETED)
    (record,) = activity.recent(limit=5)
    return record.phase


def test_a_healthy_state_directory_yields_a_durable_history(tmp_path):
    activity = create_tech_lead_run_activity(_config(tmp_path))

    assert _records_a_run(activity, tmp_path / "run") is TechLeadRunPhase.COMPLETED
    assert (state_dir(tmp_path) / "tech_lead_runs.sqlite").exists()


def test_an_unwritable_state_directory_still_composes_an_engine(tmp_path):
    """A file where the state directory belongs: ``mkdir`` cannot succeed."""
    blocker = tmp_path / ".issue-orchestrator"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a directory", encoding="utf-8")

    activity = create_tech_lead_run_activity(_config(tmp_path))

    # The engine composes, and runs are still recorded — in memory, for the life
    # of this process, which is what the warning in the log says.
    assert _records_a_run(activity, tmp_path / "run") is TechLeadRunPhase.COMPLETED


def test_a_corrupt_history_database_still_composes_an_engine(tmp_path):
    """Schema execution raises ``sqlite3.DatabaseError`` on a non-database file."""
    db_path = state_dir(tmp_path) / "tech_lead_runs.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"this is not a sqlite database" * 16)

    activity = create_tech_lead_run_activity(_config(tmp_path))

    assert _records_a_run(activity, tmp_path / "run") is TechLeadRunPhase.COMPLETED
