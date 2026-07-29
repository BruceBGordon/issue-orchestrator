"""Tests for the execution-owned repository setup filesystem adapter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from issue_orchestrator.control.repository_setup import RepositorySetupCommand
from issue_orchestrator.domain.repository_config_name import RepositoryConfigName
from issue_orchestrator.execution.repository_setup_files import (
    RepositorySetupFileSystemAdapter,
)
from issue_orchestrator.infra.config import Config, get_config_dir
from issue_orchestrator.ports.repository_setup import RepositorySetupFileSystemError
from issue_orchestrator.ports.repository_setup import RepositorySetupNamedConfig


def _command(repo_root: Path) -> RepositorySetupCommand:
    return RepositorySetupCommand(
        repo_root=repo_root,
        repo_name="owner/repo",
        worker_agent_label="agent:dev",
        model="sonnet",
    )


def test_setup_file_adapter_plans_and_writes_runnable_contained_artifacts(
    tmp_path: Path,
) -> None:
    command = _command(tmp_path)
    adapter = RepositorySetupFileSystemAdapter()

    plan = adapter.plan(
        repo_root=tmp_path,
        config_target=RepositorySetupNamedConfig(command.config_name),
        config=command.build_config(),
        include_prompts=True,
    )

    config_file = next(file for file in plan.files if file.kind == "config")
    assert config_file.path.parent.resolve() == get_config_dir(tmp_path).resolve()
    assert config_file.path.name == "default.yaml"
    assert {file.agent for file in plan.files if file.kind == "prompt"} == {
        "agent:dev",
        "agent:tech-lead",
    }

    written = adapter.apply(plan)

    assert written == tuple(file.path for file in plan.files)
    assert Config.load(config_file.path).validate() == []


def test_setup_file_adapter_revalidates_forged_config_name(
    tmp_path: Path,
) -> None:
    forged = object.__new__(RepositoryConfigName)
    object.__setattr__(forged, "value", "../../escaped.yaml")
    command = _command(tmp_path)

    with pytest.raises(ValueError, match="Invalid config_name"):
        RepositorySetupFileSystemAdapter().plan(
            repo_root=tmp_path,
            config_target=RepositorySetupNamedConfig(forged),
            config=command.build_config(),
            include_prompts=False,
        )

    assert not (tmp_path.parent / "escaped.yaml").exists()


def test_setup_file_adapter_refuses_create_when_target_appears_after_plan(
    tmp_path: Path,
) -> None:
    adapter = RepositorySetupFileSystemAdapter()
    plan = adapter.plan(
        repo_root=tmp_path,
        config_target=RepositorySetupNamedConfig(RepositoryConfigName("default")),
        config=_command(tmp_path).build_config(),
        include_prompts=False,
    )
    config_file = plan.files[0]
    assert config_file.action == "create"
    config_file.path.parent.mkdir(parents=True)
    config_file.path.write_text("sentinel", encoding="utf-8")

    with pytest.raises(RepositorySetupFileSystemError) as exc_info:
        adapter.apply(plan)

    assert isinstance(exc_info.value.cause, FileExistsError)
    assert exc_info.value.applied_paths == ()
    assert config_file.path.read_text(encoding="utf-8") == "sentinel"


def test_setup_file_adapter_preserves_existing_file_when_atomic_replace_fails(
    tmp_path: Path,
) -> None:
    adapter = RepositorySetupFileSystemAdapter()
    config_path = get_config_dir(tmp_path) / "default.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("sentinel", encoding="utf-8")
    plan = adapter.plan(
        repo_root=tmp_path,
        config_target=RepositorySetupNamedConfig(RepositoryConfigName("default")),
        config=_command(tmp_path).build_config(),
        include_prompts=False,
    )
    assert plan.files[0].action == "overwrite"

    with (
        patch(
            "issue_orchestrator.infra.atomic_io.os.replace",
            side_effect=OSError("replace failed"),
        ),
        pytest.raises(RepositorySetupFileSystemError) as exc_info,
    ):
        adapter.apply(plan)

    assert exc_info.value.applied_paths == ()
    assert config_path.read_text(encoding="utf-8") == "sentinel"


def test_setup_file_adapter_plans_shared_prompt_target_once(
    tmp_path: Path,
) -> None:
    config = _command(tmp_path).build_config()
    config["agents"] = {
        "agent:frontend": {"prompt": ".io/shared.md"},
        "agent:backend": {"prompt": ".io/../.io/shared.md"},
    }
    adapter = RepositorySetupFileSystemAdapter()

    plan = adapter.plan(
        repo_root=tmp_path,
        config_target=RepositorySetupNamedConfig(RepositoryConfigName("default")),
        config=config,
        include_prompts=True,
    )

    prompt_files = [file for file in plan.files if file.kind == "prompt"]
    assert len(prompt_files) == 1
    assert prompt_files[0].path == (tmp_path / ".io" / "shared.md").resolve()
    assert prompt_files[0].agent == "agent:frontend"
    assert "# Frontend Agent Prompt" in prompt_files[0].content
