"""Behavior tests for the repository setup command."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.control.repository_setup import RepositorySetupCommand
from issue_orchestrator.entrypoints.setup_wizard_common import (
    write_config,
    write_missing_setup_prompts,
)
from issue_orchestrator.infra.config import Config


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
        "repo_name": "owner/repo",
        "worker_agent_label": "agent:dev",
        "model": "sonnet",
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        RepositorySetupCommand(**values)
