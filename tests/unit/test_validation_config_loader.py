"""Direct tests for lightweight validation config loading."""

from dataclasses import asdict
from pathlib import Path

import pytest

from issue_orchestrator.infra.config_models import ValidationConfig
from issue_orchestrator.infra.validation_config_loader import (
    default_validation_config,
    extract_validation_config,
    load_validation_config,
    load_validation_config_from_file,
    load_runtime_validation_config,
)


def test_default_validation_config_uses_config_model_defaults() -> None:
    assert default_validation_config() == asdict(ValidationConfig())


def test_extract_validation_config_merges_nested_defaults() -> None:
    result = extract_validation_config(
        {
            "validation": {
                "quick": {"cmd": "make verify"},
                "coverage_guardrail": {"enabled": True},
            }
        }
    )

    assert result == {
        "quick": {
            "cmd": "make verify",
            "timeout_seconds": 300,
        },
        "publish": {
            "cmd": None,
            "timeout_seconds": 1800,
            "dirty_check": "tracked",
        },
        "junit_xml_paths": (),
        "coverage_guardrail": {
            "enabled": True,
            "min_percent": None,
            "apply_to": "changed",
            "scope": [],
            "coverage_type": "line",
            "exclude": [],
        },
    }


def test_load_validation_config_returns_defaults_when_default_config_missing(
    tmp_path: Path,
) -> None:
    assert load_validation_config(tmp_path) == default_validation_config()


def test_load_validation_config_raises_when_named_config_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_validation_config(tmp_path, config_name="missing")


def test_load_validation_config_swallows_bad_yaml_for_default_config(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".issue-orchestrator" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "default.yaml").write_text("validation: [not-a-mapping")

    assert load_validation_config(tmp_path) == default_validation_config()


def test_load_validation_config_from_file_reads_explicit_file(tmp_path: Path) -> None:
    config_path = tmp_path / "validation.yaml"
    config_path.write_text(
        """
validation:
  quick:
    cmd: make validate
    timeout_seconds: 120
  publish:
    cmd: make validate-pr
    timeout_seconds: 1800
    dirty_check: all
  coverage_guardrail:
    enabled: true
    min_percent: 85
"""
    )

    result = load_validation_config_from_file(config_path)

    assert result["quick"]["cmd"] == "make validate"
    assert result["quick"]["timeout_seconds"] == 120
    assert result["publish"]["cmd"] == "make validate-pr"
    assert result["publish"]["dirty_check"] == "all"
    assert result["coverage_guardrail"]["enabled"] is True
    assert result["coverage_guardrail"]["min_percent"] == 85


def test_extract_validation_config_uses_only_validation_junit_paths() -> None:
    result = extract_validation_config(
        {
            "validation": {
                "junit_xml_paths": ["validation.xml", "shared.xml"],
            },
            "e2e": {
                "junit_xml_paths": ["shared.xml", "e2e.xml"],
            },
        }
    )

    assert result["junit_xml_paths"] == ("validation.xml", "shared.xml")


def test_runtime_validation_config_resolves_name_with_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / ".issue-orchestrator/config/modes/codex/main.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("validation:\n  quick:\n    cmd: codex-check\n")
    monkeypatch.delenv("ISSUE_ORCHESTRATOR_CONFIG_PATH", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_CONFIG_PATH", raising=False)
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_CONFIG_NAME", "main.yaml")
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_MODE", "codex")

    result = load_runtime_validation_config(tmp_path)

    assert result["quick"]["cmd"] == "codex-check"


def test_runtime_validation_config_rejects_explicit_path_mode_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / ".issue-orchestrator/config/modes/default/main.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("validation: {}\n")
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_CONFIG_NAME", "main.yaml")
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_MODE", "codex")

    with pytest.raises(ValueError, match="path mode"):
        load_runtime_validation_config(tmp_path)
