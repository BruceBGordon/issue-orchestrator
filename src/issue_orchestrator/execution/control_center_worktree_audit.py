"""Control Center owner for selection- and activity-aware worktree audits."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from ..control.worktree_reconciliation import (
    WorktreeActivityEvidence,
    WorktreeAuditEntry,
    WorktreeAuditOwner,
)
from ..domain.repository_launch_selection import RepositoryLaunchSelection
from ..ports.repository_engine_supervisor import SupervisorOps
from .orchestrator_http_api import OrchestratorHttpApi


class WorktreeActivityReader(Protocol):
    """Observe whether Repository Engine worktree activity is known."""

    def read(
        self,
        repo_root: Path,
        selection: RepositoryLaunchSelection,
    ) -> WorktreeActivityEvidence: ...


class RepositoryEngineStatusReader(Protocol):
    """Read one Repository Engine status payload by local port."""

    def read_status(self, port: int) -> dict[str, object] | None: ...


class HttpRepositoryEngineStatusReader:
    """HTTP adapter for one Repository Engine's status endpoint."""

    def read_status(self, port: int) -> dict[str, object] | None:
        api = OrchestratorHttpApi(
            base_url_provider=lambda: f"http://127.0.0.1:{port}",
            timeout_seconds=2.0,
        )
        try:
            return api.status()
        except Exception:
            return None
        finally:
            api.close()


class RepositoryEngineWorktreeActivityReader:
    """Read active worktree paths from every live Repository Engine."""

    def __init__(
        self,
        supervisor: SupervisorOps,
        status_reader: RepositoryEngineStatusReader,
    ) -> None:
        self._supervisor = supervisor
        self._status_reader = status_reader

    def read(
        self,
        repo_root: Path,
        selection: RepositoryLaunchSelection,
    ) -> WorktreeActivityEvidence:
        from .control_center_runtime import (
            detect_repository_orchestrators,
            live_repository_engine_statuses,
        )

        supervised_engines = live_repository_engine_statuses(
            repo_root,
            self._supervisor,
            selection,
        )
        if supervised_engines:
            payloads: list[dict[str, object]] = []
            for engine in supervised_engines:
                if engine.port is None:
                    return WorktreeActivityEvidence.unknown()
                payload = self._status_reader.read_status(engine.port)
                if payload is None:
                    return WorktreeActivityEvidence.unknown()
                payloads.append(payload)
            return _activity_from_status_payloads(payloads)

        detected = detect_repository_orchestrators(repo_root)
        if not detected:
            return WorktreeActivityEvidence.known(set())
        payloads = []
        for engine in detected:
            payload = engine.get("status")
            if not isinstance(payload, dict):
                return WorktreeActivityEvidence.unknown()
            payloads.append(payload)
        return _activity_from_status_payloads(payloads)


def _activity_from_status_payloads(
    payloads: Iterable[Mapping[str, object]],
) -> WorktreeActivityEvidence:
    """Translate external engine JSON to typed, fail-closed activity evidence."""
    active_paths: set[Path] = set()
    for payload in payloads:
        if payload.get("startup_status") != "complete":
            return WorktreeActivityEvidence.unknown()
        sessions = payload.get("active_sessions")
        if not isinstance(sessions, list):
            return WorktreeActivityEvidence.unknown()
        for session in sessions:
            if not isinstance(session, dict):
                return WorktreeActivityEvidence.unknown()
            worktree = session.get("worktree_path")
            if not isinstance(worktree, str) or not worktree:
                return WorktreeActivityEvidence.unknown()
            active_paths.add(Path(worktree))
    return WorktreeActivityEvidence.known(active_paths)


@dataclass(frozen=True)
class ControlCenterWorktreeAuditReport:
    """Typed result consumed by the Control Center command adapter."""

    worktrees: tuple[WorktreeAuditEntry, ...]
    issue_cleanup_enabled: bool | None
    activity_evidence: Literal["known", "unknown"]
    audit_unavailable: bool = False
    scope: Literal["configured", "repo-parent-fallback"] = "configured"
    note: str | None = None

    def to_payload(self) -> dict[str, object]:
        entries = [entry.to_dict() for entry in self.worktrees]
        candidates = [
            entry for entry in entries
            if entry["disposition"] == "cleanup_candidate"
        ]
        if self.audit_unavailable:
            message = (
                "Worktree audit unavailable: selected path is not a Git repository."
            )
        elif self.activity_evidence == "unknown":
            message = (
                "Read-only audit complete. Disposable worktrees were retained "
                "because Repository Engine activity could not be verified."
            )
        else:
            message = (
                "Read-only audit complete. Startup removes safe disposable "
                "orphans; issue worktrees wait for the configured review gate."
            )
        return {
            "worktrees": entries,
            "cleanup_candidates": candidates,
            "stale_worktrees": candidates,
            "message": message,
            "issue_cleanup_enabled": self.issue_cleanup_enabled,
            "activity_evidence": self.activity_evidence,
            "audit_unavailable": self.audit_unavailable,
            "scope": self.scope,
            "note": self.note,
        }


class ControlCenterWorktreeAuditOwner:
    """Own config selection, live activity observation, and audit policy input."""

    def __init__(
        self,
        audit_owner: WorktreeAuditOwner,
        activity_reader: WorktreeActivityReader,
    ) -> None:
        self._audit_owner = audit_owner
        self._activity_reader = activity_reader

    def audit(
        self,
        repo_root: Path,
        selection: RepositoryLaunchSelection,
    ) -> ControlCenterWorktreeAuditReport:
        from .control_center_runtime import load_config_for_selection

        if not (repo_root / ".git").exists():
            return ControlCenterWorktreeAuditReport(
                worktrees=(),
                issue_cleanup_enabled=None,
                activity_evidence="unknown",
                audit_unavailable=True,
            )

        scope: Literal["configured", "repo-parent-fallback"] = "configured"
        note: str | None = None
        try:
            config = load_config_for_selection(repo_root, selection)
        except FileNotFoundError:
            config = None
            worktree_base = repo_root.parent
            scope = "repo-parent-fallback"
            note = "No selected config found; audited registered worktrees against the repo parent."
        else:
            worktree_base = config.worktree_base

        activity = self._activity_reader.read(repo_root, selection)
        entries = self._audit_owner.audit(
            repo_root=repo_root,
            worktree_base=worktree_base,
            activity=activity,
        )
        cleanup_enabled: bool | None = None
        if config is not None:
            cleanup = (
                config.cleanup.with_tech_lead
                if config.tech_lead_enabled
                else config.cleanup.without_tech_lead
            )
            cleanup_enabled = cleanup.remove_worktrees
        return ControlCenterWorktreeAuditReport(
            worktrees=entries,
            issue_cleanup_enabled=cleanup_enabled,
            activity_evidence="known" if activity.is_known else "unknown",
            scope=scope,
            note=note,
        )


__all__ = [
    "ControlCenterWorktreeAuditOwner",
    "ControlCenterWorktreeAuditReport",
    "HttpRepositoryEngineStatusReader",
    "RepositoryEngineWorktreeActivityReader",
    "RepositoryEngineStatusReader",
    "WorktreeActivityReader",
]
