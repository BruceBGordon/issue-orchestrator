"""SQLite database registry for startup checks and backups."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config
from .repo_identity import state_dir


@dataclass(frozen=True)
class SQLiteDatabase:
    key: str
    label: str
    path_fn: Callable[[Config], Path]
    enabled_fn: Callable[[Config], bool]
    backup: bool = True
    enforce_pragmas: bool = True


def _state_db(config: Config, name: str) -> Path:
    return state_dir(config.repo_root) / name


def list_sqlite_databases(config: Config) -> list[SQLiteDatabase]:
    """Return the list of SQLite DBs used by the orchestrator."""
    return [
        SQLiteDatabase(
            key="session_registry",
            label="Session Registry",
            path_fn=lambda cfg: _state_db(cfg, "session_registry.sqlite"),
            enabled_fn=lambda cfg: True,
            backup=True,
            enforce_pragmas=True,
        ),
        SQLiteDatabase(
            key="goal_pilot",
            label="Goal Pilot",
            path_fn=lambda cfg: _state_db(cfg, "goal_pilot.sqlite"),
            enabled_fn=lambda cfg: True,
            backup=True,
            enforce_pragmas=True,
        ),
        SQLiteDatabase(
            key="e2e_results",
            label="E2E Results",
            path_fn=lambda cfg: cfg.repo_root / ".issue-orchestrator" / "e2e.db",
            enabled_fn=lambda cfg: cfg.e2e.enabled,
            backup=True,
            enforce_pragmas=True,
        ),
        SQLiteDatabase(
            key="provider_circuit",
            label="Provider Circuit",
            path_fn=lambda cfg: _state_db(cfg, "provider_circuit.sqlite"),
            enabled_fn=lambda cfg: True,
            backup=True,
            enforce_pragmas=True,
        ),
        SQLiteDatabase(
            # The only durable record of work that has LEFT its pending queue
            # (#6999 F10). Losing or corrupting it is direct queued-work loss,
            # so it gets the same startup integrity checks, pragma enforcement
            # and backups as every other authoritative store.
            key="pending_work_claims",
            label="Pending Work Claims",
            path_fn=lambda cfg: _state_db(cfg, "pending_work_claims.sqlite"),
            enabled_fn=lambda cfg: True,
            backup=True,
            enforce_pragmas=True,
        ),
        SQLiteDatabase(
            key="queue_cache",
            label="Queue Cache",
            path_fn=lambda cfg: _state_db(cfg, "queue_cache.sqlite"),
            enabled_fn=lambda cfg: True,
            backup=True,
            enforce_pragmas=True,
        ),
        SQLiteDatabase(
            key="label_store",
            label="Label Store",
            path_fn=lambda cfg: _state_db(cfg, "label_store.sqlite"),
            enabled_fn=lambda cfg: True,
            backup=True,
            enforce_pragmas=True,
        ),
        SQLiteDatabase(
            key="timeline",
            label="Timeline",
            path_fn=lambda cfg: _state_db(cfg, "timeline.sqlite"),
            enabled_fn=lambda cfg: True,
            backup=True,
            enforce_pragmas=True,
        ),
        # Orchestrator-owned tech_lead launch authority (ADR-0031 / #6769 F3).
        # Backed up and pragma-enforced like the other state stores: losing
        # it mid-run turns every in-flight tech_lead completion into a
        # missing-authority rejection.
        SQLiteDatabase(
            key="tech_lead_authority",
            label="Tech Lead Authority",
            path_fn=lambda cfg: _state_db(cfg, "tech_lead_authority.sqlite"),
            enabled_fn=lambda cfg: True,
            backup=True,
            enforce_pragmas=True,
        ),
        # Rebuildable GitHub open-issue fingerprint cache used by the tech-lead
        # create_issue dedup gate (#6881).
        SQLiteDatabase(
            key="open_issue_corpus",
            label="Open Issue Corpus",
            path_fn=lambda cfg: _state_db(cfg, "open_issue_corpus.sqlite"),
            enabled_fn=lambda cfg: cfg.tech_lead.dedup.enabled,
            backup=True,
            enforce_pragmas=True,
        ),
    ]
