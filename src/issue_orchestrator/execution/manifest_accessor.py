"""Semantic run artifact access for a specific run identity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any

from ..domain.review_artifacts import (
    REVIEW_DECISION_ARTIFACT,
    REVIEW_DECISION_FILENAME,
    REVIEW_REPORT_ARTIFACT,
    REVIEW_REPORT_FILENAME,
)
from ..domain.run_manifest import RunManifest
from ..domain.tech_lead_artifacts import (
    TECH_LEAD_DECISION_ARTIFACT,
    TECH_LEAD_DECISION_FILENAME,
    TECH_LEAD_REPORT_ARTIFACT,
    TECH_LEAD_REPORT_FILENAME,
)
from ..domain.tech_lead_run_artifacts import TECH_LEAD_DATA_DIRNAME
from .session_output_adapter import (
    RETRY_PROMPT_NAME,
    SESSION_PROMPT_NAME,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunIdentity:
    """Stable identity for a single run instance."""

    issue_number: int
    run_dir: Path
    run_id: str | None = None


@dataclass(frozen=True)
class ArtifactDescriptor:
    """Descriptor for an artifact stream."""

    artifact_type: str
    run_identity: RunIdentity
    content_type: str
    encoding: str
    source_backend: str
    source_ref: str
    length_bytes: int | None
    updated_at: str | None


@dataclass(frozen=True)
class ArtifactStream:
    """Resolved artifact stream + descriptor."""

    descriptor: ArtifactDescriptor
    path: Path


class ArtifactNotFoundError(FileNotFoundError):
    """Raised when a run-scoped artifact cannot be resolved."""


@dataclass(frozen=True)
class _ArtifactPolicy:
    """Where one artifact type may live inside a run, and how it reads.

    One table entry per type, so "is this path a real persisted artifact?" has a
    single answer per type instead of a branch chain that grew a new leg each
    time a surface needed a new artifact (#6858 F4).
    """

    # Run-relative directory the artifact must sit directly inside.
    parent_parts: tuple[str, ...]
    filename: str
    # What a refused path is told it is not. Per-type prose, so the diagnostic
    # names the artifact family the caller actually asked for.
    refusal: str
    # Review-exchange turn artifacts are written with a per-turn prefix, so the
    # canonical name is a SUFFIX there and an exact match everywhere else.
    prefixed: bool
    content_type: str
    require_json: bool

    def accepts_name(self, name: str) -> bool:
        if self.prefixed:
            return name.endswith(f".{self.filename}")
        return name == self.filename


_REVIEW_TURNS_PARTS = ("review-exchange", "turns")
_TECH_LEAD_DATA_PARTS = (TECH_LEAD_DATA_DIRNAME,)

_ARTIFACT_POLICIES: dict[str, _ArtifactPolicy] = {
    REVIEW_REPORT_ARTIFACT: _ArtifactPolicy(
        parent_parts=_REVIEW_TURNS_PARTS,
        filename=REVIEW_REPORT_FILENAME,
        refusal="not a persisted review turn artifact",
        prefixed=True,
        content_type="text/markdown",
        require_json=False,
    ),
    REVIEW_DECISION_ARTIFACT: _ArtifactPolicy(
        parent_parts=_REVIEW_TURNS_PARTS,
        filename=REVIEW_DECISION_FILENAME,
        refusal="not a persisted review turn artifact",
        prefixed=True,
        content_type="application/json",
        require_json=True,
    ),
    TECH_LEAD_REPORT_ARTIFACT: _ArtifactPolicy(
        parent_parts=_TECH_LEAD_DATA_PARTS,
        filename=TECH_LEAD_REPORT_FILENAME,
        refusal="not a persisted tech-lead artifact",
        prefixed=False,
        content_type="text/markdown",
        require_json=False,
    ),
    TECH_LEAD_DECISION_ARTIFACT: _ArtifactPolicy(
        parent_parts=_TECH_LEAD_DATA_PARTS,
        filename=TECH_LEAD_DECISION_FILENAME,
        refusal="not a persisted tech-lead artifact",
        prefixed=False,
        content_type="application/json",
        require_json=True,
    ),
}


def _artifact_policy(artifact_type: str) -> _ArtifactPolicy:
    policy = _ARTIFACT_POLICIES.get(artifact_type)
    if policy is None:
        raise ArtifactNotFoundError(f"unsupported review artifact type: {artifact_type}")
    return policy


def worktree_path_from_run_dir(run_dir: Path) -> Path | None:
    """Return the worktree owning a canonical session-run directory."""
    resolved_run_dir = run_dir.resolve()
    sessions_dir = resolved_run_dir.parent
    orchestrator_dir = sessions_dir.parent
    if sessions_dir.name != "sessions" or orchestrator_dir.name != ".issue-orchestrator":
        return None
    worktree_path = orchestrator_dir.parent
    if worktree_path == orchestrator_dir:
        return None
    return worktree_path


@dataclass(frozen=True)
class ManifestAccessor:
    """Semantic accessor for run artifacts."""

    run_identity: RunIdentity

    def get_terminal_recording(self, *, allow_empty: bool = False) -> ArtifactStream:
        """Return the canonical raw terminal recording for this run."""
        run_dir = self.run_identity.run_dir
        self._require_run_dir_exists(run_dir)
        path = run_dir / "terminal-recording.jsonl"
        if path.exists() and (allow_empty or path.stat().st_size > 0):
            return self._artifact_stream(
                "terminal_recording",
                path,
                content_type="application/x-ndjson",
            )
        if path.exists():
            raise ArtifactNotFoundError(f"terminal recording is empty: {path}")
        raise ArtifactNotFoundError(f"terminal recording not found in run-scoped path: {path}")

    def get_review_exchange_phase_terminal_recording(
        self,
        *,
        round_index: int,
        role: str,
        allow_empty: bool = False,
    ) -> ArtifactStream:
        """Return the raw terminal recording for one review-exchange phase.

        Resolution order (newest layout first):

        1. **Persistent runner slice (B2 onward).** The per-exchange
           manifest carries ``coder_recording`` / ``reviewer_recording``
           keys pointing at ``<run_dir>/<role>/terminal-recording.jsonl``.
           The continuous pair-scoped recordings live under the coder
           worktree and are exposed separately via ``*_recording_pair``.
           Chapter offsets in ``chapters.json`` tell the replay UI how
           to scrub each per-exchange slice.
        2. **B1 / pre-B2 persistent layout.**
           ``<run_dir>/<role>/terminal-recording.jsonl`` —
           per-exchange recording, no manifest indirection.
        3. **Legacy spawn-per-phase layout.**
           ``<run_dir>/review-exchange/round-NNN/<role>/terminal-recording.jsonl``
           for runs from before the persistent-session cutover.
        """
        run_dir = self.run_identity.run_dir
        self._require_run_dir_exists(run_dir)
        normalized_role = str(role).strip().lower()
        if round_index <= 0:
            raise ArtifactNotFoundError(f"invalid review exchange round: {round_index}")
        if normalized_role not in {"reviewer", "coder"}:
            raise ArtifactNotFoundError(f"invalid review exchange role: {role}")

        manifest_path = self._read_manifest_recording_path(normalized_role)
        persistent_path = run_dir / normalized_role / "terminal-recording.jsonl"
        legacy_path = (
            run_dir
            / "review-exchange"
            / f"round-{round_index:03d}"
            / normalized_role
            / "terminal-recording.jsonl"
        )

        for candidate in (manifest_path, persistent_path, legacy_path):
            if candidate is None:
                continue
            if candidate.exists() and (
                allow_empty or candidate.stat().st_size > 0
            ):
                return self._artifact_stream(
                    "terminal_recording",
                    candidate,
                    content_type="application/x-ndjson",
                )

        # All candidates either missing or empty; preserve the
        # informative-empty diagnostic the previous code emitted.
        for candidate in (manifest_path, persistent_path, legacy_path):
            if candidate is not None and candidate.exists():
                raise ArtifactNotFoundError(
                    f"review exchange recording is empty: {candidate}"
                )
        raise ArtifactNotFoundError(
            f"review exchange recording not found for "
            f"round={round_index} role={normalized_role}; "
            f"checked manifest={manifest_path} "
            f"persistent={persistent_path} legacy={legacy_path}"
        )

    def _read_manifest_recording_path(self, role: str) -> Path | None:
        """Resolve the role's pair-scoped recording from the manifest.

        Returns ``None`` if the manifest is absent, is unreadable, or
        does not carry a ``<role>_recording`` key (i.e. pre-B2 runs).
        """
        manifest_file = self.run_identity.run_dir / "manifest.json"
        if not manifest_file.exists():
            return None
        try:
            payload = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        key = f"{role}_recording"
        value = payload.get(key) if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value:
            return None
        return Path(value)

    def get_agent_log(self, *, allow_empty: bool = False) -> ArtifactStream:
        """Return the canonical run-scoped agent recording stream."""
        run_dir = self.run_identity.run_dir
        self._require_run_dir_exists(run_dir)
        terminal_path = run_dir / "terminal-recording.jsonl"
        if terminal_path.exists() and terminal_path.stat().st_size > 0:
            artifact = self._artifact_stream(
                "agent_log",
                terminal_path,
                content_type="application/x-ndjson",
            )
            return artifact
        if terminal_path.exists() and allow_empty:
            return self._artifact_stream(
                "agent_log",
                terminal_path,
                content_type="application/x-ndjson",
            )
        if terminal_path.exists():
            raise ArtifactNotFoundError(f"terminal recording is empty: {terminal_path}")
        raise ArtifactNotFoundError(f"agent log not found in run-scoped path: {run_dir}")

    def _require_run_dir_exists(self, run_dir: Path) -> None:
        if not run_dir.exists():
            raise ArtifactNotFoundError(f"run_dir does not exist: {run_dir}")

    def _recorded_claude_log_path(
        self,
        run_dir: Path,
        manifest: dict[str, Any],
    ) -> Path:
        """Return only the Claude log explicitly bound to this recorded run."""
        log_path = manifest.get("claude_log_path")
        if not isinstance(log_path, str) or not log_path:
            raise ArtifactNotFoundError("manifest missing claude_log_path")
        candidate = Path(log_path)
        return candidate if candidate.is_absolute() else run_dir / candidate

    def get_claude_log(self) -> ArtifactStream:
        """Return the Claude transcript explicitly recorded for this run."""
        manifest = self._load_manifest()
        path = self._recorded_claude_log_path(
            self.run_identity.run_dir,
            manifest.to_dict(),
        )
        if path.exists() and path.stat().st_size > 0:
            return self._artifact_stream("claude_log", path)
        raise ArtifactNotFoundError(f"claude log not found: {path}")

    def get_review_exchange_transcript(self, *, allow_empty: bool = False) -> ArtifactStream:
        """Return the dedicated review-exchange transcript for this run."""
        manifest = self._load_manifest()
        transcript_path = manifest.to_dict().get("review_exchange_transcript_path")
        if not transcript_path:
            raise ArtifactNotFoundError("manifest missing review_exchange_transcript_path")
        path = Path(str(transcript_path))
        if not path.is_absolute():
            path = self.run_identity.run_dir / path
        if not path.exists():
            raise ArtifactNotFoundError(f"review exchange transcript not found: {path}")
        if not allow_empty:
            self._require_non_empty(path, artifact_name="review exchange transcript")
        return self._artifact_stream("review_exchange_transcript", path)

    def get_review_artifact(
        self,
        *,
        artifact_path: str,
        artifact_type: str,
    ) -> ArtifactStream:
        """Return one agent-authored report/decision artifact scoped to this run.

        Serves the reviewer's pair and the tech lead's pair (#6858 F4). Each type
        declares WHERE it is allowed to live in ``_ARTIFACT_POLICIES``, so a
        caller-supplied path is checked against that one table rather than
        against a rule this method re-decides per type.
        """
        policy = _artifact_policy(artifact_type)
        run_dir = self.run_identity.run_dir.resolve()
        self._require_run_dir_exists(run_dir)
        resolved = self._contained_artifact_path(run_dir, artifact_path)
        expected_parent = run_dir.joinpath(*policy.parent_parts).resolve()
        if resolved.parent != expected_parent or not policy.accepts_name(resolved.name):
            raise ArtifactNotFoundError(
                f"artifact path is {policy.refusal}: {resolved}"
            )
        if not resolved.exists() or not resolved.is_file():
            raise ArtifactNotFoundError(f"{artifact_type} not found: {resolved}")
        self._require_non_empty(resolved, artifact_name=artifact_type)
        if policy.require_json:
            self._require_valid_json(resolved, artifact_name=artifact_type)
        return self._artifact_stream(
            artifact_type,
            resolved,
            content_type=policy.content_type,
        )

    def _contained_artifact_path(self, run_dir: Path, artifact_path: str) -> Path:
        """Resolve ``artifact_path`` and refuse anything outside ``run_dir``."""
        candidate = Path(artifact_path)
        if not candidate.is_absolute():
            candidate = run_dir / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(run_dir)
        except (OSError, ValueError) as exc:
            raise ArtifactNotFoundError(
                f"run artifact path escapes run_dir: {candidate}"
            ) from exc
        return resolved

    def get_session_prompt(self) -> ArtifactStream:
        """Return the run-scoped launch prompt for this run.

        This is the prompt the session was actually launched with — the
        manifest's ``session_prompt_path`` when present, otherwise the run's
        own ``session-prompt.txt`` / ``retry-prompt.md`` / newest
        review-exchange round prompt, in that order. It is NOT the static
        agent template.

        Every candidate is resolved against ``run_dir`` and rejected when it
        escapes the run directory, so a malformed or stale manifest cannot
        point this reader at a file outside the selected run. The first
        contained, real, non-empty candidate wins; if none qualify an
        ``ArtifactNotFoundError`` is raised.
        """
        run_dir = self.run_identity.run_dir
        self._require_run_dir_exists(run_dir)
        resolved_run_dir = run_dir.resolve()
        for candidate in self._session_prompt_candidates(run_dir):
            if not self._is_within_run_dir(candidate, resolved_run_dir):
                continue
            if candidate.is_file() and candidate.stat().st_size > 0:
                return self._artifact_stream("session_prompt", candidate)
        raise ArtifactNotFoundError(
            f"no run-scoped session prompt artifact found under {run_dir}"
        )

    def _session_prompt_candidates(self, run_dir: Path) -> list[Path]:
        """Ordered launch-prompt candidates for this run.

        Order: manifest ``session_prompt_path`` → ``session-prompt.txt`` →
        ``retry-prompt.md`` → newest review-exchange round prompt. This only
        assembles candidate paths; containment and existence are enforced by
        the caller so the fallback order is preserved even when an earlier
        candidate is rejected.
        """
        candidates: list[Path] = []
        manifest_candidate = self._manifest_session_prompt_candidate(run_dir)
        if manifest_candidate is not None:
            candidates.append(manifest_candidate)
        candidates.append(run_dir / SESSION_PROMPT_NAME)
        candidates.append(run_dir / RETRY_PROMPT_NAME)
        exchange_candidate = self._latest_review_exchange_prompt(run_dir)
        if exchange_candidate is not None:
            candidates.append(exchange_candidate)
        return candidates

    def _manifest_session_prompt_candidate(self, run_dir: Path) -> Path | None:
        """Return the manifest's ``session_prompt_path`` as a path, if set.

        Relative values resolve against ``run_dir``. Returns ``None`` when the
        manifest is absent/unreadable or carries no usable value; containment
        is enforced by the caller, not here.
        """
        manifest_file = run_dir / "manifest.json"
        if not manifest_file.exists():
            return None
        try:
            payload = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        value = payload.get("session_prompt_path") if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value:
            return None
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = run_dir / candidate
        return candidate

    def _latest_review_exchange_prompt(self, run_dir: Path) -> Path | None:
        """Return the newest review-exchange round prompt under run_dir, if any."""
        exchange_root = run_dir / "review-exchange"
        if not exchange_root.exists():
            return None
        candidates = sorted(
            list(exchange_root.glob("round-*/coder-prompt.txt"))
            + list(exchange_root.glob("round-*/reviewer-prompt.txt")),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def _is_within_run_dir(self, candidate: Path, resolved_run_dir: Path) -> bool:
        """Return ``True`` only when ``candidate`` stays inside ``run_dir``.

        Resolves symlinks and ``..`` traversal before comparing, so an
        absolute path outside the run, a ``../outside`` escape, or a symlink
        pointing out of the run are all rejected. ``resolved_run_dir`` must
        already be resolved.
        """
        try:
            candidate.resolve().relative_to(resolved_run_dir)
        except (OSError, ValueError):
            logger.warning(
                "session prompt candidate escapes run_dir; skipping: %s",
                candidate,
            )
            return False
        return True

    def get_completion_record(self) -> ArtifactStream:
        """Return the completion record stream for this run."""
        manifest = self._load_manifest()
        completion_path = manifest.completion_path
        if not completion_path:
            raise ArtifactNotFoundError("manifest missing completion_path")
        path = Path(completion_path)
        if not path.is_absolute():
            worktree = worktree_path_from_run_dir(self.run_identity.run_dir)
            if not worktree:
                raise ArtifactNotFoundError("failed to infer worktree for completion path")
            path = worktree / path
        if not path.exists():
            raise ArtifactNotFoundError(f"completion record not found: {path}")
        self._require_non_empty(path, artifact_name="completion record")
        self._require_valid_json(path, artifact_name="completion record")
        return self._artifact_stream(
            "completion_record",
            path,
            content_type="application/json",
        )

    def get_validation_record(self) -> ArtifactStream:
        """Return the validation record stream for this run."""
        manifest = self._load_manifest()
        validation_path = manifest.validation_record_path
        if not validation_path:
            raise ArtifactNotFoundError("manifest missing validation_record_path")
        path = Path(validation_path)
        if not path.is_absolute():
            path = self.run_identity.run_dir / path
        if not path.exists():
            raise ArtifactNotFoundError(f"validation record not found: {path}")
        self._require_non_empty(path, artifact_name="validation record")
        self._require_valid_json(path, artifact_name="validation record")
        return self._artifact_stream(
            "validation_record",
            path,
            content_type="application/json",
        )

    def get_diagnostic(self) -> ArtifactStream:
        """Return the diagnostic file recorded by this exact run."""
        manifest = self._load_manifest()
        diagnostic_path = manifest.diagnostic_path
        if not diagnostic_path:
            raise ArtifactNotFoundError("manifest missing diagnostic_path")
        path = Path(diagnostic_path)
        if not path.is_absolute():
            path = self.run_identity.run_dir / path
        if not path.exists():
            raise ArtifactNotFoundError(f"diagnostic not found: {path}")
        self._require_non_empty(path, artifact_name="diagnostic")
        self._require_valid_json(path, artifact_name="diagnostic")
        return self._artifact_stream(
            "diagnostic",
            path,
            content_type="application/json",
        )

    def _load_manifest(self) -> RunManifest:
        return RunManifest.load(self.run_identity.run_dir)

    def _artifact_stream(
        self,
        artifact_type: str,
        path: Path,
        *,
        content_type: str = "text/plain",
        encoding: str = "utf-8",
    ) -> ArtifactStream:
        stat = path.stat()
        descriptor = ArtifactDescriptor(
            artifact_type=artifact_type,
            run_identity=self.run_identity,
            content_type=content_type,
            encoding=encoding,
            source_backend="fs",
            source_ref=str(path),
            length_bytes=stat.st_size,
            updated_at=datetime.fromtimestamp(
                stat.st_mtime,
                tz=timezone.utc,
            ).isoformat(),
        )
        return ArtifactStream(descriptor=descriptor, path=path)

    def _require_non_empty(self, path: Path, *, artifact_name: str) -> None:
        size = path.stat().st_size
        if size <= 0:
            raise ArtifactNotFoundError(f"{artifact_name} is empty: {path}")

    def _require_valid_json(self, path: Path, *, artifact_name: str) -> None:
        try:
            text = path.read_text(encoding="utf-8")
            json.loads(text)
        except UnicodeDecodeError as exc:
            raise ArtifactNotFoundError(f"{artifact_name} is not utf-8: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ArtifactNotFoundError(f"{artifact_name} is invalid JSON: {path}") from exc
