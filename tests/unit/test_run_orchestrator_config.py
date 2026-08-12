"""Repository Engine child configuration-boundary tests."""

from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.run_orchestrator import _load_config_for_instance
from issue_orchestrator.infra.config_identity import (
    ConfigurationFingerprintMismatch,
    EXPECTED_CONFIG_FINGERPRINT_ENV,
)


def _write_mode_config(repo: Path, content: str = "agents: {}\n") -> Path:
    path = repo / ".issue-orchestrator/config/modes/codex/main.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_child_rejects_maintenance_config_before_runtime_start(tmp_path: Path) -> None:
    path = tmp_path / ".issue-orchestrator/config/maintenance/hooks-validate.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("agents: {}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="maintenance config cannot launch"):
        _load_config_for_instance(tmp_path, path, None)


def test_child_rejects_config_changed_after_parent_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_mode_config(tmp_path)
    monkeypatch.setenv(EXPECTED_CONFIG_FINGERPRINT_ENV, "preflight-fingerprint")

    with pytest.raises(ConfigurationFingerprintMismatch):
        _load_config_for_instance(tmp_path, path, None, mode="codex")
