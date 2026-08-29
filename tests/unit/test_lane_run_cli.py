"""Composition-root behavior of the lane-run CLI."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor.lane_executor import (
    PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE,
)
from issue_orchestrator.adapters.json_lane_runtime_history import (
    JsonLaneRuntimeHistory,
)
from issue_orchestrator.domain.lane_execution import (
    LaneCommand,
    LaneCompleted,
    LaneOutcome,
    LaneResources,
    LaneWorkKey,
)
from issue_orchestrator.entrypoints.cli_tools import lane_run as lane_run_module
from issue_orchestrator.infra.lane_declarations import (
    LaneDeclaration,
    LaneDeclarationError,
)
from issue_orchestrator.ports.lane_dispatch_journal import (
    LaneDispatchJournalError,
    LaneDispatchRecord,
)
from issue_orchestrator.ports.machine_state import MachineState
from issue_orchestrator.entrypoints.cli_tools.lane_run import (
    BACKEND_ENVIRONMENT_VARIABLE,
    main,
)

pytestmark = pytest.mark.timeout(180)


@pytest.fixture(autouse=True)
def isolated_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> JsonLaneRuntimeHistory:
    """Every test gets its own history store.

    Without this, any test that drives main() to a successful lane
    would read from and record into the repository's real shared
    runtime history — polluting the store that orders the actual gate.
    """
    history = JsonLaneRuntimeHistory(tmp_path / "lane-runtime-history")
    monkeypatch.setattr(lane_run_module, "_build_history", lambda: history)
    return history


@pytest.fixture(autouse=True)
def declared_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test resolves a default declaration for cli.test.

    Without this, main() would read the repository's real lanes.yaml,
    where the test's work key is (correctly) not declared."""
    monkeypatch.setattr(
        lane_run_module,
        "_load_declaration",
        lambda work_key: LaneDeclaration(
            request_cpus=1, memory_mb=1024, suspendable=False
        ),
    )


class _FakeJournal:
    """Captures records; the CLI is tested against the PORT, never the
    storage transport (A1, #7122 review)."""

    def __init__(self) -> None:
        self.records: list[LaneDispatchRecord] = []

    def record(self, record: LaneDispatchRecord) -> None:
        self.records.append(record)


@pytest.fixture(autouse=True)
def fake_journal(monkeypatch: pytest.MonkeyPatch) -> _FakeJournal:
    """Every test gets a faked journal, for the same reason the
    history store is isolated: tests run inside the real repository."""
    journal = _FakeJournal()
    monkeypatch.setattr(lane_run_module, "_build_journal", lambda: journal)
    return journal


_FAKE_READING = MachineState(
    sampled_at=datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc),
    loadavg_1m=7.91,
    loadavg_5m=12.51,
    loadavg_15m=9.0,
    cpu_idle_percent=85.68,
    cpu_idle_source="fake",
    physical_cores=18,
    probe_error=None,
)


class _FakeMachineStateSampler:
    def __init__(self, state: MachineState) -> None:
        self.state = state

    def sample(self) -> MachineState:
        return self.state


@pytest.fixture(autouse=True)
def fake_machine_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """A faked host probe: these tests assert what the CLI puts on the
    record, and the real probe is a subprocess whose answer depends on
    the machine running the suite."""
    monkeypatch.setattr(
        lane_run_module,
        "_build_machine_state_sampler",
        lambda: _FakeMachineStateSampler(_FAKE_READING),
    )


def _run(*command: str, flags: tuple[str, ...] = (), timeout: str = "60") -> int:
    return main(
        [
            "--work-key",
            "cli.test",
            "--timeout-seconds",
            timeout,
            *flags,
            "--",
            *command,
        ]
    )


class _CapturingExecutor:
    """Records what crosses the port; completes with a fixed outcome."""

    def __init__(self, outcome: LaneOutcome) -> None:
        self.outcome = outcome
        self.resources: list[LaneResources] = []

    def run(self, command: LaneCommand, resources: LaneResources) -> LaneOutcome:
        self.resources.append(resources)
        return self.outcome


def _capture(
    monkeypatch: pytest.MonkeyPatch, outcome: LaneOutcome
) -> _CapturingExecutor:
    executor = _CapturingExecutor(outcome)
    monkeypatch.setattr(
        lane_run_module, "build_lane_executor", lambda backend: executor
    )
    return executor


def test_direct_backend_returns_the_lane_exit_code() -> None:
    assert _run(sys.executable, "-c", "raise SystemExit(0)") == 0
    assert _run(sys.executable, "-c", "raise SystemExit(9)") == 9


def test_deadline_reports_124() -> None:
    assert _run(sys.executable, "-c", "import time; time.sleep(60)", timeout="1") == 124


def test_missing_separator_is_a_usage_error() -> None:
    assert (
        main(
            [
                "--work-key",
                "cli.test",
                "--timeout-seconds",
                "60",
                "/usr/bin/true",
            ]
        )
        == 78
    )


def test_missing_lane_executable_fails_as_configuration_error() -> None:
    assert _run("definitely-not-a-real-binary-anywhere") == 78


def test_opted_in_condor_without_tools_fails_loudly_not_silently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv(
        PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE, str(tmp_path / "no-pool")
    )
    assert _run("/usr/bin/true", flags=("--backend", "condor")) == 78


def test_environment_variable_selects_the_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(BACKEND_ENVIRONMENT_VARIABLE, "condor")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv(
        PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE, str(tmp_path / "no-pool")
    )
    assert _run("/usr/bin/true") == 78


def test_declared_facts_cross_the_port_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lanes.yaml declaration — not any flag — is what reaches the
    port. A capturing executor at the composition seam proves the
    values the port actually receives."""
    monkeypatch.setattr(
        lane_run_module,
        "_load_declaration",
        lambda work_key: LaneDeclaration(
            request_cpus=7,
            memory_mb=2048,
            suspendable=True,
            exclusive=("codex",),
        ),
    )
    executor = _capture(monkeypatch, LaneCompleted(0, 1.0, 0.0))
    assert _run("/usr/bin/true") == 0
    assert len(executor.resources) == 1
    assert executor.resources[0].request_cpus == 7
    assert executor.resources[0].request_memory_mb == 2048
    assert executor.resources[0].suspendable is True
    assert executor.resources[0].exclusive == ("codex",)


def test_priority_cannot_be_declared_by_the_client() -> None:
    """Dispatch order is learned, never declared: the flag must not
    exist. A client's only vocabulary is the logical work key."""
    with pytest.raises(SystemExit):
        _run("/usr/bin/true", flags=("--priority", "7"))


def test_empty_history_is_the_naive_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero history means priority 0 — exactly today's behavior."""
    executor = _capture(monkeypatch, LaneCompleted(0, 1.0, 0.0))
    assert _run("/usr/bin/true") == 0
    assert executor.resources[0].priority == 0


def test_learned_priority_crosses_the_port_boundary(
    monkeypatch: pytest.MonkeyPatch, isolated_history: JsonLaneRuntimeHistory
) -> None:
    """The rolling median of recorded runtimes becomes the submitted
    priority — the whole learning loop, observed at the port."""
    key = LaneWorkKey("cli.test")
    for runtime in (30.0, 90.0, 60.0):
        isolated_history.record_success(key, runtime)
    executor = _capture(monkeypatch, LaneCompleted(0, 1.0, 0.0))
    assert _run("/usr/bin/true") == 0
    assert executor.resources[0].priority == 60


def test_successful_lane_seeds_the_next_run(
    monkeypatch: pytest.MonkeyPatch, isolated_history: JsonLaneRuntimeHistory
) -> None:
    _capture(monkeypatch, LaneCompleted(0, 42.0, 0.0))
    assert _run("/usr/bin/true") == 0
    assert isolated_history.learned_priority(LaneWorkKey("cli.test")) == 42


def test_failed_lane_teaches_nothing(
    monkeypatch: pytest.MonkeyPatch, isolated_history: JsonLaneRuntimeHistory
) -> None:
    """A failed run's duration is the failure's, not the lane's — a
    482s provider stall must never become a lane's learned weight."""
    _capture(monkeypatch, LaneCompleted(1, 482.0, 0.0))
    assert _run("/usr/bin/true") == 1
    assert isolated_history.learned_priority(LaneWorkKey("cli.test")) == 0


def test_completed_lane_journals_one_dispatch_record(
    monkeypatch: pytest.MonkeyPatch,
    isolated_history: JsonLaneRuntimeHistory,
    fake_journal: _FakeJournal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Priority used, queue wait, runtime, and exit reach the journal
    port as a typed record AND the gate log as a stderr line —
    dispatch quality must be readable in place and queryable across
    runs without pool archaeology."""
    key = LaneWorkKey("cli.test")
    for runtime in (30.0, 90.0, 60.0):
        isolated_history.record_success(key, runtime)
    _capture(monkeypatch, LaneCompleted(0, 45.0, 12.0))
    assert _run("/usr/bin/true") == 0
    (record,) = fake_journal.records
    assert record.work_key == LaneWorkKey("cli.test")
    assert record.priority == 60
    assert record.queue_wait_seconds == 12.0
    assert record.observed_runtime_seconds == 45.0
    assert record.exit_code == 0
    stderr = capsys.readouterr().err
    assert (
        "[lane-dispatch] cli.test backend=direct priority=60 "
        "queue_wait=12.0s runtime=45.0s exit=0" in stderr
    )


def test_failed_lane_still_journals_its_dispatch_facts(
    monkeypatch: pytest.MonkeyPatch, fake_journal: _FakeJournal
) -> None:
    """Failures teach the learning loop nothing, but their dispatch
    facts are diagnosis — the record is journaled either way."""
    _capture(monkeypatch, LaneCompleted(1, 482.0, 3.0))
    assert _run("/usr/bin/true") == 1
    (record,) = fake_journal.records
    assert record.exit_code == 1
    assert record.observed_runtime_seconds == 482.0


def test_every_dispatch_record_carries_the_machine_state_envelope(
    monkeypatch: pytest.MonkeyPatch, fake_journal: _FakeJournal
) -> None:
    """Acceptance (#7127): the CLI samples the host and puts the
    reading on the record, so a 482s lane can be told apart from a 482s
    lane that ran against a saturated machine."""
    _capture(monkeypatch, LaneCompleted(0, 45.0, 12.0))
    assert _run("/usr/bin/true") == 0
    (record,) = fake_journal.records
    assert record.machine_state == _FAKE_READING


def test_a_broken_host_probe_never_fails_the_lane(
    monkeypatch: pytest.MonkeyPatch, fake_journal: _FakeJournal
) -> None:
    """The owner decision, at the boundary that matters: an
    observability probe that raises mid-record must not turn a green
    lane red. The failure is recorded, not swallowed."""

    class _Exploding:
        def sample(self) -> MachineState:
            raise RuntimeError("probe host melted")

    monkeypatch.setattr(
        lane_run_module, "_build_machine_state_sampler", lambda: _Exploding()
    )
    _capture(monkeypatch, LaneCompleted(0, 45.0, 12.0))
    assert _run("/usr/bin/true") == 0
    (record,) = fake_journal.records
    assert record.machine_state.probe_error is not None
    assert "probe host melted" in record.machine_state.probe_error


def test_journal_failure_is_a_backend_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistence failure has one explicit owner: the journal raises
    its typed error and the CLI reports a backend fault (70)."""

    class _BrokenJournal:
        def record(self, record: LaneDispatchRecord) -> None:
            raise LaneDispatchJournalError("disk gone")

    monkeypatch.setattr(
        lane_run_module, "_build_journal", lambda: _BrokenJournal()
    )
    _capture(monkeypatch, LaneCompleted(0, 1.0, 0.0))
    assert _run("/usr/bin/true") == 70


def test_corrupt_history_fails_loudly_not_naively(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / "corrupt-history"
    directory.mkdir()
    (directory / "cli.test.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(
        lane_run_module,
        "_build_history",
        lambda: JsonLaneRuntimeHistory(directory),
    )
    assert _run("/usr/bin/true") == 70


def test_memory_budget_domain_validation_rejects_nonsense() -> None:
    bad_values: tuple[int, ...] = (0, -5, True)
    for bad in bad_values:
        with pytest.raises(ValueError):
            LaneResources(request_cpus=1, request_memory_mb=bad)
    with pytest.raises(ValueError):
        LaneResources(
            request_cpus=1,
            request_memory_mb=1.5,  # type: ignore[arg-type]
        )
    assert LaneResources(request_cpus=1, request_memory_mb=2048).request_memory_mb == 2048


def test_observed_runtime_domain_validation_rejects_nonsense() -> None:
    for bad in (float("nan"), float("inf"), -1.0):
        with pytest.raises(ValueError):
            LaneCompleted(0, bad, 0.0)
    with pytest.raises(ValueError):
        # The annotation's numeric tower accepts an int; the runtime
        # guard does not — observed runtimes are always measured floats.
        LaneCompleted(0, 5, 0.0)
    assert LaneCompleted(0, 0.0, 0.0).observed_runtime_seconds == 0.0


def test_scheduling_facts_cannot_be_declared_as_flags() -> None:
    """One configuration home: cpus, memory, suspendability, and
    exclusives are lanes.yaml rows, and the flags must not exist — a
    second declaration surface would drift from the first."""
    for flag in (
        ("--request-cpus", "4"),
        ("--request-memory-mb", "2048"),
        ("--suspendable",),
        ("--not-suspendable",),
        ("--exclusive", "codex"),
    ):
        with pytest.raises(SystemExit):
            _run("/usr/bin/true", flags=flag)


def test_undeclared_lane_fails_as_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No policy-by-absence: a lane missing from lanes.yaml must not
    run with invented resources."""

    def refuse(work_key: str) -> LaneDeclaration:
        raise LaneDeclarationError(f"lane {work_key!r} is not declared")

    monkeypatch.setattr(lane_run_module, "_load_declaration", refuse)
    assert _run("/usr/bin/true") == 78


def test_suspendable_domain_validation_rejects_non_bool() -> None:
    for bad in (1, "yes", None):
        with pytest.raises(ValueError):
            LaneResources(request_cpus=1, suspendable=bad)  # type: ignore[arg-type]
