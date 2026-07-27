"""Ensure public contract schemas are generated and kept in sync."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from issue_orchestrator.contracts.public import (
    DashboardDataContract,
    TimelineEventContract,
    TimelineIssueContract,
    generate_public_schemas,
)
from issue_orchestrator.ports.timeline_store import TimelineRecord
from issue_orchestrator.timeline import TimelineStream


def test_public_contract_schemas_are_current():
    base_dir = Path(__file__).resolve().parents[2]
    schema_dir = base_dir / "contracts" / "public"
    file_map = {
        path.stem: json.loads(path.read_text())
        for path in schema_dir.glob("*.json")
    }

    generated = generate_public_schemas()
    assert set(file_map) == set(generated)

    for name, schema in generated.items():
        assert file_map[name] == schema


def _dashboard_data_payload(**overrides: object) -> dict[str, object]:
    """A minimal complete ``dashboard_data`` payload for contract validation."""
    payload: dict[str, object] = {
        "startupComplete": True,
        "paused": False,
        "e2eRunning": False,
        "queueRefreshSeconds": 300,
        "repo": "test/repo",
        "repoRoot": "/tmp/repo",
        "githubOwner": "test",
        "githubRepo": "repo",
        "agents": ["agent:web"],
        "validationConfigured": False,
        "providerCircuit": {
            "any_open": False,
            "open_count": 0,
            "open_providers": [],
            "summary_text": "",
            "next_retry_at": None,
            "entries": [],
        },
    }
    payload.update(overrides)
    return payload


def test_validation_configured_is_a_required_dashboard_data_field():
    # Issue #4109: the dashboard's "no validation configured" warning rides on
    # ``dashboard_data.validationConfigured``. It must be a *required* field on
    # the public contract (no default fallback) so a missing flag fails the
    # contract loudly instead of silently defaulting to ``True`` and hiding the
    # warning. It must also appear in the durable schema's ``required`` list.
    assert "validationConfigured" in DashboardDataContract.model_fields
    assert DashboardDataContract.model_fields["validationConfigured"].is_required()

    schema = generate_public_schemas()["dashboard.view_model"]
    data_schema = schema["$defs"]["DashboardDataContract"]
    assert "validationConfigured" in data_schema["properties"]
    assert "validationConfigured" in data_schema["required"]
    # No baked-in default that could substitute for a dropped producer value.
    assert "default" not in data_schema["properties"]["validationConfigured"]


def test_dashboard_data_contract_rejects_missing_validation_configured():
    # A complete payload validates; dropping ``validationConfigured`` must fail
    # rather than default to ``True`` (which would silently suppress the safety
    # warning). Issue #4109.
    DashboardDataContract.model_validate(_dashboard_data_payload())

    incomplete = _dashboard_data_payload()
    del incomplete["validationConfigured"]
    with pytest.raises(ValidationError):
        DashboardDataContract.model_validate(incomplete)


def test_role_feedback_response_type_is_a_declared_public_timeline_field():
    # Issue #6428: the per-role verdict on ``review_exchange.role_feedback``
    # rides on ``response_type``, which the in-round Story progress projection
    # reads. It must be a *declared* field on the public timeline contract — not
    # silently tolerated as a permissive extra — and must round-trip through the
    # real producer (``TimelineStream`` -> ``to_dict``) into the contract.
    assert "response_type" in TimelineEventContract.model_fields

    record = TimelineRecord(
        event_id="review_exchange.role_feedback-reviewer-1",
        timestamp="2026-03-22T13:36:00Z",
        event="review_exchange.role_feedback",
        data={
            "issue_number": 4057,
            "round_index": 1,
            "role": "reviewer",
            "response_type": "changes_requested",
        },
    )
    payload = TimelineStream.from_records(4057, [record]).to_dict()

    issue = TimelineIssueContract.model_validate(payload)
    feedback = next(
        event for event in issue.events
        if event.event == "review_exchange.role_feedback"
    )
    assert feedback.response_type == "changes_requested"


def test_generated_public_timeline_schema_documents_response_type():
    # The on-disk public schema (the durable UI/test contract) must document the
    # field, so consumers of ``/api/timeline/{issue_number}`` can rely on it.
    schema = generate_public_schemas()["timeline.issue"]
    event_schema = schema["$defs"]["TimelineEventContract"]
    assert "response_type" in event_schema["properties"]
