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
    LaneSuspendability,
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
    assert "request_memory = 1024" in compiled.text
    assert "should_transfer_files = NO" in compiled.text
    assert (
        "periodic_remove = (JobStatus == 2) && "
        "((time() - JobCurrentStartDate - (CumulativeSuspensionTime ?: 0)) > 600)" in compiled.text
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


def test_fractional_deadlines_round_up_never_down(tmp_path: Path) -> None:
    compiled = compile_submit_description(
        _command(("/bin/true",), 1.9),
        LaneResources(request_cpus=1),
        tmp_path,
    )
    assert "> 2)" in compiled.text

    compiled = compile_submit_description(
        _command(("/bin/true",), 0.4),
        LaneResources(request_cpus=1),
        tmp_path,
    )
    assert "> 1)" in compiled.text


def test_memory_budget_sizes_the_slot(tmp_path: Path) -> None:
    """Without an explicit request, the scheduler derives the slot from
    the tiny exec wrapper's image size and the real workload OOMs at a
    ~256MB ceiling - the memory budget must always be emitted."""
    compiled = compile_submit_description(
        _command(("/bin/true",)),
        LaneResources(request_cpus=1, request_memory_mb=4096),
        tmp_path,
    )
    assert "request_memory = 4096" in compiled.text


def test_learned_priority_is_emitted_when_known(tmp_path: Path) -> None:
    compiled = compile_submit_description(
        _command(("/bin/true",)),
        LaneResources(request_cpus=1, priority=60),
        tmp_path,
    )
    assert "priority = 60" in compiled.text


def test_naive_run_emits_no_priority_line(tmp_path: Path) -> None:
    """Zero history compiles to a submit file with no priority at all -
    the naive first run is byte-for-byte the pre-learning behavior."""
    compiled = compile_submit_description(
        _command(("/bin/true",)),
        LaneResources(request_cpus=1),
        tmp_path,
    )
    assert "priority" not in compiled.text


def test_deadline_charges_executing_time_never_frozen_time(tmp_path: Path) -> None:
    """Suspension (machine-load backoff) must not burn the lane's
    budget: a frozen job's deadline clock stops, or a long freeze
    manufactures a timeout the lane never earned. The ?: guard keeps
    the expression defined before any suspension has happened."""
    compiled = compile_submit_description(
        _command(("/bin/true",), 60.0),
        LaneResources(request_cpus=1),
        tmp_path,
    )
    assert (
        "periodic_remove = (JobStatus == 2) && "
        "((time() - JobCurrentStartDate - (CumulativeSuspensionTime ?: 0)) > 60)"
        in compiled.text
    )


def test_suspendability_is_declared_explicitly_all_three_ways(
    tmp_path: Path,
) -> None:
    """The attribute is always present and carries the classification
    name itself — and the unclassified default serializes as "never":
    an undeclared lane is not eligible for freezing (fail-safe, A1
    #7118 review; three-valued per #7124)."""
    default = compile_submit_description(
        _command(("/bin/true",)),
        LaneResources(request_cpus=1),
        tmp_path,
    )
    assert '+SuspendableLane = "never"' in default.text

    hermetic = compile_submit_description(
        _command(("/bin/true",)),
        LaneResources(
            request_cpus=1, suspendability=LaneSuspendability.ANYWHERE
        ),
        tmp_path,
    )
    assert '+SuspendableLane = "anywhere"' in hermetic.text

    cooperative = compile_submit_description(
        _command(("/bin/true",)),
        LaneResources(
            request_cpus=1, suspendability=LaneSuspendability.COOPERATIVE
        ),
        tmp_path,
    )
    assert '+SuspendableLane = "cooperative"' in cooperative.text


def test_cooperative_lanes_start_unsafe_with_the_chirp_prerequisite(
    tmp_path: Path,
) -> None:
    """A cooperative lane starts UNSAFE (SafeToSuspend = False) so an
    advertisement that never arrives degrades to never-frozen, and
    WantIOProxy is enabled so condor_chirp can reach the job ad. The
    other classifications carry neither line."""
    cooperative = compile_submit_description(
        _command(("/bin/true",)),
        LaneResources(
            request_cpus=1, suspendability=LaneSuspendability.COOPERATIVE
        ),
        tmp_path,
    )
    assert "+SafeToSuspend = False" in cooperative.text
    assert "+WantIOProxy = True" in cooperative.text

    for other in (LaneSuspendability.NEVER, LaneSuspendability.ANYWHERE):
        compiled = compile_submit_description(
            _command(("/bin/true",)),
            LaneResources(request_cpus=1, suspendability=other),
            tmp_path,
        )
        assert "SafeToSuspend" not in compiled.text
        assert "WantIOProxy" not in compiled.text


def test_work_key_is_the_batch_name(tmp_path: Path) -> None:
    """Targeted queue operations (suspend THIS lane, remove THIS lane)
    need a job-addressable handle; pool-wide -all operations from tests
    or tooling can freeze unrelated work (B4, #7118 review)."""
    compiled = compile_submit_description(
        _command(("/bin/true",)),
        LaneResources(request_cpus=1),
        tmp_path,
    )
    assert "batch_name = test-unit" in compiled.text


def test_submitter_worktree_is_tagged_on_the_job(tmp_path: Path) -> None:
    """The pool is shared by every worktree on the machine and
    concurrent gates are normal; each job names its submitting
    worktree so attribution never requires Iwd archaeology."""
    compiled = compile_submit_description(
        _command(("/bin/true",)),
        LaneResources(request_cpus=1),
        tmp_path,
    )
    assert '+LaneSubmitter = "worktree"' in compiled.text


def test_unquotable_submitter_name_is_rejected(tmp_path: Path) -> None:
    command = LaneCommand(
        work_key=LaneWorkKey("test-unit"),
        arguments=("/bin/true",),
        working_directory=Path('/repo/bad"name'),
        deadline=LaneDeadline(300.0),
    )
    with pytest.raises(ValueError, match="unusable as submitter tag"):
        compile_submit_description(
            command, LaneResources(request_cpus=1), tmp_path
        )
