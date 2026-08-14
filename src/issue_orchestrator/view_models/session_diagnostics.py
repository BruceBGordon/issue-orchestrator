"""Typed session-diagnostics payload and evidence presentation policy."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..domain.artifact_contracts import (
    ValidationOutcome,
    validation_outcome_from_manifest_fields,
)
from .timeline_evidence_view import TimelineEvidenceView, parse_timeline_evidence


@dataclass(frozen=True)
class EvidenceRow:
    label: str
    value: str
    value_kind: Literal["timestamp"] | None = None


@dataclass(frozen=True)
class SessionEvidencePresentation:
    """User-visible evidence state plus its artifact-access decisions."""

    rows: tuple[EvidenceRow, ...]
    artifacts_available: bool
    show_orchestrator_log: bool
    show_full_orchestrator_log: bool


@dataclass(frozen=True)
class SessionDiagnosticsContext:
    issue_number: int
    session_name: str
    started_at: str
    run_id: str
    backend: str
    agent_label: str
    claude_session_id: str
    worktree: str
    retention_tier: str
    retention_expires_at: str
    retention_pinned: str
    run_dir: str
    claude_log_path: str
    claude_log_dir: str
    orchestrator_log: str
    orchestrator_tail: str
    diagnostic_path: str
    validation_path: str
    validation_output_path: str
    validation_stderr_path: str
    run_audit_path: str
    validation_outcome: ValidationOutcome | None
    branch: str
    task: str
    claude_args: str
    claude_prompt_mode: str
    provider: str
    model: str
    permission_mode: str
    timeout_minutes: str
    extra_provider_args: str
    session_settings_path: str
    timeline_evidence: TimelineEvidenceView | None

    @classmethod
    def from_payload(
        cls,
        issue_number: int,
        manifest_payload: Mapping[str, object],
    ) -> SessionDiagnosticsContext:
        manifest = _as_mapping(manifest_payload.get("manifest"))
        session_identity = _as_mapping(manifest_payload.get("session_identity"))
        worktree = str(manifest.get("worktree") or "")
        session_name = str(
            manifest.get("session_name")
            or manifest_payload.get("session_name")
            or ""
        )
        diagnostic_path = _join_worktree_path(
            worktree, manifest.get("diagnostic_path")
        )
        validation_path = _join_worktree_path(
            worktree, manifest.get("validation_record_path")
        )
        validation_output_path = _join_worktree_path(
            worktree,
            manifest.get("validation_output_path")
            or manifest.get("validation_stdout"),
        )
        validation_stderr_path = _join_worktree_path(
            worktree,
            manifest.get("validation_stderr"),
        )
        run_audit_path = _join_worktree_path(
            worktree, manifest.get("run_audit_path")
        )
        run_dir = str(
            manifest.get("run_dir") or manifest_payload.get("run_dir") or ""
        )
        return cls(
            issue_number=issue_number,
            session_name=session_name,
            started_at=str(manifest.get("started_at") or ""),
            run_id=str(manifest.get("run_id") or ""),
            backend=str(manifest.get("backend") or ""),
            agent_label=str(manifest.get("agent_label") or ""),
            claude_session_id=str(manifest.get("claude_session_id") or ""),
            worktree=worktree,
            retention_tier=str(manifest.get("retention_tier") or ""),
            retention_expires_at=str(manifest.get("retention_expires_at") or ""),
            retention_pinned=str(
                manifest.get("retention_pinned")
                if "retention_pinned" in manifest
                else ""
            ),
            run_dir=run_dir,
            claude_log_path=str(manifest.get("claude_log_path") or ""),
            claude_log_dir=str(manifest.get("claude_log_dir") or ""),
            orchestrator_log=str(manifest.get("orchestrator_log") or ""),
            orchestrator_tail=str(manifest.get("orchestrator_tail") or ""),
            diagnostic_path=diagnostic_path,
            validation_path=validation_path,
            validation_output_path=validation_output_path,
            validation_stderr_path=validation_stderr_path,
            run_audit_path=run_audit_path,
            validation_outcome=validation_outcome_from_manifest_fields(
                validation_passed=_optional_bool(
                    manifest.get("validation_passed")
                ),
                validation_status=_optional_str(
                    manifest.get("validation_status")
                ),
                validation_reason=_optional_str(
                    manifest.get("validation_reason")
                ),
            ),
            branch=str(session_identity.get("branch") or ""),
            task=str(session_identity.get("task") or ""),
            claude_args=str(session_identity.get("claude_args") or ""),
            claude_prompt_mode=str(
                session_identity.get("claude_prompt_mode") or ""
            ),
            provider=str(session_identity.get("provider") or ""),
            model=str(session_identity.get("model") or ""),
            permission_mode=str(session_identity.get("permission_mode") or ""),
            timeout_minutes=str(session_identity.get("timeout_minutes") or ""),
            extra_provider_args=_format_extra_provider_args(
                session_identity.get("extra_provider_args")
            ),
            session_settings_path=(
                str(Path(run_dir) / "session-identity.json") if run_dir else ""
            ),
            timeline_evidence=parse_timeline_evidence(
                manifest_payload.get("timeline_evidence")
            ),
        )


@dataclass(frozen=True)
class SessionDiagnosticAnalysis:
    """Human-oriented diagnostic summary for the current run."""

    headline: str
    detail: str | None = None
    suggestions: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: object) -> SessionDiagnosticAnalysis | None:
        if not isinstance(payload, Mapping):
            return None
        headline = _nonempty_string(payload.get("headline"))
        if headline is None:
            return None
        detail = _nonempty_string(payload.get("detail"))
        suggestions = _nonempty_strings(payload.get("suggestions"))
        return cls(
            headline=headline,
            detail=detail,
            suggestions=suggestions,
        )

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {"headline": self.headline}
        _assign_if_present(payload, "detail", self.detail)
        _assign_if_present(payload, "suggestions", list(self.suggestions))
        return payload


@dataclass(frozen=True)
class SessionDiagnosticFollowUpIssue:
    title: str
    reason: str
    evidence: str | None = None
    suggested_labels: tuple[str, ...] = ()
    blocking: bool = False

    @classmethod
    def from_payload(
        cls, payload: object
    ) -> SessionDiagnosticFollowUpIssue | None:
        if not isinstance(payload, Mapping):
            return None
        title = _nonempty_string(payload.get("title"))
        reason = _nonempty_string(payload.get("reason"))
        if title is None:
            return None
        if reason is None:
            return None
        evidence = payload.get("evidence")
        suggested_labels = _nonempty_strings(payload.get("suggested_labels"))
        blocking = payload.get("blocking", False)
        return cls(
            title=title,
            reason=reason,
            evidence=evidence
            if isinstance(evidence, str) and evidence.strip()
            else None,
            suggested_labels=suggested_labels,
            blocking=blocking if isinstance(blocking, bool) else False,
        )

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "title": self.title,
            "reason": self.reason,
            "blocking": self.blocking,
        }
        _assign_if_present(payload, "evidence", self.evidence)
        _assign_if_present(
            payload, "suggested_labels", list(self.suggested_labels)
        )
        return payload


def present_session_evidence(
    ctx: SessionDiagnosticsContext,
) -> SessionEvidencePresentation:
    """Own Timeline-evidence labels and artifact-action visibility."""
    evidence = ctx.timeline_evidence
    if evidence is not None:
        evidence_retention = {
            "active": "Active — retention starts when the run ends",
            "retained": "Retained",
            "pinned": "Pinned — retained until unpinned",
            "expired": "Expired — artifacts are no longer available",
            "missing": "Unavailable — retained artifacts could not be found",
        }[evidence.status]
        primary_row = EvidenceRow("Timeline Evidence", evidence_retention)
    elif ctx.retention_pinned == "True":
        primary_row = EvidenceRow(
            "Timeline Evidence", "Pinned — retained until unpinned"
        )
    elif ctx.retention_expires_at:
        primary_row = EvidenceRow(
            "Timeline Evidence", ctx.retention_expires_at, "timestamp"
        )
    else:
        primary_row = EvidenceRow(
            "Timeline Evidence", "Starts when the run ends"
        )

    expiry_rows = (
        (EvidenceRow("Evidence Expires", evidence.expires_at, "timestamp"),)
        if evidence is not None
        and evidence.status == "retained"
        and evidence.expires_at
        else ()
    )
    archived = evidence is not None and evidence.archived
    artifacts_available = evidence is None or evidence.available
    return SessionEvidencePresentation(
        rows=(primary_row, *expiry_rows),
        artifacts_available=artifacts_available,
        show_orchestrator_log=(not archived or bool(ctx.orchestrator_tail)),
        show_full_orchestrator_log=not archived,
    )


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _nonempty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _nonempty_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if _nonempty_string(item) is not None)


def _assign_if_present(
    payload: MutableMapping[str, object], key: str, value: object
) -> None:
    if value:
        payload[key] = value


def _join_worktree_path(worktree: str, rel_path: object) -> str:
    rel_value = str(rel_path or "")
    if not rel_value:
        return ""
    rel_candidate = Path(rel_value)
    if rel_candidate.is_absolute():
        return str(rel_candidate)
    if not worktree:
        return ""
    return str(Path(worktree) / rel_candidate)


def _format_extra_provider_args(raw: object) -> str:
    if not isinstance(raw, Mapping) or not raw:
        return ""
    parts = [f"{key}={value}" for key, value in sorted(raw.items())]
    return ", ".join(parts)
