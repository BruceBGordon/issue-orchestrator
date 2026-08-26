"""Public behavior for directory-backed configuration modes."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

from issue_orchestrator.domain.repository_launch_selection import (
    RepositoryLaunchSelection,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.entrypoints.cli_support import load_config
from issue_orchestrator.infra.config_paths import (
    get_config_path,
    list_configs,
    list_modes,
    repo_root_from_config_path,
    require_engine_launch_config_path,
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


def test_flat_managed_configs_are_not_discovered_or_resolved(tmp_path: Path) -> None:
    flat = tmp_path / ".issue-orchestrator/config/main.yaml"
    flat.parent.mkdir(parents=True)
    flat.write_text("agents: {}\n", encoding="utf-8")

    assert list_modes(tmp_path) == []
    assert list_configs(tmp_path, "default") == []
    assert get_config_path(tmp_path, "main", "default") == (
        tmp_path / ".issue-orchestrator/config/modes/default/main.yaml"
    )


def test_flat_managed_config_is_rejected_as_engine_launch_config(
    tmp_path: Path,
) -> None:
    flat = tmp_path / ".issue-orchestrator/config/main.yaml"
    flat.parent.mkdir(parents=True)
    flat.write_text("agents: {}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="config/modes/<mode>/"):
        require_engine_launch_config_path(flat)

    with pytest.raises(ValueError, match="config/modes/<mode>/"):
        Config.load(flat)


def test_cli_rejects_explicit_flat_managed_config(tmp_path: Path) -> None:
    flat = tmp_path / ".issue-orchestrator/config/main.yaml"
    flat.parent.mkdir(parents=True)
    flat.write_text("repo:\n  name: owner/repo\nagents: {}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="config/modes/<mode>/"):
        load_config(argparse.Namespace(config=str(flat), mode=None, set=[]))


def test_cli_rejects_symlinked_mode_config_before_loading(tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text("repo:\n  name: owner/repo\nagents: {}\n", encoding="utf-8")
    config_path = tmp_path / ".issue-orchestrator/config/modes/default/main.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.symlink_to(outside)

    with pytest.raises(ValueError, match="must not be symbolic links"):
        load_config(argparse.Namespace(config=str(config_path), mode=None, set=[]))


def test_cli_hook_policy_accepts_maintenance_config(tmp_path: Path) -> None:
    maintenance = (
        tmp_path / ".issue-orchestrator/config/maintenance/hooks-validate.yaml"
    )
    maintenance.parent.mkdir(parents=True)
    maintenance.write_text(
        "repo:\n  name: owner/repo\nagents: {}\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(config=str(maintenance), mode=None, set=[])

    config = load_config(args, allow_maintenance_config=True)

    assert config.config_path == maintenance.resolve()
    with pytest.raises(ValueError, match="maintenance config cannot launch"):
        load_config(args)


def test_doctor_rejects_explicit_flat_managed_config(tmp_path: Path) -> None:
    from issue_orchestrator.infra.doctor.checks.config import load_config_with_checks

    flat = tmp_path / ".issue-orchestrator/config/main.yaml"
    flat.parent.mkdir(parents=True)
    flat.write_text("agents: {}\n", encoding="utf-8")

    config, checks, should_stop = load_config_with_checks(None, flat)

    assert config is None
    assert should_stop
    assert checks[0].status == "error"
    assert "config/modes/<mode>/" in checks[0].detail


def test_flat_managed_config_cannot_be_preloaded(tmp_path: Path) -> None:
    flat = tmp_path / ".issue-orchestrator/config/main.yaml"
    flat.parent.mkdir(parents=True)
    flat.write_text("agents: {}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="config/modes/<mode>/"):
        Config.load(flat)


def test_empty_default_mode_directory_is_not_launchable(
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


# ---------------------------------------------------------------------------
# Shipped mode files. Modes have no inheritance, so the single-provider modes
# (claude, codex) are complete copies of default/main.yaml whose only delta is
# the agents section. These tests pin that drift contract to the committed
# files: without them a change to the default mode silently diverges the
# copies (as #7093 did before this suite existed).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHIPPED_MODES_DIR = _REPO_ROOT / ".issue-orchestrator" / "config" / "modes"

# Per-mode purity contract: every agent in the mode runs on this provider and
# pins the provider-specific reasoning ceiling, so the claude and codex modes
# stay a controlled A/B pair (effort: xhigh <-> reasoning_effort: xhigh).
_SINGLE_PROVIDER_MODES = {
    "claude": {
        "provider": "claude-code",
        "ai_system": "claude-code",
        "effort_key": "effort",
    },
    "codex": {
        "provider": "codex",
        "ai_system": "codex",
        "effort_key": "reasoning_effort",
    },
}


def _load_shipped_mode(mode: str) -> dict:
    path = _SHIPPED_MODES_DIR / mode / "main.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_shipped_modes_are_discoverable_and_load_clean() -> None:
    modes = list_modes(_REPO_ROOT)
    assert modes == ["default", *sorted(_SINGLE_PROVIDER_MODES)]
    for mode in modes:
        config_names = list_configs(_REPO_ROOT, mode)
        assert config_names, f"mode {mode!r} ships no config files"
        for name in config_names:
            config = Config.load(get_config_path(_REPO_ROOT, name, mode))
            assert config.validate() == [], f"{mode}/{name} failed validation"


@pytest.mark.parametrize("mode", sorted(_SINGLE_PROVIDER_MODES))
def test_single_provider_modes_match_default_outside_agents(mode: str) -> None:
    default_doc = _load_shipped_mode("default")
    mode_doc = _load_shipped_mode(mode)
    del default_doc["agents"]
    del mode_doc["agents"]

    assert mode_doc == default_doc, (
        f"modes/{mode}/main.yaml drifted from modes/default/main.yaml outside "
        "the agents section; modes do not inherit, so sync the full non-agent "
        "configuration"
    )


@pytest.mark.parametrize("mode", sorted(_SINGLE_PROVIDER_MODES))
def test_single_provider_modes_are_provider_pure(mode: str) -> None:
    contract = _SINGLE_PROVIDER_MODES[mode]
    agents = _load_shipped_mode(mode)["agents"]

    assert agents, f"modes/{mode}/main.yaml ships no agents"
    for agent_name, agent in agents.items():
        assert agent["provider"] == contract["provider"], agent_name
        assert agent["ai_system"] == contract["ai_system"], agent_name


@pytest.mark.parametrize("mode", sorted(_SINGLE_PROVIDER_MODES))
def test_single_provider_modes_pin_the_effort_ceiling(mode: str) -> None:
    contract = _SINGLE_PROVIDER_MODES[mode]
    agents = _load_shipped_mode(mode)["agents"]

    for agent_name, agent in agents.items():
        effort = agent.get("provider_args", {}).get(contract["effort_key"])
        assert effort == "xhigh", (
            f"{agent_name} in modes/{mode}/main.yaml does not pin "
            f"{contract['effort_key']}: xhigh (found {effort!r}), so the "
            "claude/codex A/B is uncontrolled"
        )
