"""Session outcome decision payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..domain.models import SessionStatus
from ..ports.provider_readiness import ProviderReadiness
from ..ports.provider_resilience import ProviderErrorType

if TYPE_CHECKING:
    from .completion_processor import ProcessingResult
    from ..infra.provider_resilience import ProviderStatus


@dataclass(frozen=True)
class ProviderSuccessDecision:
    """Provider-circuit recovery evidence to apply on the tick thread.

    Completion effects can drain in a different order from provider attempts,
    so the observation time is part of the fact rather than application
    metadata. The circuit owner uses it to reject stale recovery evidence.
    """

    provider: str
    observed_at: datetime


@dataclass(frozen=True)
class ProviderTransientFailureDecision:
    """Provider-circuit failure effect to apply on the tick thread."""

    provider: str | None
    error_summary: str | None
    attempts: int | None
    observed_at: datetime


@dataclass(frozen=True)
class ProviderAuthFailureDecision:
    """Provider-circuit AUTH effect to apply on the tick thread (#6999).

    A separate type from the transient one on purpose: the circuit owner treats
    the two differently (its own threshold, its own long cooldown), and a caller
    must not be able to smuggle a credential outage into the transient ladder.

    ``sample_id`` is the identity of the credential probe result this verdict
    was confirmed against, carried so the circuit owner counts one physical
    observation once even when a launch check already saw the same sample
    (#6999 F2).
    """

    provider: str
    error_summary: str
    sample_id: str = ""


@dataclass(frozen=True)
class ProviderQuotaFailureDecision:
    """Provider-circuit QUOTA effect to apply on the tick thread (#7096).

    Its own type for the same reason AUTH has one: exhaustion has its own
    cooldown dimension, and a caller must not be able to smuggle it into the
    transient ladder, whose premise — that waiting helps — is false here.

    Unlike AUTH there is no ``sample_id``. No launch-time probe can observe an
    exhausted balance: ``claude auth status`` and ``codex login status`` both
    report on the credential, which is valid. Exhaustion is only ever learned
    from what a session printed, so every verdict is its own observation and
    there is no shared sample to de-duplicate against.
    """

    provider: str
    error_summary: str
    observed_at: datetime


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
    sample_id: str = ""

    @classmethod
    def from_readiness(
        cls, readiness: ProviderReadiness | None
    ) -> "ProviderAuthOutcome":
        """Build from the observation's typed readiness, or fail loudly.

        A missing, unnamed or non-auth readiness cannot produce a usable
        outcome: the
        circuit owner would get no provider to record against and the
        provider-impact command would have nothing to assess, so the session
        would end BLOCKED with the outage invisible. Manufacturing an empty
        provider to keep going is exactly the silent degradation this codebase
        forbids — the malformed observation is the bug, and it should surface
        as one (#6999 F9).
        """
        if readiness is None or not readiness.provider or not readiness.human_fixable:
            raise ValueError(
                "a PROVIDER_AUTH_FAILED observation must carry a named, "
                f"auth-expired ProviderReadiness; got {readiness!r}"
            )
        return cls(
            provider=readiness.provider,
            detail=readiness.detail or "provider is not authenticated",
            sample_id=readiness.sample_id,
        )

    def event_payload(self, issue_number: int, session_name: str) -> dict[str, Any]:
        return {
            "issue_number": issue_number,
            "session_name": session_name,
            "provider": self.provider,
            "detail": self.detail,
        }

    def as_decision(self) -> "SessionDecision":
        """Blocked, never failed or timed out.

        The work is untouched and becomes launchable again the moment a human
        re-authenticates. The typed AUTH verdict rides along so the circuit
        owner can act on it and the reaction model can decline to mint a
        substance investigation for a credential problem.

        No ``blocked_label`` is set. The provider-blocked label and its durable
        issue-scoped record are two halves of one transition owned by
        :class:`~.provider_impact.ApplyProviderImpactAction`; naming a label
        here would route it through generic blocked handling as a bare
        ``AddLabelAction`` and leave the history behind (#6999 F5). The typed
        ``provider_error_type`` is what tells completion planning to ask the
        provider-impact owner instead.
        """
        return SessionDecision(
            status=SessionStatus.BLOCKED,
            reason=f"Provider not authenticated: {self.detail}",
            blocked_reason=self.detail,
            provider_error_type=ProviderErrorType.AUTH,
            provider_auth_failure=ProviderAuthFailureDecision(
                provider=self.provider,
                error_summary=self.detail,
                sample_id=self.sample_id,
            ),
        )


def _provider_observed_at(status: "ProviderStatus") -> datetime:
    """Parse the provider attempt timestamp as an aware datetime or fail loudly."""
    try:
        observed_at = datetime.fromisoformat(
            status.last_attempt_at.replace("Z", "+00:00")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            "ProviderStatus.last_attempt_at must be a valid ISO timestamp; "
            f"got {status.last_attempt_at!r}"
        ) from exc
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError(
            "ProviderStatus.last_attempt_at must include a timezone; "
            f"got {status.last_attempt_at!r}"
        )
    return observed_at


def provider_success_from_status(
    status: "ProviderStatus | None",
) -> ProviderSuccessDecision | None:
    if status and status.succeeded:
        provider = status.provider
        if provider is None:
            return None
        return ProviderSuccessDecision(
            provider=provider,
            observed_at=_provider_observed_at(status),
        )
    return None


def provider_failure_from_status(
    status: "ProviderStatus",
) -> ProviderTransientFailureDecision:
    return ProviderTransientFailureDecision(
        provider=status.provider,
        error_summary=status.last_error_summary,
        attempts=status.attempts,
        observed_at=_provider_observed_at(status),
    )


def provider_quota_failure_from_status(
    status: "ProviderStatus",
) -> ProviderQuotaFailureDecision:
    """Build a usable QUOTA effect from provider output, or fail loudly.

    A quota verdict without provider identity cannot be recorded by the circuit
    owner or routed through provider-impact policy. Silently skipping it would
    manufacture the TIMED_OUT outcome this verdict exists to prevent.
    """
    if (
        status.error_type is not ProviderErrorType.QUOTA
        or status.succeeded
        or not status.provider
    ):
        raise ValueError(
            "a failed QUOTA ProviderStatus must carry a named provider; "
            f"got {status!r}"
        )
    return ProviderQuotaFailureDecision(
        provider=status.provider,
        error_summary=status.last_error_summary or "Provider quota exhausted",
        observed_at=_provider_observed_at(status),
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
    provider_success: ProviderSuccessDecision | None = None
    provider_transient_failure: ProviderTransientFailureDecision | None = None
    provider_auth_failure: ProviderAuthFailureDecision | None = None
    provider_quota_failure: ProviderQuotaFailureDecision | None = None
    # The typed provider verdict this session ended on, when there was one.
    # Downstream owners (notably the tech-lead reaction model) branch on this
    # rather than re-reading labels or log text: an AUTH outcome says nothing
    # about the issue's substance, so it must not mint an investigation.
    provider_error_type: ProviderErrorType | None = None
