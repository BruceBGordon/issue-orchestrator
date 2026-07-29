"""Tests for the execution-owned repository setup filesystem adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.control.repository_setup import RepositorySetupCommand
from issue_orchestrator.domain.repository_config_name import RepositoryConfigName
from issue_orchestrator.execution.repository_setup_files import (
    RepositorySetupFileSystemAdapter,
)
from issue_orchestrator.infra.config import Config, get_config_dir


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
        config_name=command.config_name,
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
            config_name=forged,
            config=command.build_config(),
            include_prompts=False,
        )

    assert not (tmp_path.parent / "escaped.yaml").exists()
