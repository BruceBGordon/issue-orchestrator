"""Inbound anti-corruption: captured event logs classify to typed states.

The fixtures reproduce real user-log output captured from a live
personal pool (25.8.2), so the parser is proven against the actual
format, not a paraphrase of it.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.adapters.condor.event_classifier import (
    LaneJobDeadlineRemoved,
    LaneJobExited,
    LaneJobFaulted,
    LaneJobKilledBySignal,
    LaneJobPending,
    LaneJobRemoved,
    LaneJobRunning,
    classify_event_log,
)

_SUBMITTED = (
    "000 (002.000.000) 2026-08-26 14:08:29 Job submitted from host: "
    "<192.168.86.29:57485?addrs=192.168.86.29-57485&alias=host.lan>\n"
    "...\n"
)
_EXECUTING = (
    "001 (002.000.000) 2026-08-26 14:08:32 Job executing on host: "
    "<192.168.86.29:57485?addrs=192.168.86.29-57485&alias=host.lan>\n"
    "\tSlotName: slot1_1@host.lan\n"
    "\tCpus = 1\n"
    "...\n"
)
_IMAGE_SIZE = (
    "006 (002.000.000) 2026-08-26 14:08:32 Image size of job updated: 100\n"
    "\t2  -  MemoryUsage of job (MB)\n"
    "...\n"
)
_TERMINATED_ZERO = (
    "005 (002.000.000) 2026-08-26 14:08:32 Job terminated.\n"
    "\t(1) Normal termination (return value 0)\n"
    "\t\tUsr 0 00:00:00, Sys 0 00:00:00  -  Run Remote Usage\n"
    "...\n"
)
_TERMINATED_SEVENTEEN = (
    "005 (002.000.000) 2026-08-26 14:08:32 Job terminated.\n"
    "\t(1) Normal termination (return value 17)\n"
    "...\n"
)
_TERMINATED_SIGNAL = (
    "005 (002.000.000) 2026-08-26 14:08:32 Job terminated.\n"
    "\t(0) Abnormal termination (signal 9)\n"
    "...\n"
)
_ABORTED_BY_DEADLINE = (
    "009 (002.000.000) 2026-08-26 14:09:32 Job was aborted.\n"
    "\tThe job attribute PeriodicRemove expression "
    "'(JobStatus == 2) && ((time() - JobCurrentStartDate) > 60)' "
    "evaluated to TRUE\n"
    "...\n"
)
_ABORTED_BY_OPERATOR = (
    "009 (002.000.000) 2026-08-26 14:09:32 Job was aborted.\n"
    "\tvia condor_rm (by user brucegordon)\n"
    "...\n"
)
_HELD = (
    "012 (002.000.000) 2026-08-26 14:09:32 Job was held.\n"
    "\tError from slot1_1@host.lan: Failed to execute\n"
    "\tCode 6 Subcode 2\n"
    "...\n"
)


def test_empty_log_is_pending() -> None:
    assert type(classify_event_log("")) is LaneJobPending


def test_submitted_only_is_pending() -> None:
    assert type(classify_event_log(_SUBMITTED)) is LaneJobPending


def test_executing_is_running_and_updates_are_informational() -> None:
    assert type(classify_event_log(_SUBMITTED + _EXECUTING)) is LaneJobRunning
    assert (
        type(classify_event_log(_SUBMITTED + _EXECUTING + _IMAGE_SIZE))
        is LaneJobRunning
    )


def test_normal_termination_carries_the_exact_exit_code() -> None:
    zero = classify_event_log(_SUBMITTED + _EXECUTING + _TERMINATED_ZERO)
    seventeen = classify_event_log(_SUBMITTED + _EXECUTING + _TERMINATED_SEVENTEEN)
    assert zero == LaneJobExited(0, 0.0)
    assert seventeen == LaneJobExited(17, 0.0)


def test_signal_termination_carries_the_signal() -> None:
    state = classify_event_log(_SUBMITTED + _EXECUTING + _TERMINATED_SIGNAL)
    assert state == LaneJobKilledBySignal(9, 0.0)


def test_deadline_removal_is_distinguished_from_operator_removal() -> None:
    deadline = classify_event_log(_SUBMITTED + _EXECUTING + _ABORTED_BY_DEADLINE)
    operator = classify_event_log(_SUBMITTED + _EXECUTING + _ABORTED_BY_OPERATOR)
    assert type(deadline) is LaneJobDeadlineRemoved
    assert type(operator) is LaneJobRemoved
    assert "condor_rm" in operator.detail


def test_held_job_is_a_fault_with_detail() -> None:
    state = classify_event_log(_SUBMITTED + _EXECUTING + _HELD)
    assert type(state) is LaneJobFaulted
    assert "Failed to execute" in state.detail


def test_complete_termination_without_verdict_fails_loudly() -> None:
    corrupt = (
        "005 (002.000.000) 2026-08-26 14:08:32 Job terminated.\n"
        "\tunrecognized verdict line\n"
        "...\n"
    )
    with pytest.raises(ValueError, match="neither a return value nor a signal"):
        classify_event_log(_SUBMITTED + _EXECUTING + corrupt)


def test_unfinished_trailing_record_keeps_the_prior_state() -> None:
    """A poll may land while the scheduler is mid-record. A terminal
    banner whose body and delimiter have not been written yet must not
    raise and must not be classified — the state stays at the last
    complete record."""
    torn_termination = "005 (002.000.000) 2026-08-26 14:08:32 Job terminated.\n"
    torn_abort = "009 (002.000.000) 2026-08-26 14:09:32 Job was aborted.\n"
    base = _SUBMITTED + _EXECUTING
    assert type(classify_event_log(base + torn_termination)) is LaneJobRunning
    assert type(classify_event_log(base + torn_abort)) is LaneJobRunning


def test_every_prefix_of_a_full_log_classifies_without_error() -> None:
    """Progressive truncation: every byte-prefix a poll could observe
    yields a state, never an exception, and terminal classification
    appears only once the closing delimiter is present."""
    full = _SUBMITTED + _EXECUTING + _IMAGE_SIZE + _ABORTED_BY_DEADLINE
    states = [classify_event_log(full[:cut]) for cut in range(len(full) + 1)]
    assert type(states[-1]) is LaneJobDeadlineRemoved
    for cut in range(len(full)):
        state = states[cut]
        if type(state) is LaneJobDeadlineRemoved:
            # Only permissible once the abort record's delimiter is in.
            assert "..." in full[:cut].rsplit("009 ", 1)[-1]
        else:
            assert type(state) in (LaneJobPending, LaneJobRunning)


_EXECUTING_LATER = _EXECUTING.replace("14:08:32", "14:08:40")
_TERMINATED_AT_0905 = _TERMINATED_ZERO.replace("14:08:32", "14:09:05")


def test_runtime_is_the_event_log_span_not_an_observation_clock() -> None:
    """B1 (#7117 review): execute 14:08:32 → terminate 14:09:05 must
    report 33s even when the whole log is only read AFTER the job is
    long dead — the case where a poll clock collapses toward zero."""
    state = classify_event_log(_SUBMITTED + _EXECUTING + _TERMINATED_AT_0905)
    assert state == LaneJobExited(0, 33.0)


def test_restarted_job_reports_the_final_execution_span() -> None:
    state = classify_event_log(
        _SUBMITTED + _EXECUTING + _EXECUTING_LATER + _TERMINATED_AT_0905
    )
    assert state == LaneJobExited(0, 25.0)


def test_terminal_without_execute_fails_loudly() -> None:
    with pytest.raises(ValueError, match="no execute event"):
        classify_event_log(_SUBMITTED + _TERMINATED_ZERO)


def test_backwards_timestamps_fail_loudly() -> None:
    backwards = _TERMINATED_ZERO.replace("14:08:32", "14:07:00")
    with pytest.raises(ValueError, match="backwards"):
        classify_event_log(_SUBMITTED + _EXECUTING + backwards)


def test_deadline_removal_carries_the_span() -> None:
    aborted_later = _ABORTED_BY_DEADLINE.replace("14:09:32", "14:09:32")
    state = classify_event_log(_SUBMITTED + _EXECUTING + aborted_later)
    assert type(state) is LaneJobDeadlineRemoved
    assert state.runtime_seconds == 60.0
