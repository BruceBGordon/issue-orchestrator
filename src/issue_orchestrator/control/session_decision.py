"""Session outcome decision payloads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..domain.models import SessionStatus
from ..ports.provider_readiness import ProviderReadiness
from ..ports.provider_resilience import ProviderErrorType

if TYPE_CHECKING:
    from .completion_processor import ProcessingResult
    from ..infra.provider_resilience import ProviderStatus


@dataclass(frozen=True)
class ProviderTransientFailureDecision:
    """Provider-circuit failure effect to apply on the tick thread."""

    provider: str | None
    error_summary: str | None
    attempts: int | None


@dataclass(frozen=True)
class ProviderAuthFailureDecision:
    """Provider-circuit AUTH effect to apply on the tick thread (#6999).

    A separate type from the transient one on purpose: the circuit owner treats
    the two differently (its own threshold, its own long cooldown), and a caller
    must not be able to smuggle a credential outage into the transient ladder.
    """

    provider: str
    error_summary: str


@dataclass(frozen=True)
class ProviderAuthOutcome:
    """What an auth-dead session means, in one place (#6999).

    Owns the whole consequence of "this session's provider is not
    authenticated": the wording, the event payload, the circuit effect, and the
    resulting :class:`SessionDecision`. The controller reads none of that back
    out — it logs, emits, and returns.
    """

    provider: str
    detail: str

    @classmethod
    def from_readiness(
        cls, readiness: ProviderReadiness | None
    ) -> "ProviderAuthOutcome":
        """Build from a :class:`ProviderReadiness`, tolerating an absent one.

        The observation that carries this always sets ``provider_readiness``;
        the ``None`` branch keeps a malformed observation reportable rather than
        turning a diagnosable problem into an AttributeError.
        """
        return cls(
            provider=readiness.provider if readiness else "",
            detail=(
                readiness.detail
                if readiness and readiness.detail
                else "provider is not authenticated"
            ),
        )

    def event_payload(self, issue_number: int, session_name: str) -> dict[str, Any]:
        return {
            "issue_number": issue_number,
            "session_name": session_name,
            "provider": self.provider,
            "detail": self.detail,
        }

    def as_decision(self, *, blocked_label: str | None) -> "SessionDecision":
        """Blocked, never failed or timed out.

        The work is untouched and becomes launchable again the moment a human
        re-authenticates. The typed AUTH verdict rides along so the circuit
        owner can act on it and the reaction model can decline to mint a
        substance investigation for a credential problem.
        """
        return SessionDecision(
            status=SessionStatus.BLOCKED,
            reason=f"Provider not authenticated: {self.detail}",
            blocked_label=blocked_label,
            blocked_reason=self.detail,
            provider_error_type=ProviderErrorType.AUTH,
            provider_auth_failure=(
                ProviderAuthFailureDecision(
                    provider=self.provider, error_summary=self.detail
                )
                if self.provider
                else None
            ),
        )


def provider_success_from_status(status: "ProviderStatus | None") -> str | None:
    if status and status.succeeded:
        return status.provider
    return None


def provider_failure_from_status(
    status: "ProviderStatus",
) -> ProviderTransientFailureDecision:
    return ProviderTransientFailureDecision(
        provider=status.provider,
        error_summary=status.last_error_summary,
        attempts=status.attempts,
    )


@dataclass
class SessionDecision:
    """Decision about a session's outcome."""

    status: SessionStatus
    processing_result: "ProcessingResult | None" = None
    completion_processed: bool = False
    recovered_from_timeout: bool = False
    reason: str = ""
    validation_passed: bool | None = None
    validation_error: str | None = None
    validation_error_file: Path | None = None
    blocked_label: str | None = None
    blocked_reason: str | None = None
    completion_detail: dict[str, Any] | None = None
    diagnostic_path: str | None = None
    provider_success: str | None = None
    provider_transient_failure: ProviderTransientFailureDecision | None = None
    provider_auth_failure: ProviderAuthFailureDecision | None = None
    # The typed provider verdict this session ended on, when there was one.
    # Downstream owners (notably the tech-lead reaction model) branch on this
    # rather than re-reading labels or log text: an AUTH outcome says nothing
    # about the issue's substance, so it must not mint an investigation.
    provider_error_type: ProviderErrorType | None = None
