"""Fail-fast contracts for typed validation timing evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_orchestrator.domain.validation_timing import (
    PrepushGateTimingSummary,
    ValidationConfiguration,
    ValidationDiskDeltaStatus,
    ValidationDiskObservation,
    ValidationHostContext,
    ValidationResourceSample,
    ValidationRunTimingContext,
    ValidationTimingEnvelope,
    merge_validation_timing_fields,
)


def _envelope() -> ValidationTimingEnvelope:
    timestamp = datetime(2026, 8, 24, tzinfo=timezone.utc).isoformat()
    return ValidationTimingEnvelope(1.0, timestamp, timestamp, 1.0)


def test_timing_context_rejects_untyped_host_context(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="host must have exact type"):
        ValidationRunTimingContext(
            run_id="run-1",
            command="make validate-pr",
            worktree=tmp_path,
            branch="main",
            host={"cpu_count": 18},  # type: ignore[arg-type]
        )


def test_resource_sample_rejects_untyped_nested_disk() -> None:
    with pytest.raises(ValueError, match="disk must have exact type"):
        ValidationResourceSample(
            recorded_at="2026-08-24T00:00:00+00:00",
            loadavg_1m=1.0,
            loadavg_5m=1.0,
            loadavg_15m=1.0,
            memory_free_percent=80,
            swap=None,
            disk={"megabytes_total": 1.0},  # type: ignore[arg-type]
        )


def test_configuration_parser_fails_on_malformed_or_duplicate_fields() -> None:
    with pytest.raises(ValueError, match="invalid validation configuration field"):
        ValidationConfiguration.parse("host_cpus=18 malformed")
    with pytest.raises(ValueError, match="entry names must be unique"):
        ValidationConfiguration.parse("host_cpus=18 host_cpus=12")


def test_timing_field_merge_rejects_schema_collisions() -> None:
    with pytest.raises(ValueError, match="duplicate validation timing fields"):
        merge_validation_timing_fields({"kind": "one"}, {"kind": "two"})


def test_preconfiguration_summary_accepts_explicit_zero_timeout() -> None:
    summary = PrepushGateTimingSummary(
        head_sha=None,
        command=None,
        timeout_seconds=0,
        dirty_check="not-loaded",
        dirty_only=False,
        dirty_elapsed_seconds=None,
        dirty_exit_code=None,
        validation_elapsed_seconds=None,
        validation_cache_hit=None,
        validation_allowed=None,
        validation_reason=None,
        validation_record_exit_code=None,
        validation_record_timed_out=None,
        final_exit_code=None,
        phase="config-load",
        error_type="RuntimeError",
        envelope=_envelope(),
    )

    assert summary.timeout_seconds == 0


def test_host_and_disk_measurements_reject_boolean_numbers() -> None:
    with pytest.raises(ValueError, match="cpu_count must be an integer"):
        ValidationHostContext("host", "Darwin", "25", "arm64", True, 64)
    with pytest.raises(ValueError, match="transfers_total must be finite"):
        ValidationDiskObservation(
            True,
            1.0,
            None,
            None,
            ValidationDiskDeltaStatus.BASELINE_UNAVAILABLE,
            ValidationDiskDeltaStatus.BASELINE_UNAVAILABLE,
        )


def test_timing_envelope_preserves_signed_wall_clock_adjustment() -> None:
    envelope = ValidationTimingEnvelope(
        monotonic_elapsed_seconds=5.0,
        wall_started_at="2026-08-24T12:00:00+00:00",
        wall_ended_at="2026-08-24T11:00:00+00:00",
        wall_elapsed_seconds=-3600.0,
    )

    assert envelope.monotonic_elapsed_seconds == 5.0
    assert envelope.wall_elapsed_seconds == -3600.0
