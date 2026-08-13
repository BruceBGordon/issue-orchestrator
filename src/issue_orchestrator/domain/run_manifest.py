"""Typed wrapper over the session run manifest.

The manifest is the session's single artifact of record — everything worth
knowing about a session is captured here.  It is written progressively:

1. **Launch** — identity + paths (session_name, run_id, log_path, …)
2. **Completion** — agent detail from CompletionRecord + runtime stats + log tail
3. **Validation** — validation_passed / validation_status / validation_reason
   (the three legacy flat fields are still the on-disk format; readers
   reconstruct a typed ``ValidationOutcome`` via ``validation_outcome``.)

Downstream consumers (SessionAnalyzer, failure diagnosis, UI) read this
one file instead of rummaging across completion.json, terminal-recording.jsonl,
validation-stderr.log, review-feedback/, etc.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from ..contracts.run_manifest import validate_run_manifest_payload
from .artifact_contracts import (
    ValidationOutcome,
    validation_outcome_from_manifest_fields,
)
from .timeline_evidence import manifest_is_retention_protected

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "manifest.json"


@dataclass
class RunManifest:
    """Typed view of a session's ``manifest.json``."""

    # ------------------------------------------------------------------
    # Identity (set at start_run)
    # ------------------------------------------------------------------
    session_name: str
    run_id: str
    run_dir: Path

    issue_number: int | None = None
    agent_label: str | None = None
    backend: str | None = None
    worktree: str | None = None
    started_at: str | None = None

    # ------------------------------------------------------------------
    # Timing (set at completion)
    # ------------------------------------------------------------------
    ended_at: str | None = None
    runtime_minutes: float | None = None
    timeout_minutes: int | None = None

    # ------------------------------------------------------------------
    # Timeline evidence retention (window finalized at terminal outcome)
    # ------------------------------------------------------------------
    retention_tier: str | None = None
    retention_days: int | None = None
    retention_expires_at: str | None = None
    retention_pinned: bool = False
    evidence_archived_at: str | None = None
    evidence_expired_at: str | None = None
    evidence_available: bool | None = None

    # ------------------------------------------------------------------
    # Outcome (set at completion)
    # ------------------------------------------------------------------
    outcome: str | None = None  # completed, blocked, timed_out, failed, needs_human

    # ------------------------------------------------------------------
    # Agent detail (from CompletionRecord — set at completion)
    # ------------------------------------------------------------------
    implementation: str | None = None
    problems: str | None = None
    attempted: str | None = None
    blocked_reason: str | None = None
    blocked_by: list[int] | None = None
    question: str | None = None
    review_summary: str | None = None
    review_issues: str | None = None
    risk_level: str | None = None
    follow_up_issues: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    # Validation outcome — three flat fields preserved as the on-disk
    # format. Readers should NOT use these directly; consume the
    # ``validation_outcome`` property below to get a typed
    # ``ValidationOutcome`` that enforces consistency. Writers must go
    # through ``SessionOutput.update_validation_outcome`` (which routes
    # through ``validation_outcome_to_manifest_fields``) so all three
    # fields are written atomically and a stale ``validation_reason``
    # cannot survive into a fresh ``passed`` outcome.
    validation_passed: bool | None = None
    validation_status: str | None = None  # passed, retry, failed
    validation_reason: str | None = None

    # ------------------------------------------------------------------
    # Log excerpt (set at completion)
    # ------------------------------------------------------------------
    log_tail: str | None = None  # Last ~20 lines

    # ------------------------------------------------------------------
    # Artifact paths (accumulated during lifecycle)
    # ------------------------------------------------------------------
    log_path: str | None = None
    session_prompt_path: str | None = None
    completion_path: str | None = None
    completion_record_path: str | None = None
    validation_record_path: str | None = None
    validation_stdout: str | None = None
    validation_stderr: str | None = None
    diagnostic_path: str | None = None
    claude_log_path: str | None = None
    claude_session_id: str | None = None
    orchestrator_tail: str | None = None
    run_audit_path: str | None = None
    artifacts: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Extra fields from the raw manifest that we don't model explicitly.
    # Preserved on load → save round-trips so we don't lose data.
    # ------------------------------------------------------------------
    _extra: dict[str, Any] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    # Class-level cache of known field names (for load performance)
    # ------------------------------------------------------------------
    _KNOWN_FIELDS: frozenset[str] = field(
        default=frozenset(),
        init=False,
        repr=False,
        compare=False,
    )

    @classmethod
    def _known_field_names(cls) -> frozenset[str]:
        """Lazily compute and cache the set of known dataclass field names."""
        if not cls._KNOWN_FIELDS:
            # Exclude _extra and _KNOWN_FIELDS themselves
            names = frozenset(
                f.name for f in fields(cls)
                if not f.name.startswith("_")
            )
            # Set on the class, not instance
            cls._KNOWN_FIELDS = names
        return cls._KNOWN_FIELDS

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, run_dir: Path) -> RunManifest:
        """Load a manifest from a run directory.

        Missing new fields default to ``None``. Unknown fields are
        preserved in ``_extra`` so save() round-trips don't lose data.

        Raises ``FileNotFoundError`` if the manifest doesn't exist.
        """
        manifest_path = run_dir / MANIFEST_FILENAME
        raw = json.loads(manifest_path.read_text())
        raw.setdefault("run_dir", str(run_dir))
        raw.setdefault("session_name", "")
        raw.setdefault("run_id", "")
        raw = validate_run_manifest_payload(raw)

        known = cls._known_field_names()
        known_kwargs: dict[str, Any] = {}
        extra: dict[str, Any] = {}

        for key, value in raw.items():
            if key in known:
                known_kwargs[key] = value
            else:
                extra[key] = value

        # Ensure required identity fields
        known_kwargs.setdefault("session_name", "")
        known_kwargs.setdefault("run_id", "")
        known_kwargs["run_dir"] = run_dir

        return cls(**known_kwargs, _extra=extra)

    @classmethod
    def has_retained_timeline_evidence(
        cls,
        run_dir: Path,
        *,
        now: datetime,
    ) -> bool:
        """Return whether count-based cleanup must preserve an exact run."""
        try:
            manifest = cls.load(run_dir)
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return False
        return manifest_is_retention_protected(
            expires_at=manifest.retention_expires_at,
            pinned=bool(manifest.retention_pinned),
            now=now,
        )

    def save(self) -> None:
        """Write the manifest to disk."""
        manifest_path = self.run_dir / MANIFEST_FILENAME
        payload = validate_run_manifest_payload(
            self.to_dict(),
            strict_required_artifacts=True,
        )
        manifest_path.write_text(json.dumps(payload, indent=2) + "\n")

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for JSON.

        Merges known fields with ``_extra``, omitting ``None`` values
        from known fields to keep the file clean.
        """
        result: dict[str, Any] = {}

        # Start with extra fields (lowest priority)
        result.update(self._extra)

        # Overlay known fields, skipping None and private
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            value = getattr(self, f.name)
            if value is None:
                continue
            if f.name == "run_dir":
                result[f.name] = str(value)
            else:
                result[f.name] = value

        return result

    def update(self, **kwargs: Any) -> None:
        """Update fields and save to disk.

        Only accepts known field names.  Raises ``TypeError`` for
        unknown fields to catch typos early.
        """
        known = self._known_field_names()
        for key, value in kwargs.items():
            if key not in known:
                raise TypeError(f"RunManifest has no field {key!r}")
            setattr(self, key, value)
        self.save()

    @property
    def validation_outcome(self) -> ValidationOutcome | None:
        """Typed view of the validation outcome.

        Reconstructs the discriminated union from the three legacy flat
        fields (``validation_passed`` / ``validation_status`` /
        ``validation_reason``) that remain the on-disk format. Returns
        ``None`` when no outcome has been recorded yet.

        On encountering an inconsistent triple from old manifests
        (e.g. ``status="passed"`` paired with a stale failure
        ``reason``), the typed status is the source of truth — the
        property surfaces ``ValidationPassed`` and the stale reason is
        dropped. New writes go through
        ``SessionOutput.update_validation_outcome`` so this
        inconsistency is unrepresentable going forward.
        """
        return validation_outcome_from_manifest_fields(
            validation_passed=self.validation_passed,
            validation_status=self.validation_status,
            validation_reason=self.validation_reason,
        )

    def artifact_paths(
        self,
        *,
        kind: str,
        key_prefix: str | None = None,
    ) -> tuple[str, ...]:
        """Return recorded artifact paths matching a kind and optional key prefix."""
        artifacts = self.artifacts if isinstance(self.artifacts, dict) else {}
        paths: list[str] = []
        for key, artifact in artifacts.items():
            if key_prefix is not None and not str(key).startswith(key_prefix):
                continue
            if not isinstance(artifact, dict):
                continue
            if artifact.get("kind") != kind:
                continue
            path = artifact.get("path")
            if isinstance(path, str) and path.strip():
                paths.append(path)
        return tuple(dict.fromkeys(paths))

    def junit_xml_paths(self, *, key_prefix: str | None = None) -> tuple[str, ...]:
        """Return recorded JUnit XML artifact paths."""
        return self.artifact_paths(kind="junit_xml", key_prefix=key_prefix)

    def enrich_from_completion_record(
        self,
        record: Any,  # CompletionRecord — avoid circular import
    ) -> None:
        """Copy relevant fields from a CompletionRecord into the manifest.

        This is the completion-time enrichment that makes the manifest
        the session's complete story.
        """
        _FIELDS = (
            "implementation",
            "problems",
            "attempted",
            "blocked_reason",
            "blocked_by",
            "question",
            "review_summary",
            "review_issues",
            "risk_level",
            "follow_up_issues",
        )
        for name in _FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                if name == "follow_up_issues":
                    setattr(
                        self,
                        name,
                        [issue.to_dict() for issue in value],
                    )
                    continue
                setattr(self, name, value)
