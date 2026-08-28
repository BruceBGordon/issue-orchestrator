"""Run-directory ownership on preparation/submission failures.

Hermetic: scheduler tools are shell stubs, no pool required.
"""

from __future__ import annotations

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
    LaneDeadline,
    LaneExecutorError,
    LaneResources,
    LaneWorkKey,
)


def _stub_tools(tmp_path: Path, submit_exit: int) -> CondorTools:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stubs = {
        "condor_submit": f"#!/bin/sh\necho 'submit refused' >&2\nexit {submit_exit}\n",
        "condor_rm": "#!/bin/sh\nexit 0\n",
        "condor_q": "#!/bin/sh\nexit 0\n",
        "condor_config_val": "#!/bin/sh\nexit 0\n",
    }
    for name, body in stubs.items():
        tool = binaries / name
        tool.write_text(body)
        tool.chmod(0o755)
    return CondorTools(
        submit=binaries / "condor_submit",
        remove=binaries / "condor_rm",
        query=binaries / "condor_q",
        config_query=binaries / "condor_config_val",
    )


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
