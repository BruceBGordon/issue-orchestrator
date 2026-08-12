"""Tests for the execution-owned repository setup filesystem adapter."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from issue_orchestrator.control.repository_setup import RepositorySetupCommand
from issue_orchestrator.domain.repository_config_name import RepositoryConfigName
from issue_orchestrator.execution.repository_setup_files import (
    RepositorySetupFileSystemAdapter,
)
from issue_orchestrator.execution.repository_setup_github_authorization import (
    repository_setup_github_authorization_codec,
)
from issue_orchestrator.infra.config import Config, get_config_dir
from issue_orchestrator.infra.config_paths import get_mode_dir
from issue_orchestrator.ports.repository_setup import RepositorySetupFileSystemError
from issue_orchestrator.ports.repository_setup import RepositorySetupNamedConfig


def _command(repo_root: Path) -> RepositorySetupCommand:
    return RepositorySetupCommand(
        repo_root=repo_root,
        repo_name="owner/repo",
        worker_agent_label="agent:dev",
        model="sonnet",
        validation_quick_command="make test-quick",
        validation_publish_command="make validate",
    )


def test_setup_file_adapter_plans_and_writes_runnable_contained_artifacts(
    tmp_path: Path,
) -> None:
    command = _command(tmp_path)
    adapter = RepositorySetupFileSystemAdapter()

    plan = adapter.plan(
        repo_root=tmp_path,
        config_target=RepositorySetupNamedConfig(command.config_name),
        config=command.build_config(repository_setup_github_authorization_codec),
        include_prompts=True,
    )

    config_file = next(file for file in plan.files if file.kind == "config")
    assert config_file.path.parent.resolve() == get_mode_dir(
        tmp_path, "default"
    ).resolve()
    assert config_file.path.name == "default.yaml"
    assert {file.agent for file in plan.files if file.kind == "prompt"} == {
        "agent:dev",
        "agent:reviewer",
        "agent:tech-lead",
    }

    written = adapter.apply(plan)

    assert written == tuple(file.path for file in plan.files)
    assert Config.load(config_file.path).validate() == []


def test_setup_command_choice_plans_internal_reviewer_instructions(
    tmp_path: Path,
) -> None:
    command = RepositorySetupCommand(
        repo_root=tmp_path,
        repo_name="owner/repo",
        worker_agent_label="agent:dev",
        model="sonnet",
        validation_quick_command="make test-quick",
        validation_publish_command="make validate",
        configure_internal_reviewer=True,
    )

    plan = RepositorySetupFileSystemAdapter().plan(
        repo_root=tmp_path,
        config_target=RepositorySetupNamedConfig(command.config_name),
        config=command.build_config(repository_setup_github_authorization_codec),
        include_prompts=True,
    )

    internal_prompt = next(
        file for file in plan.files if file.agent == "internal-review"
    )
    assert internal_prompt.path == tmp_path / ".io" / "internal-review.md"
    assert "Return exactly one conversational verdict" in internal_prompt.content


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
            config=command.build_config(repository_setup_github_authorization_codec),
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
        config=_command(tmp_path).build_config(
            repository_setup_github_authorization_codec
        ),
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


@pytest.mark.parametrize("failure_stage", ["write", "close"])
def test_setup_file_adapter_does_not_publish_incomplete_create(
    tmp_path: Path,
    failure_stage: str,
) -> None:
    adapter = RepositorySetupFileSystemAdapter()
    plan = adapter.plan(
        repo_root=tmp_path,
        config_target=RepositorySetupNamedConfig(RepositoryConfigName("default")),
        config=_command(tmp_path).build_config(
            repository_setup_github_authorization_codec
        ),
        include_prompts=False,
    )
    config_file = plan.files[0]
    real_fdopen = os.fdopen

    class FailingFile:
        def __init__(self, fd: int, mode: str) -> None:
            self._file = real_fdopen(fd, mode)

        def __enter__(self) -> FailingFile:
            self._file.__enter__()
            return self

        def write(self, payload: bytes) -> int:
            if failure_stage == "write":
                self._file.write(payload[:5])
                raise OSError("injected mid-write failure")
            return self._file.write(payload)

        def flush(self) -> None:
            self._file.flush()

        def fileno(self) -> int:
            return self._file.fileno()

        def __exit__(self, *args: object) -> bool:
            result = self._file.__exit__(*args)
            if failure_stage == "close" and args[0] is None:
                raise OSError("injected close failure")
            return bool(result)

    with (
        patch(
            "issue_orchestrator.infra.atomic_io.os.fdopen",
            side_effect=FailingFile,
        ),
        pytest.raises(RepositorySetupFileSystemError) as exc_info,
    ):
        adapter.apply(plan)

    assert exc_info.value.applied_paths == ()
    assert not config_file.path.exists()
    assert list(config_file.path.parent.glob(f".{config_file.path.name}.*.tmp")) == []


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
        config=_command(tmp_path).build_config(
            repository_setup_github_authorization_codec
        ),
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
    config = _command(tmp_path).build_config(
        repository_setup_github_authorization_codec
    )
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


def test_setup_file_adapter_plans_enabled_internal_review_instructions(
    tmp_path: Path,
) -> None:
    config = _command(tmp_path).build_config(
        repository_setup_github_authorization_codec
    )
    config["review"]["internal"] = {
        "enabled": True,
        "max_rounds": 5,
        "instructions": ".io/internal-review.md",
    }

    plan = RepositorySetupFileSystemAdapter().plan(
        repo_root=tmp_path,
        config_target=RepositorySetupNamedConfig(RepositoryConfigName("default")),
        config=config,
        include_prompts=True,
    )

    internal = [file for file in plan.files if file.agent == "internal-review"]
    assert len(internal) == 1
    assert internal[0].path == (tmp_path / ".io" / "internal-review.md").resolve()
    assert "Spawn exactly one internal reviewer" in internal[0].content
    assert "Return exactly one conversational verdict" in internal[0].content


@pytest.mark.parametrize("instructions", ["../outside.md", "/tmp/outside.md"])
def test_setup_file_adapter_rejects_internal_review_prompt_outside_repository(
    tmp_path: Path,
    instructions: str,
) -> None:
    config = _command(tmp_path).build_config(
        repository_setup_github_authorization_codec
    )
    config["review"]["internal"] = {
        "enabled": True,
        "instructions": instructions,
    }

    with pytest.raises(ValueError, match="repository-relative|inside"):
        RepositorySetupFileSystemAdapter().plan(
            repo_root=tmp_path,
            config_target=RepositorySetupNamedConfig(RepositoryConfigName("default")),
            config=config,
            include_prompts=True,
        )


def test_setup_file_adapter_rejects_internal_review_prompt_directory(
    tmp_path: Path,
) -> None:
    config = _command(tmp_path).build_config(
        repository_setup_github_authorization_codec
    )
    (tmp_path / ".io").mkdir()
    config["review"]["internal"] = {
        "enabled": True,
        "instructions": " .io ",
    }

    with pytest.raises(ValueError, match="must reference a file"):
        RepositorySetupFileSystemAdapter().plan(
            repo_root=tmp_path,
            config_target=RepositorySetupNamedConfig(RepositoryConfigName("default")),
            config=config,
            include_prompts=True,
        )
