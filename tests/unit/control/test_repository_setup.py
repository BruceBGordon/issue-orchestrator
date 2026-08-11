"""Behavior tests for the repository setup command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.repository_setup import (
    RepositorySetupCommand,
    RepositorySetupConflictError,
    RepositorySetupExecutionError,
    RepositorySetupOwner,
    RepositorySetupRequest,
)
from issue_orchestrator.domain.repository_config_name import RepositoryConfigName
from issue_orchestrator.domain.repository_setup_auth import (
    RepositorySetupGitHubAuthorization,
)
from issue_orchestrator.execution.repository_setup_github_authorization import (
    repository_setup_github_authorization_codec,
)
from issue_orchestrator.ports.repository_setup import (
    RepositorySetupArtifactPlan,
    RepositorySetupConfigTarget,
    RepositorySetupFileSystemError,
    RepositorySetupGitHubVerification,
    RepositorySetupNamedConfig,
    RepositorySetupPlannedFile,
)

_VALIDATION_COMMANDS = {
    "validation_quick_command": "make test-quick",
    "validation_publish_command": "make validate",
}


def test_setup_command_defaults_to_complete_review_pipeline(
    tmp_path: Path,
) -> None:
    command = RepositorySetupCommand(
        repo_root=tmp_path,
        repo_name="owner/repo",
        worker_agent_label="agent:dev",
        model="sonnet",
        **_VALIDATION_COMMANDS,
    )

    config = command.build_config(repository_setup_github_authorization_codec)

    assert set(config["agents"]) == {
        "agent:dev",
        "agent:reviewer",
        "agent:tech-lead",
    }
    assert config["worktrees"]["base"] == f"../worktrees/{tmp_path.name}"
    assert config["agents"]["agent:dev"]["sandbox"] is True
    assert config["agents"]["agent:dev"]["provider_args"] == {"effort": "high"}
    assert config["agents"]["agent:reviewer"]["sandbox"] is True
    assert config["agents"]["agent:reviewer"]["provider_args"] == {"effort": "high"}
    assert config["agents"]["agent:tech-lead"]["sandbox"] is True
    assert config["agents"]["agent:tech-lead"]["provider_args"] == {
        "effort": "high"
    }
    assert config["review"]["enabled"] is True
    assert config["review"]["default"] == "agent:reviewer"
    assert config["review"]["max_rework_cycles"] == 5
    assert config["review"]["nits"] == {
        "default_policy": "surface",
        "by_agent": {},
    }
    assert config["review"]["exchange"] == {
        "mode": "via-local-loop",
        "loop": {
            "max_rounds": 10,
            "max_no_progress": 2,
            "require_validation": True,
        },
    }
    assert config["review"]["internal"] == {
        "enabled": False,
        "max_rounds": 5,
        "instructions": ".io/internal-review.md",
    }
    assert config["validation"] == {
        "quick": {
            "cmd": "make test-quick",
            "timeout_seconds": 300,
        },
        "publish": {
            "cmd": "make validate",
            "timeout_seconds": 1800,
            "dirty_check": "tracked",
        },
    }
    assert config["review"]["tech_lead_review_agent"] == "agent:tech-lead"
    assert config["review"]["tech_lead_follow_up_agent"] == "agent:dev"
    assert config["review"]["tech_lead_review_label"] == "needs-tech-lead-review"
    assert config["review"]["tech_lead_review_threshold"] == 1
    assert config["tech_lead"]["enabled"] is True


def test_setup_command_enables_internal_reviewer_with_owned_artifact(
    tmp_path: Path,
) -> None:
    config = RepositorySetupCommand(
        repo_root=tmp_path,
        repo_name="owner/repo",
        worker_agent_label="agent:dev",
        model="sonnet",
        **_VALIDATION_COMMANDS,
        configure_internal_reviewer=True,
        internal_review_max_rounds=3,
        internal_review_instructions=" .io/fast-review.md ",
    ).build_config(repository_setup_github_authorization_codec)

    assert config["review"]["internal"] == {
        "enabled": True,
        "max_rounds": 3,
        "instructions": ".io/fast-review.md",
    }


def test_setup_command_can_explicitly_disable_tech_lead(tmp_path: Path) -> None:
    command = RepositorySetupCommand(
        repo_root=tmp_path,
        repo_name="owner/repo",
        worker_agent_label="agent:dev",
        model="sonnet",
        **_VALIDATION_COMMANDS,
        configure_tech_lead=False,
    )

    config = command.build_config(repository_setup_github_authorization_codec)

    assert set(config["agents"]) == {"agent:dev", "agent:reviewer"}
    assert config["agents"]["agent:dev"]["sandbox"] is True
    assert config["review"]["enabled"] is True
    assert config["review"]["default"] == "agent:reviewer"
    assert "tech_lead_review_agent" not in config["review"]


def test_setup_command_can_explicitly_disable_reviewer_and_tech_lead(
    tmp_path: Path,
) -> None:
    command = RepositorySetupCommand(
        repo_root=tmp_path,
        repo_name="owner/repo",
        worker_agent_label="agent:dev",
        model="sonnet",
        **_VALIDATION_COMMANDS,
        configure_reviewer=False,
        configure_tech_lead=False,
    )

    config = command.build_config(repository_setup_github_authorization_codec)

    assert set(config["agents"]) == {"agent:dev"}
    assert config["review"]["enabled"] is False
    assert "default" not in config["review"]
    assert "tech_lead_review_agent" not in config["review"]


def test_setup_command_preserves_role_specific_model_effort_and_cadence(
    tmp_path: Path,
) -> None:
    config = RepositorySetupCommand(
        repo_root=tmp_path,
        repo_name="owner/repo",
        worker_agent_label="agent:dev",
        model="opus",
        **_VALIDATION_COMMANDS,
        effort="xhigh",
        reviewer_model="haiku",
        reviewer_effort="medium",
        tech_lead_model="sonnet",
        tech_lead_effort="max",
        tech_lead_review_threshold=5,
    ).build_config(repository_setup_github_authorization_codec)

    assert config["agents"]["agent:dev"]["model"] == "opus"
    assert config["agents"]["agent:dev"]["provider_args"] == {"effort": "xhigh"}
    assert config["agents"]["agent:reviewer"]["model"] == "haiku"
    assert config["agents"]["agent:reviewer"]["provider_args"] == {
        "effort": "medium"
    }
    assert config["agents"]["agent:tech-lead"]["model"] == "sonnet"
    assert config["agents"]["agent:tech-lead"]["provider_args"] == {"effort": "max"}
    assert config["review"]["tech_lead_review_threshold"] == 5


def test_setup_command_preserves_explicit_worktree_base(tmp_path: Path) -> None:
    config = RepositorySetupCommand(
        repo_root=tmp_path,
        repo_name="owner/repo",
        worker_agent_label="agent:dev",
        model="sonnet",
        **_VALIDATION_COMMANDS,
        worktree_base="../agent-worktrees/repo",
    ).build_config(repository_setup_github_authorization_codec)

    assert config["worktrees"]["base"] == "../agent-worktrees/repo"


def test_setup_command_persists_only_personal_keyring_reference(
    tmp_path: Path,
) -> None:
    authorization = RepositorySetupGitHubAuthorization(
        kind="personal",
        keyring_service="issue-orchestrator",
        keyring_username="github-token:owner/repo",
    )

    config = RepositorySetupCommand(
        repo_root=tmp_path,
        repo_name="owner/repo",
        worker_agent_label="agent:dev",
        model="sonnet",
        **_VALIDATION_COMMANDS,
        github_authorization=authorization,
    ).build_config(repository_setup_github_authorization_codec)

    assert config["repo"]["github"] == {
        "keyring_service": "issue-orchestrator",
        "keyring_username": "github-token:owner/repo",
    }
    assert "token" not in config["repo"]["github"]


def test_setup_command_persists_only_github_app_key_reference(
    tmp_path: Path,
) -> None:
    authorization = RepositorySetupGitHubAuthorization(
        kind="github_app",
        app_client_id="Iv23example",
        app_installation_id="145305179",
        app_private_key_env="ISSUE_ORCH_GITHUB_APP_PRIVATE_KEY",
    )

    config = RepositorySetupCommand(
        repo_root=tmp_path,
        repo_name="owner/repo",
        worker_agent_label="agent:dev",
        model="sonnet",
        **_VALIDATION_COMMANDS,
        github_authorization=authorization,
    ).build_config(repository_setup_github_authorization_codec)

    assert config["repo"]["github"] == {
        "app": {
            "client_id": "Iv23example",
            "installation_id": "145305179",
            "private_key_env": "ISSUE_ORCH_GITHUB_APP_PRIVATE_KEY",
        }
    }


def test_setup_request_detaches_nested_config_from_surface_mutation(
    tmp_path: Path,
) -> None:
    config = {"repo": {"name": "owner/repo"}, "agents": {"agent:dev": {}}}
    request = RepositorySetupRequest(
        repo_root=tmp_path,
        repo_name="owner/repo",
        config=config,
        github_authorization=RepositorySetupGitHubAuthorization(kind="detected"),
    )

    config["agents"]["agent:dev"]["model"] = "changed"

    assert request.config["agents"]["agent:dev"] == {}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("repo_name", "", "repo_name is required"),
        (
            "worker_agent_label",
            "developer",
            "worker_agent_label must match",
        ),
        (
            "worker_agent_label",
            "agent:",
            "worker_agent_label must match",
        ),
        (
            "worker_agent_label",
            "agent:reviewer",
            "worker_agent_label must match",
        ),
        (
            "worker_agent_label",
            "agent:tech-lead",
            "worker_agent_label must match",
        ),
        ("model", "unknown", "worker model must be one of"),
        ("effort", "unknown", "worker effort must be one of"),
        ("reviewer_model", "unknown", "reviewer model must be one of"),
        ("reviewer_effort", "unknown", "reviewer effort must be one of"),
        ("tech_lead_model", "unknown", "tech lead model must be one of"),
        ("tech_lead_effort", "unknown", "tech lead effort must be one of"),
        (
            "validation_quick_command",
            "",
            "validation_quick_command is required",
        ),
        (
            "validation_publish_command",
            "",
            "validation_publish_command is required",
        ),
        (
            "tech_lead_review_threshold",
            -1,
            "tech_lead_review_threshold must be between",
        ),
        (
            "tech_lead_review_threshold",
            51,
            "tech_lead_review_threshold must be between",
        ),
        (
            "internal_review_max_rounds",
            0,
            "internal_review_max_rounds must be between",
        ),
        (
            "internal_review_max_rounds",
            51,
            "internal_review_max_rounds must be between",
        ),
        (
            "internal_review_instructions",
            "../outside.md",
            "internal_review_instructions must be a contained",
        ),
        ("worktree_base", "", "worktree_base is required"),
    ],
)
def test_setup_command_rejects_invalid_choices(
    field: str,
    value: object,
    message: str,
) -> None:
    values = {
        "repo_root": Path("/repo"),
        "repo_name": "owner/repo",
        "worker_agent_label": "agent:dev",
        "model": "sonnet",
        **_VALIDATION_COMMANDS,
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        RepositorySetupCommand(**values)


class _FakeSetupFileSystem:
    def __init__(
        self,
        plan: RepositorySetupArtifactPlan,
        *,
        apply_error: RepositorySetupFileSystemError | None = None,
    ) -> None:
        self.plan_result = plan
        self.apply_error = apply_error
        self.apply_calls = 0
        self.planned_config_targets: list[RepositorySetupConfigTarget] = []

    def plan(
        self,
        *,
        config_target: RepositorySetupConfigTarget,
        **_kwargs,
    ) -> RepositorySetupArtifactPlan:
        self.planned_config_targets.append(config_target)
        return self.plan_result

    def apply(self, plan: RepositorySetupArtifactPlan) -> tuple[Path, ...]:
        self.apply_calls += 1
        if self.apply_error is not None:
            raise self.apply_error
        return tuple(file.path for file in plan.files)


def _artifact_plan(
    tmp_path: Path,
    *,
    config_action: str = "create",
) -> RepositorySetupArtifactPlan:
    return RepositorySetupArtifactPlan(
        config_yaml="repo:\n  name: owner/repo\n",
        files=(
            RepositorySetupPlannedFile(
                path=tmp_path / ".issue-orchestrator/config/default.yaml",
                content="repo:\n  name: owner/repo\n",
                action=config_action,
                kind="config",
            ),
            RepositorySetupPlannedFile(
                path=tmp_path / ".io/dev.md",
                content="# Dev\n",
                action="create",
                kind="prompt",
                agent="agent:dev",
            ),
        ),
    )


def _owner(
    file_system: _FakeSetupFileSystem,
    host: MagicMock,
    labels: list[tuple[str, str, str]] | None = None,
) -> RepositorySetupOwner:
    verification = RepositorySetupGitHubVerification(
        identity="setup-user",
        repository="owner/repo",
        auth_kind="personal",
        source="Environment variable ISSUE_ORCH_GITHUB_TOKEN",
        normalized_authorization=RepositorySetupGitHubAuthorization(kind="detected"),
    )
    return RepositorySetupOwner(
        file_system=file_system,
        repository_host_factory=lambda _repo_name, _authorization: host,
        github_verifier=lambda _repo_name, _authorization: verification,
        github_authorization_codec=repository_setup_github_authorization_codec,
        label_planner=lambda _config: labels or [],
    )


def _request(tmp_path: Path, **overrides) -> RepositorySetupRequest:
    values = {
        "repo_root": tmp_path,
        "repo_name": "owner/repo",
        "worker_agent_label": "agent:dev",
        "model": "sonnet",
        **_VALIDATION_COMMANDS,
        "config_name": RepositoryConfigName.default(),
    }
    values.update(overrides)
    return RepositorySetupCommand(**values).to_request(
        repository_setup_github_authorization_codec
    )


def test_setup_owner_preview_is_non_mutating(tmp_path: Path) -> None:
    file_system = _FakeSetupFileSystem(_artifact_plan(tmp_path))
    host = MagicMock()

    preview = _owner(file_system, host).preview(_request(tmp_path))

    assert preview.yaml == "repo:\n  name: owner/repo\n"
    assert preview.worktree_base == (tmp_path.parent / "worktrees" / tmp_path.name)
    assert preview.github_authorization.identity == "setup-user"
    assert [file.kind for file in preview.files] == ["config", "prompt"]
    assert file_system.apply_calls == 0
    assert file_system.planned_config_targets == [
        RepositorySetupNamedConfig(RepositoryConfigName.default())
    ]
    host.assert_not_called()


def test_setup_owner_requires_explicit_existing_config_replacement(
    tmp_path: Path,
) -> None:
    file_system = _FakeSetupFileSystem(
        _artifact_plan(tmp_path, config_action="overwrite")
    )
    host = MagicMock()

    with pytest.raises(RepositorySetupConflictError):
        _owner(file_system, host).execute(_request(tmp_path))

    assert file_system.apply_calls == 0
    host.assert_not_called()


def test_setup_owner_reports_partial_prompt_failure(tmp_path: Path) -> None:
    plan = _artifact_plan(tmp_path)
    config_path = plan.files[0].path
    file_system = _FakeSetupFileSystem(
        plan,
        apply_error=RepositorySetupFileSystemError(
            operation="write prompt",
            applied_paths=(config_path,),
            cause=OSError("disk full"),
        ),
    )

    with pytest.raises(RepositorySetupExecutionError) as error:
        _owner(file_system, MagicMock()).execute(_request(tmp_path))

    assert error.value.stage == "files"
    assert error.value.applied_files == (config_path,)
    assert "disk full" in error.value.detail


def test_setup_owner_authorization_failure_precedes_file_planning(
    tmp_path: Path,
) -> None:
    file_system = _FakeSetupFileSystem(_artifact_plan(tmp_path))
    host_factory = MagicMock()
    owner = RepositorySetupOwner(
        file_system=file_system,
        repository_host_factory=host_factory,
        github_verifier=MagicMock(side_effect=RuntimeError("token cannot access repo")),
        github_authorization_codec=repository_setup_github_authorization_codec,
        label_planner=lambda _config: [],
    )

    with pytest.raises(RepositorySetupExecutionError) as error:
        owner.execute(_request(tmp_path))

    assert error.value.stage == "authorization"
    assert "token cannot access repo" in error.value.detail
    assert file_system.planned_config_targets == []
    assert file_system.apply_calls == 0
    host_factory.assert_not_called()


def test_setup_owner_uses_selected_authorization_for_label_mutations(
    tmp_path: Path,
) -> None:
    authorization = RepositorySetupGitHubAuthorization(
        kind="personal",
        token_env="PORCHPIN_GITHUB_TOKEN",
    )
    host = MagicMock()
    host.list_labels.return_value = []
    received_authorizations = []
    verification = RepositorySetupGitHubVerification(
        identity="porchpin-owner",
        repository="owner/repo",
        auth_kind="personal",
        source="Environment variable PORCHPIN_GITHUB_TOKEN",
        normalized_authorization=authorization,
    )
    owner = RepositorySetupOwner(
        file_system=_FakeSetupFileSystem(_artifact_plan(tmp_path)),
        repository_host_factory=lambda _repo, selected: (
            received_authorizations.append(selected) or host
        ),
        github_verifier=lambda _repo, _selected: verification,
        github_authorization_codec=repository_setup_github_authorization_codec,
        label_planner=lambda _config: [("agent:dev", "1D76DB", "worker")],
    )

    owner.execute(_request(tmp_path, github_authorization=authorization))

    assert received_authorizations == [authorization]
    host.create_label.assert_called_once()


def test_setup_owner_reports_partial_label_failure(tmp_path: Path) -> None:
    plan = _artifact_plan(tmp_path)
    host = MagicMock()
    host.list_labels.return_value = []
    host.create_label.side_effect = [None, RuntimeError("GitHub unavailable")]
    labels = [
        ("agent:dev", "1D76DB", "worker"),
        ("in-progress", "5319E7", "working"),
    ]

    with pytest.raises(RepositorySetupExecutionError) as error:
        _owner(_FakeSetupFileSystem(plan), host, labels).execute(_request(tmp_path))

    assert error.value.stage == "labels"
    assert error.value.applied_files == tuple(file.path for file in plan.files)
    assert error.value.created_labels == ("agent:dev",)
    assert "GitHub unavailable" in error.value.detail


def test_setup_owner_never_mutates_duplicate_label_twice(tmp_path: Path) -> None:
    host = MagicMock()
    host.list_labels.return_value = []
    duplicate = ("code-reviewed", "0E8A16", "reviewed")
    owner = _owner(
        _FakeSetupFileSystem(_artifact_plan(tmp_path)),
        host,
        [duplicate, duplicate],
    )

    result = owner.execute(_request(tmp_path))

    assert result.created_labels == ("code-reviewed",)
    host.create_label.assert_called_once()
