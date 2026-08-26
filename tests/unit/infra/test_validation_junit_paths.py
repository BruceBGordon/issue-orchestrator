from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_orchestrator.infra.e2e_reports import (
    JUNIT_REPORT_FRESHNESS_GRACE_SECONDS,
)
from issue_orchestrator.infra.validation_junit_paths import (
    validation_record_junit_modified_after,
    validation_started_epoch,
)
from issue_orchestrator.ports.session_output import ValidationRecord


def _validation_record(*, started_at: str) -> ValidationRecord:
    ended_at = (datetime.fromisoformat(started_at) + timedelta(seconds=1)).isoformat()
    return ValidationRecord(
        schema_version=1,
        suite="publish_gate",
        head_sha="a" * 40,
        passed=True,
        exit_code=0,
        command="pytest",
        started_at=started_at,
        ended_at=ended_at,
    )


def test_validation_started_epoch_respects_explicit_timezone_offset() -> None:
    expected = datetime(2026, 5, 7, 14, 30, tzinfo=timezone.utc).timestamp()

    assert validation_started_epoch("2026-05-07T09:30:00-05:00") == pytest.approx(
        expected
    )


def test_validation_started_epoch_interprets_naive_values_as_local_time() -> None:
    raw_started_at = "2026-05-07T09:30:00"

    assert validation_started_epoch(raw_started_at) == pytest.approx(
        datetime.fromisoformat(raw_started_at).timestamp()
    )


def test_validation_record_junit_modified_after_applies_shared_grace() -> None:
    started_epoch = datetime(2026, 5, 7, 14, 30, tzinfo=timezone.utc).timestamp()

    assert validation_record_junit_modified_after(
        _validation_record(started_at="2026-05-07T14:30:00+00:00")
    ) == pytest.approx(started_epoch - JUNIT_REPORT_FRESHNESS_GRACE_SECONDS)
