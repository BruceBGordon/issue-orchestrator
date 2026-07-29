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
)
from issue_orchestrator.domain.repository_config_name import RepositoryConfigName
from issue_orchestrator.entrypoints.setup_wizard_common import (
    write_config,
    write_missing_setup_prompts,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.repository_setup import (
    RepositorySetupArtifactPlan,
    RepositorySetupFileSystemError,
    RepositorySetupPlannedFile,
)


def _load_generated_config(
    tmp_path: Path,
    command: RepositorySetupCommand,
) -> Config:
    config_path = tmp_path / ".issue-orchestrator" / "config" / "default.yaml"
    config_path.parent.mkdir(parents=True)
    config = command.build_config()
    write_config(config, config_path, include_header=False)
    write_missing_setup_prompts(config, tmp_path)
    return Config.load(config_path)


def test_setup_command_defaults_to_runnable_worker_and_tech_lead_config(
    tmp_path: Path,
) -> None:
    command = RepositorySetupCommand(
        repo_root=tmp_path,
        repo_name="owner/repo",
        worker_agent_label="agent:dev",
        model="sonnet",
    )

    config = _load_generated_config(tmp_path, command)

    assert set(config.agents) == {"agent:dev", "agent:tech-lead"}
    assert config.tech_lead_review_agent == "agent:tech-lead"
    assert config.tech_lead_follow_up_agent == "agent:dev"
    assert config.tech_lead_review_label == "needs-tech-lead-review"
    assert (tmp_path / ".io" / "dev.md").is_file()
    assert (tmp_path / ".io" / "tech-lead.md").is_file()
    assert config.validate() == []


def test_setup_command_can_explicitly_disable_tech_lead(tmp_path: Path) -> None:
    command = RepositorySetupCommand(
        repo_root=tmp_path,
        repo_name="owner/repo",
        worker_agent_label="agent:dev",
        model="sonnet",
        configure_tech_lead=False,
    )

    config = _load_generated_config(tmp_path, command)

    assert set(config.agents) == {"agent:dev"}
    assert config.tech_lead_review_agent is None
    assert config.tech_lead_follow_up_agent is None
    assert not (tmp_path / ".io" / "tech-lead.md").exists()
    assert config.validate() == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("repo_name", "", "repo_name is required"),
        (
            "worker_agent_label",
            "developer",
            "worker_agent_label must start with 'agent:'",
        ),
        (
            "worker_agent_label",
            "agent:tech-lead",
            "worker_agent_label must identify a worker",
        ),
        ("model", "unknown", "model must be one of"),
    ],
)
def test_setup_command_rejects_invalid_choices(
    field: str,
    value: str,
    message: str,
) -> None:
    values = {
        "repo_root": Path("/repo"),
        "repo_name": "owner/repo",
        "worker_agent_label": "agent:dev",
        "model": "sonnet",
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

    def plan(self, **_kwargs) -> RepositorySetupArtifactPlan:
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
    return RepositorySetupOwner(
        file_system=file_system,
        repository_host_factory=lambda _repo_name: host,
        label_planner=lambda _config: labels or [],
    )


def _command(tmp_path: Path, **overrides) -> RepositorySetupCommand:
    values = {
        "repo_root": tmp_path,
        "repo_name": "owner/repo",
        "worker_agent_label": "agent:dev",
        "model": "sonnet",
        "config_name": RepositoryConfigName.default(),
    }
    values.update(overrides)
    return RepositorySetupCommand(**values)


def test_setup_owner_preview_is_non_mutating(tmp_path: Path) -> None:
    file_system = _FakeSetupFileSystem(_artifact_plan(tmp_path))
    host = MagicMock()

    preview = _owner(file_system, host).preview(_command(tmp_path))

    assert preview.yaml == "repo:\n  name: owner/repo\n"
    assert [file.kind for file in preview.files] == ["config", "prompt"]
    assert file_system.apply_calls == 0
    host.assert_not_called()


def test_setup_owner_requires_explicit_existing_config_replacement(
    tmp_path: Path,
) -> None:
    file_system = _FakeSetupFileSystem(
        _artifact_plan(tmp_path, config_action="overwrite")
    )
    host = MagicMock()

    with pytest.raises(RepositorySetupConflictError):
        _owner(file_system, host).execute(_command(tmp_path))

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
        _owner(file_system, MagicMock()).execute(_command(tmp_path))

    assert error.value.stage == "files"
    assert error.value.applied_files == (config_path,)
    assert "disk full" in error.value.detail


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
        _owner(_FakeSetupFileSystem(plan), host, labels).execute(_command(tmp_path))

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

    result = owner.execute(_command(tmp_path))

    assert result.created_labels == ("code-reviewed",)
    host.create_label.assert_called_once()
