"""Startup recovery and audit policy for orchestrator-owned worktrees."""

from __future__ import annotations

import logging
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..ports.worktree_manager import RegisteredWorktree, WORKTREE_ID_MARKER

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from ..ports.worktree_manager import WorktreeManager
    from .cleanup_manager import CleanupManager

logger = logging.getLogger(__name__)

_REVIEW_ARTIFACTS = (
    Path(".issue-orchestrator/review-report.md"),
    Path(".issue-orchestrator/review-response.json"),
)
_REVIEW_TIMESTAMP = r"\d{8}T\d{12}Z"


@dataclass(frozen=True)
class WorktreeAuditEntry:
    """One registered worktree classified by the shared cleanup policy."""

    path: Path
    kind: str
    disposition: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "path": str(self.path),
            "name": self.path.name,
            "kind": self.kind,
            "disposition": self.disposition,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WorktreeRecoverySummary:
    """Counts produced by startup worktree reconciliation."""

    disposable_removed: int
    ordinary_removed: int
    retained: int


@dataclass(frozen=True)
class WorktreeActivityEvidence:
    """Authoritative active worktree paths, or an explicit unknown state."""

    active_paths: frozenset[Path] | None

    @classmethod
    def known(cls, paths: set[Path] | frozenset[Path]) -> WorktreeActivityEvidence:
        return cls(frozenset(path.resolve() for path in paths))

    @classmethod
    def unknown(cls) -> WorktreeActivityEvidence:
        return cls(None)

    @property
    def is_known(self) -> bool:
        return self.active_paths is not None


def _is_regular_owned_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _has_orchestrator_identity(path: Path) -> bool:
    marker = path / WORKTREE_ID_MARKER
    if not _is_regular_owned_file(marker):
        return False
    try:
        return marker.read_text(encoding="utf-8").strip().startswith("wt-")
    except OSError:
        return False


def _has_legacy_reviewer_evidence(path: Path) -> bool:
    return any(
        _is_regular_owned_file(path / artifact) for artifact in _REVIEW_ARTIFACTS
    )


@dataclass(frozen=True)
class _WorktreePatterns:
    ordinary: re.Pattern[str]
    scratch: re.Pattern[str]
    reviewer: re.Pattern[str]


def _worktree_patterns(repo_root: Path) -> _WorktreePatterns:
    repo_name = re.escape(repo_root.name)
    return _WorktreePatterns(
        ordinary=re.compile(rf"^{repo_name}-(\d+)$"),
        scratch=re.compile(rf"^{repo_name}-tech-lead-(\d+)-([0-9a-f]{{12}})$"),
        reviewer=re.compile(
            rf"^(?:{repo_name}-\d+|{repo_name}-tech-lead-\d+-[0-9a-f]{{12}})"
            rf"-review-{_REVIEW_TIMESTAMP}$"
        ),
    )


def _disposable_entry(
    path: Path,
    item: RegisteredWorktree,
    *,
    kind: str,
    candidate_reason: str,
    activity: WorktreeActivityEvidence,
) -> WorktreeAuditEntry:
    active_paths = activity.active_paths
    if active_paths is None:
        return WorktreeAuditEntry(
            path,
            kind,
            "retained",
            "Repository Engine activity could not be verified",
        )
    if path in active_paths:
        return WorktreeAuditEntry(
            path,
            kind,
            "retained",
            "active session restored at startup",
        )
    if item.locked:
        return WorktreeAuditEntry(path, kind, "retained", "git worktree is locked")
    return WorktreeAuditEntry(path, kind, "cleanup_candidate", candidate_reason)


def _classify_scratch(
    path: Path,
    item: RegisteredWorktree,
    match: re.Match[str],
    activity: WorktreeActivityEvidence,
) -> WorktreeAuditEntry:
    issue_number, token = match.groups()
    expected_branch = f"tech-lead-investigation-{issue_number}-{token}"
    if not _has_orchestrator_identity(path) or item.branch != expected_branch:
        return WorktreeAuditEntry(
            path,
            "external",
            "retained",
            "scratch-like name lacks matching orchestrator identity and branch",
        )
    return _disposable_entry(
        path,
        item,
        kind="tech_lead_scratch",
        candidate_reason="owned disposable scratch worktree is inactive",
        activity=activity,
    )


def _classify_reviewer(
    path: Path,
    item: RegisteredWorktree,
    activity: WorktreeActivityEvidence,
) -> WorktreeAuditEntry:
    owned = _has_orchestrator_identity(path) or _has_legacy_reviewer_evidence(path)
    if item.branch is not None or not owned:
        return WorktreeAuditEntry(
            path,
            "external",
            "retained",
            "reviewer-like name lacks detached HEAD and orchestrator ownership evidence",
        )
    parent_name = path.name.rsplit("-review-", 1)[0]
    if (
        activity.active_paths is not None
        and (path.parent / parent_name).resolve() in activity.active_paths
    ):
        return WorktreeAuditEntry(
            path,
            "reviewer",
            "retained",
            "review exchange parent session is active",
        )
    return _disposable_entry(
        path,
        item,
        kind="reviewer",
        candidate_reason="owned detached reviewer worktree is inactive",
        activity=activity,
    )


def _classify_registered_worktree(
    item: RegisteredWorktree,
    *,
    repo_root: Path,
    worktree_base: Path,
    patterns: _WorktreePatterns,
    activity: WorktreeActivityEvidence,
) -> WorktreeAuditEntry | None:
    path = item.path.resolve()
    if path == repo_root:
        return None
    if path.parent != worktree_base:
        return WorktreeAuditEntry(
            path,
            "external",
            "retained",
            "outside the configured worktree base",
        )
    if scratch_match := patterns.scratch.fullmatch(path.name):
        return _classify_scratch(path, item, scratch_match, activity)
    if patterns.reviewer.fullmatch(path.name):
        return _classify_reviewer(path, item, activity)
    if patterns.ordinary.fullmatch(path.name) and _has_orchestrator_identity(path):
        return WorktreeAuditEntry(
            path,
            "issue",
            "managed",
            "managed issue worktree; removal waits for the configured review gate",
        )
    return WorktreeAuditEntry(
        path,
        "external",
        "retained",
        "not an owned orchestrator lifecycle worktree",
    )


def audit_registered_worktrees(
    *,
    repo_root: Path,
    worktree_base: Path,
    registered: tuple[RegisteredWorktree, ...],
    activity: WorktreeActivityEvidence,
) -> tuple[WorktreeAuditEntry, ...]:
    """Classify registered worktrees using exact identity and ownership evidence."""
    repo_root = repo_root.resolve()
    worktree_base = worktree_base.resolve()
    patterns = _worktree_patterns(repo_root)
    classified = (
        _classify_registered_worktree(
            item,
            repo_root=repo_root,
            worktree_base=worktree_base,
            patterns=patterns,
            activity=activity,
        )
        for item in registered
    )
    return tuple(entry for entry in classified if entry is not None)


class WorktreeAuditOwner:
    """Behavior owner for registered-worktree inventory and safety policy."""

    def __init__(self, worktree_manager: WorktreeManager) -> None:
        self._worktree_manager = worktree_manager

    def audit(
        self,
        *,
        repo_root: Path,
        worktree_base: Path,
        activity: WorktreeActivityEvidence,
    ) -> tuple[WorktreeAuditEntry, ...]:
        registered = self._worktree_manager.list_registered(repo_root)
        return apply_disposable_removal_safety(
            audit_registered_worktrees(
                repo_root=repo_root,
                worktree_base=worktree_base,
                registered=registered,
                activity=activity,
            ),
            self._worktree_manager,
            registered,
        )


def apply_disposable_removal_safety(
    entries: tuple[WorktreeAuditEntry, ...],
    worktree_manager: WorktreeManager,
    registered: tuple[RegisteredWorktree, ...],
) -> tuple[WorktreeAuditEntry, ...]:
    """Retain reviewer candidates unless their identity and state are harmless."""
    registered_by_path = {item.path.resolve(): item for item in registered}
    checked: list[WorktreeAuditEntry] = []
    for entry in entries:
        if entry.kind != "reviewer" or entry.disposition != "cleanup_candidate":
            checked.append(entry)
            continue
        reviewer = registered_by_path[entry.path]
        parent_name = entry.path.name.rsplit("-review-", 1)[0]
        parent = registered_by_path.get((entry.path.parent / parent_name).resolve())
        owned_head = worktree_manager.read_reviewer_head_ownership(entry.path)
        if owned_head.marker_present:
            head_is_owned = owned_head.expected_head == reviewer.head
            retained_reason = "detached HEAD differs from its persisted reviewer tip"
        else:
            head_is_owned = (
                parent is not None
                and parent.branch is not None
                and reviewer.head == parent.head
            )
            retained_reason = "legacy reviewer HEAD is not the registered parent tip"
        if not head_is_owned:
            checked.append(
                replace(
                    entry,
                    disposition="retained",
                    reason=retained_reason,
                )
            )
            continue
        try:
            safe = worktree_manager.can_remove_without_user_changes(entry.path)
        except Exception as exc:
            logger.warning(
                "[CLEANUP] Retaining reviewer worktree; safety check failed: path=%s error=%s",
                entry.path,
                exc,
            )
            checked.append(
                replace(
                    entry,
                    disposition="retained",
                    reason="removal safety check failed",
                )
            )
            continue
        if not safe:
            checked.append(
                replace(
                    entry,
                    disposition="retained",
                    reason="local changes are not known orchestrator runtime artifacts",
                )
            )
            continue
        checked.append(entry)
    return tuple(checked)


class StartupWorktreeReconciler:
    """Single owner for crash recovery of ordinary and disposable worktrees."""

    def __init__(
        self,
        config: Config,
        cleanup_manager: CleanupManager,
        worktree_manager: WorktreeManager,
        audit_owner: WorktreeAuditOwner,
    ) -> None:
        self._config = config
        self._cleanup_manager = cleanup_manager
        self._worktree_manager = worktree_manager
        self._audit_owner = audit_owner

    def audit(self, state: OrchestratorState) -> tuple[WorktreeAuditEntry, ...]:
        activity = WorktreeActivityEvidence.known(
            {session.worktree_path for session in state.active_sessions}
        )
        return self._audit_owner.audit(
            repo_root=self._config.repo_root,
            worktree_base=self._config.worktree_base,
            activity=activity,
        )

    def recover(self, state: OrchestratorState) -> WorktreeRecoverySummary:
        """Remove proven disposable orphans, then run review-gated issue cleanup."""
        disposable_removed = 0
        retained = 0
        for entry in self.audit(state):
            if entry.disposition != "cleanup_candidate":
                retained += 1
                continue
            try:
                self._worktree_manager.remove_checkout_and_branch(
                    entry.path,
                    force=True,
                )
            except Exception as exc:
                logger.warning(
                    "[CLEANUP] Failed to remove disposable worktree: path=%s kind=%s error=%s",
                    entry.path,
                    entry.kind,
                    exc,
                )
                retained += 1
            else:
                disposable_removed += 1
                logger.info(
                    "[CLEANUP] Removed disposable orphan: path=%s kind=%s",
                    entry.path,
                    entry.kind,
                )

        ordinary_removed = self._cleanup_manager.recover_orphaned_cleanups(
            lambda message: setattr(state, "startup_message", message),
        )
        summary = WorktreeRecoverySummary(
            disposable_removed=disposable_removed,
            ordinary_removed=ordinary_removed,
            retained=retained,
        )
        logger.info(
            "[startup] Worktree reconciliation complete: disposable_removed=%d "
            "ordinary_removed=%d retained=%d",
            summary.disposable_removed,
            summary.ordinary_removed,
            summary.retained,
        )
        return summary
