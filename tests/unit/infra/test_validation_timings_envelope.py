"""Every validation-timing record carries the machine-state envelope.

Acceptance for #7127's half (a): the covariate that separates a
contention-inflated sample from a real regression must ride EVERY row
of validate-timings.jsonl, not the ones someone remembered to stamp.
The sampler is faked, so these assert what the recorder writes rather
than what the host running the suite happened to be doing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_orchestrator.infra.validation_timings import (
    ValidateTimingRecorder,
    append_validation_timing,
)
from issue_orchestrator.ports.machine_state import MachineState

_READING = MachineState(
    sampled_at=datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc),
    loadavg_1m=7.91,
    loadavg_5m=12.51,
    loadavg_15m=9.0,
    cpu_idle_percent=85.68,
    cpu_idle_source="fake",
    physical_cores=18,
    probe_error=None,
)

_EXPECTED_ENVELOPE = {
    "sampled_at": "2026-08-29T12:00:00+00:00",
    "loadavg_1m": 7.91,
    "loadavg_5m": 12.51,
    "loadavg_15m": 9.0,
    "cpu_idle_percent": 85.68,
    "cpu_idle_source": "fake",
    "physical_cores": 18,
    "probe_error": None,
}


class _FakeSampler:
    def __init__(self, state: MachineState = _READING) -> None:
        self.state = state

    def sample(self) -> MachineState:
        return self.state


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    (worktree / ".git").mkdir()
    return worktree


def _rows(repo: Path) -> list[dict[str, object]]:
    path = repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _drive_every_record_kind(repo: Path, sampler: _FakeSampler) -> None:
    recorder = ValidateTimingRecorder(
        worktree=repo, command="make validate", machine_state=sampler
    )
    recorder.process_line(
        "[validate-timing] CONFIG validate_jobs=10 unit_parallel=auto"
    )
    recorder.process_line(
        "[validate-timing] START target=test-unit at=2026-03-14T09:10:13-0600"
    )
    recorder.process_line(
        "[validate-timing] END target=test-unit status=0 elapsed=12s "
        "at=2026-03-14T09:10:25-0600"
    )
    recorder.append_resource_sample({"recorded_at": "2026-03-14T09:10:26-0600"})
    recorder.finalize(exit_code=0, total_elapsed_seconds=12.5)
    append_validation_timing(repo, {"kind": "prepush"}, sampler)


def test_every_record_kind_carries_the_envelope(repo: Path) -> None:
    _drive_every_record_kind(repo, _FakeSampler())
    rows = _rows(repo)
    assert {str(row["kind"]) for row in rows} == {
        "target_timing",
        "resource_sample",
        "run_summary",
        "prepush",
    }
    for row in rows:
        assert row["machine_state"] == _EXPECTED_ENVELOPE, row["kind"]


def test_the_envelope_outranks_a_colliding_caller_key(repo: Path) -> None:
    """Stamped last on purpose: a caller's own 'machine_state' key must
    not be able to displace the measured covariate."""
    append_validation_timing(
        repo, {"kind": "prepush", "machine_state": "not a reading"}, _FakeSampler()
    )
    (row,) = _rows(repo)
    assert row["machine_state"] == _EXPECTED_ENVELOPE


def test_a_broken_sampler_never_fails_the_record(repo: Path) -> None:
    """The owner decision: an observability probe that raises mid-record
    is recorded as a failed probe, never propagated into the gate."""

    class _Exploding:
        def sample(self) -> MachineState:
            raise RuntimeError("probe host melted")

    recorder = ValidateTimingRecorder(
        worktree=repo, command="make validate", machine_state=_Exploding()
    )
    recorder.finalize(exit_code=0, total_elapsed_seconds=1.0)
    (row,) = _rows(repo)
    envelope = row["machine_state"]
    assert isinstance(envelope, dict)
    assert set(envelope) == set(_EXPECTED_ENVELOPE)
    assert "probe host melted" in str(envelope["probe_error"])
    assert envelope["loadavg_1m"] is None


def test_the_load_average_is_no_longer_duplicated_beside_the_envelope(
    repo: Path,
) -> None:
    """One fact, one owner: resource samples used to compute their own
    load average, which would now sit beside the envelope's under a
    second name and a second collection rule."""
    _drive_every_record_kind(repo, _FakeSampler())
    for row in _rows(repo):
        assert "loadavg_1m" not in row
