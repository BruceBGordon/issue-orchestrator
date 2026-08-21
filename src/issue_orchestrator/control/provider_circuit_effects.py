"""Apply a session decision's provider-circuit effects, in one place.

Extracted from ``session_completion`` (#7096). Completion coordinates the
mechanics of a finished session; deciding which typed provider verdict reaches
which circuit method is its own concern, and it grew a third cause the moment
quota exhaustion became a first-class outage.

Keeping it here means the mapping from "what the decision carries" to "what the
circuit is told" is a single readable list, and adding a fourth cause is one
edit in one file rather than another branch inside the completion path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .provider_resilience import ProviderResilienceManager
    from .session_controller import SessionDecision


def record_provider_resilience_effects(
    decision: "SessionDecision",
    provider_resilience: "ProviderResilienceManager | None",
) -> None:
    """Apply provider-circuit effects on the tick thread.

    Each cause is independent and may co-occur: a decision can carry a success
    for one dimension while another stays open. The circuit owner decides what
    each verdict means for its own deadline — this function only routes.
    """
    if provider_resilience is None:
        return
    if decision.provider_success:
        provider_resilience.record_success(decision.provider_success)
    if decision.provider_transient_failure:
        failure = decision.provider_transient_failure
        provider_resilience.record_transient_failure(
            failure.provider,
            error_summary=failure.error_summary,
            attempts=failure.attempts,
        )
    if decision.provider_auth_failure:
        auth_failure = decision.provider_auth_failure
        provider_resilience.record_auth_failure(
            auth_failure.provider,
            error_summary=auth_failure.error_summary,
            # The credential sample this verdict was confirmed against. Sharing
            # it with the launch-side check means one physical observation is
            # counted once, not once per consumer (#6999 F2).
            sample_id=auth_failure.sample_id,
        )
    if decision.provider_quota_failure:
        quota_failure = decision.provider_quota_failure
        provider_resilience.record_quota_failure(
            quota_failure.provider,
            error_summary=quota_failure.error_summary,
        )
