"""A finished republish job may finalize ONLY on a successful terminal publish.

Extracted from ``drain_completed_retries``, whose five inline arms shared one
rule that was easy to lose in the loop's token/tombstone bookkeeping: anything
short of success LEAVES THE ISSUE RETRYABLE — never a permanent lockout.
"""

from dataclasses import dataclass

import pytest

from issue_orchestrator.control.publish_retry_drain import (
    DrainedRetryDisposition,
    classify_drained_retry,
)


@dataclass
class _Result:
    success: bool = True
    is_non_terminal: bool = False
    message: str = ""
    review_exchange_deferred: bool = False
    validation_failed_rerouted: bool = False


def test_a_successful_terminal_publish_may_finalize():
    outcome = classify_drained_retry(job_error=None, result=_Result())

    assert outcome.may_finalize
    assert outcome.disposition is DrainedRetryDisposition.FINALIZE


def test_a_raised_job_leaves_the_issue_retryable_and_reads_as_a_fault():
    outcome = classify_drained_retry(job_error=RuntimeError("boom"), result=None)

    assert not outcome.may_finalize
    assert outcome.faulted
    assert "boom" in outcome.reason


def test_a_job_with_no_result_is_a_fault_too():
    outcome = classify_drained_retry(job_error=None, result=None)

    assert not outcome.may_finalize
    assert outcome.faulted
    assert "without a result" in outcome.reason


def test_a_non_terminal_republish_is_not_a_fault_but_never_finalizes():
    """A deferred review exchange means publish has not completed at all."""
    outcome = classify_drained_retry(
        job_error=None,
        result=_Result(is_non_terminal=True, review_exchange_deferred=True),
    )

    assert not outcome.may_finalize
    assert not outcome.faulted
    assert "non-terminal" in outcome.reason


def test_a_failed_republish_leaves_the_issue_retryable():
    outcome = classify_drained_retry(
        job_error=None, result=_Result(success=False, message="push rejected")
    )

    assert not outcome.may_finalize
    assert "push rejected" in outcome.reason


@pytest.mark.parametrize(
    "job_error,result",
    [
        (RuntimeError("boom"), _Result()),
        (None, _Result(is_non_terminal=True, success=True)),
    ],
)
def test_a_success_flag_never_overrides_an_earlier_disqualifier(job_error, result):
    """Order matters: a raised job or a non-terminal result wins over success."""
    assert not classify_drained_retry(job_error=job_error, result=result).may_finalize
