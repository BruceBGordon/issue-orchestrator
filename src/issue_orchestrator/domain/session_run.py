"""Typed ownership contracts for active session run artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .path_guards import require_absolute_path, require_path_under
from .session_watchdog import ScheduledSessionWatchdog


@dataclass(frozen=True, slots=True)
class SessionRunIdentity:
    """Stable identity for a single session run."""

    session_name: str
    run_id: str
    started_at: str

    def __post_init__(self) -> None:
        _require_non_empty(self.session_name, "session_name")
        _require_non_empty(self.run_id, "run_id")
        _require_non_empty(self.started_at, "started_at")


@dataclass(frozen=True, slots=True)
class RunContainedFile:
    """A required file path contained by an owned run directory."""

    run_dir: Path
    path: Path

    def __post_init__(self) -> None:
        _require_absolute_path(self.run_dir, "run_dir")
        _require_absolute_path(self.path, "path")
        _require_under_run_dir(self.path, self.run_dir, "path")


@dataclass(frozen=True, slots=True)
class ValidationArtifactPaths:
    """The validation artifacts active control writes for a run."""

    run_dir: Path
    record_path: Path
    stdout_path: Path
    stderr_path: Path

    def __post_init__(self) -> None:
        _require_absolute_path(self.run_dir, "run_dir")
        _require_contained_file(self.record_path, self.run_dir, "record_path")
        _require_contained_file(self.stdout_path, self.run_dir, "stdout_path")
        _require_contained_file(self.stderr_path, self.run_dir, "stderr_path")


@dataclass(frozen=True, slots=True)
class CompletionRecordArtifactPath:
    """Run-scoped copy of the completion record."""

    run_dir: Path
    path: Path

    def __post_init__(self) -> None:
        _require_absolute_path(self.run_dir, "run_dir")
        _require_contained_file(self.path, self.run_dir, "path")


@dataclass(frozen=True, slots=True)
class DiagnosticArtifactPath:
    """Run-scoped diagnostic artifact with an owned relative location."""

    run_dir: Path
    path: Path

    def __post_init__(self) -> None:
        _require_absolute_path(self.run_dir, "run_dir")
        _require_contained_file(self.path, self.run_dir, "path")


@dataclass(frozen=True, slots=True)
class SessionRunAssets:
    """Owned artifact contract for one active session run.

    Lower-level code should depend on the narrow leaf type it needs. This
    aggregate exists at owner/mid-level boundaries because it proves all standard
    artifact paths belong to the same run identity and root.
    """

    identity: SessionRunIdentity
    worktree_path: Path
    run_dir: Path
    manifest: RunContainedFile
    terminal_recording: RunContainedFile

    def __post_init__(self) -> None:
        _require_absolute_path(self.worktree_path, "worktree_path")
        _require_absolute_path(self.run_dir, "run_dir")
        _require_run_dir_under_worktree(self.run_dir, self.worktree_path)
        _require_run_dir_names_this_run(self.run_dir, self.identity)
        if self.manifest.run_dir != self.run_dir:
            raise ValueError("SessionRunAssets.manifest must belong to run_dir")
        if self.terminal_recording.run_dir != self.run_dir:
            raise ValueError("SessionRunAssets.terminal_recording must belong to run_dir")

    @classmethod
    def from_paths(
        cls,
        *,
        session_name: str,
        run_id: str,
        worktree_path: Path,
        run_dir: Path,
        terminal_recording_path: Path,
        manifest_path: Path,
        started_at: str,
    ) -> "SessionRunAssets":
        return cls(
            identity=SessionRunIdentity(
                session_name=session_name,
                run_id=run_id,
                started_at=started_at,
            ),
            worktree_path=worktree_path,
            run_dir=run_dir,
            manifest=RunContainedFile(run_dir=run_dir, path=manifest_path),
            terminal_recording=RunContainedFile(
                run_dir=run_dir,
                path=terminal_recording_path,
            ),
        )

    @classmethod
    def from_manifest_payload(
        cls,
        *,
        run_dir: Path,
        manifest: Mapping[str, object],
    ) -> "SessionRunAssets":
        session_name = _required_manifest_string(manifest, "session_name")
        run_id = _required_manifest_string(manifest, "run_id")
        started_at = _required_manifest_string(manifest, "started_at")
        worktree_path = Path(_required_manifest_string(manifest, "worktree"))
        manifest_run_dir = Path(_required_manifest_string(manifest, "run_dir"))
        log_path = Path(_required_manifest_string(manifest, "log_path"))
        if manifest_run_dir.resolve() != run_dir.resolve():
            raise ValueError(
                f"session run manifest run_dir mismatch: {manifest_run_dir} != {run_dir}"
            )
        return cls.from_paths(
            session_name=session_name,
            run_id=run_id,
            worktree_path=worktree_path,
            run_dir=run_dir,
            terminal_recording_path=log_path,
            manifest_path=run_dir / "manifest.json",
            started_at=started_at,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionRunAssets":
        return cls.from_paths(
            session_name=_required_manifest_string(data, "session_name"),
            run_id=_required_manifest_string(data, "run_id"),
            worktree_path=Path(_required_manifest_string(data, "worktree_path")),
            run_dir=Path(_required_manifest_string(data, "run_dir")),
            terminal_recording_path=Path(
                _required_manifest_string(data, "terminal_recording_path")
            ),
            manifest_path=Path(_required_manifest_string(data, "manifest_path")),
            started_at=_required_manifest_string(data, "started_at"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "session_name": self.session_name,
            "run_id": self.run_id,
            "worktree_path": str(self.worktree_path),
            "run_dir": str(self.run_dir),
            "terminal_recording_path": str(self.terminal_recording.path),
            "manifest_path": str(self.manifest.path),
            "started_at": self.started_at,
        }

    @property
    def session_name(self) -> str:
        return self.identity.session_name

    @property
    def run_id(self) -> str:
        return self.identity.run_id

    @property
    def started_at(self) -> str:
        return self.identity.started_at

    @property
    def log_path(self) -> Path:
        return self.terminal_recording.path

    @property
    def manifest_path(self) -> Path:
        return self.manifest.path

    @property
    def validation_artifacts(self) -> ValidationArtifactPaths:
        return ValidationArtifactPaths(
            run_dir=self.run_dir,
            record_path=self.run_dir / "validation-record.json",
            stdout_path=self.run_dir / "validation-stdout.log",
            stderr_path=self.run_dir / "validation-stderr.log",
        )

    @property
    def completion_record_copy(self) -> CompletionRecordArtifactPath:
        return CompletionRecordArtifactPath(
            run_dir=self.run_dir,
            path=self.run_dir / "completion-record.json",
        )

    def diagnostic_artifact(self, filename: str) -> DiagnosticArtifactPath:
        _require_non_empty(filename, "diagnostic filename")
        if "/" in filename or "\\" in filename:
            raise ValueError(
                f"diagnostic filename must not contain path separators: {filename}"
            )
        return DiagnosticArtifactPath(run_dir=self.run_dir, path=self.run_dir / filename)


@dataclass(frozen=True, slots=True)
class RestoredSessionRun:
    """Verified active-run assets and their exact persisted outer watchdog."""

    assets: SessionRunAssets
    watchdog: ScheduledSessionWatchdog

    def __post_init__(self) -> None:
        if type(self.assets) is not SessionRunAssets:
            raise ValueError("RestoredSessionRun.assets must be SessionRunAssets")
        if type(self.watchdog) is not ScheduledSessionWatchdog:
            raise ValueError(
                "RestoredSessionRun.watchdog must be ScheduledSessionWatchdog"
            )

    @classmethod
    def from_manifest_payload(
        cls,
        *,
        run_dir: Path,
        manifest: Mapping[str, object],
    ) -> "RestoredSessionRun":
        return cls(
            assets=SessionRunAssets.from_manifest_payload(
                run_dir=run_dir,
                manifest=manifest,
            ),
            watchdog=ScheduledSessionWatchdog.from_manifest_payload(manifest),
        )


def _required_manifest_string(manifest: Mapping[str, Any], key: str) -> str:
    raw = manifest.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"session run manifest missing required {key!r}")
    return raw


def _require_non_empty(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


# The worktree-relative namespace one session run's artifacts live in. Named once
# so the assets contract and the archive's trust anchor cannot drift into two
# spellings of the same relationship (#6858 round 6 F16).
SESSION_ARTIFACT_PARTS: tuple[str, ...] = (".issue-orchestrator", "sessions")

_RUN_DIR_NAME_SEPARATOR = "__"


def canonical_run_dir_name(run_id: str, session_name: str) -> str:
    """The ONE directory name a session run's artifacts live in.

    The allocator that creates the directory and every boundary that later has to
    decide "is this identity the identity of that directory?" call this, so the
    relationship has one spelling. Without it, a value object can carry a run's
    identity while pointing at a different run's directory — which is how one
    run's bytes get filed under another run's name (#6858 round 7 F17).
    """
    return f"{run_id}{_RUN_DIR_NAME_SEPARATOR}{session_name}"


def _require_absolute_path(value: object, field_name: str) -> None:
    require_absolute_path(value, field_name)


def _require_contained_file(path: Path, run_dir: Path, field_name: str) -> None:
    _require_absolute_path(path, field_name)
    _require_under_run_dir(path, run_dir, field_name)


def _require_under_run_dir(path: Path, run_dir: Path, field_name: str) -> None:
    require_path_under(path, run_dir, field_name)


def _require_run_dir_names_this_run(
    run_dir: Path, identity: "SessionRunIdentity"
) -> None:
    """The directory must be the one this identity names, not a sibling.

    The identity comes from a worktree manifest an agent can write, and downstream
    owners trust the two halves for DIFFERENT things — the archive names its
    durable destination from the identity while reading bytes from the directory.
    Letting those disagree files one run's evidence under another run's name and
    can replace that run's receipt, so they are bound here, once, where the pair
    is first assembled (#6858 round 7 F17/A6).
    """
    expected = canonical_run_dir_name(identity.run_id, identity.session_name)
    if run_dir.name != expected:
        raise ValueError(
            f"session run directory {run_dir.name!r} does not name run"
            f" {identity.run_id}/{identity.session_name} (expected {expected!r})"
        )


def _require_run_dir_under_worktree(run_dir: Path, worktree_path: Path) -> None:
    sessions_root = worktree_path.joinpath(*SESSION_ARTIFACT_PARTS)
    try:
        run_dir.resolve().relative_to(sessions_root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"run_dir must live under worktree session artifacts: {run_dir}"
        ) from exc
