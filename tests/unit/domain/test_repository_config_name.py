"""Tests for repository-local config-name validation."""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.repository_config_name import RepositoryConfigName


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        ".yaml",
        "../escaped",
        "nested/default",
        r"nested\default",
        "/tmp/escaped.yaml",
    ],
)
def test_repository_config_name_rejects_empty_or_path_like_values(raw: str) -> None:
    with pytest.raises(ValueError, match="Invalid config_name"):
        RepositoryConfigName.parse(raw, default="default.yaml")
    with pytest.raises(ValueError, match="Invalid config_name"):
        RepositoryConfigName(raw)


def test_repository_config_name_normalizes_yaml_extension() -> None:
    assert RepositoryConfigName.parse("custom").value == "custom.yaml"
    assert RepositoryConfigName("custom").value == "custom.yaml"
    assert RepositoryConfigName.parse(None, default="default.yaml").value == (
        "default.yaml"
    )
