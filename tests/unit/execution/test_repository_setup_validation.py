"""Behavior tests for repository-native setup validation detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.control.repository_setup import RepositorySetupCommand
from issue_orchestrator.control.validation import (
    ValidationRecordStore,
    ValidationRunner,
)
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.repository_setup_github_authorization import (
    repository_setup_github_authorization_codec,
)
from issue_orchestrator.execution.repository_setup_validation import (
    RepositorySetupValidationDetectorAdapter,
)


def test_makefile_defaults_run_a_repository_gate_and_catch_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generated mandatory quick gate must fail when repository tests fail."""
    # The repository suite itself runs beneath Make. Do not let recursive-make
    # flags such as --touch change the semantics of the target under test.
    for variable in ("MAKEFLAGS", "MFLAGS", "GNUMAKEFLAGS", "MAKELEVEL"):
        monkeypatch.delenv(variable, raising=False)
    (tmp_path / "Makefile").write_text(
        (
            "validate-fast:\n\t@exit 7\n\n"
            "validate-pr-raw:\n\t@true\n\n"
            "validate-pr:\n\t@false\n"
        ),
        encoding="utf-8",
    )
    defaults = RepositorySetupValidationDetectorAdapter()(tmp_path)
    command = RepositorySetupCommand(
        repo_root=tmp_path,
        repo_name="owner/repo",
        worker_agent_label="agent:dev",
        model="sonnet",
        validation_quick_command=defaults.quick_command or "",
        validation_publish_command=defaults.publish_command or "",
    )
    config = command.build_config(repository_setup_github_authorization_codec)

    record = ValidationRunner(
        ValidationRecordStore(tmp_path),
        LocalCommandRunner(),
    ).run(
        "agent_gate",
        "deliberate-failure",
        config["validation"]["quick"]["cmd"],
        timeout_seconds=10,
        cwd=tmp_path,
        session_output_dir=tmp_path / ".issue-orchestrator" / "test-validation",
    )

    assert defaults.quick_command == "make validate-fast"
    assert defaults.publish_command == "make validate-pr-raw"
    assert record.passed is False
    assert record.exit_code != 0


def test_makefile_detector_never_selects_cache_aware_publish_wrapper(
    tmp_path: Path,
) -> None:
    (tmp_path / "Makefile").write_text(
        "validate-fast:\n\t@true\n\nvalidate-pr:\n\t@true\n",
        encoding="utf-8",
    )

    defaults = RepositorySetupValidationDetectorAdapter()(tmp_path)

    assert defaults.quick_command is None
    assert defaults.publish_command is None
    assert "Enter quick and publish commands" in defaults.source


def test_unknown_repository_requires_explicit_validation_commands(
    tmp_path: Path,
) -> None:
    defaults = RepositorySetupValidationDetectorAdapter()(tmp_path)

    assert defaults.quick_command is None
    assert defaults.publish_command is None
    assert "Enter quick and publish commands" in defaults.source
