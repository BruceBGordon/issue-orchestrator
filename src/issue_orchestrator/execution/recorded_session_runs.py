"""Typed lookup owner for previously recorded session runs."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from ..domain.run_manifest import RunManifest
from ..domain.session_run import SessionRunAssets
from ..ports.session_output import SessionOutput
from .manifest_accessor import ManifestAccessor, RunIdentity

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExactRecordedRun:
    """Validated capability for one explicitly requested local run.

    Consumers receive the manifest, typed run assets, and semantic artifact
    accessor from this owner instead of rebuilding a ``RunIdentity`` from URL
    input. Construction is centralized in ``resolve_exact_recorded_run`` so
    issue ownership is checked exactly once.
    """

    issue_number: int
    manifest: RunManifest
    run_assets: SessionRunAssets
    artifacts: ManifestAccessor

    @property
    def run_dir(self) -> Path:
        return self.run_assets.run_dir

    @property
    def worktree_path(self) -> Path:
        return self.run_assets.worktree_path

    @property
    def session_name(self) -> str:
        return self.run_assets.session_name


@dataclass(frozen=True, slots=True)
class InvalidRecordedRunReference:
    """The caller supplied a malformed run-directory reference."""

    detail: str


@dataclass(frozen=True, slots=True)
class RecordedRunNotFound:
    """The exact requested run or its manifest no longer exists."""

    detail: str


@dataclass(frozen=True, slots=True)
class RecordedRunIssueMismatch:
    """The exact requested run belongs to another issue."""

    expected_issue_number: int
    actual_issue_number: int


@dataclass(frozen=True, slots=True)
class RecordedRunUnreadable:
    """The exact requested run exists but cannot be trusted or read."""

    detail: str


ExactRecordedRunResult: TypeAlias = (
    ExactRecordedRun
    | InvalidRecordedRunReference
    | RecordedRunNotFound
    | RecordedRunIssueMismatch
    | RecordedRunUnreadable
)


@dataclass(frozen=True, slots=True)
class _LoadedExactRun:
    """Validated manifest and typed ownership for one recorded run."""

    manifest: RunManifest
    assets: SessionRunAssets

_RunDirectoryResult: TypeAlias = (
    Path | InvalidRecordedRunReference | RecordedRunNotFound
)


def resolve_exact_recorded_run(
    raw_run_dir: str,
    *,
    issue_number: int,
) -> ExactRecordedRunResult:
    """Resolve only the named run; never substitute a current or latest run."""
    run_result = _resolve_run_directory(raw_run_dir)
    if not isinstance(run_result, Path):
        return run_result
    resolved_run_dir = run_result

    manifest_result = _load_exact_run(resolved_run_dir)
    if isinstance(manifest_result, RecordedRunNotFound | RecordedRunUnreadable):
        return manifest_result
    manifest = manifest_result.manifest
    if manifest.issue_number is None:
        return RecordedRunUnreadable(
            detail=f"Requested run manifest has no issue number: {resolved_run_dir}"
        )
    if manifest.issue_number != issue_number:
        return RecordedRunIssueMismatch(
            expected_issue_number=issue_number,
            actual_issue_number=manifest.issue_number,
        )
    assets = manifest_result.assets
    run_dir = assets.run_dir.resolve()
    return ExactRecordedRun(
        issue_number=issue_number,
        manifest=manifest,
        run_assets=assets,
        artifacts=ManifestAccessor(
            RunIdentity(issue_number=issue_number, run_dir=run_dir)
        ),
    )


def _resolve_run_directory(raw_run_dir: str) -> _RunDirectoryResult:
    if not raw_run_dir or raw_run_dir != raw_run_dir.strip():
        return InvalidRecordedRunReference(
            detail="run_dir must be a non-empty canonical path"
        )
    candidate = Path(raw_run_dir)
    if not candidate.is_absolute():
        return InvalidRecordedRunReference(detail="run_dir must be absolute")
    try:
        resolved_run_dir = candidate.resolve(strict=True)
    except FileNotFoundError:
        return RecordedRunNotFound(
            detail=f"Requested run directory not found: {candidate}"
        )
    except NotADirectoryError:
        return InvalidRecordedRunReference(
            detail=f"Requested run path has a non-directory parent: {candidate}"
        )
    if not resolved_run_dir.is_dir():
        return InvalidRecordedRunReference(
            detail=f"Requested run path is not a directory: {candidate}"
        )
    return resolved_run_dir


def _load_exact_run(
    resolved_run_dir: Path,
) -> _LoadedExactRun | RecordedRunNotFound | RecordedRunUnreadable:
    try:
        manifest_path = resolved_run_dir / "manifest.json"
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw_manifest, dict):
            raise ValueError("manifest root must be an object")
        manifest = RunManifest.load(resolved_run_dir)
        assets = SessionRunAssets.from_manifest_payload(
            run_dir=resolved_run_dir,
            manifest=raw_manifest,
        )
        return _LoadedExactRun(manifest=manifest, assets=assets)
    except FileNotFoundError:
        return RecordedRunNotFound(
            detail=f"Requested run has no manifest: {resolved_run_dir}"
        )
    except Exception as exc:
        return RecordedRunUnreadable(
            detail=f"Failed to read requested run manifest: {exc}"
        )


@dataclass(frozen=True, slots=True)
class RecordedDebugRunResumeTarget:
    """Manifest-backed target for resuming a blocked debug run."""

    run_assets: SessionRunAssets
    completion_path: str

    def __post_init__(self) -> None:
        rel = Path(self.completion_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("completion_path must be relative and contained")

    @property
    def run_dir(self) -> Path:
        return self.run_assets.run_dir

    def completion_file(self) -> Path:
        return self.run_assets.worktree_path / self.completion_path


@dataclass(frozen=True, slots=True)
class RecordedSessionRunLookup:
    """Owner API for exact recorded session-run asset lookup."""

    session_output: SessionOutput

    def assets_for_exact_session(
        self,
        worktree_path: Path,
        session_name: str,
    ) -> SessionRunAssets | None:
        run_dir = self.session_output.find_run_dir(
            worktree_path,
            session_name=session_name,
        )
        if run_dir is None:
            return None
        manifest = self.session_output.read_manifest(run_dir)
        if not isinstance(manifest, dict):
            return None
        try:
            return SessionRunAssets.from_manifest_payload(
                run_dir=run_dir,
                manifest=manifest,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Recorded session %s has invalid run assets at %s: %s",
                session_name,
                run_dir,
                exc,
            )
            return None

    def debug_resume_target(
        self,
        worktree_path: Path,
        issue_number: int,
    ) -> RecordedDebugRunResumeTarget | None:
        session_name = f"debug-{issue_number}"
        assets = self.assets_for_exact_session(
            worktree_path.resolve(),
            session_name,
        )
        if assets is None:
            return None
        if assets.session_name != session_name:
            return None
        if assets.worktree_path.resolve() != worktree_path.resolve():
            return None

        manifest = self.session_output.read_manifest(assets.run_dir)
        if not isinstance(manifest, dict):
            return None
        completion_path = _manifest_completion_path(manifest)
        if completion_path is None:
            return None
        try:
            return RecordedDebugRunResumeTarget(
                run_assets=assets,
                completion_path=completion_path,
            )
        except ValueError:
            return None


def _manifest_completion_path(manifest: Mapping[str, object]) -> str | None:
    raw_completion_path = manifest.get("completion_path")
    if not isinstance(raw_completion_path, str) or not raw_completion_path.strip():
        return None
    completion_path = raw_completion_path.strip()
    rel = Path(completion_path)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    return completion_path
