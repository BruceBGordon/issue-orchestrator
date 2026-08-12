"""Tests for the typed runtime configuration reference."""

from pathlib import Path

from issue_orchestrator.domain.repository_launch_selection import (
    RepositoryLaunchSelection,
)
from issue_orchestrator.domain.runtime_config import RuntimeConfigReference


def test_nested_mode_runtime_environment_uses_one_typed_selection(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".issue-orchestrator/config/modes/codex/main.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("agents: {}\n")
    selection = RepositoryLaunchSelection.parse(
        mode="codex",
        config_name="main.yaml",
    )

    reference = RuntimeConfigReference(
        config_path=config_path.resolve(),
        selection=selection,
    )

    assert reference.to_env() == {
        "ISSUE_ORCHESTRATOR_CONFIG_NAME": "main.yaml",
        "ISSUE_ORCHESTRATOR_CONFIG_PATH": str(config_path.resolve()),
        "ORCHESTRATOR_CONFIG_NAME": "main.yaml",
        "ORCHESTRATOR_CONFIG_PATH": str(config_path.resolve()),
        "ISSUE_ORCHESTRATOR_MODE": "codex",
        "ORCHESTRATOR_MODE": "codex",
    }


def test_runtime_reference_rejects_path_selection_name_drift(tmp_path: Path) -> None:
    config_path = tmp_path / "main.yaml"
    config_path.write_text("agents: {}\n")

    try:
        RuntimeConfigReference(
            config_path=config_path.resolve(),
            selection=RepositoryLaunchSelection.parse(
                mode="codex",
                config_name="other.yaml",
            ),
        )
    except ValueError as exc:
        assert "must match" in str(exc)
    else:
        raise AssertionError("path/selection drift must fail")
