"""Durable schema for the tech_lead authority store, and its migrations.

Split from the adapter because it is a distinct concern with a distinct
lifetime: the adapter's methods describe how the orchestrator READS AND WRITES
its ledgers today, while this file is the accumulating record of every shape
those ledgers have ever had. The two change for unrelated reasons — a new
column here, a new query there — and keeping them together made the adapter
grow past its hotspot budget for reasons that had nothing to do with its
behavior.

One entry point: :func:`initialize_tech_lead_authority_schema` creates whatever
is missing and backfills whatever an older database is short of.
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS tech_lead_launch_authority (
    run_id TEXT NOT NULL,
    session_name TEXT NOT NULL,
    authority TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (run_id, session_name)
);
CREATE TABLE IF NOT EXISTS tech_lead_proposal_ops (
    issue_number INTEGER PRIMARY KEY,
    op TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tech_lead_patterns (
    signature TEXT PRIMARY KEY,
    issue_number INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    observation_count INTEGER NOT NULL DEFAULT 1,
    fix_class TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL DEFAULT '',
    diagnosis TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tech_lead_pattern_observations (
    signature TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (signature, observation_id)
);
CREATE TABLE IF NOT EXISTS tech_lead_pending_case_files (
    signature TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    idempotency_marker TEXT NOT NULL,
    body_observation_id TEXT NOT NULL,
    fix_class TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL DEFAULT '',
    diagnosis TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tech_lead_pending_promotions (
    signature TEXT PRIMARY KEY,
    case_file_issue_number INTEGER NOT NULL,
    target_repo TEXT NOT NULL,
    title TEXT NOT NULL,
    idempotency_marker TEXT NOT NULL,
    area TEXT NOT NULL DEFAULT '',
    body_observations INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tech_lead_promoted_findings (
    signature TEXT PRIMARY KEY,
    case_file_issue_number INTEGER NOT NULL,
    target_repo TEXT NOT NULL,
    target_issue_number INTEGER NOT NULL,
    state TEXT NOT NULL,
    area TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    shipped_pr_url TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL,
    reported_observations INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tech_lead_shipped_fixes (
    issue_number INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    pr_url TEXT NOT NULL,
    area TEXT NOT NULL,
    merged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tech_lead_storm_cohorts (
    anchor_issue_number INTEGER PRIMARY KEY,
    cohort TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""

# (table, column, DDL) added after that table first shipped. ``CREATE TABLE IF
# NOT EXISTS`` leaves an already-created table alone, so a database written by
# an earlier version keeps its original columns and needs these backfilled in
# place. Every default preserves the pre-existing meaning exactly.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # #6957 promotion facts on the #6781 pattern ledger. The defaults mean "one
    # recorded observation, unclassified" — therefore not promotable until the
    # tech lead classifies the signature on a later observation.
    ("tech_lead_patterns", "observation_count", "INTEGER NOT NULL DEFAULT 1"),
    ("tech_lead_patterns", "fix_class", "TEXT NOT NULL DEFAULT ''"),
    ("tech_lead_patterns", "area", "TEXT NOT NULL DEFAULT ''"),
    ("tech_lead_patterns", "diagnosis", "TEXT NOT NULL DEFAULT ''"),
    # #6957 later-evidence high-water mark on the promotion ledger.
    (
        "tech_lead_promoted_findings",
        "reported_observations",
        "INTEGER NOT NULL DEFAULT 0",
    ),
)


def initialize_tech_lead_authority_schema(conn: sqlite3.Connection) -> None:
    """Create every table and backfill every column an older database lacks.

    ``tech_lead_pattern_observations`` (#6957 review F1) needs no backfill: an
    older row's ``observation_count`` stays exactly as recorded, and every
    observation from this version forward contributes its own identity row — so
    a legacy count is never re-counted and never lost.
    """
    conn.executescript(SCHEMA)
    added = False
    for table, column, ddl in _ADDED_COLUMNS:
        existing = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            added = True
    if added:
        conn.commit()
