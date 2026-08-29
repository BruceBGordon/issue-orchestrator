# pyright: strict
"""HTCondor adapter for the LaneExecutor port."""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
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
from ...infra.containment import TEARDOWN_SIGNALS, describe_exception
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
from .tools import TOOL_TIMEOUT_SECONDS, CondorTools
from .tools import TOOL_TIMEOUT_SECONDS, CondorTools

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


@dataclass(frozen=True, slots=True)
class _CollectionBudget:
    """One wall-clock allowance for an ENTIRE collection attempt.

    Round 2 finding 1: bounding only the file-wait left the earlier
    stages unbounded, so the configuration lookup could spend the whole
    tool timeout (30s) before a 2s "wait" even started — a 2s
    cancellation budget measured 2.56s with a slow lookup, and a slower
    one would have measured far worse. A budget belongs to the whole
    operation: every stage asks what is left, a stage with nothing left
    is skipped rather than started, and the subprocess that dominates
    the cost is capped by the same clock as the poll loop.
    """

    expires_at: float

    @classmethod
    def lasting(cls, seconds: float) -> _CollectionBudget:
        if type(seconds) is not float or seconds < 0:
            raise ValueError("_CollectionBudget.lasting needs non-negative seconds")
        return cls(time.monotonic() + seconds)

    def remaining_seconds(self) -> float:
        return self.expires_at - time.monotonic()

    def exhausted(self) -> bool:
        return self.remaining_seconds() <= 0.0


def _job_accounting_budget() -> _CollectionBudget:
    """The allowance for collecting a concluded lane's accounting.

    Not a cancellation: nobody is waiting on a keystroke, so this is the
    generous bound. The cancellation path builds its own, much tighter
    one and spends it across the whole wind-down.
    """
    return _CollectionBudget.lasting(_JOB_ACCOUNTING_WAIT_SECONDS)


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
                self._collect_job_accounting(
                    job_id, run_directory, _job_accounting_budget()
                )
            return terminal
        except LaneExecutorError as error:
            if job_id is not None and streams is not None:
                self._remove(job_id, TOOL_TIMEOUT_SECONDS)
                streams.pump()
                self._collect_job_accounting(
                    job_id, run_directory, _job_accounting_budget()
                )
            raise LaneExecutorError(
                f"{error} (lane diagnostics retained at {run_directory})"
            ) from error
        except BaseException as unwinding:
            # Cancellation or supervisor death: the job must not outlive us.
            if job_id is not None and streams is not None:
                self._wind_down_cancelled(
                    job_id, streams, run_directory, unwinding
                )
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

    def _wind_down_cancelled(
        self,
        job_id: str,
        streams: _OutputStreamer,
        run_directory: Path,
        unwinding: BaseException,
    ) -> None:
        """Wind a lane down when its caller is being torn down.

        The job must not outlive us, its last output belongs in the
        retained directory, and so does its accounting — the removal
        here is exactly what makes the ClassAd appear.

        ONE clock and ONE policy, both from the first instruction (round
        3). Round 2 put the budget and the exception boundary around the
        accounting only, which left the two stages before it — the
        removal and the stream drain — outside both: a slow ``condor_rm``
        spent the general tool timeout on top of the budget, a second
        interrupt during it propagated with no ``__cause__``, and a
        ``SystemExit`` during it replaced the original ending outright.
        A method that owns an operation owns all of it, so the budget is
        created here and every stage below draws from it.

        Nothing this does may rewrite why the lane ended, so ordinary
        failures and ``SystemExit`` are contained — and RECORDED, because
        a containment that reports nothing is indistinguishable from a
        bug. The single exception is the teardown policy: a second
        interrupt arriving during cleanup means the operator is no longer
        willing to wait for it, so it wins over the first — chained,
        never substituted in silence, so the original ending stays
        readable as ``__cause__`` even when both are interrupts.
        """
        try:
            # Inside the boundary, not before it: NOTHING in this body
            # may precede the policy, and "the budget" was the last
            # instruction still doing so (round 4). Reading the clock is
            # about the least likely thing here to fail, which is
            # exactly why it was easy to leave outside and exactly why
            # leaving it there was wrong — the guarantee is structural,
            # not a bet on which statements can throw.
            budget = _CollectionBudget.lasting(
                _CANCELLED_ACCOUNTING_WAIT_SECONDS
            )
            self._remove(job_id, budget.remaining_seconds())
            if not budget.exhausted():
                streams.pump()
            self._collect_job_accounting(job_id, run_directory, budget)
        except TEARDOWN_SIGNALS as interrupt:
            raise interrupt from unwinding
        except BaseException as contained:
            print(
                "condor lane: cancellation cleanup gave up after "
                f"{describe_exception(contained)}",
                file=sys.stderr,
            )

    def _collect_job_accounting(
        self,
        job_id: str,
        run_directory: Path,
        budget: _CollectionBudget,
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

        The ``budget`` belongs to the caller's whole operation, not to
        this collection: on the cancellation path the removal has
        already drawn from it. Every stage below asks what is left
        before it starts, so nothing new begins after expiry (round 2
        finding 1, round 3 finding 1).
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
        if budget.exhausted():
            print(
                "condor lane: no time left in the per-job accounting budget; "
                "nothing was collected",
                file=sys.stderr,
            )
            return
        try:
            directory = self._per_job_history_directory(budget)
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
        # The SAME budget the lookup drew from: whatever it spent is
        # gone. Exhaustion is checked BEFORE the stat, not after it, so
        # a stuck filesystem cannot start one more probe past the
        # deadline, and the sleep never runs past the end of it.
        while not budget.exhausted() and not source.is_file():
            time.sleep(min(_POLL_INTERVAL_SECONDS, budget.remaining_seconds()))
        if budget.exhausted():
            print(
                "condor lane: the per-job accounting budget ran out before "
                f"{source} could be collected",
                file=sys.stderr,
            )
            return
        try:
            shutil.copyfile(source, run_directory / _JOB_ACCOUNTING_FILE_NAME)
        except OSError as error:
            print(
                f"condor lane: could not collect per-job accounting from "
                f"{source}: {error}",
                file=sys.stderr,
            )

    def _per_job_history_directory(
        self, budget: _CollectionBudget
    ) -> Path | None:
        """Where this pool drops each job's final ClassAd, or None.

        Read from the pool's own effective configuration rather than
        recomputed here, so the helpers that WRITE the knob remain its
        only authors — and read through ``read_configuration``, the
        scrubbed channel (#7132), because this is a question ABOUT the
        pool: a ``_CONDOR_PER_JOB_HISTORY_DIR`` exported into this
        process would otherwise answer for the caller's environment and
        send the collector looking somewhere the daemons never write.

        Capped by the CALLER'S budget, not by the general tool timeout:
        this lookup is one stage of a bounded collection, and a pool
        whose tools have gone slow must not spend a cancelling lane's
        whole allowance here (round 2 finding 1).
        """
        completed = self._tools.read_configuration(
            _PER_JOB_HISTORY_CONFIGURATION_KNOB,
            timeout_seconds=budget.remaining_seconds(),
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

    def _remove(self, job_id: str, timeout_seconds: float) -> None:
        """Remove the job, within the CALLER'S allowance.

        The timeout is required rather than defaulted: a removal during
        a cancellation draws from the same budget as everything else
        that cleanup does, and one that quietly took the general tool
        timeout instead is what made a 2s cancellation budget measure
        5.35s (round 3 finding 1).
        """
        try:
            self._tools.invoke(
                (str(self._tools.remove), job_id),
                timeout_seconds=timeout_seconds,
            )
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
