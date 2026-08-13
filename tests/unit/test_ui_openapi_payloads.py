"""Validate UI payloads against the UI OpenAPI schema."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import warnings

warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message="jsonschema.RefResolver is deprecated",
)

import pytest
from jsonschema import Draft202012Validator, RefResolver
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from issue_orchestrator.contracts.ui_openapi_models import (
    E2ERunDetailPayload,
    E2ERunTimelinePayload,
    IssueDetailActionPayload,
    IssueDetailPayload,
    RepositorySetupCommandPayload,
    RepositorySetupConflictPayload,
    RepositorySetupDetectionPayload,
    RepositorySetupFailurePayload,
    RepositorySetupPrerequisitesPayload,
    RepositorySetupPreviewPayload,
    RepositorySetupResultPayload,
    ViewModelSnapshotPayload,
    WorktreeAuditRequestPayload,
    WorktreeAuditResponsePayload,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    OrchestratorState,
    Session,
    SessionHistoryEntry,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.dependencies import (
    Dependency,
    DependencyMode,
    DependencyState,
)
from issue_orchestrator.domain.dependency_gates import (
    DependencyGateSnapshot,
    build_gate_report,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.provider_resilience import NO_PROVIDER_CIRCUIT_STATUS
from issue_orchestrator.view_models.dashboard import build_dashboard_view_model
from tests.unit.session_run_helpers import make_session_run_assets
from issue_orchestrator.view_models.dialogs import (
    build_blocked_issues_dialog,
    build_config_dialog,
    build_debug_dialog,
    build_doctor_dialog,
    build_info_dialog,
    build_phase_dialog,
    build_session_diagnostics_dialog,
    build_validation_failure_dialog,
)
from issue_orchestrator.view_models.issue_detail import build_issue_detail_view_model
from issue_orchestrator.view_models.lifecycle_semantics import (
    OutcomeBadge,
    AgentIdentity,
    CompletedCodingAttempt,
    CompletionRecordEvidence,
    DashboardIteration,
    DashboardTimelineContainer,
    E2ERunIteration,
    E2ERunLifecycle,
    E2ESuiteTimelineContainer,
    IssueCycle,
    IssueLifecycle,
    OpenCompletionRecordCommand,
    OpenValidationDetailsCommand,
    PassedE2ETestExecution,
    ReviewNotReached,
    SessionRecordingUnavailable,
    ShowEventDetailsCommand,
    TimelineSubject,
    ValidationPassed,
)


@dataclass
class _OrchestratorStub:
    state: OrchestratorState
    config: Config
    shutdown_requested: bool = False


# OutcomeBadge constructor shim for tests (PR #6333): the
# projection layer owns tone classification, but tests construct
# IssueCycle/JourneyRun directly with bare label strings.  This
# helper wraps any label in the typed shape so the assertions
# stay focused on cycle/run shape, not tone bookkeeping.
def _ob(label: str, tone: str = "neutral") -> OutcomeBadge:
    """Test helper: wrap a bare outcome label in an OutcomeBadge.
    Tone defaults to neutral; tests that care about tone pass it
    explicitly."""
    return OutcomeBadge(label=label, tone=tone)  # type: ignore[arg-type]


def _make_config() -> Config:
    config = Config()
    config.repo = "test/repo"
    config.repo_root = Path("/tmp/repo")
    config.queue_refresh_seconds = 300
    config.terminal_adapter = "subprocess"
    config.e2e.enabled = False
    return config


def _make_agent_config() -> AgentConfig:
    return AgentConfig(
        prompt_path=Path("/tmp/prompt.txt"),
        model="sonnet",
        timeout_minutes=30,
    )


def _validator(component: str) -> Draft202012Validator:
    schema = Path("docs/api/ui-openapi.json").read_text()
    data = __import__("json").loads(schema)
    resolver = RefResolver.from_schema(data)
    return Draft202012Validator(
        data["components"]["schemas"][component], resolver=resolver
    )


def _assert_response_view_is_timeline_view_constrained(
    payload: dict, schema_name: str, model_cls: type
) -> None:
    """Issue #5976: a response payload's ``view`` field echoes the rendered
    lens and must be the shared ``TimelineView`` enum, not a free ``str`` —
    enforced on both the JSON-schema and generated-Pydantic sides.

    Without the ``$ref``, an out-of-vocabulary ``view`` (e.g. ``"detail"``)
    would validate as a plain string on both sides and a generated client
    could accept or propagate a value the runtime normalizer never produces.
    """
    from pydantic import ValidationError

    validator = _validator(schema_name)
    for view in ("user", "ops", "debug", "raw"):
        candidate = {**payload, "view": view}
        validator.validate(candidate)
        model_cls.model_validate(candidate)

    invalid = {**payload, "view": "detail"}
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(invalid)
    with pytest.raises(ValidationError):
        model_cls.model_validate(invalid)


def _schema_error_messages(errors: list[JsonSchemaValidationError]) -> str:
    messages: list[str] = []
    pending = list(errors)
    while pending:
        error = pending.pop()
        messages.append(error.message)
        pending.extend(error.context)
    return "\n".join(messages)


def _e2e_timeline_event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "event_id": "event:e2e-test",
        "timestamp": "2026-04-21T11:00:00Z",
        "event": "e2e.test_completed",
        "issue_number": -88,
        "phase": "execution",
        "step": "test_completed",
        "status": "completed",
        "level": "info",
        "summary": "tests/e2e/test_example.py::test_passes: passed",
        "parent_key": "e2e-run-88",
        "detail": None,
        "run_id": None,
        "run_dir": None,
        "artifacts": [],
        "unsupported_schema": False,
        "review_oriented": False,
        "event_intent": "system",
        "nodeid": "tests/e2e/test_example.py::test_passes",
        "outcome": "passed",
    }
    event.update(overrides)
    return event


def _e2e_timeline_cycle(*events: dict[str, object]) -> dict[str, object]:
    return {
        "cycle": 1,
        "start": "2026-04-21T11:00:00Z",
        "end": "2026-04-21T11:02:00Z",
        "status": "completed",
        "phases": ["execution"],
        "events": list(events),
        "summary": "E2E execution",
    }


def test_worktree_audit_request_and_response_match_ui_openapi() -> None:
    request = {"repo_root": "/repos/project"}
    response = {
        "worktrees": [
            {
                "path": "/repos/project-41",
                "name": "project-41",
                "kind": "issue",
                "disposition": "managed",
                "reason": "review-gated cleanup",
            },
            {
                "path": "/repos/project-tech-lead-42-abcdef123456",
                "name": "project-tech-lead-42-abcdef123456",
                "kind": "tech_lead_scratch",
                "disposition": "retained",
                "reason": "active session restored at startup",
            },
        ],
        "cleanup_candidates": [],
        "stale_worktrees": [],
        "message": "Read-only audit complete.",
        "issue_cleanup_enabled": True,
        "activity_evidence": "known",
        "audit_unavailable": False,
        "scope": "configured",
        "note": None,
    }

    _validator("WorktreeAuditRequestPayload").validate(request)
    _validator("WorktreeAuditResponsePayload").validate(response)
    WorktreeAuditRequestPayload.model_validate(request)
    WorktreeAuditResponsePayload.model_validate(response)

    schema = __import__("json").loads(Path("docs/api/ui-openapi.json").read_text())
    operation = schema["paths"]["/control/tools/worktrees/cleanup"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/WorktreeAuditRequestPayload"
    }
    assert operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/WorktreeAuditResponsePayload"}


def test_repository_setup_command_and_results_match_ui_openapi() -> None:
    command = {
        "repo_root": "/repos/porchpin",
        "repo_name": "owner/porchpin",
        "worker_agent_label": "agent:dev",
        "model": "sonnet",
        "effort": "xhigh",
        "configure_reviewer": True,
        "reviewer_model": "opus",
        "reviewer_effort": "max",
        "configure_internal_reviewer": True,
        "internal_review_max_rounds": 4,
        "internal_review_instructions": ".io/internal-review.md",
        "validation_quick_command": "make validate-quick",
        "validation_publish_command": "make validate-pr-raw",
        "worktree_base": "../worktrees/porchpin",
        "github_authorization": {
            "kind": "detected",
            "api_url": "https://api.github.com",
            "http_timeout_seconds": 20,
        },
        "configure_tech_lead": True,
        "tech_lead_model": "sonnet",
        "tech_lead_effort": "high",
        "tech_lead_review_threshold": 1,
        "config_name": "default.yaml",
        "create_prompts": True,
        "create_labels": True,
        "replace_existing": False,
    }
    preview = {
        "yaml": "repo:\n  name: owner/porchpin\n",
        "worktree_base": "/repos/worktrees/porchpin",
        "github_authorization": {
            "verified": True,
            "identity": "setup-user",
            "repository": "owner/porchpin",
            "auth_kind": "personal",
            "source": "Environment variable ISSUE_ORCH_GITHUB_TOKEN",
            "authorship_notice": "Pull requests are authored as setup-user.",
            "verification_note": (
                "Setup verified identity and repository access without "
                "making GitHub writes."
            ),
            "required_permissions": [
                "Contents: read and write",
                "Issues: read and write",
            ],
            "authorization": {
                "kind": "personal",
                "token_env": "ISSUE_ORCH_GITHUB_TOKEN",
                "api_url": "https://api.github.com",
                "http_timeout_seconds": 20,
            },
        },
        "files": [
            {
                "path": "/repos/porchpin/.issue-orchestrator/config/default.yaml",
                "action": "create",
                "size": 33,
            },
            {
                "path": "/repos/porchpin/.io/reviewer.md",
                "action": "create",
                "type": "prompt",
                "agent": "agent:reviewer",
            },
            {
                "path": "/repos/porchpin/.io/tech-lead.md",
                "action": "create",
                "type": "prompt",
                "agent": "agent:tech-lead",
            },
        ],
    }
    result = {
        "status": "saved",
        "config_path": "/repos/porchpin/.issue-orchestrator/config/default.yaml",
        "created_files": [
            "/repos/porchpin/.io/reviewer.md",
            "/repos/porchpin/.io/tech-lead.md",
        ],
        "created_labels": [
            "agent:reviewer",
            "agent:tech-lead",
            "needs-tech-lead-review",
        ],
    }
    conflict = {
        "error": "replace_confirmation_required",
        "detail": "Replacement confirmation is required",
        "config_path": "/repos/porchpin/.issue-orchestrator/config/default.yaml",
    }
    failure = {
        "error": "repository_setup_failed",
        "stage": "labels",
        "detail": "GitHub unavailable",
        "applied_files": [
            "/repos/porchpin/.issue-orchestrator/config/default.yaml",
        ],
        "created_labels": ["agent:dev"],
    }

    for schema_name, model, payload in (
        ("RepositorySetupCommandPayload", RepositorySetupCommandPayload, command),
        ("RepositorySetupConflictPayload", RepositorySetupConflictPayload, conflict),
        ("RepositorySetupFailurePayload", RepositorySetupFailurePayload, failure),
        ("RepositorySetupPreviewPayload", RepositorySetupPreviewPayload, preview),
        ("RepositorySetupResultPayload", RepositorySetupResultPayload, result),
    ):
        _validator(schema_name).validate(payload)
        model.model_validate(payload)

    malformed = {**command, "config": {"agents": {}}}
    with pytest.raises(JsonSchemaValidationError):
        _validator("RepositorySetupCommandPayload").validate(malformed)
    with pytest.raises(ValueError):
        RepositorySetupCommandPayload.model_validate(malformed)

    for invalid_label in ("agent:", "agent:reviewer", "agent:tech-lead"):
        malformed_label = {**command, "worker_agent_label": invalid_label}
        with pytest.raises(JsonSchemaValidationError):
            _validator("RepositorySetupCommandPayload").validate(malformed_label)
        with pytest.raises(ValueError):
            RepositorySetupCommandPayload.model_validate(malformed_label)


def test_repository_setup_get_payloads_match_ui_openapi() -> None:
    prereqs = {
        "all_ok": True,
        "checks": {
            "git": {"ok": True, "detail": "git version 2.50"},
            "ai_provider_clis": {"ok": True, "detail": "Available: claude-code"},
        },
        "agent_checks": [
            {"name": "Agent CLI", "ok": True, "detail": "Config not detected yet"}
        ],
    }
    detection = {
        "repo_root": "/repos/porchpin",
        "repo": "owner/porchpin",
        "existing_config": None,
        "config_path": None,
        "github_labels": [],
        "agent_labels": [],
        "prompt_candidates": [],
        "worktree_base_default": "../worktrees/porchpin",
        "worktree_base_resolved": "/repos/worktrees/porchpin",
        "github_authorization": {
            "authorization": {
                "kind": "detected",
                "api_url": "https://github.example/api/v3",
                "http_timeout_seconds": 47,
            },
            "configured_kind": "personal",
            "inline_token_migration_required": True,
        },
        "validation_defaults": {
            "quick_command": "make validate-fast",
            "publish_command": "make validate-pr",
            "source": "Makefile targets",
        },
    }

    for schema_name, model, payload in (
        (
            "RepositorySetupPrerequisitesPayload",
            RepositorySetupPrerequisitesPayload,
            prereqs,
        ),
        (
            "RepositorySetupDetectionPayload",
            RepositorySetupDetectionPayload,
            detection,
        ),
    ):
        _validator(schema_name).validate(payload)
        model.model_validate(payload)


def test_repository_setup_get_paths_reference_typed_responses() -> None:
    import json

    contract = json.loads(Path("docs/api/ui-openapi.json").read_text())

    assert contract["paths"]["/control/setup/prereqs"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RepositorySetupPrerequisitesPayload"
    }
    assert contract["paths"]["/control/setup/detect"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RepositorySetupDetectionPayload"
    }


def test_dashboard_view_model_matches_ui_openapi() -> None:
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    issue = Issue(number=12, title="Fix bug", labels=["agent:web"])
    session_key = SessionKey(issue=FakeIssueKey("12"), task=TaskKind.REVIEW)
    session = Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id="review-12",
        worktree_path=Path("/tmp/worktree-12"),
        branch_name="feature/12",
        run_assets=make_session_run_assets(
            Path("/tmp/worktree-12"),
            session_name="review-12",
        ),
        started_at=datetime.now() - timedelta(minutes=3),
    )

    state = OrchestratorState(active_sessions=[session], startup_status="complete")
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="active",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    validator = _validator("DashboardViewModelPayload")
    validator.validate(view_model.to_dict())


def test_dashboard_view_model_history_and_e2e_items_match_ui_openapi() -> None:
    """Real history/E2E producers must stamp required IssueItem fields."""
    config = _make_config()
    config.e2e.enabled = True
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}
    now = datetime.now().timestamp()
    state = OrchestratorState(
        startup_status="complete",
        session_history=[
            SessionHistoryEntry(
                issue_number=4057,
                title="Closed completed issue",
                agent_type="agent:web",
                status="closed",
                runtime_minutes=12,
                pr_url="https://github.com/test/repo/pull/4057",
            ),
        ],
        issue_last_refreshed_at={4057: now - 3600},
    )
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="e2e",
        e2e_page=1,
        e2e_status_provider=lambda _: {
            "enabled": True,
            "running": True,
            "needs_attention": True,
            "untriaged_count": 1,
            "last_run": {"id": 88, "relative_time": "1m ago"},
            "failed_tests": [
                {
                    "nodeid": "tests/e2e/test_example.py::test_fails",
                    "duration_seconds": 0.4,
                },
            ],
        },
    )

    assert view_model.history_items
    assert view_model.e2e_items
    assert all("show_stale_badge" in item for item in view_model.history_items)
    assert all("show_stale_badge" in item for item in view_model.e2e_items)
    _validator("DashboardViewModelPayload").validate(view_model.to_dict())


def _provider_circuit_payload(**overrides: object) -> dict[str, object]:
    """A minimal complete ``ProviderCircuitStatusPayload`` (hidden/healthy)."""
    payload: dict[str, object] = {
        "any_open": False,
        "open_count": 0,
        "open_providers": [],
        "summary_text": "",
        "next_retry_at": None,
        "entries": [],
        "status_unavailable": False,
    }
    payload.update(overrides)
    return payload


def _dashboard_data_payload(**overrides: object) -> dict[str, object]:
    """A minimal complete ``DashboardDataPayload`` for contract validation."""
    payload: dict[str, object] = {
        "startupComplete": True,
        "paused": False,
        "e2eRunning": False,
        "queueRefreshSeconds": 300,
        "repo": "test/repo",
        "repoRoot": "/tmp/repo",
        "configName": "main.yaml",
        "configMode": "codex",
        "githubOwner": "test",
        "githubRepo": "repo",
        "agents": ["agent:web"],
        "validationConfigured": False,
        "providerCircuit": _provider_circuit_payload(),
    }
    payload.update(overrides)
    return payload


def test_dashboard_data_payload_requires_validation_configured() -> None:
    """Issue #4109: ``validationConfigured`` is the safety flag that drives the
    dashboard's "no validation configured" warning. It is a *required* boolean
    on both contract layers — a missing flag must fail validation loudly, not
    default to ``True`` (JSON schema) or ``None`` (generated Pydantic) and
    silently suppress the warning.
    """
    from issue_orchestrator.contracts.ui_openapi_models import DashboardDataPayload
    from pydantic import ValidationError

    validator = _validator("DashboardDataPayload")

    valid = _dashboard_data_payload()
    validator.validate(valid)  # must not raise
    DashboardDataPayload.model_validate(valid)

    incomplete = _dashboard_data_payload()
    del incomplete["validationConfigured"]
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(incomplete)
    with pytest.raises(ValidationError):
        DashboardDataPayload.model_validate(incomplete)

    # A null flag is also rejected — the field is a strict boolean.
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(_dashboard_data_payload(validationConfigured=None))
    with pytest.raises(ValidationError):
        DashboardDataPayload.model_validate(
            _dashboard_data_payload(validationConfigured=None)
        )


def test_dashboard_data_payload_requires_provider_circuit() -> None:
    """Issue #5980: ``providerCircuit`` is a *required*, typed field on both
    contract layers. The UI reads ``window.dashboardData.providerCircuit`` to
    render the outage banner — a dropped or malformed payload must fail the UI
    OpenAPI validation loudly, not slip through as an untyped extra and silently
    hide a provider outage.
    """
    from issue_orchestrator.contracts.ui_openapi_models import DashboardDataPayload
    from pydantic import ValidationError

    data_validator = _validator("DashboardDataPayload")
    circuit_validator = _validator("ProviderCircuitStatusPayload")

    valid = _dashboard_data_payload()
    data_validator.validate(valid)  # must not raise
    DashboardDataPayload.model_validate(valid)

    # Missing entirely → rejected on both layers.
    incomplete = _dashboard_data_payload()
    del incomplete["providerCircuit"]
    with pytest.raises(JsonSchemaValidationError):
        data_validator.validate(incomplete)
    with pytest.raises(ValidationError):
        DashboardDataPayload.model_validate(incomplete)

    # Malformed circuit (missing required ``status_unavailable``) → rejected
    # because the typed ``ProviderCircuitStatusPayload`` forbids partial shapes.
    malformed_circuit = _provider_circuit_payload()
    del malformed_circuit["status_unavailable"]
    with pytest.raises(JsonSchemaValidationError):
        data_validator.validate(_dashboard_data_payload(providerCircuit=malformed_circuit))
    with pytest.raises(JsonSchemaValidationError):
        circuit_validator.validate(malformed_circuit)
    with pytest.raises(ValidationError):
        DashboardDataPayload.model_validate(
            _dashboard_data_payload(providerCircuit=malformed_circuit)
        )

    # Wrong type for a circuit field (``any_open`` must be boolean) → rejected.
    bad_type = _provider_circuit_payload(any_open="yes")
    with pytest.raises(JsonSchemaValidationError):
        data_validator.validate(_dashboard_data_payload(providerCircuit=bad_type))

    # Unknown extra key inside the strict circuit payload → rejected.
    extra_key = _provider_circuit_payload(unexpected_field=True)
    with pytest.raises(JsonSchemaValidationError):
        circuit_validator.validate(extra_key)


def test_view_model_snapshot_payload_matches_ui_openapi() -> None:
    config = _make_config()
    state = OrchestratorState(startup_status="complete")
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )
    payload = {
        "view_model": view_model.to_dict(),
        "rows": [{"issue_number": 12, "html": "<tr></tr>"}],
        "active_tab": view_model.active_tab,
        "count": 1,
    }

    ViewModelSnapshotPayload.model_validate(payload)
    _validator("ViewModelSnapshotPayload").validate(payload)


def test_issue_item_open_run_command_validates_against_ui_openapi() -> None:
    """PR #6329 reviewer Blocker 2: ``open_run_command`` is a typed
    field on ``IssueItemPayload``, not an opaque extra.

    Before the fix, ``IssueItemPayload`` was ``additionalProperties: true``
    with no declared ``open_run_command`` schema — meaning the field
    was accepted as an extra regardless of its shape.  The reviewer
    pointed out that malformed payloads (e.g. ``run_id: 0`` which the
    Pydantic model rejects) flowed through silently.

    Now ``open_run_command`` is an explicit
    ``OpenE2ERunCommandPayload | null`` field.  Valid payloads pass;
    malformed ones (wrong ``kind``, missing ``run_id``, wrong types
    on ``expand_run_details``) fail the JSON-schema validation.
    """
    validator = _validator("IssueItemPayload")

    # Valid case: a well-formed open_run_command attaches to an
    # E2E issue item.
    valid_item = {
        "issue_number": "E2E-88",
        "title": "Run details",
        "status": "passed",
        "action": "details",
        "action_hint": "View run details",
        "is_e2e": True,
        "e2e_run_id": 88,
        "show_stale_badge": False,
        "open_run_command": {
            "kind": "open_e2e_run",
            "label": "Open E2E Run",
            "run_id": 88,
            "expand_run_details": False,
        },
    }
    validator.validate(valid_item)  # must not raise

    # Null is allowed (non-E2E items don't carry the command).
    valid_item_no_command = {**valid_item, "open_run_command": None}
    validator.validate(valid_item_no_command)

    # Malformed: wrong kind discriminator (using a different command kind).
    bad_kind = {**valid_item}
    bad_kind["open_run_command"] = {
        "kind": "open_issue_timeline",
        "label": "X",
        "run_id": 88,
    }
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(bad_kind)

    # Malformed: missing required ``run_id``.
    bad_missing_run_id = {**valid_item}
    bad_missing_run_id["open_run_command"] = {
        "kind": "open_e2e_run",
        "label": "Open E2E Run",
    }
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(bad_missing_run_id)

    # Malformed: wrong type for ``expand_run_details``.
    bad_expand_type = {**valid_item}
    bad_expand_type["open_run_command"] = {
        "kind": "open_e2e_run",
        "label": "Open E2E Run",
        "run_id": 88,
        "expand_run_details": "yes",  # must be boolean
    }
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(bad_expand_type)

    # Malformed: ``run_id`` must be a positive integer (PR #6329
    # round-4 blocker — the OpenAPI schema must enforce the same
    # invariant the canonical ``OpenE2ERunCommand`` Pydantic model
    # enforces).
    bad_run_id_zero = {**valid_item}
    bad_run_id_zero["open_run_command"] = {
        "kind": "open_e2e_run",
        "label": "Open E2E Run",
        "run_id": 0,
        "expand_run_details": False,
    }
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(bad_run_id_zero)

    bad_run_id_negative = {**valid_item}
    bad_run_id_negative["open_run_command"] = {
        "kind": "open_e2e_run",
        "label": "Open E2E Run",
        "run_id": -5,
        "expand_run_details": False,
    }
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(bad_run_id_negative)


def test_issue_item_stale_badge_visibility_contract_is_required_boolean() -> None:
    """``show_stale_badge`` is the required display policy for stale chrome.

    ``is_stale`` remains a raw data freshness fact.  The UI must not infer
    whether to show the warning badge from that fact.
    """
    from issue_orchestrator.contracts.ui_openapi_models import IssueItemPayload
    from pydantic import ValidationError

    validator = _validator("IssueItemPayload")

    validator.validate({"show_stale_badge": False})
    IssueItemPayload.model_validate({"show_stale_badge": False})

    with pytest.raises(JsonSchemaValidationError):
        validator.validate({})
    with pytest.raises(JsonSchemaValidationError):
        validator.validate({"show_stale_badge": None})

    with pytest.raises(ValidationError):
        IssueItemPayload.model_validate({})
    with pytest.raises(ValidationError):
        IssueItemPayload.model_validate({"show_stale_badge": None})


def test_issue_item_open_run_command_pydantic_rejects_non_positive_run_id() -> None:
    """The GENERATED Pydantic contract must enforce the same
    ``run_id >= 1`` invariant the canonical model enforces.

    PR #6329 round-4 blocker: the generator was emitting
    ``run_id: int`` with no constraint, so
    ``IssueItemPayload.model_validate({...})`` silently accepted
    ``run_id: 0``.  After extending the generator to map
    ``minimum: 1`` → ``Field(..., ge=1)``, the generated contract
    enforces the same invariant at the Python boundary that JSON
    schema enforces at the validator boundary.
    """
    from issue_orchestrator.contracts.ui_openapi_models import IssueItemPayload
    from pydantic import ValidationError

    # Valid → succeeds.
    valid = IssueItemPayload.model_validate(
        {
            "issue_number": "E2E-88",
            "show_stale_badge": False,
            "open_run_command": {
                "kind": "open_e2e_run",
                "label": "Open E2E Run",
                "run_id": 88,
                "expand_run_details": False,
            },
        }
    )
    assert valid.open_run_command is not None
    assert valid.open_run_command.run_id == 88

    # run_id=0 → rejected by the generated Pydantic model.
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        IssueItemPayload.model_validate(
            {
                "issue_number": "E2E-88",
                "show_stale_badge": False,
                "open_run_command": {
                    "kind": "open_e2e_run",
                    "label": "Open E2E Run",
                    "run_id": 0,
                    "expand_run_details": False,
                },
            }
        )

    # Negative run_id → also rejected.
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        IssueItemPayload.model_validate(
            {
                "issue_number": "E2E-88",
                "show_stale_badge": False,
                "open_run_command": {
                    "kind": "open_e2e_run",
                    "label": "Open E2E Run",
                    "run_id": -1,
                    "expand_run_details": False,
                },
            }
        )


def test_issue_item_open_run_command_strict_int_rejects_string_and_boolean() -> None:
    """The generated UI contract enforces strict-int scalar semantics
    on ``run_id`` — no coercion from strings or booleans.

    PR #6329 round-5 blocker: ``Pydantic``'s default ``int`` field
    accepts ``"88"`` (string) and ``True`` (boolean) by coercing
    them to ``88`` and ``1`` respectively.  JSON Schema's
    ``type: integer`` rejects both.  The generator now emits
    ``Field(..., ge=1, strict=True)`` for numeric-constrained
    integer fields so the Python contract matches the wire
    contract — malformed scalars fail loudly, not silently
    normalize.
    """
    from issue_orchestrator.contracts.ui_openapi_models import IssueItemPayload
    from pydantic import ValidationError

    # String ``run_id`` → rejected.
    with pytest.raises(ValidationError):
        IssueItemPayload.model_validate(
            {
                "issue_number": "E2E-88",
                "show_stale_badge": False,
                "open_run_command": {
                    "kind": "open_e2e_run",
                    "label": "Open E2E Run",
                    "run_id": "88",  # string — should not coerce
                    "expand_run_details": False,
                },
            }
        )

    # Boolean True ``run_id`` → rejected.
    with pytest.raises(ValidationError):
        IssueItemPayload.model_validate(
            {
                "issue_number": "E2E-88",
                "show_stale_badge": False,
                "open_run_command": {
                    "kind": "open_e2e_run",
                    "label": "Open E2E Run",
                    "run_id": True,  # bool — should not coerce to 1
                    "expand_run_details": False,
                },
            }
        )

    # Boolean False ``run_id`` → rejected.
    with pytest.raises(ValidationError):
        IssueItemPayload.model_validate(
            {
                "issue_number": "E2E-88",
                "show_stale_badge": False,
                "open_run_command": {
                    "kind": "open_e2e_run",
                    "label": "Open E2E Run",
                    "run_id": False,
                    "expand_run_details": False,
                },
            }
        )


def test_expand_e2e_run_command_payload_matches_openapi() -> None:
    """Issue #6334: every ``<details>`` row in the inline runs-as-rows
    list carries an ``ExpandE2ERunCommand`` payload.  The OpenAPI
    schema and the generated Pydantic contract enforce the same
    invariants as ``OpenE2ERunCommand``:

      * ``kind`` is the literal ``expand_e2e_run``.
      * ``run_id`` is a positive integer (``minimum: 1``).
      * Strict-int scalar semantics — no coercion from strings or
        booleans.  A stale stringified payload from a refresh must
        fail validation, not silently normalize to a real run.
    """
    from issue_orchestrator.contracts.ui_openapi_models import (
        ExpandE2ERunCommandPayload,
    )
    from pydantic import ValidationError

    validator = _validator("ExpandE2ERunCommandPayload")
    valid = {
        "kind": "expand_e2e_run",
        "label": "Expand E2E Run",
        "run_id": 88,
    }
    validator.validate(valid)
    ExpandE2ERunCommandPayload.model_validate(valid)

    # Wrong kind discriminator.
    with pytest.raises(JsonSchemaValidationError):
        validator.validate({**valid, "kind": "open_e2e_run"})

    # Non-positive run_id.
    with pytest.raises(JsonSchemaValidationError):
        validator.validate({**valid, "run_id": 0})
    with pytest.raises(ValidationError):
        ExpandE2ERunCommandPayload.model_validate({**valid, "run_id": 0})
    with pytest.raises(ValidationError):
        ExpandE2ERunCommandPayload.model_validate({**valid, "run_id": -1})

    # Strict-int: reject string + boolean coercion at the Python layer.
    with pytest.raises(ValidationError):
        ExpandE2ERunCommandPayload.model_validate({**valid, "run_id": "88"})
    with pytest.raises(ValidationError):
        ExpandE2ERunCommandPayload.model_validate({**valid, "run_id": True})


def test_switch_e2e_timeline_view_command_payload_matches_openapi() -> None:
    """Issue #6334 round-2: Story/Ops/Debug buttons inside an
    expanded row emit a typed ``SwitchE2ETimelineViewCommand``.

    The OpenAPI schema and the generated Pydantic contract enforce
    the same invariants as ``OpenE2ERunCommand.run_id`` plus a
    Literal-constrained ``view`` field.  A typo in ``view`` (e.g.
    ``"detail"`` instead of ``"debug"``) must fail validation, not
    silently route to the wrong API call.
    """
    from issue_orchestrator.contracts.ui_openapi_models import (
        SwitchE2ETimelineViewCommandPayload,
    )
    from pydantic import ValidationError

    validator = _validator("SwitchE2ETimelineViewCommandPayload")
    valid = {
        "kind": "switch_e2e_timeline_view",
        "label": "Switch diagnostics timeline to Ops",
        "run_id": 88,
        "view": "ops",
    }
    validator.validate(valid)
    SwitchE2ETimelineViewCommandPayload.model_validate(valid)

    # Every ``TimelineView`` value round-trips — including ``raw``, which
    # the canonical command model previously omitted while the payload
    # allowed it (issue #5976 drift).
    for view in ("user", "ops", "debug", "raw"):
        validator.validate({**valid, "view": view})
        SwitchE2ETimelineViewCommandPayload.model_validate({**valid, "view": view})

    # ``view`` enum is strict.
    with pytest.raises(JsonSchemaValidationError):
        validator.validate({**valid, "view": "detail"})
    with pytest.raises(ValidationError):
        SwitchE2ETimelineViewCommandPayload.model_validate({**valid, "view": "detail"})

    # Strict-int + ge=1 on ``run_id`` (same family invariant).
    with pytest.raises(JsonSchemaValidationError):
        validator.validate({**valid, "run_id": 0})
    with pytest.raises(ValidationError):
        SwitchE2ETimelineViewCommandPayload.model_validate({**valid, "run_id": "88"})


def test_timeline_view_enum_is_reused_across_the_ui_openapi_contract() -> None:
    """Issue #5976: the ``user``/``ops``/``debug``/``raw`` timeline views are
    one reusable ``TimelineView`` schema, ``$ref``-ed by every query param
    and payload field instead of copied inline — which is what stops the
    wire enum drifting across routes and generated clients."""
    import json

    data = json.loads(Path("docs/api/ui-openapi.json").read_text())

    timeline_view = data["components"]["schemas"]["TimelineView"]
    assert timeline_view["enum"] == ["user", "ops", "debug", "raw"]

    ref = "#/components/schemas/TimelineView"

    # Every timeline ``view`` query param references the shared enum with no
    # inline ``enum`` copy left behind.
    view_param_paths = [
        "/api/issue-detail/{issue_number}",
        "/api/e2e-run/{run_id}/issue-detail/{issue_number}",
        "/api/e2e-run-detail/{run_id}",
        "/control/e2e/run/{run_id}/timeline",
    ]
    for path in view_param_paths:
        params = data["paths"][path]["get"]["parameters"]
        view_param = next(p for p in params if p["name"] == "view")
        assert view_param["schema"]["$ref"] == ref, path
        assert "enum" not in view_param["schema"], f"{path} kept an inline enum"

    # The typed command payload references it too.
    view_field = data["components"]["schemas"]["SwitchE2ETimelineViewCommandPayload"][
        "properties"
    ]["view"]
    assert view_field == {"$ref": ref}

    # The response payloads that echo the rendered lens back to the client
    # reference the same shared enum (with their documented default preserved),
    # so a generated client cannot accept or propagate an arbitrary response
    # ``view`` value that the runtime normalizer would never produce.
    for schema_name in ("IssueDetailPayload", "E2ERunDetailPayload"):
        response_view = data["components"]["schemas"][schema_name]["properties"]["view"]
        assert response_view["$ref"] == ref, schema_name
        assert response_view["default"] == "user", schema_name
        assert "type" not in response_view, f"{schema_name} kept an inline string type"
        assert "enum" not in response_view, f"{schema_name} kept an inline enum"


def test_timeline_view_is_the_single_python_source_of_truth() -> None:
    """The generated ``TimelineView`` Literal is the one Python source of
    truth: the runtime normalizer and the canonical
    ``SwitchE2ETimelineViewCommand`` both derive from it, so none can drift
    (issue #5976)."""
    from typing import get_args

    from pydantic import ValidationError

    from issue_orchestrator.contracts.ui_openapi_models import TimelineView
    from issue_orchestrator.view_models.lifecycle_semantics import (
        SwitchE2ETimelineViewCommand,
    )
    from issue_orchestrator.view_models.timeline_view import (
        DEFAULT_TIMELINE_VIEW,
        TIMELINE_VIEWS,
        normalize_timeline_view,
    )

    views = set(get_args(TimelineView))
    assert views == {"user", "ops", "debug", "raw"}
    assert TIMELINE_VIEWS == views
    assert DEFAULT_TIMELINE_VIEW in views

    # Unknown values coerce to the default; known ones pass through.
    assert normalize_timeline_view("nonsense") == DEFAULT_TIMELINE_VIEW
    for view in views:
        assert normalize_timeline_view(view) == view

    # The canonical command accepts every wire view, including ``raw`` (the
    # value it used to omit), and still rejects a bogus one.
    for view in views:
        assert SwitchE2ETimelineViewCommand(run_id=1, view=view).view == view  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SwitchE2ETimelineViewCommand(run_id=1, view="detail")  # type: ignore[arg-type]


def test_create_e2e_untriaged_issues_command_payload_matches_openapi() -> None:
    """Issue #6334 round-2: the "Create issue(s)" button inside an
    expanded row's untracked-failures banner emits a typed
    ``CreateE2EUntriagedIssuesCommand``.

    Same run_id invariants as the rest of the E2E command family.
    The agent does NOT live in the payload — it's read at click
    time from the row-scoped ``.unified-run-agent`` select.
    """
    from issue_orchestrator.contracts.ui_openapi_models import (
        CreateE2EUntriagedIssuesCommandPayload,
    )
    from pydantic import ValidationError

    validator = _validator("CreateE2EUntriagedIssuesCommandPayload")
    valid = {
        "kind": "create_e2e_untriaged_issues",
        "label": "Create issue(s)",
        "run_id": 88,
    }
    validator.validate(valid)
    CreateE2EUntriagedIssuesCommandPayload.model_validate(valid)

    # Payload may not carry agent / nodeids — those are runtime reads.
    with pytest.raises(JsonSchemaValidationError):
        validator.validate({**valid, "agent": "agent:web"})

    with pytest.raises(ValidationError):
        CreateE2EUntriagedIssuesCommandPayload.model_validate({**valid, "run_id": "88"})
    with pytest.raises(ValidationError):
        CreateE2EUntriagedIssuesCommandPayload.model_validate({**valid, "run_id": 0})


def test_recent_e2e_runs_payload_matches_openapi() -> None:
    """Issue #6334: the runs-as-rows panel renders from
    ``RecentE2ERunsPayload``, a typed wrapper around a list of
    ``RecentE2ERunSummary``.  Each row carries a typed
    ``OutcomeBadge``, per-outcome counts, and the typed
    ``ExpandE2ERunCommand`` it dispatches on toggle.

    Cross-payload invariant: ``expand_command.run_id`` MUST match
    the row's own ``run_id`` — caught by the Pydantic model
    validator (a mismatched expand_command would dispatch the
    wrong run id to the lazy loader).

    Top-level invariant: no two summaries share a ``run_id`` — the
    JS dispatcher resolves rows by ``data-e2e-run-id``, so a
    duplicate would silently route to whichever row got rendered
    first.
    """
    from issue_orchestrator.contracts.ui_openapi_models import (
        ExpandE2ERunCommandPayload,
        OutcomeBadgePayload,
        RecentE2ERunSummaryPayload,
        RecentE2ERunsPayload,
    )
    from pydantic import ValidationError

    summary_validator = _validator("RecentE2ERunSummaryPayload")
    payload_validator = _validator("RecentE2ERunsPayload")

    summary = {
        "run_id": 88,
        "outcome": {"label": "Passed", "tone": "passed"},
        "started_at": "2026-05-12T10:00:00Z",
        "finished_at": "2026-05-12T10:05:00Z",
        "duration_seconds": 300.0,
        "commit_sha": "abc1234",
        "branch": "main",
        "runner_kind": "pytest",
        "command_summary": "pytest tests/e2e",
        "results": {
            "passed": 36,
            "failed": 1,
            "errored": 0,
            "skipped": 2,
            "quarantined": 0,
            "total": 39,
        },
        "note": None,
        "expand_command": {
            "kind": "expand_e2e_run",
            "label": "Expand E2E Run",
            "run_id": 88,
        },
    }
    summary_validator.validate(summary)
    RecentE2ERunSummaryPayload.model_validate(summary)

    payload = {"runs": [summary]}
    payload_validator.validate(payload)
    RecentE2ERunsPayload.model_validate(payload)

    # Single-run case still validates — explicit per the issue body
    # ("Single-run case renders as a 1-element list so the idiom
    # holds even with one run.").
    payload_validator.validate({"runs": []})
    RecentE2ERunsPayload.model_validate({"runs": []})

    # Non-positive run_id rejected at the schema layer.
    with pytest.raises(JsonSchemaValidationError):
        summary_validator.validate({**summary, "run_id": 0})

    # Strict-int on run_id at the Python layer.
    with pytest.raises(ValidationError):
        RecentE2ERunSummaryPayload.model_validate({**summary, "run_id": "88"})

    # Top-level Pydantic also exposes the strict-int via the wire
    # generator — the JSON Schema doesn't enforce it, so the strict
    # contract lives on the source ``RecentE2ERunSummary`` /
    # ``ExpandE2ERunCommand`` in lifecycle_semantics.  Confirm the
    # source models enforce both invariants (run_id match + uniqueness).
    from issue_orchestrator.view_models.lifecycle_semantics import (
        ExpandE2ERunCommand,
        RecentE2ERunSummary,
        RecentE2ERunsPayload as SourceRecentE2ERunsPayload,
        E2ERunResultCounts,
    )

    # expand_command.run_id mismatch → reject.
    counts = E2ERunResultCounts(
        passed=0, failed=0, errored=0, skipped=0, quarantined=0, total=0
    )
    badge = OutcomeBadge(label="Passed", tone="passed")
    with pytest.raises(ValidationError):
        RecentE2ERunSummary(
            run_id=42,
            outcome=badge,
            started_at="2026-05-12T10:00:00Z",
            runner_kind="pytest",
            command_summary="pytest",
            results=counts,
            expand_command=ExpandE2ERunCommand(run_id=99),
        )

    # Duplicate run_ids in payload → reject.
    s1 = RecentE2ERunSummary(
        run_id=1,
        outcome=badge,
        started_at="2026-05-12T10:00:00Z",
        runner_kind="pytest",
        command_summary="pytest",
        results=counts,
        expand_command=ExpandE2ERunCommand(run_id=1),
    )
    s2 = RecentE2ERunSummary(
        run_id=1,
        outcome=badge,
        started_at="2026-05-12T10:00:00Z",
        runner_kind="pytest",
        command_summary="pytest",
        results=counts,
        expand_command=ExpandE2ERunCommand(run_id=1),
    )
    with pytest.raises(ValidationError):
        SourceRecentE2ERunsPayload(runs=(s1, s2))


def test_open_inline_agent_attempts_command_payload_matches_openapi() -> None:
    """Issue #6322 follow-up: the inline ``▸ Attempts on issue #N``
    expander emits a typed ``OpenInlineAgentAttemptsCommandPayload``
    on its ``<details>`` element.  Both the OpenAPI schema and the
    generated Pydantic contract enforce the same invariants:

      * ``kind`` is the literal ``open_inline_agent_attempts``.
      * ``issue_number`` is a positive integer (``minimum: 1``).
      * Strict-int scalar semantics — no coercion from strings or
        booleans — same as ``OpenE2ERunCommand.run_id``.
    """
    from issue_orchestrator.contracts.ui_openapi_models import (
        OpenInlineAgentAttemptsCommandPayload,
    )
    from pydantic import ValidationError

    validator = _validator("OpenInlineAgentAttemptsCommandPayload")
    # Valid payload.
    valid = {
        "kind": "open_inline_agent_attempts",
        "label": "Open Inline Agent Attempts",
        "issue_number": 4503,
    }
    validator.validate(valid)
    OpenInlineAgentAttemptsCommandPayload.model_validate(valid)

    # Wrong kind discriminator.
    with pytest.raises(JsonSchemaValidationError):
        validator.validate({**valid, "kind": "open_e2e_run"})

    # Non-positive issue numbers.
    with pytest.raises(JsonSchemaValidationError):
        validator.validate({**valid, "issue_number": 0})
    with pytest.raises(ValidationError):
        OpenInlineAgentAttemptsCommandPayload.model_validate(
            {**valid, "issue_number": 0}
        )
    with pytest.raises(ValidationError):
        OpenInlineAgentAttemptsCommandPayload.model_validate(
            {**valid, "issue_number": -1}
        )

    # Strict-int: reject string and boolean coercion at the Python layer.
    with pytest.raises(ValidationError):
        OpenInlineAgentAttemptsCommandPayload.model_validate(
            {**valid, "issue_number": "4503"}
        )
    with pytest.raises(ValidationError):
        OpenInlineAgentAttemptsCommandPayload.model_validate(
            {**valid, "issue_number": True}
        )


def test_dialog_payloads_match_ui_openapi() -> None:
    info = build_info_dialog(
        {
            "version": "1.0",
            "repo": "test/repo",
            "ui_mode": "web",
            "terminal_backend": "subprocess",
            "commit_short": "abc123",
            "max_sessions": 2,
            "active_sessions": 1,
            "completed_today": 0,
        }
    )
    _validator("InfoDialogPayload").validate(info)

    config_dialog = build_config_dialog("config: value")
    _validator("ConfigDialogPayload").validate(config_dialog)

    debug_dialog = build_debug_dialog(
        {
            "startup_options": {
                "ui_mode": "web",
                "web_port": 8080,
                "test_mode": False,
                "filtering": {},
            },
            "paused": False,
            "priority_queue": [],
            "config_path": "/tmp/config.yaml",
            "repo_root": "/tmp/repo",
        }
    )
    _validator("DebugDialogPayload").validate(debug_dialog)

    doctor_dialog = build_doctor_dialog(
        {
            "overall": "ok",
            "checks": [{"name": "health", "status": "ok", "detail": "ok"}],
        }
    )
    _validator("DoctorDialogPayload").validate(doctor_dialog)

    session_diag = build_session_diagnostics_dialog(
        42,
        {
            "manifest": {
                "session_name": "session-42",
                "started_at": "2024-01-01T00:00:00Z",
                "run_id": "run-1",
                "backend": "subprocess",
                "agent_label": "agent:web",
                "claude_session_id": "abc",
                "worktree": "/tmp/worktree",
                "follow_up_issues": [
                    {
                        "title": "Create flaky test follow-up",
                        "reason": "A flaky test was discovered while validating the assigned issue.",
                        "blocking": False,
                    }
                ],
            },
            "run_dir": "/tmp/run",
        },
    )
    _validator("SessionDiagnosticsDialogPayload").validate(session_diag)

    blocked_dialog = build_blocked_issues_dialog({"blocked_issues": [{"issue": 1}]})
    _validator("BlockedIssuesDialogPayload").validate(blocked_dialog)

    phase_dialog = build_phase_dialog(
        {"phases": [{"name": "review-1", "display_name": "Review"}]}, 12, None
    )
    _validator("PhaseDialogPayload").validate(phase_dialog)

    validation_dialog = build_validation_failure_dialog(
        42,
        {
            "manifest": {
                "session_name": "session-42",
                "worktree": "/tmp/worktree",
                "validation_record_path": ".issue-orchestrator/sessions/run-1/validation-record.json",
                "validation_stdout": ".issue-orchestrator/sessions/run-1/validation-output.log",
                "validation_stderr": ".issue-orchestrator/sessions/run-1/validation-stderr.log",
            },
            "run_dir": "/tmp/run",
            "validation_failure": {
                "reason": "Validation failed for abc123 (exit_code=2)",
                "suite": "publish_gate",
                "command": "make validate-pr",
                "exit_code": 2,
                "started_at": "2026-04-24T00:00:00Z",
                "ended_at": "2026-04-24T00:01:00Z",
                "failed_tests": ["tests/unit/test_example.py::test_breaks"],
                "stdout_excerpt": ["FAILED tests/unit/test_example.py::test_breaks"],
                "stderr_excerpt": ["make: *** [validate-pr] Error 2"],
            },
        },
    )
    _validator("ValidationFailureDialogPayload").validate(validation_dialog)


def test_issue_detail_payload_matches_ui_openapi() -> None:
    payload = build_issue_detail_view_model(
        issue_number=12,
        title="Issue #12",
        issue_url="https://github.com/test/repo/issues/12",
        events=[{"event": "session.started", "status": "started"}],
        phase_toc=[{"phase": "in_progress", "label": "In Progress"}],
        cycles=[{"cycle": 1, "status": "started", "phases": ["in_progress"]}],
    )
    _validator("IssueDetailPayload").validate(payload)
    _assert_response_view_is_timeline_view_constrained(
        payload, "IssueDetailPayload", IssueDetailPayload
    )


def _journey_event(
    event: str,
    *,
    timestamp: str,
    logical_run: int,
    logical_cycle: int,
    logical_phase: str = "coding",
    status: str = "started",
    summary: str | None = None,
    run_dir: str | None = None,
    agent: str | None = None,
) -> dict[str, object]:
    """Minimal logical-cycle-annotated event suitable for the typed pipeline."""
    return {
        "event": event,
        "timestamp": timestamp,
        "status": status,
        "logical_run": logical_run,
        "logical_cycle": logical_cycle,
        "logical_phase": logical_phase,
        "summary": summary,
        "run_dir": run_dir,
        "agent": agent,
    }


def test_issue_detail_runs_payload_uses_typed_journey_run_shape() -> None:
    """``/api/issue-detail`` exposes ``runs[].cycles[]`` as typed journey
    cycles with the new ``CycleValidationBadge`` (issue #6310 AC-1).
    The wire shape conforms to ``JourneyRunPayload`` and
    ``IssueCyclePayload``; the typed badge carries an
    ``OpenValidationDetailsCommand`` when a validation event is recorded.
    """
    from issue_orchestrator.view_models.issue_detail import IssueStoryContext

    events = [
        _journey_event(
            "session.started",
            timestamp="2026-04-21T10:00:00Z",
            logical_run=1,
            logical_cycle=1,
            run_dir="/tmp/run-1",
            agent="agent:backend",
        ),
        _journey_event(
            "validation.passed",
            timestamp="2026-04-21T10:05:00Z",
            logical_run=1,
            logical_cycle=1,
            logical_phase="coding",
            run_dir="/tmp/run-1",
            status="completed",
        ),
        _journey_event(
            "session.completed",
            timestamp="2026-04-21T10:06:00Z",
            logical_run=1,
            logical_cycle=1,
            logical_phase="coding",
            status="completed",
        ),
    ]
    payload = build_issue_detail_view_model(
        issue_number=4124,
        title="Issue #4124",
        issue_url="https://github.com/test/repo/issues/4124",
        events=events,
        phase_toc=[],
        cycles=[],
        context=IssueStoryContext(flow_stage="in_progress"),
    )
    _validator("IssueDetailPayload").validate(payload)

    runs = payload["runs"]
    assert len(runs) == 1
    run = runs[0]
    # JourneyRun typed shape
    assert run["run_number"] == 1
    assert run["run_label"] == "Run 1"
    assert run["reset_from_scratch"] is False
    cycles = run["cycles"]
    assert len(cycles) == 1
    cycle = cycles[0]
    # IssueCycle journey-overlay fields are populated (not None) because the
    # journey pipeline ran with an ``IssueProjectionContext``.
    assert cycle["lifecycle"] == 1
    assert cycle["iteration"] == 1
    assert cycle["cycle_label"] == "Cycle 1"
    assert cycle["agent"] == "backend"
    assert isinstance(cycle["steps"], list) and cycle["steps"]
    assert isinstance(cycle["phase_groups"], list) and cycle["phase_groups"]
    # Typed CycleValidationBadge with the typed command
    badge = cycle["validation"]
    assert badge is not None
    assert badge["state"] == "passed"
    assert badge["command"] is not None
    assert badge["command"]["kind"] == "open_validation_details"
    assert badge["command"]["issue_number"] == 4124
    assert badge["command"]["run_dir"] == "/tmp/run-1"


def test_issue_detail_runs_payload_rejects_arbitrary_journey_run_dicts() -> None:
    """Untyped journey run dicts no longer satisfy ``IssueDetailPayload`` —
    the public drawer field is genuinely typed (issue #6310 AC-1)."""
    payload = build_issue_detail_view_model(
        issue_number=12,
        title="Issue #12",
        issue_url="https://github.com/test/repo/issues/12",
        events=[{"event": "session.started", "status": "started"}],
        phase_toc=[],
        cycles=[],
    )
    # Inject a typed-violating run; validation must reject.
    payload["runs"] = [{"not_a_journey_run": True}]
    errors = list(_validator("IssueDetailPayload").iter_errors(payload))
    assert errors, "untyped runs dict should fail schema validation"
    messages = _schema_error_messages(errors)
    assert (
        "not_a_journey_run" in messages
        or "run_number" in messages
        or "is a required property" in messages
    ), f"unexpected validation message: {messages}"


def test_e2e_linked_issue_lifecycle_cycles_leave_journey_fields_null() -> None:
    """E2E ``linked_issue_lifecycles[].cycles[]`` has no journey context —
    the typed ``IssueCycle`` reports journey fields as ``None``, not
    sentinel placeholders (issue #6310 AC-1 / Blocker 3)."""
    from issue_orchestrator.view_models.lifecycle_projection import (
        project_e2e_suite_lifecycle_container_for_run,
    )

    container = project_e2e_suite_lifecycle_container_for_run(
        run_id=88,
        events=[
            {
                "event": "e2e.run_started",
                "timestamp": "2026-04-21T11:00:00Z",
                "run_id": 88,
            },
            {
                "event": "e2e.test_started",
                "timestamp": "2026-04-21T11:00:05Z",
                "nodeid": "tests/e2e/test_x.py::test_y",
                "run_id": 88,
            },
            {
                "event": "e2e.test_completed",
                "timestamp": "2026-04-21T11:00:25Z",
                "nodeid": "tests/e2e/test_x.py::test_y",
                "status": "passed",
                "run_id": 88,
            },
            {
                "event": "e2e.run_finished",
                "timestamp": "2026-04-21T11:00:30Z",
                "run_id": 88,
                "status": "passed",
            },
        ],
        agent_events=[
            {
                "event": "session.started",
                "issue_number": 4001,
                "timestamp": "2026-04-21T11:01:00Z",
            },
            {
                "event": "session.completed",
                "issue_number": 4001,
                "timestamp": "2026-04-21T11:02:00Z",
                "status": "completed",
            },
        ],
    )
    payload = container.model_dump(mode="json")

    # Drill into the typed linked-lifecycle cycle for issue 4001.
    runs = payload["runs"]
    linked_lifecycles = runs[0]["e2e_run"]["linked_issue_lifecycles"]
    assert linked_lifecycles, "expected at least one linked-issue lifecycle"
    linked_cycle = linked_lifecycles[0]["cycles"][0]

    # Journey fields are explicitly null (not 0 / "" / False placeholders).
    for journey_field in (
        "lifecycle",
        "iteration",
        "timestamp",
        "agent",
        "reviewer_agent",
        "retry_count",
        "reset_from_scratch",
        "cycle_label",
        "time_label",
        "expanded",
        "artifacts",
        "validation",
    ):
        assert linked_cycle[journey_field] is None, (
            f"E2E linked cycle's {journey_field} must be null when no journey "
            f"context is threaded; got {linked_cycle[journey_field]!r}"
        )
    # Empty journey collections, not absent.
    assert linked_cycle["session_run_ids"] == []
    assert linked_cycle["steps"] == []
    assert linked_cycle["phase_groups"] == []


def test_e2e_run_timeline_payload_matches_ui_openapi() -> None:
    event = _e2e_timeline_event(
        issue_affordances=[{"issue_number": 12, "run_id": 88, "label": "fixture"}],
    )
    payload = {
        "events": [event],
        "phase_toc": [{"phase": "execution", "label": "Execution"}],
        "cycles": [_e2e_timeline_cycle(event)],
        "issue_affordances": [{"issue_number": 12, "run_id": 88}],
        "lifecycle": _e2e_container().model_dump(mode="json"),
    }

    _validator("E2ERunTimelinePayload").validate(payload)
    E2ERunTimelinePayload.model_validate(payload)


def test_e2e_run_timeline_payload_rejects_untyped_aggregate_fields() -> None:
    event = _e2e_timeline_event(unexpected_event_field=True)
    payload = {
        "events": [event],
        "phase_toc": [{"phase": "execution", "label": "Execution"}],
        "cycles": [],
        "issue_affordances": [{"issue_number": 12, "run_id": 88}],
        "lifecycle": _e2e_container().model_dump(mode="json"),
    }

    errors = list(_validator("E2ERunTimelinePayload").iter_errors(payload))

    assert any(
        "unexpected_event_field" in error.message
        and "Additional properties" in error.message
        for error in errors
    )


def test_e2e_run_timeline_payload_rejects_untyped_cycle_and_affordance_fields() -> None:
    event = _e2e_timeline_event()
    payload = {
        "events": [event],
        "phase_toc": [{"phase": "execution", "label": "Execution"}],
        "cycles": [_e2e_timeline_cycle(event) | {"unexpected_cycle_field": True}],
        "issue_affordances": [
            {"issue_number": 12, "run_id": 88, "unexpected_affordance_field": True},
        ],
        "lifecycle": _e2e_container().model_dump(mode="json"),
    }

    errors = list(_validator("E2ERunTimelinePayload").iter_errors(payload))
    messages = _schema_error_messages(errors)

    assert "unexpected_cycle_field" in messages
    assert "unexpected_affordance_field" in messages


def test_e2e_run_detail_payload_matches_ui_openapi() -> None:
    payload = build_issue_detail_view_model(
        issue_number="e2e-run:88",
        title="E2E Run #88",
        issue_url="",
        events=[_e2e_timeline_event()],
        phase_toc=[{"phase": "execution", "label": "Execution"}],
        cycles=[],
    )
    payload["run"] = {
        "id": 88,
        "orchestrator_id": "test-orch",
        "started_at": "2026-04-21T11:00:00Z",
        "finished_at": "2026-04-21T11:10:00Z",
        "status": "passed",
        "exit_code": 0,
        "duration_seconds": 600.0,
        "pytest_args": ["tests/e2e", "-v"],
        "command": ["pytest", "tests/e2e", "-v"],
        "runner_kind": "pytest",
        "commit_sha": "abc123",
        "branch": "main",
        "log_path": "/tmp/run.log",
        "log_excerpt": ["pytest started", "1 passed"],
        "artifacts_dir": "/tmp/e2e-artifacts/run-88",
        "total_tests": 1,
        "current_test": None,
    }
    payload["results_summary"] = {
        "untriaged": 0,
        "has_issue": 0,
        "flaky": 0,
        "fixed": 0,
        "passed": 1,
        "quarantined": 0,
        "skipped": 0,
        "total": 1,
    }
    payload["results_by_category"] = {
        "untriaged": [],
        "has_issue": [],
        "flaky": [],
        "fixed": [],
        "passed": [
            {
                "nodeid": "tests/e2e/test_example.py::test_passes",
                "case_id": "tests/e2e/test_example.py::test_passes",
                "label": "test_passes",
                "display_name": "test_passes",
                "suite_name": "tests.e2e.test_example",
                "result_source": "junit_xml",
                "outcome": "passed",
                "duration_seconds": 1.2,
                "longrepr": None,
                "failure_summary": None,
                "retry_outcome": None,
                "is_quarantined": False,
                "updated_at": "2026-04-21T11:01:00Z",
                "history": [],
                "existing_issue": None,
                "category": "healthy",
                "result_category": "passed",
                "flip_rate": 0.0,
                "flip_rate_percent": 0.0,
                "is_likely_flaky": False,
                "captured_output": {
                    "stdout_available": True,
                    "stderr_available": False,
                },
            }
        ],
        "quarantined": [],
        "skipped": [],
    }
    payload["artifacts"] = [
        {"kind": "raw_log", "label": "Raw Output", "path": "/tmp/run.log"},
        {
            "kind": "junit_xml",
            "label": "JUnit XML",
            "path": "/tmp/e2e-artifacts/run-88/junit.xml",
        },
    ]
    payload["reports"] = [
        {
            "kind": "junit_xml",
            "label": "JUnit XML",
            "path": "/tmp/e2e-artifacts/run-88/junit.xml",
        },
    ]
    payload["artifact_diagnostic"] = {
        "state": "collected",
        "collected_count": 1,
        "configured_glob_count": 2,
    }
    payload["issue_affordances"] = [{"issue_number": 12, "run_id": 88}]
    payload["lifecycle"] = _e2e_container().model_dump(mode="json")

    _validator("E2ERunDetailPayload").validate(payload)
    E2ERunDetailPayload.model_validate(payload)
    _assert_response_view_is_timeline_view_constrained(
        payload, "E2ERunDetailPayload", E2ERunDetailPayload
    )


def test_issue_detail_action_payload_accepts_null_optional_url() -> None:
    payload = {"id": "focus", "label": "Focus", "url": None}

    _validator("IssueDetailActionPayload").validate(payload)
    IssueDetailActionPayload.model_validate(payload)


def test_e2e_run_detail_payload_rejects_untyped_detail_fields() -> None:
    payload = build_issue_detail_view_model(
        issue_number="e2e-run:88",
        title="E2E Run #88",
        issue_url="",
        events=[_e2e_timeline_event()],
        phase_toc=[{"phase": "execution", "label": "Execution"}],
        cycles=[],
    )
    payload["run"] = {
        "id": 88,
        "orchestrator_id": "test-orch",
        "started_at": "2026-04-21T11:00:00Z",
        "finished_at": "2026-04-21T11:10:00Z",
        "status": "passed",
        "exit_code": 0,
        "duration_seconds": 600.0,
        "pytest_args": ["tests/e2e", "-v"],
        "command": ["pytest", "tests/e2e", "-v"],
        "runner_kind": "pytest",
        "commit_sha": "abc123",
        "branch": "main",
        "log_path": "/tmp/run.log",
        "log_excerpt": ["pytest started", "1 passed"],
        "artifacts_dir": "/tmp/e2e-artifacts/run-88",
        "total_tests": 1,
        "current_test": None,
    }
    payload["results_summary"] = {
        "untriaged": 0,
        "has_issue": 0,
        "flaky": 0,
        "fixed": 0,
        "passed": 1,
        "quarantined": 0,
        "skipped": 0,
        "total": 1,
    }
    payload["results_by_category"] = {
        "untriaged": [],
        "has_issue": [],
        "flaky": [],
        "fixed": [],
        "passed": [],
        "quarantined": [],
        "skipped": [],
    }
    payload["artifacts"] = []
    payload["reports"] = []
    payload["artifact_diagnostic"] = {
        "state": "not_configured",
        "collected_count": 0,
        "configured_glob_count": 0,
    }
    payload["issue_affordances"] = [{"issue_number": 12, "run_id": 88}]
    payload["lifecycle"] = _e2e_container().model_dump(mode="json")
    payload["actions"] = [{"id": "retry", "label": "Retry"}]
    payload["summary"]["unexpected_summary_field"] = True
    payload["actions"][0]["unexpected_action_field"] = True
    payload["blocked_detail"] = {
        "reason": "Blocked",
        "labels": ["blocked"],
        "rework_info": None,
        "event_summary": "waiting",
        "unexpected_blocked_detail_field": True,
    }

    errors = list(_validator("E2ERunDetailPayload").iter_errors(payload))
    messages = _schema_error_messages(errors)

    assert "unexpected_summary_field" in messages
    assert "unexpected_action_field" in messages
    assert "unexpected_blocked_detail_field" in messages


def test_lifecycle_dashboard_container_payload_matches_ui_openapi() -> None:
    container = DashboardTimelineContainer(
        subject=TimelineSubject(kind="dashboard", id="dashboard", label="Dashboard"),
        current=DashboardIteration(
            subject=TimelineSubject(
                kind="dashboard",
                id="current",
                label="Current Dashboard",
            ),
            issue_lifecycles=(_issue_lifecycle(12),),
        ),
    )

    _validator("LifecycleTimelineContainerPayload").validate(
        container.model_dump(mode="json")
    )


def test_lifecycle_e2e_container_payload_matches_ui_openapi() -> None:
    _validator("LifecycleTimelineContainerPayload").validate(
        _e2e_container().model_dump(mode="json")
    )


def _e2e_container() -> E2ESuiteTimelineContainer:
    run = E2ERunLifecycle(
        run_id=88,
        started_at="2026-04-21T11:00:00Z",
        completed_at="2026-04-21T11:02:00Z",
        tests=(
            PassedE2ETestExecution(
                nodeid="tests/e2e/test_example.py::test_passes",
                started_at="2026-04-21T11:00:00Z",
                completed_at="2026-04-21T11:01:00Z",
                commands=(ShowEventDetailsCommand(event_ref="event:e2e-test"),),
            ),
        ),
        linked_issue_lifecycles=(_issue_lifecycle(12),),
    )
    return E2ESuiteTimelineContainer(
        subject=TimelineSubject(kind="e2e_suite", id="suite", label="E2E Suite"),
        runs=(
            E2ERunIteration(
                subject=TimelineSubject(kind="e2e_run", id="88", label="Run #88"),
                e2e_run=run,
            ),
        ),
    )


def _issue_lifecycle(issue_number: int) -> IssueLifecycle:
    return IssueLifecycle(
        issue_number=issue_number,
        title=f"Issue #{issue_number}",
        cycles=(
            IssueCycle(
                cycle_number=1,
                coder=CompletedCodingAttempt(
                    issue_number=issue_number,
                    agent=AgentIdentity(name="codex", role="coder"),
                    started_at="2026-04-21T10:00:00Z",
                    completed_at="2026-04-21T10:10:00Z",
                    completion_record=CompletionRecordEvidence(
                        path=f"/runs/issue-{issue_number}/completion-record.json",
                    ),
                    validation=ValidationPassed(
                        command="pytest tests/unit -q",
                        record_path=f"/runs/issue-{issue_number}/validation.json",
                        details_command=OpenValidationDetailsCommand(
                            issue_number=issue_number,
                            run_dir=f"/runs/issue-{issue_number}",
                        ),
                    ),
                    session_recording=SessionRecordingUnavailable(
                        reason="fixture has no recording",
                    ),
                    commands=(
                        ShowEventDetailsCommand(
                            event_ref=f"event:issue:{issue_number}"
                        ),
                        OpenCompletionRecordCommand(
                            path=f"/runs/issue-{issue_number}/completion-record.json",
                        ),
                    ),
                ),
                review=ReviewNotReached(reason="not_required"),
                outcome=_ob("Completed"),
            ),
        ),
    )


def _stacked_view_model_dict() -> dict:
    """A real dashboard payload whose queued flow card is a stacked successor."""
    config = _make_config()
    config.agents = {"agent:web": _make_agent_config()}
    issue = Issue(
        number=201,
        title="Stacked successor",
        labels=["agent:web"],
        body="Stack-after: #5",
    )
    dep = Dependency(
        issue_number=5, mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED
    )
    state = OrchestratorState(
        startup_status="complete",
        cached_queue_issues=[issue],
        dependency_gate_snapshot=DependencyGateSnapshot(
            reports={201: build_gate_report(201, [dep])}
        ),
    )
    view_model = build_dashboard_view_model(
        _OrchestratorStub(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        queue_page=1,
        active_tab="flow",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )
    return view_model.to_dict()


def _stacked_flow_card(payload: dict) -> dict:
    queued = next(c for c in payload["flow_columns"] if c["id"] == "queued")
    return next(i for i in queued["items"] if i["issue_number"] == 201)


def test_stack_dashboard_card_matches_ui_openapi() -> None:
    # #6597 F2: the dashboard card's stack fields are strictly typed on the
    # envelope, not passed through as generic extras.
    payload = _stacked_view_model_dict()

    _validator("DashboardViewModelPayload").validate(payload)

    card = _stacked_flow_card(payload)
    assert card["stack_dependency"] is not None
    assert card["stack_dependency"]["has_stack_edges"] is True
    assert card["stack_chip"] is not None
    assert card["stack_chip"]["status_text"]
    assert card["stack_signal"]


def test_flow_column_hidden_count_is_required_by_ui_openapi() -> None:
    payload = _stacked_view_model_dict()

    queued = next(column for column in payload["flow_columns"] if column["id"] == "queued")
    assert queued["hidden_count"] == 0
    del queued["hidden_count"]

    with pytest.raises(JsonSchemaValidationError):
        _validator("DashboardViewModelPayload").validate(payload)


def test_malformed_stack_dependency_rejected_by_ui_openapi() -> None:
    payload = _stacked_view_model_dict()
    # has_stack_edges must be a bool and required fields are missing, so this
    # violates the strict StackDependencyGateViewPayload the card field references.
    _stacked_flow_card(payload)["stack_dependency"] = {"has_stack_edges": "yes"}

    with pytest.raises(JsonSchemaValidationError):
        _validator("DashboardViewModelPayload").validate(payload)


def test_malformed_stack_chip_rejected_by_ui_openapi() -> None:
    payload = _stacked_view_model_dict()
    # tone/mode_label/status_text/title are all required on the strict
    # StackChipViewPayload; a missing required key must fail validation.
    _stacked_flow_card(payload)["stack_chip"] = {"tone": "blocked"}

    with pytest.raises(JsonSchemaValidationError):
        _validator("DashboardViewModelPayload").validate(payload)
