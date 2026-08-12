"""Tests for typed repository launch selections."""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.repository_launch_selection import (
    ConfigurationModeName,
    RepositoryLaunchSelection,
)


@pytest.mark.parametrize(
    "raw",
    ["", " ", "../codex", "Codex", "codex_mode", "codex/more", 42],
)
def test_configuration_mode_rejects_non_slug_values(raw: object) -> None:
    with pytest.raises(ValueError, match="Invalid configuration mode"):
        ConfigurationModeName.parse(raw, default="")


def test_launch_selection_normalizes_boundary_defaults() -> None:
    selection = RepositoryLaunchSelection.parse(mode=None, config_name="main")

    assert selection.mode.value == "default"
    assert selection.config.value == "main.yaml"
    assert selection.to_dict() == {"mode": "default", "config_name": "main.yaml"}
