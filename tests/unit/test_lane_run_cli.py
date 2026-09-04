"""Composition-root behavior of the lane-run CLI."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

from issue_orchestrator.adapters.condor.tools import (
    PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE,
)
from issue_orchestrator.adapters.direct_lane_executor import (
    DirectLaneExecutor,
    DirectLaneTerminationPolicy,
)
from issue_orchestrator.adapters.json_lane_runtime_history import (
    JsonLaneRuntimeHistory,
)
from issue_orchestrator.domain.lane_cpu_request import LaneCpuRequest
from issue_orchestrator.domain.lane_execution import (
    LaneCommand,
    LaneCompleted,
    LaneDeadline,
    LaneExecutorError,
    LaneExecutorUnavailableError,
    LaneOutcome,
    LaneResources,
    LaneSuspendability,
    LaneTimedOut,
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
from issue_orchestrator.ports.lane_runtime_history import LaneRuntimeHistoryError
from issue_orchestrator.execution.lane_backends import (
    BACKEND_ENVIRONMENT_VARIABLE,
)
from issue_orchestrator.ports.machine_state import MachineState
from issue_orchestrator.entrypoints.cli_tools.lane_run import _build_parser, main

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
    monkeypatch.setattr(lane_run_module, "build_runtime_history", lambda: history)
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
            request_cpus=1, memory_mb=1024, suspendability="never"
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


def _fail(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """An executor that faults instead of completing."""

    class _FailingExecutor:
        def run(self, command: LaneCommand, resources: LaneResources) -> LaneOutcome:
            del command, resources
            raise error

    monkeypatch.setattr(
        lane_run_module, "build_lane_executor", lambda backend: _FailingExecutor()
    )


class _BrokenJournal:
    """Persists nothing and fails loudly — a fault AFTER the lane ran."""

    def __init__(self) -> None:
        self.persisted: list[LaneDispatchRecord] = []

    def record(self, record: LaneDispatchRecord) -> None:
        del record
        raise LaneDispatchJournalError("disk gone")


def _break_journal(monkeypatch: pytest.MonkeyPatch) -> _BrokenJournal:
    journal = _BrokenJournal()
    monkeypatch.setattr(lane_run_module, "_build_journal", lambda: journal)
    return journal


class _BrokenHistory:
    """Reads fine, refuses to record — a fault after the row is written."""

    def record_success(
        self,
        work_key: LaneWorkKey,
        runtime_seconds: float,
        busy_cores: float | None,
    ) -> None:
        del work_key, runtime_seconds, busy_cores
        raise LaneRuntimeHistoryError("history unwritable")

    def learned_priority(self, work_key: LaneWorkKey) -> int:
        del work_key
        return 0

    def learned_busy_cores(self, work_key: LaneWorkKey) -> float | None:
        del work_key
        return None


def _break_history(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lane_run_module, "build_runtime_history", lambda: _BrokenHistory()
    )


# What the DISPATCHER means by its own failures. A lane owns the whole
# 0-255 space, so it can return these too — they are not reserved.
_COLLIDING_CODES = (70, 78, 124)


@pytest.mark.parametrize("code", _COLLIDING_CODES)
def test_a_lane_returning_a_dispatcher_code_is_passed_through_and_journaled(
    code: int, monkeypatch: pytest.MonkeyPatch, fake_journal: _FakeJournal
) -> None:
    """The exit-code space is NOT disjoint, and lane-run must not pretend.

    Remapping a lane's own 70/78/124 onto some other value would lie
    about what the lane returned and break make's view of the gate, so
    the code passes through unchanged. This pins the passthrough, and
    that a completed lane is journaled on the nominal path — nothing
    about telling the two apart, which no signal here can do.
    """
    _capture(monkeypatch, LaneCompleted(code, 1.0, 0.0))

    assert _run("/usr/bin/true") == code
    (record,) = fake_journal.records
    assert record.exit_code == code


@pytest.mark.parametrize(
    ("code", "provoke"),
    [
        (78, lambda mp: _fail(mp, LaneExecutorUnavailableError("pool is down"))),
        (70, lambda mp: _fail(mp, LaneExecutorError("backend broke mid-run"))),
        (124, lambda mp: _capture(mp, LaneTimedOut(9.0))),
    ],
    ids=["unavailable", "backend-fault", "deadline"],
)
def test_a_dispatcher_failure_on_a_colliding_code_journals_nothing(
    code: int,
    provoke: Callable[[pytest.MonkeyPatch], object],
    monkeypatch: pytest.MonkeyPatch,
    fake_journal: _FakeJournal,
) -> None:
    """The same three codes, reached because the dispatcher failed.

    These faults all happen BEFORE the lane completes, so nothing is
    journaled. Together with the two tests below — which break it in
    both directions — this is what "journal rows are best-effort"
    means concretely.
    """
    provoke(monkeypatch)

    assert _run("/usr/bin/true") == code
    assert not fake_journal.records


def test_a_completed_lane_can_leave_no_journal_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Row ABSENCE proves nothing: the lane here really returned 70.

    The fault lands after the lane completed and before its row is
    written, so a completed lane leaves none — and the fault exit and
    the lane's own exit are the same number. Any rule of the form "no
    row means the dispatcher failed" misreads this run.
    """

    journal = _break_journal(monkeypatch)
    _capture(monkeypatch, LaneCompleted(70, 1.0, 0.0))

    assert _run("/usr/bin/true") == 70
    assert not journal.persisted
    # The lane's own exit is still reported; it is simply not decisive.
    assert "exit=70" in capsys.readouterr().err


def test_a_dispatcher_fault_can_leave_a_row_that_disagrees_with_the_exit(
    monkeypatch: pytest.MonkeyPatch, fake_journal: _FakeJournal
) -> None:
    """Row PRESENCE does not settle what the process returned.

    The lane completed 0 and was journaled; recording what it taught
    then failed, so lane-run exits 70 with a row on disk saying 0. A
    row attests that a completion was persisted — not that the
    invocation succeeded, and not what it exited with.
    """

    _break_history(monkeypatch)
    _capture(monkeypatch, LaneCompleted(0, 1.0, 0.0))

    assert _run("/usr/bin/true") == 70
    (record,) = fake_journal.records
    assert record.exit_code == 0


def test_a_lane_can_forge_the_dispatcher_prefix_on_its_inherited_stderr(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Why the docs promise nothing about the `lane-run:` prefix.

    The lane inherits this process's stderr FILE DESCRIPTOR, so it can
    print the dispatcher's own prefix. Here a real subprocess emits one
    and exits 70 with no dispatcher fault anywhere - prefix present,
    dispatcher fine, and the 70 is the lane's.

    capfd, not capsys: the forgery arrives on fd 2 rather than through
    this process's `sys.stderr`, which is the whole mechanism. A silent
    fake lane shows none of this, which is how an earlier version of
    these tests came to underwrite a false claim.
    """
    assert _run("/bin/sh", "-c", 'echo "lane-run: forged by lane" >&2; exit 70') == 70
    stderr = capfd.readouterr().err
    assert "lane-run: forged by lane" in stderr
    assert "[lane-dispatch]" in stderr


def test_a_fault_while_announcing_still_returns_the_dispatcher_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unwritable stderr must not promote itself into the exit code.

    The classified fault below cannot be printed; without a guard the
    OSError escapes main and CPython exits 1 - a lane result code, and
    the one collision the total mapping exists to remove.
    """

    class _DeadStderr:
        def write(self, text: str) -> int:
            del text
            raise OSError(28, "No space left on device")

        def flush(self) -> None:
            pass

    _fail(monkeypatch, LaneExecutorError("backend broke mid-run"))
    monkeypatch.setattr(sys, "stderr", _DeadStderr())

    assert _run("/usr/bin/true") == 70


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


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_survives_the_separator_rule(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Installed on PATH, --help is the only discovery surface a caller
    outside this repository has; the '--' requirement must not eat it."""
    with pytest.raises(SystemExit) as exit_info:
        main([flag])

    assert exit_info.value.code == 0
    assert "--work-key" in capsys.readouterr().out


# Every shape where -h/--help appears among the options, with and
# without a separator and with a malformed option ahead of it.
_HELP_INVOCATIONS = [
    ["--help"],
    ["-h"],
    ["--backend", "condor", "--help"],
    ["--help", "--", "/usr/bin/true"],
    ["--backend", "not-a-backend", "--help"],
    ["--backend", "not-a-backend", "--help", "--", "/usr/bin/true"],
    ["--work-key", "--help"],
    ["--work-key", "--help", "--", "/usr/bin/true"],
]


def _outcome(call: Callable[[], object]) -> tuple[str, object]:
    """How the call ended, not just with what code.

    ``returned 0`` and ``SystemExit(0)`` are different answers that
    compare equal on the code alone, so the kind travels with it — a
    pre-scan that answers help by returning 0 must not match argparse
    exiting 0.
    """
    try:
        return ("returned", call())
    except SystemExit as exit_info:
        return ("exited", exit_info.code)


@pytest.mark.parametrize("argv", _HELP_INVOCATIONS, ids=" ".join)
def test_help_never_outranks_parser_validation(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Membership of -h/--help routes to argparse; it never decides.

    Pre-scanning argv for help and answering it directly reports a
    malformed option list as a successful help request — exit 0 where
    the parser says 2. The parser's own verdict on the same options is
    the oracle, so the two cannot diverge again.
    """
    separator = argv.index("--") if "--" in argv else None
    options = argv if separator is None else argv[:separator]

    expected = _outcome(lambda: _build_parser().parse_args(options))
    capsys.readouterr()

    assert _outcome(lambda: main(list(argv))) == expected


def test_a_lane_commands_own_help_is_not_intercepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--help after the separator belongs to the lane, not to lane-run."""
    executor = _capture(monkeypatch, LaneCompleted(0, 0.0, 0.0))

    assert _run("/usr/bin/true", "--help") == 0
    assert executor.resources, "the lane never ran - help was intercepted"


def test_an_unclassified_crash_is_a_dispatcher_fault_not_a_lane_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CPython exits 1 on an escaping exception, and 1 is a lane result
    code. A dispatcher bug reported as 1 would read as "your tests
    failed", so main's mapping must be total: 70, with the traceback
    intact so nothing is softened."""

    def explode(work_key: str) -> LaneDeclaration:
        del work_key
        raise MemoryError("unclassified")

    monkeypatch.setattr(lane_run_module, "_load_declaration", explode)

    assert _run("/usr/bin/true") == 70
    stderr = capsys.readouterr().err
    assert "lane-run: internal error:" in stderr
    assert "MemoryError: unclassified" in stderr


def test_totality_does_not_swallow_argparse_exits() -> None:
    """SystemExit is the parser rejecting input, not a dispatcher fault;
    catching it would turn every usage error into a 70."""
    with pytest.raises(SystemExit) as exit_info:
        main(["--work-key", "cli.test", "--", "/usr/bin/true"])

    assert exit_info.value.code == 2


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
            suspendability="cooperative",
            exclusive=("codex",),
        ),
    )
    executor = _capture(monkeypatch, LaneCompleted(0, 1.0, 0.0))
    assert _run("/usr/bin/true") == 0
    assert len(executor.resources) == 1
    assert executor.resources[0].request_cpus == 7
    assert executor.resources[0].request_memory_mb == 2048
    assert (
        executor.resources[0].suspendability
        is LaneSuspendability.COOPERATIVE
    )
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
        isolated_history.record_success(key, runtime, None)
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
        isolated_history.record_success(key, runtime, None)
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
        "queue_wait=12.0s runtime=45.0s exit=0 "
        "request_cpus=1/1 busy_cores=unmeasured" in stderr
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
        "build_runtime_history",
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


def test_suspendability_domain_validation_rejects_non_enum() -> None:
    """The domain speaks the enum only — raw strings and the old
    booleans are declaration-layer vocabulary that must be mapped
    before crossing into LaneResources."""
    for bad in (True, "anywhere", 1, None):
        with pytest.raises(ValueError):
            LaneResources(request_cpus=1, suspendability=bad)  # type: ignore[arg-type]
    assert (
        LaneResources(request_cpus=1).suspendability
        is LaneSuspendability.NEVER
    )


def _declare(monkeypatch: pytest.MonkeyPatch, request_cpus: int) -> None:
    monkeypatch.setattr(
        lane_run_module,
        "_load_declaration",
        lambda work_key: LaneDeclaration(
            request_cpus=request_cpus, memory_mb=1024, suspendability="never"
        ),
    )


def test_empty_cpu_history_submits_the_declared_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before anything is measured, lanes.yaml IS the request — the
    naive run is byte-for-byte the pre-learning behavior."""
    _declare(monkeypatch, 8)
    executor = _capture(monkeypatch, LaneCompleted(0, 1.0, 0.0))
    assert _run("/usr/bin/true") == 0
    assert executor.resources[0].request_cpus == 8


def test_measured_history_lowers_the_submitted_request(
    monkeypatch: pytest.MonkeyPatch, isolated_history: JsonLaneRuntimeHistory
) -> None:
    """The learning loop observed at the port: a lane declared at 8
    that keeps measuring ~2 busy cores submits 2, handing six cores
    back to the pool."""
    _declare(monkeypatch, 8)
    key = LaneWorkKey("cli.test")
    for cores in (1.8, 2.0, 1.9):
        isolated_history.record_success(key, 30.0, cores)
    executor = _capture(monkeypatch, LaneCompleted(0, 1.0, 0.0))
    assert _run("/usr/bin/true") == 0
    assert executor.resources[0].request_cpus == 2


def test_measured_history_never_raises_the_request_above_the_declaration(
    monkeypatch: pytest.MonkeyPatch, isolated_history: JsonLaneRuntimeHistory
) -> None:
    """The declaration is the ceiling as well as the seed. A lane
    'measuring' sixteen cores is far likelier to be a broken
    measurement than a lane that got eight times hungrier, and
    granting it would drain the pool."""
    _declare(monkeypatch, 2)
    key = LaneWorkKey("cli.test")
    for cores in (16.0, 16.0, 16.0):
        isolated_history.record_success(key, 30.0, cores)
    executor = _capture(monkeypatch, LaneCompleted(0, 1.0, 0.0))
    assert _run("/usr/bin/true") == 0
    assert executor.resources[0].request_cpus == 2


def test_a_measured_lane_teaches_its_cpu_demand(
    monkeypatch: pytest.MonkeyPatch, isolated_history: JsonLaneRuntimeHistory
) -> None:
    """A backend that measured this run feeds both dimensions; the
    next run consumes the CPU one."""
    _declare(monkeypatch, 8)
    _capture(monkeypatch, LaneCompleted(0, 42.0, 0.0, 3.5))
    assert _run("/usr/bin/true") == 0
    key = LaneWorkKey("cli.test")
    assert isolated_history.learned_priority(key) == 42
    assert isolated_history.learned_busy_cores(key) == 3.5


def test_an_unmeasured_lane_teaches_only_its_runtime(
    monkeypatch: pytest.MonkeyPatch, isolated_history: JsonLaneRuntimeHistory
) -> None:
    """The direct backend abstains from measuring CPU (its lanes run
    under make's own parallelism, which deflates the figure). Its runs
    must still teach dispatch order, and must not record a CPU number
    nobody observed."""
    _declare(monkeypatch, 8)
    _capture(monkeypatch, LaneCompleted(0, 42.0, 0.0))
    assert _run("/usr/bin/true") == 0
    key = LaneWorkKey("cli.test")
    assert isolated_history.learned_priority(key) == 42
    assert isolated_history.learned_busy_cores(key) is None


def test_a_failed_measured_lane_teaches_no_cpu_demand(
    monkeypatch: pytest.MonkeyPatch, isolated_history: JsonLaneRuntimeHistory
) -> None:
    """Only successes teach, in both dimensions: a lane that died
    halfway through burned a fraction of its real CPU."""
    _declare(monkeypatch, 8)
    _capture(monkeypatch, LaneCompleted(1, 5.0, 0.0, 0.4))
    assert _run("/usr/bin/true") == 1
    assert isolated_history.learned_busy_cores(LaneWorkKey("cli.test")) is None


def test_dispatch_record_shows_the_measured_versus_declared_divergence(
    monkeypatch: pytest.MonkeyPatch,
    isolated_history: JsonLaneRuntimeHistory,
    fake_journal: _FakeJournal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Declared, learned, submitted, and actually-used all reach the
    journal. A record carrying only the submitted number could not
    distinguish 'no history' from 'history agrees', nor show that
    evidence was capped."""
    _declare(monkeypatch, 4)
    key = LaneWorkKey("cli.test")
    for cores in (1.2, 1.4, 1.3):
        isolated_history.record_success(key, 30.0, cores)
    _capture(monkeypatch, LaneCompleted(0, 45.0, 0.0, 1.35))
    assert _run("/usr/bin/true") == 0
    (record,) = fake_journal.records
    assert record.cpu_request.declared_cpus == 4
    assert record.cpu_request.learned_busy_cores == 1.3
    assert record.cpu_request.request_cpus == 2
    assert record.cpu_request.is_capped is False
    assert record.observed_busy_cores == 1.35
    assert (
        "request_cpus=2/4 busy_cores=1.35" in capsys.readouterr().err
    )


def test_dispatch_record_marks_evidence_that_was_refused(
    monkeypatch: pytest.MonkeyPatch,
    isolated_history: JsonLaneRuntimeHistory,
    fake_journal: _FakeJournal,
) -> None:
    """A capped lane is the suspicious direction — the refusal must be
    on the record, not inferable only by re-deriving the policy."""
    _declare(monkeypatch, 2)
    isolated_history.record_success(LaneWorkKey("cli.test"), 30.0, 9.0)
    _capture(monkeypatch, LaneCompleted(0, 30.0, 0.0, 9.0))
    assert _run("/usr/bin/true") == 0
    (record,) = fake_journal.records
    assert record.cpu_request.is_capped is True
    assert record.cpu_request.request_cpus == 2


def test_direct_backend_records_no_cpu_measurement() -> None:
    """End to end through the real direct executor: it reports no
    busy-cores figure at all, so nothing it runs can teach a deflated
    number to a scheduler it never talks to."""
    outcome = DirectLaneExecutor(DirectLaneTerminationPolicy(1.0)).run(
        LaneCommand(
            work_key=LaneWorkKey("cli.test"),
            arguments=(sys.executable, "-c", "pass"),
            working_directory=Path.cwd(),
            deadline=LaneDeadline(60.0),
        ),
        LaneResources(request_cpus=1),
    )
    assert type(outcome) is LaneCompleted
    assert outcome.observed_busy_cores is None


def test_cpu_request_policy_has_exactly_one_home() -> None:
    """The seed/ceiling asymmetry is arithmetic the CLI delegates, not
    arithmetic it owns — so a second consumer cannot grow a second,
    subtly different version of it."""
    assert LaneCpuRequest.resolve(8, None).request_cpus == 8
    assert LaneCpuRequest.resolve(8, 1.1).request_cpus == 2
    assert LaneCpuRequest.resolve(8, 99.0).request_cpus == 8
