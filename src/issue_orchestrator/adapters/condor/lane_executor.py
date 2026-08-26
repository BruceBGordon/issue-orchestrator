# pyright: strict
"""HTCondor adapter for the LaneExecutor port."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import cast
from pathlib import Path

from ...domain.lane_execution import (
    LaneCommand,
    LaneCompleted,
    LaneExecutorError,
    LaneExecutorUnavailableError,
    LaneOutcome,
    LaneResources,
    LaneTimedOut,
)
from .event_classifier import (
    LaneJobDeadlineRemoved,
    LaneJobExited,
    LaneJobFaulted,
    LaneJobKilledBySignal,
    LaneJobPending,
    LaneJobRemoved,
    LaneJobRunning,
    LaneJobState,
    classify_event_log,
)
from .submit_compiler import CompiledSubmitDescription, compile_submit_description

_TOOL_TIMEOUT_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 0.1
# The scheduler's periodic expressions evaluate on an interval, so a
# deadline removal can land this much after the exact bound; the outer
# wait must tolerate it before declaring the backend unresponsive.
_SCHEDULER_SLACK_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class CondorTools:
    """Absolute paths to the scheduler's command-line tools."""

    submit: Path
    remove: Path
    query: Path

    def __post_init__(self) -> None:
        for field_name, value in (
            ("submit", self.submit),
            ("remove", self.remove),
            ("query", self.query),
        ):
            if not isinstance(cast(object, value), Path) or not value.is_absolute():
                raise ValueError(f"CondorTools.{field_name} must be an absolute Path")

    @classmethod
    def resolve(cls) -> CondorTools:
        """Resolve the tools from PATH, failing loudly when absent."""
        located: dict[str, Path] = {}
        for field_name, executable in (
            ("submit", "condor_submit"),
            ("remove", "condor_rm"),
            ("query", "condor_q"),
        ):
            found = shutil.which(executable)
            if found is None:
                raise LaneExecutorUnavailableError(
                    f"{executable} is not on PATH: the condor lane backend is "
                    "opt-in and requires a running HTCondor pool "
                    "(see scripts/condor-personal.sh)"
                )
            located[field_name] = Path(found).resolve()
        return cls(**located)


class CondorLaneExecutor:
    """Submit each lane as one scheduler job and follow it to its end."""

    def __init__(self, tools: CondorTools) -> None:
        if type(tools) is not CondorTools:
            raise ValueError("CondorLaneExecutor.tools must be CondorTools")
        self._tools = tools
        self._require_reachable_scheduler()

    def run(self, command: LaneCommand, resources: LaneResources) -> LaneOutcome:
        if type(command) is not LaneCommand:
            raise ValueError("CondorLaneExecutor.run requires a LaneCommand")
        if type(resources) is not LaneResources:
            raise ValueError("CondorLaneExecutor.run requires LaneResources")
        run_directory = Path(
            tempfile.mkdtemp(prefix=f"lane-{command.work_key.value}-")
        ).resolve()
        compiled = compile_submit_description(command, resources, run_directory)
        compiled.exec_script_path.write_text(
            compiled.exec_script_text, encoding="utf-8"
        )
        compiled.exec_script_path.chmod(0o755)
        submit_path = run_directory / "lane.sub"
        submit_path.write_text(compiled.text, encoding="utf-8")
        job_id = self._submit(submit_path)
        started_at = time.monotonic()
        outer_deadline = (
            started_at + command.deadline.timeout_seconds + _SCHEDULER_SLACK_SECONDS
        )
        streams = _OutputStreamer(compiled)
        try:
            while True:
                streams.pump()
                state = self._observe(compiled)
                terminal = self._map_terminal(state, started_at)
                if terminal is not None:
                    streams.pump()
                    return terminal
                if time.monotonic() >= outer_deadline:
                    raise LaneExecutorError(
                        "scheduler did not conclude the lane inside its deadline "
                        f"plus slack: lane={command.work_key.value} job={job_id}"
                    )
                time.sleep(_POLL_INTERVAL_SECONDS)
        except BaseException:
            # Cancellation or supervisor death: the job must not outlive us.
            self._remove(job_id)
            streams.pump()
            raise

    def _map_terminal(
        self, state: LaneJobState, started_at: float
    ) -> LaneOutcome | None:
        if type(state) is LaneJobPending or type(state) is LaneJobRunning:
            return None
        if type(state) is LaneJobExited:
            return LaneCompleted(state.exit_code)
        if type(state) is LaneJobKilledBySignal:
            return LaneCompleted(128 + state.signal_number)
        if type(state) is LaneJobDeadlineRemoved:
            return LaneTimedOut(time.monotonic() - started_at)
        if type(state) is LaneJobRemoved:
            raise LaneExecutorError(
                f"the lane's job was removed outside its deadline: {state.detail}"
            )
        if type(state) is LaneJobFaulted:
            raise LaneExecutorError(
                f"the scheduler held the lane's job: {state.detail}"
            )
        raise AssertionError("lane job state is a closed union")

    def _observe(self, compiled: CompiledSubmitDescription) -> LaneJobState:
        try:
            log_text = compiled.event_log_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return LaneJobPending()
        return classify_event_log(log_text)

    def _submit(self, submit_path: Path) -> str:
        completed = self._run_tool(
            (str(self._tools.submit), "-terse", str(submit_path))
        )
        if completed.returncode != 0:
            raise LaneExecutorError(
                "lane job submission failed: "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
        first_token = completed.stdout.split()
        if not first_token:
            raise LaneExecutorError(
                "lane job submission returned no job identifier"
            )
        return first_token[0]

    def _remove(self, job_id: str) -> None:
        try:
            self._run_tool((str(self._tools.remove), job_id))
        except LaneExecutorError:
            # Best-effort during unwinding; the primary error wins.
            return

    def _require_reachable_scheduler(self) -> None:
        completed = self._run_tool((str(self._tools.query), "-limit", "1"))
        if completed.returncode != 0:
            raise LaneExecutorUnavailableError(
                "the scheduler is installed but not reachable: "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )

    @staticmethod
    def _run_tool(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                arguments,
                capture_output=True,
                text=True,
                check=False,
                timeout=_TOOL_TIMEOUT_SECONDS,
                env=os.environ,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LaneExecutorError(
                f"scheduler tool invocation failed: {arguments[0]}: {error!r}"
            ) from error


class _OutputStreamer:
    """Relay the job's output files to this process as they grow."""

    def __init__(self, compiled: CompiledSubmitDescription) -> None:
        self._sources = (
            (compiled.output_path, sys.stdout),
            (compiled.error_path, sys.stderr),
        )
        self._offsets = [0, 0]

    def pump(self) -> None:
        for index, (path, sink) in enumerate(self._sources):
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(self._offsets[index])
                    fresh = handle.read()
                    self._offsets[index] = handle.tell()
            except FileNotFoundError:
                continue
            if fresh:
                sink.write(fresh)
                sink.flush()
