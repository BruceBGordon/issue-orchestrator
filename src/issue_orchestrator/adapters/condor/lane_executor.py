# pyright: strict
"""HTCondor adapter for the LaneExecutor port."""

from __future__ import annotations

import os
import re
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

# Per-job accounting (#7127). The pool is configured (by
# scripts/condor-personal.sh and the execenv image) to drop every
# finished job's complete final ClassAd at
# <PER_JOB_HISTORY_DIR>/history.<cluster>.<proc>. A retained run
# directory collects its own job's file, so a failed lane's full
# accounting travels with its diagnostics instead of living in a
# rotating global history nothing correlates back to the lane.
_PER_JOB_HISTORY_CONFIGURATION_KNOB = "PER_JOB_HISTORY_DIR"
_JOB_ACCOUNTING_FILE_NAME = "lane.classad"
# The schedd writes the ClassAd when the job leaves the QUEUE, which is
# shortly after its terminal event reaches the log. Bounded so a pool
# that never writes it costs seconds, never the gate.
_JOB_ACCOUNTING_WAIT_SECONDS = 10.0
# Much tighter while unwinding a cancellation: the operator pressed
# Ctrl-C and is owed a prompt exit, so the ClassAd gets the couple of
# seconds it normally needs after a removal and no more.
_CANCELLED_ACCOUNTING_WAIT_SECONDS = 2.0
_JOB_IDENTIFIER_RE = re.compile(r"\d+\.\d+")


# Where scripts/condor-personal.sh installs the personal pool. Resolving
# it here means a config-file opt-in works from any process — the
# orchestrator's validation runner, a bare shell, a hook — without every
# caller having to source the pool's environment first.
PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE = "ISSUE_ORCHESTRATOR_CONDOR_HOME"
_DEFAULT_PERSONAL_POOL_HOME = Path.home() / ".local/share/issue-orchestrator/condor"
# The scheduler's per-process macro override prefix: `_CONDOR_<KNOB>`
# in the environment overrides <KNOB> for that process only, never for
# the daemons. Scrubbed on the configuration-READ path (an answer about
# the pool must come from the pool) and deliberately preserved on the
# submit path (`getenv = true` carries it to the lane).
#
# The scheduler matches this prefix CASE-INSENSITIVELY while POSIX
# environments are case-SENSITIVE, so `_condor_X` and `_CoNdOr_X` are
# distinct variables that the tool nonetheless honours identically
# (verified live: all four casings injected). Matching must therefore
# be case-insensitive too, or the scrub is a lowercase bypass away
# from useless (round 4, #7132 review).
_MACRO_OVERRIDE_PREFIX = "_CONDOR_"


def _is_macro_override(name: str) -> bool:
    return name.upper().startswith(_MACRO_OVERRIDE_PREFIX)
_TOOL_EXECUTABLES = (
    ("submit", "condor_submit"),
    ("remove", "condor_rm"),
    ("query", "condor_q"),
    ("config_query", "condor_config_val"),
)


@dataclass(frozen=True, slots=True)
class CondorTools:
    """Absolute paths to the scheduler's command-line tools, and the
    single boundary through which this package invokes them.

    ``pool_config`` is the configuration file the tools must use; it is
    ``None`` for a system installation whose ambient configuration is
    already correct, and set when the tools come from the personal-pool
    install, whose configuration lives beside its binaries. Because
    every tool invocation must run under that configuration, invocation
    belongs here rather than in each caller: :meth:`invoke` is the only
    way this package runs a scheduler tool, so a caller cannot
    accidentally read a different pool than the one lanes are submitted
    to.

    ``config_query`` reads the pool's effective configuration and is
    what the policy self-check consults.
    """

    submit: Path
    remove: Path
    query: Path
    config_query: Path
    pool_config: Path | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("submit", self.submit),
            ("remove", self.remove),
            ("query", self.query),
            ("config_query", self.config_query),
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

    def invoke(
        self, arguments: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        """Run one scheduler tool against this pool, bounded in time.

        The caller's environment is passed through, deliberately. The
        submit description sets ``getenv = true``, so the environment
        this process hands to ``condor_submit`` is the environment the
        LANE ITSELF inherits — carrying it faithfully is the contract,
        not an oversight, and quietly deleting a category of variables
        from it would surprise whoever set them.

        A non-zero return code is the caller's to interpret — tools use
        it for ordinary answers as well as failures. Only an
        invocation that never produced one (missing binary, hung tool)
        is a backend fault.
        """
        return self._run(arguments, scrub_macro_overrides=False)

    def read_configuration(
        self, *query: str
    ) -> subprocess.CompletedProcess[str]:
        """Ask the pool what its own configuration says.

        Deliberately asymmetric with :meth:`invoke`, and the asymmetry
        is the point. ``_CONDOR_<KNOB>`` overrides <KNOB> for one
        process and is invisible to the DAEMONS, so an answer read
        through one describes the caller's environment rather than the
        pool — an ambient export could mask real drift (verified: the
        tool answers "Not defined" for a knob the pool genuinely sets
        wrong) or manufacture fake drift. A question asked ABOUT the
        pool must be answered BY the pool, so overrides are scrubbed
        here and only here (residual on N1, #7132 review).

        Taking the query rather than a full argv is part of the same
        guarantee: this path always runs the configuration tool, and
        no submission can be routed through it by mistake.
        """
        return self._run(
            (str(self.config_query), *query), scrub_macro_overrides=True
        )

    def _run(
        self, arguments: tuple[str, ...], *, scrub_macro_overrides: bool
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        if scrub_macro_overrides:
            environment = {
                key: value
                for key, value in environment.items()
                if not _is_macro_override(key)
            }
        if self.pool_config is not None:
            environment["CONDOR_CONFIG"] = str(self.pool_config)
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
            else:
                # Retention and accounting collection are ONE decision:
                # whatever is worth keeping the directory for is worth
                # the scheduler's own final word on the job.
                self._collect_job_accounting(job_id, run_directory)
            return terminal
        except LaneExecutorError as error:
            if job_id is not None and streams is not None:
                self._remove(job_id)
                streams.pump()
                self._collect_job_accounting(job_id, run_directory)
            raise LaneExecutorError(
                f"{error} (lane diagnostics retained at {run_directory})"
            ) from error
        except BaseException:
            # Cancellation or supervisor death: the job must not outlive us.
            if job_id is not None and streams is not None:
                self._remove(job_id)
                streams.pump()
                # This directory is retained too, so it gets the same
                # accounting as every other retained one — the removal
                # above is exactly what makes the ClassAd appear. Two
                # guards keep that from costing anything: a much shorter
                # wait, and a containment so a slow or unhappy
                # collection can never displace the exception that is
                # already unwinding (a second Ctrl-C included).
                try:
                    self._collect_job_accounting(
                        job_id,
                        run_directory,
                        wait_seconds=_CANCELLED_ACCOUNTING_WAIT_SECONDS,
                    )
                except BaseException:
                    pass
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

    def _collect_job_accounting(
        self,
        job_id: str,
        run_directory: Path,
        wait_seconds: float = _JOB_ACCOUNTING_WAIT_SECONDS,
    ) -> None:
        """Copy this job's final ClassAd into the retained diagnostics.

        Runs on EVERY path that retains the run directory, cancellation
        included (round 1 finding C) — the retention and accounting
        decisions are one, and a lane killed by Ctrl-C is exactly the
        one whose final ClassAd a reader wants.

        Best-effort by construction, and deliberately so: this runs while
        a lane is ALREADY ending badly, so a diagnostic that could raise
        would replace the real failure with its own. Every giving-up
        path says why on stderr, beside the retention line, so a pool
        that stopped writing per-job accounting is visible rather than
        quietly unhelpful.
        """
        if _JOB_IDENTIFIER_RE.fullmatch(job_id) is None:
            # The ClassAd file is named history.<cluster>.<proc>, so the
            # identifier is also a path component: never build a read
            # path out of a token that is not that shape.
            print(
                f"condor lane: unexpected job identifier {job_id!r}; no "
                "per-job accounting was collected",
                file=sys.stderr,
            )
            return
        try:
            directory = self._per_job_history_directory()
        except LaneExecutorError as error:
            print(
                f"condor lane: per-job accounting lookup failed: {error}",
                file=sys.stderr,
            )
            return
        if directory is None:
            print(
                "condor lane: this pool sets no "
                f"{_PER_JOB_HISTORY_CONFIGURATION_KNOB}, so no per-job "
                "accounting was collected (scripts/condor-personal.sh up "
                "configures it)",
                file=sys.stderr,
            )
            return
        source = directory / f"history.{job_id}"
        deadline = time.monotonic() + wait_seconds
        while not source.is_file() and time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL_SECONDS)
        try:
            shutil.copyfile(source, run_directory / _JOB_ACCOUNTING_FILE_NAME)
        except OSError as error:
            print(
                f"condor lane: could not collect per-job accounting from "
                f"{source}: {error}",
                file=sys.stderr,
            )

    def _per_job_history_directory(self) -> Path | None:
        """Where this pool drops each job's final ClassAd, or None.

        Read from the pool's own effective configuration rather than
        recomputed here, so the helpers that WRITE the knob remain its
        only authors — and read through ``read_configuration``, the
        scrubbed channel (#7132), because this is a question ABOUT the
        pool: a ``_CONDOR_PER_JOB_HISTORY_DIR`` exported into this
        process would otherwise answer for the caller's environment and
        send the collector looking somewhere the daemons never write.
        """
        completed = self._tools.read_configuration(
            _PER_JOB_HISTORY_CONFIGURATION_KNOB
        )
        if completed.returncode != 0:
            return None
        value = completed.stdout.strip()
        if not value or value.lower() == "undefined":
            return None
        return Path(value)

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
        completed = self._tools.invoke(
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
            self._tools.invoke((str(self._tools.remove), job_id))
        except LaneExecutorError:
            # Best-effort during unwinding; the primary error wins.
            return

    def _require_reachable_scheduler(self) -> None:
        completed = self._tools.invoke((str(self._tools.query), "-limit", "1"))
        if completed.returncode != 0:
            raise LaneExecutorUnavailableError(
                "the scheduler is installed but not reachable: "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )


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
