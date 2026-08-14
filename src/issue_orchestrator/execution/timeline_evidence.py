"""Filesystem owner for durable, run-scoped Timeline evidence."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from ..domain.run_manifest import MANIFEST_FILENAME, RunManifest
from ..domain.timeline_evidence import (
    FinalizeTimelineEvidenceCommand,
    SetTimelineEvidencePinCommand,
    TimelineEvidenceIdentity,
    TimelineEvidenceState,
    TimelineEvidenceStatus,
    parse_retention_timestamp,
    retention_has_expired,
)
from ..ports.timeline_store import TimelineStore

logger = logging.getLogger(__name__)

_PRUNE_MARKER = ".last-pruned"
_CLAUDE_SESSION_LOG_NAME = "claude-session.jsonl"
_CLAUDE_SESSION_PATH_NAME = "claude-session.path"
_CLAUDE_LOG_DIR_PATH_NAME = "claude-log.path"


class FileSystemTimelineEvidence:
    """Archive run assets outside worktrees and enforce their retention policy."""

    def __init__(
        self,
        *,
        archive_root: Path,
        timeline_store: TimelineStore,
        retention_days: int = 7,
        retention_tier: str = "hot",
        now: Callable[[], datetime] | None = None,
        wall_time: Callable[[], float] = time.time,
        prune_interval_seconds: int = 3600,
    ) -> None:
        self._archive_root = archive_root.resolve()
        self._timeline_store = timeline_store
        self._retention_days = max(0, retention_days)
        self._retention_tier = retention_tier
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._wall_time = wall_time
        self._prune_interval_seconds = prune_interval_seconds
        self._lock = threading.RLock()

    @property
    def archive_root(self) -> Path:
        return self._archive_root

    def describe(
        self, identity: TimelineEvidenceIdentity
    ) -> TimelineEvidenceState | None:
        with self._lock:
            return self._describe(identity)

    def _describe(
        self, identity: TimelineEvidenceIdentity
    ) -> TimelineEvidenceState | None:
        run_dir = identity.run_dir.resolve()
        archived = self._is_within(run_dir, self._archive_root)
        manifest_path = run_dir / MANIFEST_FILENAME
        if not manifest_path.is_file():
            if archived:
                return TimelineEvidenceState(
                    identity=TimelineEvidenceIdentity(identity.issue_number, run_dir),
                    status=TimelineEvidenceStatus.MISSING,
                    label="Evidence unavailable",
                    available=False,
                    pinned=False,
                    archived=True,
                    help_text="The retained files for this run are unavailable.",
                )
            return None

        manifest = RunManifest.load(run_dir)
        self._require_issue(identity.issue_number, manifest, run_dir)
        pinned = bool(manifest.retention_pinned)
        expiry = manifest.retention_expires_at
        expiry_time = parse_retention_timestamp(expiry)
        expired_without_pin = (
            expiry_time is not None
            and expiry_time <= self._now().astimezone(timezone.utc)
        )
        physically_available = manifest.evidence_available is not False

        if pinned:
            return TimelineEvidenceState(
                identity=TimelineEvidenceIdentity(identity.issue_number, run_dir),
                status=TimelineEvidenceStatus.PINNED,
                label="Pinned",
                available=physically_available,
                pinned=True,
                archived=archived,
                expires_at=expiry,
                help_text="Pinned evidence is retained until you unpin this run.",
                unpin_expires_immediately=expired_without_pin,
            )
        if expired_without_pin or not physically_available:
            return TimelineEvidenceState(
                identity=TimelineEvidenceIdentity(identity.issue_number, run_dir),
                status=TimelineEvidenceStatus.EXPIRED,
                label="Evidence expired",
                available=False,
                pinned=False,
                archived=archived,
                expires_at=expiry,
                help_text="This run passed its configured Timeline evidence retention window.",
            )
        if manifest.ended_at is None:
            return TimelineEvidenceState(
                identity=TimelineEvidenceIdentity(identity.issue_number, run_dir),
                status=TimelineEvidenceStatus.ACTIVE,
                label="Active run",
                available=True,
                pinned=False,
                archived=archived,
                expires_at=expiry,
                help_text="Evidence retention starts when this run becomes terminal.",
            )
        return TimelineEvidenceState(
            identity=TimelineEvidenceIdentity(identity.issue_number, run_dir),
            status=TimelineEvidenceStatus.RETAINED,
            label="Evidence retained",
            available=True,
            pinned=False,
            archived=archived,
            expires_at=expiry,
            help_text=(
                f"Evidence is retained until {expiry}."
                if expiry
                else "Evidence is retained for this run."
            ),
        )

    def set_pinned(
        self, command: SetTimelineEvidencePinCommand
    ) -> TimelineEvidenceState:
        with self._lock:
            return self._set_pinned(command)

    def _set_pinned(
        self, command: SetTimelineEvidencePinCommand
    ) -> TimelineEvidenceState:
        identity = command.identity
        run_dir = identity.run_dir.resolve()
        if not self._timeline_store.references_run(identity.issue_number, run_dir):
            raise ValueError("Timeline does not reference this exact issue/run pair")
        manifest = RunManifest.load(run_dir)
        self._require_issue(identity.issue_number, manifest, run_dir)
        if manifest.ended_at is None:
            raise ValueError(
                "Timeline evidence can be pinned only after a run is terminal"
            )
        if manifest.evidence_available is False:
            if command.pinned:
                raise FileNotFoundError("Timeline evidence for this run has expired")
            state = self._describe(identity)
            if state is None:
                raise RuntimeError("Expired Timeline evidence state is unavailable")
            return state
        if (
            command.pinned
            and not manifest.retention_pinned
            and retention_has_expired(
                expires_at=manifest.retention_expires_at,
                pinned=False,
                now=self._now(),
            )
        ):
            raise FileNotFoundError("Timeline evidence for this run has expired")

        manifest.retention_pinned = command.pinned
        manifest.save()
        if not command.pinned and self._is_archived_run(run_dir):
            state = self.describe(identity)
            if state is not None and state.status is TimelineEvidenceStatus.EXPIRED:
                self._expire_run(run_dir, manifest)

        state = self.describe(identity)
        if state is None:
            raise RuntimeError("Timeline evidence state disappeared after pin update")
        return state

    def finalize_terminal(
        self, command: FinalizeTimelineEvidenceCommand
    ) -> TimelineEvidenceState:
        """Finalize terminal retention once, preserving the first end time."""
        with self._lock:
            run_dir = command.identity.run_dir.resolve()
            manifest = RunManifest.load(run_dir)
            self._require_issue(
                command.identity.issue_number,
                manifest,
                run_dir,
            )
            if not command.outcome.strip():
                raise ValueError("Timeline evidence terminal outcome is required")

            was_terminal = manifest.ended_at is not None
            if manifest.outcome is None:
                manifest.outcome = command.outcome
            if manifest.ended_at is None:
                manifest.ended_at = command.ended_at or self._now().isoformat()
            if not was_terminal or manifest.retention_expires_at is None:
                ended_at = parse_retention_timestamp(manifest.ended_at)
                if ended_at is None:
                    raise ValueError("Timeline evidence terminal timestamp is required")
                manifest.retention_days = self._retention_days
                manifest.retention_tier = self._retention_tier
                manifest.retention_expires_at = (
                    ended_at + timedelta(days=self._retention_days)
                ).isoformat()
            if manifest.evidence_available is None:
                manifest.evidence_available = True
            manifest.save()

            state = self._describe(command.identity)
            if state is None:
                raise RuntimeError("Finalized Timeline evidence state is unavailable")
            return state

    def archive_worktree(self, issue_number: int, worktree_path: Path) -> int:
        """Copy exact run directories, then relocate their Timeline references."""
        with self._lock:
            return self._archive_worktree(issue_number, worktree_path)

    def _archive_worktree(self, issue_number: int, worktree_path: Path) -> int:
        sessions_root = worktree_path.resolve() / ".issue-orchestrator" / "sessions"
        if not sessions_root.exists():
            return 0
        if sessions_root.is_symlink() or not sessions_root.is_dir():
            raise RuntimeError(f"Unsafe session evidence directory: {sessions_root}")

        archived_count = 0
        for source in sorted(sessions_root.iterdir()):
            if source.is_symlink() or not source.is_dir():
                continue
            manifest_path = source / MANIFEST_FILENAME
            if not manifest_path.is_file():
                continue
            manifest = RunManifest.load(source)
            self._require_issue(issue_number, manifest, source)
            if not self._timeline_store.references_run(issue_number, source.resolve()):
                continue
            target = self._archive_target(issue_number, source.name)
            if not target.exists():
                self._copy_run_atomically(source, target, issue_number)
            else:
                self._validate_archived_run(target, issue_number)
            self._timeline_store.relocate_run(issue_number, source, target)
            archived_count += 1

            state = self.describe(TimelineEvidenceIdentity(issue_number, target))
            if state is not None and state.status is TimelineEvidenceStatus.EXPIRED:
                self._expire_run(target, RunManifest.load(target))
        return archived_count

    def prune_due(self) -> bool:
        with self._lock:
            return self._prune_due()

    def _prune_due(self) -> bool:
        marker = self._safe_prune_marker()
        try:
            elapsed = self._wall_time() - marker.stat().st_mtime
        except FileNotFoundError:
            return True
        return elapsed >= self._prune_interval_seconds

    def prune_expired(self) -> int:
        with self._lock:
            return self._prune_expired()

    def _prune_expired(self) -> int:
        self._archive_root.mkdir(parents=True, exist_ok=True)
        marker = self._safe_prune_marker()
        expired = 0
        for issue_dir in sorted(self._archive_root.iterdir()):
            if issue_dir.name == _PRUNE_MARKER:
                continue
            if issue_dir.is_symlink() or not issue_dir.is_dir():
                continue
            if not issue_dir.name.isdigit():
                continue
            issue_number = int(issue_dir.name)
            for run_dir in sorted(issue_dir.iterdir()):
                if run_dir.is_symlink() or not run_dir.is_dir():
                    continue
                manifest_path = run_dir / MANIFEST_FILENAME
                if not manifest_path.is_file():
                    continue
                manifest = RunManifest.load(run_dir)
                self._require_issue(issue_number, manifest, run_dir)
                if manifest.evidence_available is False:
                    continue
                if retention_has_expired(
                    expires_at=manifest.retention_expires_at,
                    pinned=bool(manifest.retention_pinned),
                    now=self._now(),
                ):
                    self._expire_run(run_dir, manifest)
                    expired += 1
        marker.touch()
        return expired

    def _safe_prune_marker(self) -> Path:
        marker = self._archive_root / _PRUNE_MARKER
        if marker.is_symlink():
            raise RuntimeError(f"Unsafe Timeline evidence prune marker: {marker}")
        return marker

    def _archive_target(self, issue_number: int, run_name: str) -> Path:
        if issue_number <= 0:
            raise ValueError("Timeline evidence requires a positive issue number")
        if not run_name or Path(run_name).name != run_name:
            raise ValueError("Unsafe Timeline evidence run directory name")
        target = self._archive_root / str(issue_number) / run_name
        if not self._is_within(target.resolve(), self._archive_root):
            raise ValueError("Timeline evidence archive target escaped its root")
        return target

    def _copy_run_atomically(
        self, source: Path, target: Path, issue_number: int
    ) -> None:
        self._reject_links(
            source,
            allowed_file_links=self._declared_claude_log_links(source),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.staging-{uuid4().hex}"
        try:
            shutil.copytree(source, staging)
            self._rewrite_archived_manifest(
                staging=staging,
                source=source,
                target=target,
                issue_number=issue_number,
            )
            os.replace(staging, target)
        except Exception:
            if staging.exists() and self._is_within(
                staging.resolve(), target.parent.resolve()
            ):
                shutil.rmtree(staging)
            raise

    def _rewrite_archived_manifest(
        self,
        *,
        staging: Path,
        source: Path,
        target: Path,
        issue_number: int,
    ) -> None:
        manifest_path = staging / MANIFEST_FILENAME
        payload = json.loads(manifest_path.read_text())
        old = str(source)
        new = str(target)

        def rewrite(value: object) -> object:
            if isinstance(value, str):
                if value == old:
                    return new
                if value.startswith(old + os.sep):
                    return new + value[len(old) :]
                return value
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            if isinstance(value, dict):
                return {key: rewrite(item) for key, item in value.items()}
            return value

        rewritten = rewrite(payload)
        if not isinstance(rewritten, dict):
            raise RuntimeError(f"Invalid Timeline evidence manifest: {source}")
        rewritten["run_dir"] = new
        self._rewrite_archived_claude_log(staging, target, rewritten)
        rewritten["evidence_archived_at"] = self._now().isoformat()
        rewritten["evidence_available"] = True
        rewritten_issue = rewritten.get("issue_number")
        if rewritten_issue != issue_number:
            raise ValueError(
                f"Run {source} belongs to issue {rewritten_issue}, not {issue_number}"
            )
        manifest_path.write_text(json.dumps(rewritten, indent=2) + "\n")
        self._validate_archived_run(staging, issue_number)

    @staticmethod
    def _rewrite_archived_claude_log(
        staging: Path,
        target: Path,
        manifest: dict[str, object],
    ) -> None:
        """Point a dereferenced Claude log artifact at its archived copy."""
        archived_claude_log = staging / _CLAUDE_SESSION_LOG_NAME
        if not archived_claude_log.is_file():
            return
        archived_claude_log_path = str(target / _CLAUDE_SESSION_LOG_NAME)
        manifest["claude_log_path"] = archived_claude_log_path
        manifest["claude_log_dir"] = None
        artifacts = manifest.get("artifacts")
        if isinstance(artifacts, dict):
            claude_artifact = artifacts.get("claude_log")
            if isinstance(claude_artifact, dict):
                claude_artifact["path"] = archived_claude_log_path
        claude_session_path = staging / _CLAUDE_SESSION_PATH_NAME
        if claude_session_path.is_file():
            claude_session_path.write_text(archived_claude_log_path)
        claude_log_dir_path = staging / _CLAUDE_LOG_DIR_PATH_NAME
        if claude_log_dir_path.is_file():
            claude_log_dir_path.write_text(str(target))

    def _expire_run(self, run_dir: Path, manifest: RunManifest) -> None:
        if not self._is_archived_run(run_dir):
            raise ValueError(f"Refusing to prune non-archived run: {run_dir}")
        self._reject_links(run_dir)
        for child in tuple(run_dir.iterdir()):
            if child.name == MANIFEST_FILENAME:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        manifest.evidence_available = False
        manifest.evidence_expired_at = self._now().isoformat()
        manifest.save()

    def _validate_archived_run(self, run_dir: Path, issue_number: int) -> None:
        if run_dir.is_symlink() or not run_dir.is_dir():
            raise RuntimeError(f"Unsafe archived Timeline run: {run_dir}")
        self._reject_links(run_dir)
        manifest = RunManifest.load(run_dir)
        self._require_issue(issue_number, manifest, run_dir)

    def _declared_claude_log_links(self, run_dir: Path) -> frozenset[Path]:
        candidate = run_dir / _CLAUDE_SESSION_LOG_NAME
        if not candidate.is_symlink():
            return frozenset()
        manifest = RunManifest.load(run_dir)
        if not manifest.claude_log_path:
            return frozenset()
        try:
            declared_target = Path(manifest.claude_log_path).resolve(strict=True)
            link_target = candidate.resolve(strict=True)
        except FileNotFoundError:
            return frozenset()
        if link_target != declared_target or not link_target.is_file():
            return frozenset()
        return frozenset({candidate})

    def _reject_links(
        self,
        root: Path,
        *,
        allowed_file_links: frozenset[Path] = frozenset(),
    ) -> None:
        if root.is_symlink():
            raise RuntimeError(f"Timeline evidence may not contain symlinks: {root}")
        for current_root, dir_names, file_names in os.walk(root, followlinks=False):
            current = Path(current_root)
            for name in (*dir_names, *file_names):
                candidate = current / name
                if candidate.is_symlink() and candidate not in allowed_file_links:
                    raise RuntimeError(
                        f"Timeline evidence may not contain symlinks: {candidate}"
                    )

    def _require_issue(
        self, issue_number: int, manifest: RunManifest, run_dir: Path
    ) -> None:
        if manifest.issue_number != issue_number:
            raise ValueError(
                f"Run {run_dir} belongs to issue {manifest.issue_number}, "
                f"not {issue_number}"
            )

    def _is_archived_run(self, run_dir: Path) -> bool:
        if not self._is_within(run_dir.resolve(), self._archive_root):
            return False
        relative = run_dir.resolve().relative_to(self._archive_root)
        return len(relative.parts) == 2 and relative.parts[0].isdigit()

    @staticmethod
    def _is_within(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
        except ValueError:
            return False
        return True
