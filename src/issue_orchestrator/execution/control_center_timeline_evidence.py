"""Repository-scoped Timeline evidence access for the Control Center."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator

from ..domain.timeline_evidence import (
    SetTimelineEvidencePinCommand,
    TimelineEvidenceIdentity,
    TimelineEvidenceState,
    TimelineEvidenceStatus,
)
from ..infra.e2e_worktree import get_e2e_worktree_path
from ..infra.repo_identity import state_dir
from .timeline_evidence import FileSystemTimelineEvidence
from .timeline_store import TimelineStoreConfig

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.repository_engine_supervisor import SupervisorOps
    from ..ports.timeline_store import TimelineRecord
    from .timeline_store import SqliteTimelineStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ControlCenterTimelineIssue:
    """One issue's records and retention owner inside a bounded store lease."""

    records: tuple[TimelineRecord, ...]
    evidence: FileSystemTimelineEvidence


class ControlCenterTimelineEvidenceAccess:
    """Own transient storage/config access for standalone Control Center routes."""

    def __init__(
        self,
        *,
        get_supervisor: Callable[[], SupervisorOps],
        load_config: Callable[[Path, SupervisorOps], Config] | None = None,
    ) -> None:
        self._get_supervisor = get_supervisor
        self._load_config = load_config or _load_effective_config

    @contextmanager
    def open_issue(
        self,
        repo_root: Path,
        issue_number: int,
    ) -> Iterator[ControlCenterTimelineIssue | None]:
        """Open the first base/E2E Timeline containing an issue, then close it."""
        config = self._config(repo_root)
        for database in _timeline_databases(repo_root):
            if not database.is_file():
                continue
            store: SqliteTimelineStore | None = None
            try:
                store = _open_store(database, config)
                records = tuple(store.read(issue_number, limit=5000))
            except Exception:
                if store is not None:
                    store.close()
                logger.debug(
                    "Could not read Control Center Timeline from %s",
                    database,
                    exc_info=True,
                )
                continue
            if not records:
                store.close()
                continue
            try:
                yield ControlCenterTimelineIssue(
                    records=records,
                    evidence=_evidence_owner(database, store, config),
                )
            finally:
                store.close()
            return
        yield None

    def describe(
        self,
        repo_root: Path,
        identity: TimelineEvidenceIdentity,
    ) -> TimelineEvidenceState:
        """Describe one exact referenced run, failing closed when it is unknown."""
        config = self._config(repo_root)
        for database in _timeline_databases(repo_root):
            if not database.is_file():
                continue
            with _open_store(database, config) as store:
                if not store.references_run(identity.issue_number, identity.run_dir):
                    continue
                state = _evidence_owner(database, store, config).describe(identity)
                if state is not None:
                    return state
        return TimelineEvidenceState(
            identity=identity,
            status=TimelineEvidenceStatus.MISSING,
            label="Evidence unavailable",
            available=False,
            pinned=False,
            archived=False,
            help_text="This repository Timeline does not reference the requested run.",
        )

    def set_pinned(
        self,
        repo_root: Path,
        command: SetTimelineEvidencePinCommand,
    ) -> TimelineEvidenceState:
        """Set an exact run's pin through the matching repository Timeline owner."""
        config = self._config(repo_root)
        identity = command.identity
        for database in _timeline_databases(repo_root):
            if not database.is_file():
                continue
            with _open_store(database, config) as store:
                if not store.references_run(identity.issue_number, identity.run_dir):
                    continue
                return _evidence_owner(database, store, config).set_pinned(command)
        raise ValueError("Timeline does not reference this exact issue/run pair")

    def _config(self, repo_root: Path) -> Config:
        return self._load_config(repo_root, self._get_supervisor())


def _load_effective_config(repo_root: Path, supervisor: SupervisorOps) -> Config:
    from .control_center_runtime import (
        get_effective_launch_selection,
        load_config_for_selection,
    )

    selection = get_effective_launch_selection(repo_root, supervisor)
    return load_config_for_selection(repo_root, selection)


def _timeline_databases(repo_root: Path) -> tuple[Path, Path]:
    return (
        state_dir(repo_root) / "timeline.sqlite",
        state_dir(get_e2e_worktree_path(repo_root)) / "timeline.sqlite",
    )


def _open_store(database: Path, config: Config) -> SqliteTimelineStore:
    from .timeline_store import SqliteTimelineStore

    return SqliteTimelineStore(
        database,
        TimelineStoreConfig(max_records=config.timeline.max_records),
    )


def _evidence_owner(
    database: Path,
    store: SqliteTimelineStore,
    config: Config,
) -> FileSystemTimelineEvidence:
    return FileSystemTimelineEvidence(
        archive_root=database.parent / "timeline-evidence",
        timeline_store=store,
        retention_days=config.session_output_retention_days,
        retention_tier=config.session_output_retention_tier,
    )


__all__ = [
    "ControlCenterTimelineEvidenceAccess",
    "ControlCenterTimelineIssue",
]
