"""Issue-runtime owner resolution shared by reset/retry entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..control.review_exchange_lifecycle import (
    has_active_issue_runtime,
    terminate_issue_runtime,
)

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState


@dataclass(frozen=True)
class ResetRetryRuntimeOwners:
    """The exact runtime-owner set observed and terminated by reset/retry."""

    pair_registry: Any
    job_supervisor: Any
    session_manager: Any
    active_sessions: Any
    publish_recovery: Any
    review_exchange_canceller: Any


def reset_retry_runtime_owners(
    state: OrchestratorState,
    deps: Any,
) -> ResetRetryRuntimeOwners:
    """Resolve one owner snapshot for both freshness and teardown."""
    services = configured_attr(deps, "services")
    return ResetRetryRuntimeOwners(
        pair_registry=configured_attr(services, "pair_registry"),
        job_supervisor=configured_attr(services, "background_job_supervisor"),
        session_manager=configured_attr(deps, "session_manager"),
        active_sessions=state.active_sessions,
        publish_recovery=configured_attr(deps, "publish_recovery"),
        review_exchange_canceller=configured_owner_method(
            deps,
            "completion_processor",
            "cancel_review_exchange_for_issue",
        ),
    )


def has_active_reset_retry_runtime(
    *,
    issue_number: int,
    state: OrchestratorState,
    deps: Any,
) -> bool:
    """Return whether reset/retry would terminate live work for an issue."""
    owners = reset_retry_runtime_owners(state, deps)
    return has_active_issue_runtime(
        issue_number=issue_number,
        pair_registry=owners.pair_registry,
        job_supervisor=owners.job_supervisor,
        session_manager=owners.session_manager,
        active_sessions=owners.active_sessions,
        publish_recovery=owners.publish_recovery,
    )


def terminate_reset_retry_runtime(
    *,
    issue_number: int,
    state: OrchestratorState,
    deps: Any,
) -> None:
    """Apply the behavior-complete reset/retry runtime boundary."""
    owners = reset_retry_runtime_owners(state, deps)
    terminate_issue_runtime(
        issue_number=issue_number,
        reason="reset-retry",
        review_exchange_canceller=require_callable_dependency(
            owners.review_exchange_canceller,
            "review-exchange cancellation owner is not configured",
        ),
        session_manager=owners.session_manager,
        active_sessions=owners.active_sessions,
        publish_recovery=owners.publish_recovery,
    )


def configured_attr(obj: Any, name: str) -> Any | None:
    """Read explicit dataclass/test wiring without creating mock children."""
    if obj is None:
        return None
    try:
        values = vars(obj)
    except TypeError:
        return getattr(obj, name, None)
    return values.get(name)


def configured_owner_method(
    dependencies: Any,
    owner_name: str,
    method_name: str,
) -> Any | None:
    owner = configured_attr(dependencies, owner_name)
    return getattr(owner, method_name, None) if owner is not None else None


def require_callable_dependency(value: Any, error: str) -> Any:
    if not callable(value):
        raise RuntimeError(error)
    return value


__all__ = [
    "configured_attr",
    "has_active_reset_retry_runtime",
    "terminate_reset_retry_runtime",
]
