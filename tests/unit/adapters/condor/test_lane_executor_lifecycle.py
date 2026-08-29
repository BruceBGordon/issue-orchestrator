"""Run-directory ownership, and the per-job accounting it collects.

Hermetic: scheduler tools are shell stubs, no pool required.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor.lane_executor import (
    CondorLaneExecutor,
    CondorTools,
)
from issue_orchestrator.domain.lane_execution import (
    LaneCommand,
    LaneCompleted,
    LaneDeadline,
    LaneExecutorError,
    LaneResources,
    LaneWorkKey,
)

_JOB_ID = "7.0"
# What a lane's user log holds after a job ran and exited nonzero.
_TERMINATED_NONZERO_EVENT_LOG = (
    "000 (007.000.000) 2026-08-29 12:00:00 Job submitted from host: <127.0.0.1>\n"
    "...\n"
    "001 (007.000.000) 2026-08-29 12:00:01 Job executing on host: <127.0.0.1>\n"
    "...\n"
    "005 (007.000.000) 2026-08-29 12:00:05 Job terminated.\n"
    "\t(1) Normal termination (return value 3)\n"
    "...\n"
)


def _write_stubs(binaries: Path, stubs: dict[str, str]) -> None:
    binaries.mkdir(parents=True, exist_ok=True)
    for name, body in stubs.items():
        tool = binaries / name
        tool.write_text(body)
        tool.chmod(0o755)


def _stub_tools(tmp_path: Path, submit_exit: int) -> CondorTools:
    binaries = tmp_path / "bin"
    _write_stubs(
        binaries,
        {
            "condor_submit": (
                f"#!/bin/sh\necho 'submit refused' >&2\nexit {submit_exit}\n"
            ),
            "condor_rm": "#!/bin/sh\nexit 0\n",
            "condor_q": "#!/bin/sh\nexit 0\n",
            "condor_config_val": "#!/bin/sh\nexit 0\n",
        },
    )
    return CondorTools(
        submit=binaries / "condor_submit",
        remove=binaries / "condor_rm",
        query=binaries / "condor_q",
        config_query=binaries / "condor_config_val",
    )


def _completing_tools(tmp_path: Path, *, history_directory: str) -> CondorTools:
    """Stubs for a job that runs and exits 3.

    ``condor_submit`` plays the scheduler: it reads the ``log =`` line
    out of the submit description and writes the terminal event log the
    executor polls, so the whole lifecycle runs with no pool.
    ``condor_config_val`` answers with the per-job history location the
    pool helper would have configured.
    """
    binaries = tmp_path / "bin"
    submit = (
        "#!/bin/sh\n"
        'log=$(awk -F" = " \'/^log/{print $2}\' "$2")\n'
        f"cat > \"$log\" <<'EVENTS'\n{_TERMINATED_NONZERO_EVENT_LOG}EVENTS\n"
        f"echo '{_JOB_ID}'\n"
    )
    _write_stubs(
        binaries,
        {
            "condor_submit": submit,
            "condor_rm": "#!/bin/sh\nexit 0\n",
            "condor_q": "#!/bin/sh\nexit 0\n",
            "condor_config_val": f"#!/bin/sh\necho '{history_directory}'\n",
        },
    )
    return CondorTools(
        submit=binaries / "condor_submit",
        remove=binaries / "condor_rm",
        query=binaries / "condor_q",
        config_query=binaries / "condor_config_val",
    )


def _run_lane(
    tools: CondorTools, tmp_path: Path, work_key: str
) -> tuple[LaneCompleted, set[Path]]:
    """Run one stubbed lane, returning its outcome and any run
    directory it retained."""
    before = set(Path(tempfile.gettempdir()).glob(f"lane-{work_key}*"))
    outcome = CondorLaneExecutor(tools).run(
        LaneCommand(
            work_key=LaneWorkKey(work_key),
            arguments=(sys.executable, "-c", "pass"),
            working_directory=tmp_path,
            deadline=LaneDeadline(30.0),
        ),
        LaneResources(request_cpus=1),
    )
    assert type(outcome) is LaneCompleted
    retained = set(Path(tempfile.gettempdir()).glob(f"lane-{work_key}*")) - before
    return outcome, retained


def test_submission_failure_retains_diagnostics_and_names_the_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    executor = CondorLaneExecutor(_stub_tools(tmp_path, submit_exit=1))
    before = set(Path(tempfile.gettempdir()).glob("lane-lifecycle.submitfail*"))

    with pytest.raises(LaneExecutorError) as caught:
        executor.run(
            LaneCommand(
                work_key=LaneWorkKey("lifecycle.submitfail"),
                arguments=(sys.executable, "-c", "pass"),
                working_directory=tmp_path,
                deadline=LaneDeadline(30.0),
            ),
            LaneResources(request_cpus=1),
        )

    assert "submit refused" in str(caught.value)
    assert "diagnostics retained at" in str(caught.value)
    retained = set(Path(tempfile.gettempdir()).glob("lane-lifecycle.submitfail*")) - before
    assert retained, "submission failure must retain the run directory"
    for directory in retained:
        assert (directory / "lane.sub").exists(), "the submit file is the diagnostic"
        import shutil

        shutil.rmtree(directory, ignore_errors=True)
    stderr = capsys.readouterr().err
    assert "diagnostics retained at" in stderr


def test_failed_lane_collects_its_per_job_accounting(tmp_path: Path) -> None:
    """Acceptance (#7127): a lane that exits nonzero retains its run
    directory, and the scheduler's COMPLETE final ClassAd for that job
    lands in it — the accounting travels with the diagnostics instead
    of staying in a rotating global history nothing correlates back."""
    history = tmp_path / "per-job-history"
    history.mkdir()
    classad = "ExitCode = 3\nMemoryUsage = 128\nRemoteWallClockTime = 4.0\n"
    (history / f"history.{_JOB_ID}").write_text(classad)

    outcome, retained = _run_lane(
        _completing_tools(tmp_path, history_directory=str(history)),
        tmp_path,
        "lifecycle.accounting",
    )
    assert outcome.exit_code == 3
    assert retained, "a nonzero lane must retain its diagnostics"
    for directory in retained:
        assert (directory / "lane.classad").read_text() == classad
        shutil.rmtree(directory, ignore_errors=True)


def test_a_pool_without_per_job_accounting_still_reports_the_lane(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Collection is best-effort by construction: it runs while a lane
    is already failing, so an unconfigured pool must cost the ClassAd
    and a stderr line — never the lane's own result."""
    outcome, retained = _run_lane(
        _completing_tools(tmp_path, history_directory="undefined"),
        tmp_path,
        "lifecycle.noaccounting",
    )
    assert outcome.exit_code == 3
    stderr = capsys.readouterr().err
    assert "PER_JOB_HISTORY_DIR" in stderr
    assert "diagnostics retained at" in stderr
    for directory in retained:
        assert not (directory / "lane.classad").exists()
        shutil.rmtree(directory, ignore_errors=True)


def test_an_unexpected_job_identifier_is_never_turned_into_a_read_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ClassAd file is named history.<cluster>.<proc>, so the job
    identifier is also a path component. Anything not that shape is
    refused rather than joined onto the history directory."""
    history = tmp_path / "per-job-history"
    history.mkdir()
    tools = _completing_tools(tmp_path, history_directory=str(history))
    tools.submit.write_text(
        "#!/bin/sh\n"
        'log=$(awk -F" = " \'/^log/{print $2}\' "$2")\n'
        f"cat > \"$log\" <<'EVENTS'\n{_TERMINATED_NONZERO_EVENT_LOG}EVENTS\n"
        "echo '../../../etc/passwd'\n"
    )
    tools.submit.chmod(0o755)

    outcome, retained = _run_lane(tools, tmp_path, "lifecycle.badjobid")
    assert outcome.exit_code == 3
    assert "unexpected job identifier" in capsys.readouterr().err
    for directory in retained:
        assert not (directory / "lane.classad").exists()
        shutil.rmtree(directory, ignore_errors=True)


def test_a_clean_lane_collects_nothing_and_keeps_nothing(tmp_path: Path) -> None:
    """The retention decision and the collection decision are one: a
    clean completion leaves no directory, so there is nothing to
    collect into."""
    history = tmp_path / "per-job-history"
    history.mkdir()
    (history / f"history.{_JOB_ID}").write_text("ExitCode = 0\n")
    tools = _completing_tools(tmp_path, history_directory=str(history))
    zero_exit = _TERMINATED_NONZERO_EVENT_LOG.replace("return value 3", "return value 0")
    submit = tools.submit
    submit.write_text(
        "#!/bin/sh\n"
        'log=$(awk -F" = " \'/^log/{print $2}\' "$2")\n'
        f"cat > \"$log\" <<'EVENTS'\n{zero_exit}EVENTS\n"
        f"echo '{_JOB_ID}'\n"
    )
    submit.chmod(0o755)

    outcome, retained = _run_lane(tools, tmp_path, "lifecycle.clean")
    assert outcome.exit_code == 0
    assert not retained, "a clean completion must leave no run directory"
