"""Public behavior for directory-backed configuration modes."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.repository_launch_selection import (
    RepositoryLaunchSelection,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_paths import (
    get_config_path,
    list_configs,
    list_modes,
    repo_root_from_config_path,
    selection_from_config_path,
)


def _write_config(path: Path, *, repo: str = "owner/repo") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "repo:",
                f"  name: {repo}",
                "agents:",
                "  agent:worker:",
                "    prompt: prompt.md",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (path.parents[4] / "prompt.md").write_text("Fix it", encoding="utf-8")


def test_mode_discovery_and_resolution_are_scoped_by_mode(tmp_path: Path) -> None:
    codex = tmp_path / ".issue-orchestrator/config/modes/codex/main.yaml"
    default = tmp_path / ".issue-orchestrator/config/modes/default/main.yaml"
    _write_config(codex)
    _write_config(default)

    assert list_modes(tmp_path) == ["default", "codex"]
    assert list_configs(tmp_path, "codex") == ["main.yaml"]
    assert get_config_path(tmp_path, "main", "codex") == codex
    assert repo_root_from_config_path(codex) == tmp_path.resolve()
    assert selection_from_config_path(codex).to_dict() == {
        "mode": "codex",
        "config_name": "main.yaml",
    }


def test_config_load_records_mode_and_effective_fingerprint(tmp_path: Path) -> None:
    config_path = tmp_path / ".issue-orchestrator/config/modes/codex/main.yaml"
    _write_config(config_path)

    config = Config.load(config_path)

    assert config.configuration_mode == "codex"
    assert config.config_name == "main.yaml"


def test_runtime_config_reference_rejects_path_selection_mode_drift(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".issue-orchestrator/config/modes/codex/main.yaml"
    _write_config(config_path)
    config = Config.load(config_path)
    config.launch_selection = RepositoryLaunchSelection.parse(
        mode="claude",
        config_name="main.yaml",
    )

    with pytest.raises(
        ValueError,
        match="config_path and launch selection must match",
    ):
        config.runtime_config_reference()


def test_runtime_config_reference_requires_the_loaded_file(tmp_path: Path) -> None:
    config_path = tmp_path / ".issue-orchestrator/config/modes/codex/main.yaml"
    _write_config(config_path)
    config = Config.load(config_path)
    config_path.unlink()

    with pytest.raises(ValueError, match="must point to an existing file"):
        config.runtime_config_reference()


def test_effective_fingerprint_refresh_is_stable_and_override_sensitive(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".issue-orchestrator/config/modes/codex/main.yaml"
    _write_config(config_path)
    config = Config.load(config_path)
    initial = config.config_fingerprint

    assert config.refresh_config_fingerprint() == initial
    assert config.refresh_config_fingerprint() == initial

    config.filtering.label = "urgent"
    changed = config.refresh_config_fingerprint()

    assert changed != initial
    assert config.refresh_config_fingerprint() == changed
    assert len(config.config_fingerprint) == 64


def test_non_default_mode_never_falls_back_to_flat_config(tmp_path: Path) -> None:
    legacy = tmp_path / ".issue-orchestrator/config/main.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("agents: {}\n", encoding="utf-8")

    assert list_modes(tmp_path) == ["default"]
    assert list_configs(tmp_path, "codex") == []
    assert get_config_path(tmp_path, "main", "codex") != legacy


def test_nested_default_mode_disables_per_file_legacy_fallback(tmp_path: Path) -> None:
    nested = tmp_path / ".issue-orchestrator/config/modes/default"
    nested.mkdir(parents=True)
    (nested / "main.yaml").write_text("agents: {}\n", encoding="utf-8")
    legacy = tmp_path / ".issue-orchestrator/config/legacy.yaml"
    legacy.write_text("agents: {}\n", encoding="utf-8")

    resolved = get_config_path(tmp_path, "legacy.yaml", "default")

    assert resolved == nested / "legacy.yaml"
    assert not resolved.exists()


def test_empty_default_mode_directory_disables_legacy_mode_discovery(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".issue-orchestrator/config"
    (config_dir / "modes/default").mkdir(parents=True)
    (config_dir / "legacy.yaml").write_text("agents: {}\n", encoding="utf-8")

    assert list_modes(tmp_path) == []


def test_mode_config_symlink_is_rejected(tmp_path: Path) -> None:
    mode_dir = tmp_path / ".issue-orchestrator/config/modes/codex"
    mode_dir.mkdir(parents=True)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-mode-config.yaml"
    outside.write_text("agents: {}\n", encoding="utf-8")
    (mode_dir / "main.yaml").symlink_to(outside)

    with pytest.raises(ValueError, match="must not be symbolic links"):
        get_config_path(tmp_path, "main.yaml", "codex")


def test_symlinked_config_ancestor_is_rejected_even_when_target_is_inside_repo(
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "real-config-root"
    mode_dir = real_root / "config/modes/codex"
    mode_dir.mkdir(parents=True)
    (mode_dir / "main.yaml").write_text("agents: {}\n", encoding="utf-8")
    (tmp_path / ".issue-orchestrator").symlink_to(real_root, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be symbolic links"):
        get_config_path(tmp_path, "main.yaml", "codex")


def test_mode_path_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Invalid configuration mode"):
        get_config_path(tmp_path, "main", "../codex")
