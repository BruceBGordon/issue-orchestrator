"""Dashboard tech-lead run command surface (#6994).

One authenticated command endpoint with a discriminated scope. The route does
exactly three things: parse the generated request model, hand ONE typed command
to the run-admission owner, and map its typed outcome onto the response. It
never inspects the pending queue, never mutates a state collection, and never
launches a session — those are the coordinator's and the planner's jobs, and
keeping them there is what stops a second copy of the admission policy growing
inside a web handler.

It also never shells out to ``orchestrator health-review``: that one-shot
command builds its OWN in-process orchestrator, takes the repository lock, and
pauses planning, so it cannot coexist with the engine this dashboard is serving.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..contracts.ui_openapi_models import (
    TechLeadIssueScopePayload,
    TechLeadRunAdmissionPayload,
    TechLeadRunRequestPayload,
)
from ..domain.tech_lead_run import (
    GlobalHealthReviewScope,
    IssueInvestigationScope,
    TechLeadRunAdmission,
    TechLeadRunOutcome,
    TechLeadRunRequest,
    TechLeadRunScope,
    TechLeadRunTrigger,
)
from .web_session_context import WebOrchestratorDependency

logger = logging.getLogger(__name__)

web_tech_lead_router = APIRouter()

# HTTP mapping for every typed outcome. Exhaustive by construction (a missing
# entry raises at response time rather than silently degrading to 200), so a new
# outcome cannot quietly start reporting success to the dashboard.
_OUTCOME_STATUS: dict[TechLeadRunOutcome, int] = {
    # Idempotent successes: the requested run exists. Repeated clicks and
    # interleaved automatic triggers land here and must NOT read as errors.
    TechLeadRunOutcome.QUEUED: 200,
    TechLeadRunOutcome.ALREADY_QUEUED: 200,
    TechLeadRunOutcome.ALREADY_RUNNING: 200,
    # Refusals the operator can act on — surfaced as durable warnings.
    TechLeadRunOutcome.PAUSED: 409,
    TechLeadRunOutcome.NOT_CONFIGURED: 409,
    TechLeadRunOutcome.NOT_ELIGIBLE: 409,
    TechLeadRunOutcome.CLAIM_CONFLICT: 409,
    # Nothing is running to admit the request.
    TechLeadRunOutcome.NOT_RUNNING: 503,
    # The request was admissible but the run could not be prepared.
    TechLeadRunOutcome.FAILED: 502,
}


def _domain_scope(payload: TechLeadRunRequestPayload) -> TechLeadRunScope:
    """Project the wire scope onto the control-layer scope value."""
    scope = payload.scope
    if isinstance(scope, TechLeadIssueScopePayload):
        return IssueInvestigationScope(scope.issue_number)
    return GlobalHealthReviewScope()


def admission_payload(admission: TechLeadRunAdmission) -> TechLeadRunAdmissionPayload:
    """Project a typed admission onto the generated response contract."""
    return TechLeadRunAdmissionPayload(
        outcome=admission.outcome.value,
        scope_kind=admission.scope_kind.value,
        run_key=admission.run_key,
        reason=admission.reason,
        detail=admission.detail,
        issue_number=admission.issue_number,
        behind_global_barrier=admission.behind_global_barrier,
        admitted=admission.outcome.admitted,
    )


@web_tech_lead_router.post(
    "/api/tech-lead/runs",
    response_model=TechLeadRunAdmissionPayload,
)
async def request_tech_lead_run(
    payload: TechLeadRunRequestPayload,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Request a scoped tech-lead run from the repository dashboard.

    Every answer — including "the engine is not running" — leaves here as the
    SAME typed :class:`TechLeadRunAdmissionPayload`. An ad hoc ``{"error": ...}``
    body for the 503 would be a shape the declared contract cannot describe and
    the dashboard cannot branch on, which is exactly how an untyped escape hatch
    reappears in a typed command surface (#6994 round 1 F5).
    """
    request = TechLeadRunRequest(
        scope=_domain_scope(payload),
        trigger=TechLeadRunTrigger.DASHBOARD,
    )
    admission = (
        TechLeadRunAdmission.engine_not_running(request.scope, request.trigger)
        if orchestrator is None
        else orchestrator.request_tech_lead_run(request)
    )
    logger.info(
        "[tech-lead] Dashboard run request: scope=%s run_key=%s outcome=%s reason=%s",
        admission.scope_kind.value,
        admission.run_key,
        admission.outcome.value,
        admission.reason,
    )
    body = admission_payload(admission)
    return JSONResponse(
        body.model_dump(mode="json"),
        status_code=_OUTCOME_STATUS[admission.outcome],
    )
