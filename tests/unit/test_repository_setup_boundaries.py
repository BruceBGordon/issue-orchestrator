"""Cross-surface boundary tests for repository setup."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.contracts.ui_openapi_models import (
    RepositorySetupCommandPayload,
)
from issue_orchestrator.control.repository_setup import (
    RepositorySetupExecutionError,
)
from issue_orchestrator.domain.repository_setup_auth import (
    RepositorySetupGitHubAuthorization,
)
from issue_orchestrator.entrypoints.bootstrap_repository_setup import (
    build_repository_setup_owner,
)
from issue_orchestrator.entrypoints.cli_tools.setup_wizard_repository_setup import (
    apply_cli_repository_setup,
    build_cli_repository_setup_request,
)
from issue_orchestrator.entrypoints.control_api_setup_routes import (
    repository_setup_request_from_payload,
)
from issue_orchestrator.entrypoints.control_api_setup_support import (
    ControlApiSetupDependencies,
)
from issue_orchestrator.infra.config import get_config_path
from issue_orchestrator.ports.repository_setup import (
    RepositorySetupExplicitConfig,
    RepositorySetupGitHubVerification,
    RepositorySetupValidationDefaults,
)


class _Prompter:
    def __init__(self) -> None:
        self.printed: list[str] = []

    def print(self, message: str) -> None:
        self.printed.append(message)

    def input(self, question: str, default: str = "") -> str:
        raise AssertionError((question, default))

    def yes_no(self, question: str, default: bool = True) -> bool:
        raise AssertionError((question, default))

    def choice(
        self,
        question: str,
        choices: list[str],
        allow_custom: bool = False,
    ) -> str:
        raise AssertionError((question, choices, allow_custom))


def test_cli_and_http_produce_equivalent_owner_request_and_preview(
    tmp_path: Path,
) -> None:
    host = MagicMock()
    host.list_labels.return_value = []

    def repository_host_factory(
        repo_name: str,
        _authorization: RepositorySetupGitHubAuthorization,
    ) -> MagicMock:
        assert repo_name == "owner/repo"
        return host

    verification = RepositorySetupGitHubVerification(
        identity="setup-user",
        repository="owner/repo",
        auth_kind="personal",
        source="Environment variable ISSUE_ORCH_GITHUB_TOKEN",
        normalized_authorization=RepositorySetupGitHubAuthorization(kind="detected"),
    )
    owner = build_repository_setup_owner(
        repository_host_factory,
        lambda _repo_name, _authorization: verification,
    )
    dependencies = ControlApiSetupDependencies(
        validate_repo_root=lambda raw: Path(raw).resolve() if raw else None,
        setup_owner=owner,
        github_token_store=lambda authorization, *, repo: (
            RepositorySetupGitHubAuthorization(
                kind="personal",
                keyring_service="issue-orchestrator",
                keyring_username=f"github-token:{repo}",
                api_url=authorization.api_url,
                http_timeout_seconds=authorization.http_timeout_seconds,
            )
        ),
        validation_detector=lambda _repo_root: RepositorySetupValidationDefaults(
            "make test",
            "make validate",
            "Makefile targets",
        ),
    )
    payload = RepositorySetupCommandPayload(
        repo_root=str(tmp_path),
        repo_name="owner/repo",
        worker_agent_label="agent:dev",
        model="sonnet",
        effort="high",
        configure_reviewer=True,
        reviewer_model="opus",
        reviewer_effort="max",
        configure_internal_reviewer=True,
        internal_review_max_rounds=4,
        internal_review_instructions=".io/internal-review.md",
        validation_quick_command="make test-quick",
        validation_publish_command="make validate",
        worktree_base="../worktrees/repo",
        github_authorization={
            "kind": "detected",
            "api_url": "https://api.github.com",
            "http_timeout_seconds": 20,
        },
        configure_tech_lead=True,
        tech_lead_model="sonnet",
        tech_lead_effort="high",
        tech_lead_review_threshold=1,
        config_name="default",
        create_prompts=True,
        create_labels=True,
        replace_existing=False,
    )

    http_request = repository_setup_request_from_payload(payload, dependencies)
    cli_request = build_cli_repository_setup_request(
        owner=owner,
        config=http_request.config,
        repo_root=tmp_path,
        config_path=get_config_path(tmp_path, "default.yaml"),
    )

    assert cli_request == http_request
    assert owner.preview(cli_request) == owner.preview(http_request)
    assert any(
        planned.agent == "internal-review"
        for planned in owner.preview(http_request).files
    )
    assert "priority:high" in {
        name for name, _color, _description in owner.preview(cli_request).labels
    }


def test_cli_request_preserves_explicit_legacy_config_path(tmp_path: Path) -> None:
    config_path = tmp_path / ".issue-orchestrator.yaml"

    request = build_cli_repository_setup_request(
        owner=build_repository_setup_owner(MagicMock(), MagicMock()),
        config={"repo": {"name": "owner/repo"}, "agents": {}},
        repo_root=tmp_path,
        config_path=config_path,
    )

    assert request.config_target == RepositorySetupExplicitConfig(config_path.resolve())


def test_cli_surfaces_owner_partial_outcome() -> None:
    owner = MagicMock()
    owner.execute.side_effect = RepositorySetupExecutionError(
        stage="labels",
        detail="GitHub unavailable",
        applied_files=(Path("/repo/config.yaml"),),
        created_labels=("agent:dev",),
    )
    prompter = _Prompter()
    request = build_cli_repository_setup_request(
        owner=owner,
        config={"repo": {"name": "owner/repo"}, "agents": {}},
        repo_root=Path("/repo"),
        config_path=Path("/repo/config.yaml"),
    )

    with pytest.raises(SystemExit):
        apply_cli_repository_setup(
            owner=owner,
            request=request,
            prompter=prompter,
        )

    output = "\n".join(prompter.printed)
    assert "failed during labels" in output
    assert "Already written: /repo/config.yaml" in output
    assert "Already created label: agent:dev" in output
