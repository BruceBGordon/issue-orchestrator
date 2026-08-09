from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.sqlite_maintenance import run_backups_if_due
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.infra.sqlite_registry import list_sqlite_databases


def _create_sqlite_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()


def test_run_backups_if_due_creates_backup(tmp_path):
    config = Config()
    config.repo_root = tmp_path

    publish_db = state_dir(tmp_path) / "goal_pilot.sqlite"
    session_db = state_dir(tmp_path) / "session_registry.sqlite"
    _create_sqlite_db(publish_db)
    _create_sqlite_db(session_db)

    results = run_backups_if_due(config)

    assert any(r.db.key == "goal_pilot" and r.performed for r in results)
    assert any(r.db.key == "session_registry" and r.performed for r in results)

    backup_root = tmp_path / ".issue-orchestrator" / "backups" / "sqlite"
    date_str = datetime.now(timezone.utc).date().isoformat()
    publish_daily = backup_root / "goal_pilot" / "daily" / f"{date_str}.db"
    session_daily = backup_root / "session_registry" / "daily" / f"{date_str}.db"

    assert publish_daily.exists()
    assert session_daily.exists()


def test_run_backups_if_due_respects_cadence(tmp_path):
    config = Config()
    config.repo_root = tmp_path

    publish_db = state_dir(tmp_path) / "goal_pilot.sqlite"
    _create_sqlite_db(publish_db)

    first = run_backups_if_due(config)
    assert any(r.db.key == "goal_pilot" and r.performed for r in first)

    second = run_backups_if_due(config)
    publish_result = next(r for r in second if r.db.key == "goal_pilot")
    assert publish_result.performed is False
    assert publish_result.reason == "cadence"


def test_retention_zero_disables_backups(tmp_path):
    config = Config()
    config.repo_root = tmp_path
    config.sqlite_backup.retention_daily = 0
    config.sqlite_backup.retention_weekly = 0

    publish_db = state_dir(tmp_path) / "goal_pilot.sqlite"
    _create_sqlite_db(publish_db)

    results = run_backups_if_due(config)
    publish_result = next(r for r in results if r.db.key == "goal_pilot")
    assert publish_result.performed is False
    assert publish_result.reason == "retention=0"

    backup_root = tmp_path / ".issue-orchestrator" / "backups" / "sqlite"
    assert not (backup_root / "goal_pilot").exists()


def test_sqlite_registry_includes_timeline_db(tmp_path):
    config = Config()
    config.repo_root = tmp_path

    databases = list_sqlite_databases(config)
    timeline = next((db for db in databases if db.key == "timeline"), None)

    assert timeline is not None
    assert timeline.path_fn(config) == state_dir(tmp_path) / "timeline.sqlite"


def test_sqlite_registry_includes_tech_lead_authority_db(tmp_path):
    """The tech_lead launch-authority store is a first-class state DB (#6769 F3):
    registered for doctor checks, backups, and pragma enforcement."""
    config = Config()
    config.repo_root = tmp_path

    databases = list_sqlite_databases(config)
    entry = next((db for db in databases if db.key == "tech_lead_authority"), None)

    assert entry is not None
    assert entry.label == "Tech Lead Authority"
    assert entry.path_fn(config) == state_dir(tmp_path) / "tech_lead_authority.sqlite"
    assert entry.enabled_fn(config) is True
    assert entry.backup is True
    assert entry.enforce_pragmas is True


def test_tech_lead_authority_db_is_backed_up_when_due(tmp_path):
    """The registered policy actually drives a backup for the real file."""
    config = Config()
    config.repo_root = tmp_path

    from issue_orchestrator.infra.tech_lead_authority_store import (
        SqliteTechLeadAuthorityStore,
    )

    SqliteTechLeadAuthorityStore.for_repo(tmp_path)  # creates the DB file

    results = run_backups_if_due(config)

    assert any(r.db.key == "tech_lead_authority" and r.performed for r in results)
    date_str = datetime.now(timezone.utc).date().isoformat()
    backup = (
        tmp_path / ".issue-orchestrator" / "backups" / "sqlite"
        / "tech_lead_authority" / "daily" / f"{date_str}.db"
    )
    assert backup.exists()


def test_sqlite_registry_includes_open_issue_corpus_cache(tmp_path):
    config = Config()
    config.repo_root = tmp_path

    entry = next(
        db for db in list_sqlite_databases(config) if db.key == "open_issue_corpus"
    )

    assert entry.label == "Open Issue Corpus"
    assert entry.path_fn(config) == state_dir(tmp_path) / "open_issue_corpus.sqlite"
    assert entry.enabled_fn(config) is True
    assert entry.backup is True
    assert entry.enforce_pragmas is True


def test_open_issue_corpus_db_is_backed_up_when_due(tmp_path):
    config = Config()
    config.repo_root = tmp_path
    from issue_orchestrator.infra.open_issue_corpus_store import (
        SqliteOpenIssueCorpusStore,
    )

    SqliteOpenIssueCorpusStore.for_repo(tmp_path).replace_all(
        (), watermark="2026-07-23T12:00:00Z"
    )

    results = run_backups_if_due(config)

    assert any(db.db.key == "open_issue_corpus" and db.performed for db in results)


def test_pending_work_claims_is_registered_for_backup_and_pragmas(tmp_path):
    """The only durable record of dequeued work must not be unmanaged (#6999 F10).

    Losing or corrupting this database is direct queued-work loss, so it needs
    the same startup integrity checks, pragma enforcement and backups as every
    other authoritative store.
    """
    config = Config()
    config.repo_root = tmp_path

    registered = {db.key: db for db in list_sqlite_databases(config)}

    claims = registered["pending_work_claims"]
    assert claims.backup
    assert claims.enforce_pragmas
    assert claims.enabled_fn(config)
    assert claims.path_fn(config) == state_dir(tmp_path) / "pending_work_claims.sqlite"


def test_pending_work_claims_is_backed_up(tmp_path):
    from datetime import datetime, timezone

    config = Config()
    config.repo_root = tmp_path
    _create_sqlite_db(state_dir(tmp_path) / "pending_work_claims.sqlite")

    results = run_backups_if_due(config)

    assert any(r.db.key == "pending_work_claims" and r.performed for r in results)
    date_str = datetime.now(timezone.utc).date().isoformat()
    backup = (
        tmp_path / ".issue-orchestrator" / "backups" / "sqlite"
        / "pending_work_claims" / "daily" / f"{date_str}.db"
    )
    assert backup.exists()


def test_the_real_claim_store_writes_where_the_registry_says(tmp_path):
    """The registry entry and the adapter must not drift apart."""
    from issue_orchestrator.execution.pending_work_claim_store import (
        SqlitePendingWorkClaimStore,
    )

    config = Config()
    config.repo_root = tmp_path
    SqlitePendingWorkClaimStore.for_repo(tmp_path)

    registered = {db.key: db for db in list_sqlite_databases(config)}
    assert registered["pending_work_claims"].path_fn(config).is_file()


def test_sqlite_registry_includes_the_tech_lead_run_history(tmp_path):
    """ADR-0033's local run history is a first-class state DB (#6858 F1).

    It holds the ONLY durable record that a tech-lead run happened and what it
    concluded, and nothing rebuilds it — the runs it describes executed in
    worktrees that no longer exist. So it gets the same startup integrity check,
    pragma enforcement, and backups as every other authoritative store.
    """
    config = Config()
    config.repo_root = tmp_path

    entry = next(
        db for db in list_sqlite_databases(config) if db.key == "tech_lead_runs"
    )

    assert entry.label == "Tech Lead Runs"
    assert entry.path_fn(config) == state_dir(tmp_path) / "tech_lead_runs.sqlite"
    assert entry.enabled_fn(config) is True
    assert entry.backup is True
    assert entry.enforce_pragmas is True


def test_tech_lead_run_history_db_is_backed_up_when_due(tmp_path):
    """The registered policy actually drives a backup for the real file."""
    config = Config()
    config.repo_root = tmp_path

    from issue_orchestrator.infra.tech_lead_run_record_store import (
        SqliteTechLeadRunRecordStore,
    )

    SqliteTechLeadRunRecordStore.for_repo(tmp_path)  # creates the DB file

    results = run_backups_if_due(config)

    assert any(r.db.key == "tech_lead_runs" and r.performed for r in results)
    date_str = datetime.now(timezone.utc).date().isoformat()
    backup = (
        tmp_path / ".issue-orchestrator" / "backups" / "sqlite"
        / "tech_lead_runs" / "daily" / f"{date_str}.db"
    )
    assert backup.exists()
