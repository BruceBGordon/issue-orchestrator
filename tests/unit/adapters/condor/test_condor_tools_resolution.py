"""CondorTools resolution: PATH first, personal install second, loud last."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor.lane_executor import (
    PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE,
    CondorTools,
)
from issue_orchestrator.domain.lane_execution import LaneExecutorUnavailableError

_TOOL_NAMES = ("condor_submit", "condor_rm", "condor_q")


def _write_tools(binaries: Path) -> None:
    binaries.mkdir(parents=True)
    for name in _TOOL_NAMES:
        tool = binaries / name
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)


def _empty_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def test_path_installation_wins_and_needs_no_pool_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    on_path = tmp_path / "system-bin"
    _write_tools(on_path)
    monkeypatch.setenv("PATH", str(on_path))
    monkeypatch.setenv(
        PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE, str(tmp_path / "unused")
    )

    tools = CondorTools.resolve()

    assert tools.submit == (on_path / "condor_submit").resolve()
    assert tools.pool_config is None


def test_personal_install_resolves_with_its_pool_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _empty_environment(monkeypatch, tmp_path)
    home = tmp_path / "pool-home"
    install = home / "condor-25.8.2-x86_64_macOS13-stripped"
    _write_tools(install / "bin")
    (install / "etc").mkdir(parents=True)
    (install / "etc" / "condor_config").write_text("# pool config\n")
    monkeypatch.setenv(PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE, str(home))

    tools = CondorTools.resolve()

    assert tools.submit == (install / "bin" / "condor_submit").resolve()
    assert tools.pool_config == (install / "etc" / "condor_config").resolve()
    assert os.access(tools.submit, os.X_OK)


def test_personal_install_without_config_is_not_a_pool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _empty_environment(monkeypatch, tmp_path)
    home = tmp_path / "pool-home"
    _write_tools(home / "condor-25.8.2-x86_64_macOS13-stripped" / "bin")
    monkeypatch.setenv(PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE, str(home))

    with pytest.raises(LaneExecutorUnavailableError, match="opt-in"):
        CondorTools.resolve()


def test_nothing_anywhere_fails_loudly_with_setup_pointer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _empty_environment(monkeypatch, tmp_path)
    monkeypatch.setenv(
        PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE, str(tmp_path / "missing")
    )

    with pytest.raises(
        LaneExecutorUnavailableError, match="condor-personal.sh"
    ):
        CondorTools.resolve()
