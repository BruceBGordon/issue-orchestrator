"""Outbound anti-corruption: lane specs compile to exact job descriptions."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor.submit_compiler import (
    compile_submit_description,
)
from issue_orchestrator.domain.lane_execution import (
    LaneCommand,
    LaneDeadline,
    LaneResources,
    LaneWorkKey,
)


def _command(
    arguments: tuple[str, ...],
    timeout_seconds: float = 300.0,
) -> LaneCommand:
    return LaneCommand(
        work_key=LaneWorkKey("test-unit"),
        arguments=arguments,
        working_directory=Path("/repo/worktree"),
        deadline=LaneDeadline(timeout_seconds),
    )


def test_compiles_complete_description_with_runtime_deadline(
    tmp_path: Path,
) -> None:
    compiled = compile_submit_description(
        _command(("/usr/bin/gmake", "test-unit", "PARALLEL=8"), 600.0),
        LaneResources(request_cpus=12),
        tmp_path,
    )

    assert f"executable = {tmp_path / 'lane.exec'}" in compiled.text
    assert "arguments" not in compiled.text
    assert compiled.exec_script_path == tmp_path / "lane.exec"
    assert compiled.exec_script_text == (
        "#!/bin/sh\nexec /usr/bin/gmake test-unit PARALLEL=8\n"
    )
    assert "initialdir = /repo/worktree" in compiled.text
    assert "getenv = true" in compiled.text
    assert "request_cpus = 12" in compiled.text
    assert "should_transfer_files = NO" in compiled.text
    assert (
        "periodic_remove = (JobStatus == 2) && "
        "((time() - JobCurrentStartDate) > 600)" in compiled.text
    )
    assert compiled.text.rstrip().endswith("queue")
    assert compiled.output_path == tmp_path / "lane.out"
    assert compiled.error_path == tmp_path / "lane.err"
    assert compiled.event_log_path == tmp_path / "lane.events"


def test_exclusive_tokens_compile_to_concurrency_limits(tmp_path: Path) -> None:
    compiled = compile_submit_description(
        _command(("/bin/true",)),
        LaneResources(request_cpus=1, exclusive=("codex", "browser")),
        tmp_path,
    )

    assert "concurrency_limits = codex,browser" in compiled.text


def test_without_exclusive_tokens_no_limits_line_is_emitted(
    tmp_path: Path,
) -> None:
    compiled = compile_submit_description(
        _command(("/bin/true",)),
        LaneResources(request_cpus=1),
        tmp_path,
    )

    assert "concurrency_limits" not in compiled.text


def test_exec_shim_survives_spaces_quotes_and_newlines(tmp_path: Path) -> None:
    import subprocess

    compiled = compile_submit_description(
        _command(
            (
                "/bin/echo",
                "print('hello world')",
                'say "hi"',
                "line one\nline two",
            )
        ),
        LaneResources(request_cpus=1),
        tmp_path,
    )

    compiled.exec_script_path.write_text(compiled.exec_script_text)
    compiled.exec_script_path.chmod(0o755)
    produced = subprocess.run(
        [str(compiled.exec_script_path)], capture_output=True, text=True
    )
    assert produced.returncode == 0
    assert produced.stdout == "print('hello world') say \"hi\" line one\nline two\n"


def test_relative_run_directory_is_rejected() -> None:
    with pytest.raises(ValueError, match="absolute"):
        compile_submit_description(
            _command(("/bin/true",)),
            LaneResources(request_cpus=1),
            Path("relative/dir"),
        )
