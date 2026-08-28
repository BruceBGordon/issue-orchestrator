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
    LaneJobSuspended,
    classify_event_log,
)
from .submit_compiler import CompiledSubmitDescription, compile_submit_description

_TOOL_TIMEOUT_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 0.1
# The scheduler's periodic expressions evaluate on an interval, so a
# deadline removal can land this much after the exact bound; the outer
# wait must tolerate it before declaring the backend unresponsive.
_SCHEDULER_SLACK_SECONDS = 120.0
# Queue-health bound, deliberately independent of the lane's own
# deadline: queue wait is scheduling machinery and is never billed to
# the lane's budget (a job may legitimately wait behind an exclusive
# token or a full pool for longer than its own runtime deadline). This
# bound only catches a structurally dead pool that accepts submissions
# and never matches them.
_ADMISSION_TIMEOUT_SECONDS = 600.0


# Where scripts/condor-personal.sh installs the personal pool. Resolving
# it here means a config-file opt-in works from any process — the
# orchestrator's validation runner, a bare shell, a hook — without every
# caller having to source the pool's environment first.
PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE = "ISSUE_ORCHESTRATOR_CONDOR_HOME"
_DEFAULT_PERSONAL_POOL_HOME = Path.home() / ".local/share/issue-orchestrator/condor"
_TOOL_EXECUTABLES = (
    ("submit", "condor_submit"),
    ("remove", "condor_rm"),
    ("query", "condor_q"),
)


@dataclass(frozen=True, slots=True)
class CondorTools:
    """Absolute paths to the scheduler's command-line tools.

    ``pool_config`` is the configuration file the tools must use; it is
    ``None`` for a system installation whose ambient configuration is
    already correct, and set when the tools come from the personal-pool
    install, whose configuration lives beside its binaries.
    """

    submit: Path
    remove: Path
    query: Path
    pool_config: Path | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("submit", self.submit),
            ("remove", self.remove),
            ("query", self.query),
        ):
            if not isinstance(cast(object, value), Path) or not value.is_absolute():
                raise ValueError(f"CondorTools.{field_name} must be an absolute Path")
        if self.pool_config is not None and (
            not isinstance(cast(object, self.pool_config), Path)
            or not self.pool_config.is_absolute()
        ):
            raise ValueError("CondorTools.pool_config must be an absolute Path")

    @classmethod
    def resolve(cls) -> CondorTools:
        """Resolve from PATH first, then the personal-pool install.

        Fails loudly when neither exists: the backend is opt-in and a
        configured-but-missing pool must never degrade silently.
        """
        from_path = cls._resolve_from_path()
        if from_path is not None:
            return from_path
        from_personal = cls._resolve_from_personal_install()
        if from_personal is not None:
            return from_personal
        raise LaneExecutorUnavailableError(
            "no scheduler tools on PATH and no personal pool under "
            f"{cls._personal_pool_home()}: the condor lane backend is opt-in "
            "and requires a running HTCondor pool "
            "(run scripts/condor-personal.sh up, see docs/user/condor_lanes.md)"
        )

    @classmethod
    def _resolve_from_path(cls) -> CondorTools | None:
        located: dict[str, Path] = {}
        for field_name, executable in _TOOL_EXECUTABLES:
            found = shutil.which(executable)
            if found is None:
                return None
            located[field_name] = Path(found).resolve()
        return cls(**located)

    @classmethod
    def _resolve_from_personal_install(cls) -> CondorTools | None:
        home = cls._personal_pool_home()
        for install in sorted(home.glob("condor-*"), reverse=True):
            binaries = install / "bin"
            pool_config = install / "etc" / "condor_config"
            located: dict[str, Path] = {}
            for field_name, executable in _TOOL_EXECUTABLES:
                candidate = binaries / executable
                if not candidate.is_file() or not os.access(candidate, os.X_OK):
                    located.clear()
                    break
                located[field_name] = candidate.resolve()
            if located and pool_config.is_file():
                return cls(pool_config=pool_config.resolve(), **located)
        return None

    @staticmethod
    def _personal_pool_home() -> Path:
        override = os.environ.get(PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE)
        if override:
            return Path(override)
        return _DEFAULT_PERSONAL_POOL_HOME


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
        # Run-directory lifecycle owns the directory from birth: a clean
        # completion deletes it; every other ending — including
        # preparation and submission failures — retains it as the
        # diagnostic record and says so. Diagnostics are worthless if
        # silently discarded and unbounded if silently retained.
        retain_run_directory = True
        job_id: str | None = None
        streams: _OutputStreamer | None = None
        try:
            compiled = compile_submit_description(command, resources, run_directory)
            compiled.exec_script_path.write_text(
                compiled.exec_script_text, encoding="utf-8"
            )
            compiled.exec_script_path.chmod(0o755)
            submit_path = run_directory / "lane.sub"
            submit_path.write_text(compiled.text, encoding="utf-8")
            streams = _OutputStreamer(compiled)
            job_id = self._submit(submit_path)
            terminal = self._follow_job(command, compiled, job_id, streams)
            if type(terminal) is LaneCompleted and terminal.exit_code == 0:
                retain_run_directory = False
            return terminal
        except LaneExecutorError as error:
            if job_id is not None and streams is not None:
                self._remove(job_id)
                streams.pump()
            raise LaneExecutorError(
                f"{error} (lane diagnostics retained at {run_directory})"
            ) from error
        except BaseException:
            # Cancellation or supervisor death: the job must not outlive us.
            if job_id is not None and streams is not None:
                self._remove(job_id)
                streams.pump()
            raise
        finally:
            if retain_run_directory:
                print(
                    f"condor lane {command.work_key.value}: diagnostics "
                    f"retained at {run_directory}",
                    file=sys.stderr,
                )
            else:
                shutil.rmtree(run_directory, ignore_errors=True)

    def _follow_job(
        self,
        command: LaneCommand,
        compiled: CompiledSubmitDescription,
        job_id: str,
        streams: _OutputStreamer,
    ) -> LaneOutcome:
        """Poll the job to a terminal outcome under the two watchdogs.

        Admission and runtime are bounded independently: queue wait is
        scheduling machinery and is never billed to the lane's budget.
        """
        submitted_at = time.monotonic()
        execute_observed_at: float | None = None
        # Frozen time (machine-load backoff) is charged to nothing: not
        # the runtime watchdog, not the observed runtime the learning
        # loop records. Accumulated from observation, poll by poll.
        suspension_observed_seconds = 0.0
        previous_poll_at = time.monotonic()
        while True:
            streams.pump()
            state = self._observe(compiled)
            now = time.monotonic()
            if type(state) is LaneJobSuspended:
                suspension_observed_seconds += now - previous_poll_at
            previous_poll_at = now
            if execute_observed_at is None and type(state) is not LaneJobPending:
                execute_observed_at = now
            terminal = self._map_terminal(state)
            if terminal is not None:
                streams.pump()
                return terminal
            self._enforce_watchdogs(
                command,
                job_id,
                submitted_at,
                execute_observed_at,
                suspension_observed_seconds,
            )
            time.sleep(_POLL_INTERVAL_SECONDS)

    @staticmethod
    def _enforce_watchdogs(
        command: LaneCommand,
        job_id: str,
        submitted_at: float,
        execute_observed_at: float | None,
        suspension_observed_seconds: float,
    ) -> None:
        now = time.monotonic()
        if execute_observed_at is None:
            if now >= submitted_at + _ADMISSION_TIMEOUT_SECONDS:
                raise LaneExecutorError(
                    "scheduler never started the lane: queued for "
                    f"{_ADMISSION_TIMEOUT_SECONDS:.0f}s "
                    f"(lane={command.work_key.value} job={job_id})"
                )
        elif now >= (
            execute_observed_at
            + command.deadline.timeout_seconds
            + suspension_observed_seconds
            + _SCHEDULER_SLACK_SECONDS
        ):
            raise LaneExecutorError(
                "scheduler did not conclude the running lane inside its "
                f"deadline plus slack: lane={command.work_key.value} "
                f"job={job_id}"
            )

    def _map_terminal(self, state: LaneJobState) -> LaneOutcome | None:
        if (
            type(state) is LaneJobPending
            or type(state) is LaneJobRunning
            or type(state) is LaneJobSuspended
        ):
            return None
        # Runtime is the scheduler's own record: the classifier computes
        # execute→terminal from the event-log timestamps and subtracts
        # suspended intervals, so neither queue wait, frozen time, nor
        # this process's poll-observation lag reaches the number. The
        # poll-side suspension accumulator below serves only the
        # watchdog.
        if type(state) is LaneJobExited:
            return LaneCompleted(
                state.exit_code, state.runtime_seconds, state.queue_wait_seconds
            )
        if type(state) is LaneJobKilledBySignal:
            return LaneCompleted(
                128 + state.signal_number,
                state.runtime_seconds,
                state.queue_wait_seconds,
            )
        if type(state) is LaneJobDeadlineRemoved:
            return LaneTimedOut(state.runtime_seconds)
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
        try:
            return classify_event_log(log_text)
        except ValueError as error:
            raise LaneExecutorError(
                f"scheduler event log is malformed: {error}"
            ) from error

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

    def _run_tool(
        self, arguments: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        if self._tools.pool_config is not None:
            environment["CONDOR_CONFIG"] = str(self._tools.pool_config)
        try:
            return subprocess.run(
                arguments,
                capture_output=True,
                text=True,
                check=False,
                timeout=_TOOL_TIMEOUT_SECONDS,
                env=environment,
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
