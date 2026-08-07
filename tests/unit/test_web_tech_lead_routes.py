"""The dashboard tech-lead command surface stays thin and truthful (#6994).

Three properties are pinned here, because each one is a way the feature could
quietly go wrong:

1. **Contract**: every request and response validates through the generated UI
   OpenAPI models — no hand-rolled JSON on either side of the wire.
2. **Delegation**: the route hands ONE typed command to the run-admission owner
   and reports its verdict. It never touches the pending queue, never launches,
   and never shells out to the one-shot ``orchestrator health-review`` CLI
   (which would take the repository lock out from under the running engine).
3. **Truthfulness**: each typed outcome maps to an HTTP status the dashboard can
   act on — idempotent successes read as success, refusals read as refusals.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.contracts.ui_openapi_models import (
    TechLeadRunAdmissionPayload,
    TechLeadRunRequestPayload,
)
from issue_orchestrator.domain.tech_lead_run import (
    REASON_ADMITTED,
    REASON_DUPLICATE_REQUEST,
    REASON_NO_LONGER_BLOCKED,
    REASON_NO_TECH_LEAD_AGENT,
    REASON_ORCHESTRATOR_PAUSED,
    GlobalHealthReviewScope,
    IssueInvestigationScope,
    TechLeadRunAdmission,
    TechLeadRunOutcome,
    TechLeadRunScopeKind,
    TechLeadRunTrigger,
)
from issue_orchestrator.entrypoints.web import app, set_orchestrator

ENDPOINT = "/api/tech-lead/runs"


def _admission(
    outcome: TechLeadRunOutcome,
    *,
    reason: str = REASON_ADMITTED,
    scope_kind: TechLeadRunScopeKind = TechLeadRunScopeKind.ISSUE,
    run_key: str = "issue:42",
    issue_number: int | None = 42,
    behind_global_barrier: bool = False,
) -> TechLeadRunAdmission:
    return TechLeadRunAdmission(
        outcome=outcome,
        scope_kind=scope_kind,
        run_key=run_key,
        reason=reason,
        detail=f"detail for {outcome.value}",
        trigger=TechLeadRunTrigger.DASHBOARD,
        issue_number=issue_number,
        behind_global_barrier=behind_global_barrier,
    )


class RecordingOrchestrator:
    """Records the typed command the route hands over, and nothing else.

    Deliberately exposes NO queue, state, or launch surface: if the route ever
    reaches past ``request_tech_lead_run``, these tests fail with AttributeError
    rather than silently passing.
    """

    def __init__(self, admission: TechLeadRunAdmission) -> None:
        self._admission = admission
        self.requests: list[Any] = []

    def request_tech_lead_run(self, request: Any) -> TechLeadRunAdmission:
        self.requests.append(request)
        return self._admission


@pytest.fixture
def wired_orchestrator():
    """Install a recording orchestrator and restore the previous one after."""

    def _install(admission: TechLeadRunAdmission) -> RecordingOrchestrator:
        orchestrator = RecordingOrchestrator(admission)
        set_orchestrator(orchestrator)
        return orchestrator

    yield _install
    set_orchestrator(None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Delegation: one typed command, no state access
# ---------------------------------------------------------------------------


def test_issue_scope_is_delegated_as_one_typed_command(client, wired_orchestrator):
    orchestrator = wired_orchestrator(_admission(TechLeadRunOutcome.QUEUED))

    response = client.post(ENDPOINT, json={"scope": {"kind": "issue", "issue_number": 42}})

    assert response.status_code == 200
    assert len(orchestrator.requests) == 1
    request = orchestrator.requests[0]
    assert request.scope == IssueInvestigationScope(42)
    assert request.trigger is TechLeadRunTrigger.DASHBOARD
    # The route never invents failure context or a title — those belong to the
    # triggers that actually observed a failure.
    assert request.failure is None


def test_global_scope_is_delegated_as_one_typed_command(client, wired_orchestrator):
    orchestrator = wired_orchestrator(
        _admission(
            TechLeadRunOutcome.QUEUED,
            scope_kind=TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW,
            run_key="global:health_review",
            issue_number=900,
        )
    )

    response = client.post(ENDPOINT, json={"scope": {"kind": "global_health_review"}})

    assert response.status_code == 200
    assert orchestrator.requests[0].scope == GlobalHealthReviewScope()


def test_the_route_never_invokes_the_one_shot_health_review_cli(
    client, wired_orchestrator, monkeypatch
):
    """The one-shot CLI builds its own orchestrator and takes the repo lock.

    Running it from the web process would pause planning under the live engine,
    so any subprocess launch from this route is a defect, not a detail.
    """
    import subprocess

    def _forbidden(*args: Any, **kwargs: Any):
        raise AssertionError("the dashboard route must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    wired_orchestrator(
        _admission(
            TechLeadRunOutcome.QUEUED,
            scope_kind=TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW,
            run_key="global:health_review",
            issue_number=900,
        )
    )

    response = client.post(ENDPOINT, json={"scope": {"kind": "global_health_review"}})

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Contract: request and response both validate through the generated models
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope",
    [{"kind": "issue", "issue_number": 42}, {"kind": "global_health_review"}],
)
def test_request_bodies_validate_through_the_generated_model(scope):
    assert TechLeadRunRequestPayload.model_validate({"scope": scope})


def test_response_validates_through_the_generated_model(client, wired_orchestrator):
    wired_orchestrator(_admission(TechLeadRunOutcome.QUEUED, behind_global_barrier=True))

    response = client.post(ENDPOINT, json={"scope": {"kind": "issue", "issue_number": 42}})

    payload = TechLeadRunAdmissionPayload.model_validate(response.json())
    assert payload.outcome == "queued"
    assert payload.scope_kind == "issue"
    assert payload.run_key == "issue:42"
    assert payload.reason == REASON_ADMITTED
    assert payload.issue_number == 42
    assert payload.admitted is True
    assert payload.behind_global_barrier is True


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"scope": {"kind": "issue"}},
        {"scope": {"kind": "issue", "issue_number": 0}},
        {"scope": {"kind": "nonsense"}},
        {"scope": {"kind": "issue", "issue_number": 42}, "extra": True},
    ],
)
def test_malformed_requests_are_rejected_before_reaching_the_owner(
    client, wired_orchestrator, body
):
    orchestrator = wired_orchestrator(_admission(TechLeadRunOutcome.QUEUED))

    response = client.post(ENDPOINT, json=body)

    assert response.status_code == 422
    assert orchestrator.requests == []


# ---------------------------------------------------------------------------
# Truthfulness: outcome -> HTTP mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "reason", "status", "admitted"),
    [
        (TechLeadRunOutcome.QUEUED, REASON_ADMITTED, 200, True),
        (TechLeadRunOutcome.ALREADY_QUEUED, REASON_DUPLICATE_REQUEST, 200, False),
        (TechLeadRunOutcome.ALREADY_RUNNING, REASON_DUPLICATE_REQUEST, 200, False),
        (TechLeadRunOutcome.PAUSED, REASON_ORCHESTRATOR_PAUSED, 409, False),
        (TechLeadRunOutcome.NOT_CONFIGURED, REASON_NO_TECH_LEAD_AGENT, 409, False),
        (TechLeadRunOutcome.NOT_ELIGIBLE, REASON_NO_LONGER_BLOCKED, 409, False),
        (TechLeadRunOutcome.CLAIM_CONFLICT, "claimed_by_peer", 409, False),
        (TechLeadRunOutcome.FAILED, "anchor_unavailable", 502, False),
    ],
)
def test_every_typed_outcome_maps_to_an_actionable_status(
    client, wired_orchestrator, outcome, reason, status, admitted
):
    wired_orchestrator(_admission(outcome, reason=reason))

    response = client.post(ENDPOINT, json={"scope": {"kind": "issue", "issue_number": 42}})

    assert response.status_code == status
    payload = TechLeadRunAdmissionPayload.model_validate(response.json())
    assert payload.outcome == outcome.value
    assert payload.reason == reason
    assert payload.admitted is admitted
    # The typed rejection detail is always present, so the UI toast can explain
    # WHY rather than showing a bare status code.
    assert payload.detail


def test_every_outcome_has_an_http_mapping():
    """A new outcome must not silently degrade to a 200 "success"."""
    from issue_orchestrator.entrypoints.web_tech_lead_routes import _OUTCOME_STATUS

    assert set(_OUTCOME_STATUS) == set(TechLeadRunOutcome)


def test_a_stopped_engine_answers_with_the_declared_admission_contract(client):
    """503 is the SAME typed body every other outcome uses (#6994 round 1 F5).

    An ad hoc ``{"error": ...}`` shape is one the OpenAPI contract cannot
    describe and the dashboard cannot branch on — an untyped escape hatch in a
    typed command surface.
    """
    set_orchestrator(None)

    response = client.post(ENDPOINT, json={"scope": {"kind": "global_health_review"}})

    assert response.status_code == 503
    body = TechLeadRunAdmissionPayload.model_validate(response.json())
    assert body.outcome == "not_running"
    assert body.reason == "engine_not_running"
    assert body.admitted is False
    assert body.run_key == "global:health_review"
    assert "not running" in body.detail


# ---------------------------------------------------------------------------
# Authentication + CSRF
# ---------------------------------------------------------------------------


def test_request_without_credentials_is_rejected(
    auth_enabled_dashboard_client: TestClient,
):
    set_orchestrator(MagicMock())
    try:
        response = auth_enabled_dashboard_client.post(
            ENDPOINT, json={"scope": {"kind": "global_health_review"}}
        )
    finally:
        set_orchestrator(None)

    assert response.status_code == 401


def test_logged_in_request_without_csrf_is_rejected(
    logged_in_dashboard_client: TestClient,
):
    set_orchestrator(MagicMock())
    try:
        response = logged_in_dashboard_client.post(
            ENDPOINT, json={"scope": {"kind": "global_health_review"}}
        )
    finally:
        set_orchestrator(None)

    assert response.status_code == 403


def test_logged_in_request_with_csrf_reaches_the_owner(
    logged_in_dashboard_client: TestClient,
    fake_browser_auth,
):
    orchestrator = RecordingOrchestrator(
        _admission(
            TechLeadRunOutcome.QUEUED,
            scope_kind=TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW,
            run_key="global:health_review",
            issue_number=900,
        )
    )
    set_orchestrator(orchestrator)
    try:
        response = logged_in_dashboard_client.post(
            ENDPOINT,
            json={"scope": {"kind": "global_health_review"}},
            headers=fake_browser_auth.csrf_headers(logged_in_dashboard_client),
        )
    finally:
        set_orchestrator(None)

    assert response.status_code == 200
    assert len(orchestrator.requests) == 1
