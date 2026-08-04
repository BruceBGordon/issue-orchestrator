"""SQL for the tech-lead crash-window outbox (#6957 round-3 review F10/F11).

Two of the lane's writes span systems in a fixed order — create the GitHub
issue, then record the ledger row — and a crash in between leaves an issue the
orchestrator knows nothing about. A marker lookup can find that issue again,
but it cannot say WHICH command wrote it or what that command meant, and the
retry is not guaranteed to be the same command.

These two tables are that missing authority: written before the remote create,
read to finalize an interrupted one, discarded as soon as the ledger row lands.
They are the same shape and the same lifetime, so their SQL lives together and
apart from the durable ledgers in ``tech_lead_authority_store`` — those record
what IS true, these record what was ABOUT to become true.

Plain functions over a connection, not a class: the owning store already owns
the connection, the write lock, and the transaction boundary, and these must
run inside them.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ..domain.tech_lead_findings import PendingCaseFile, PendingPromotion
from ..ports.tech_lead_authority import TechLeadPendingIntentConflictError


def case_file_from_row(row: sqlite3.Row) -> PendingCaseFile:
    """Project one in-flight case-file row onto its typed value."""
    return PendingCaseFile(
        signature=str(row["signature"]),
        title=str(row["title"]),
        idempotency_marker=str(row["idempotency_marker"]),
        body_observation_id=str(row["body_observation_id"]),
        fix_class=str(row["fix_class"]),
        area=str(row["area"]),
        diagnosis=str(row["diagnosis"]),
    )


def promotion_from_row(row: sqlite3.Row) -> PendingPromotion:
    """Project one in-flight promotion row onto its typed value."""
    return PendingPromotion(
        signature=str(row["signature"]),
        case_file_issue_number=int(row["case_file_issue_number"]),
        target_repo=str(row["target_repo"]),
        title=str(row["title"]),
        idempotency_marker=str(row["idempotency_marker"]),
        area=str(row["area"]),
        body_observations=int(row["body_observations"]),
    )


def insert_case_file(tx: sqlite3.Connection, pending: PendingCaseFile) -> None:
    """Record an in-flight case-file creation (create-once).

    An identical payload is a no-op; a DIFFERENT one raises. A later command
    must never silently replace an earlier one's authority — that is exactly
    how a retry came to attribute an older issue body to itself.
    """
    row = tx.execute(
        "SELECT * FROM tech_lead_pending_case_files WHERE signature = ?",
        (pending.signature,),
    ).fetchone()
    if row is not None:
        if case_file_from_row(row) == pending:
            return
        raise TechLeadPendingIntentConflictError(
            f"a different in-flight case file is already recorded for signature"
            f" {pending.signature!r}"
        )
    tx.execute(
        "INSERT INTO tech_lead_pending_case_files (signature, title,"
        " idempotency_marker, body_observation_id, fix_class, area, diagnosis,"
        " recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pending.signature,
            pending.title,
            pending.idempotency_marker,
            pending.body_observation_id,
            pending.fix_class,
            pending.area,
            pending.diagnosis,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def select_case_file(
    conn: sqlite3.Connection, signature: str
) -> PendingCaseFile | None:
    """Return a signature's in-flight creation, or None when absent."""
    row = conn.execute(
        "SELECT * FROM tech_lead_pending_case_files WHERE signature = ?",
        (signature,),
    ).fetchone()
    return case_file_from_row(row) if row is not None else None


def delete_case_file(tx: sqlite3.Connection, signature: str) -> None:
    """Remove an in-flight creation row. No-op if absent."""
    tx.execute(
        "DELETE FROM tech_lead_pending_case_files WHERE signature = ?", (signature,)
    )


def insert_promotion(tx: sqlite3.Connection, pending: PendingPromotion) -> None:
    """Record an in-flight promotion filing (create-once; see above)."""
    row = tx.execute(
        "SELECT * FROM tech_lead_pending_promotions WHERE signature = ?",
        (pending.signature,),
    ).fetchone()
    if row is not None:
        if promotion_from_row(row) == pending:
            return
        raise TechLeadPendingIntentConflictError(
            f"a different in-flight promotion is already recorded for signature"
            f" {pending.signature!r}"
        )
    tx.execute(
        "INSERT INTO tech_lead_pending_promotions (signature,"
        " case_file_issue_number, target_repo, title, idempotency_marker, area,"
        " body_observations, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pending.signature,
            pending.case_file_issue_number,
            pending.target_repo,
            pending.title,
            pending.idempotency_marker,
            pending.area,
            pending.body_observations,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def select_promotion(
    conn: sqlite3.Connection, signature: str
) -> PendingPromotion | None:
    """Return a signature's in-flight filing, or None when absent."""
    row = conn.execute(
        "SELECT * FROM tech_lead_pending_promotions WHERE signature = ?",
        (signature,),
    ).fetchone()
    return promotion_from_row(row) if row is not None else None


def delete_promotion(tx: sqlite3.Connection, signature: str) -> None:
    """Remove an in-flight filing row. No-op if absent."""
    tx.execute(
        "DELETE FROM tech_lead_pending_promotions WHERE signature = ?", (signature,)
    )
