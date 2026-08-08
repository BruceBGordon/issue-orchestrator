"""Admission and the shared ledger are atomic ACROSS THREADS (#6994 R2 F8/A5).

One process is not one thread. The tick runs in a worker thread and holds the
orchestrator's state lock; the dashboard command surface answers on another
thread and, before this fix, went straight to the admission owner without
taking that lock. Two callers could therefore resolve from the same ledger
snapshot, both be granted, and overwrite one another — in the DEFAULT
claims-disabled deployment, where the in-memory store was the only coordination
there is.

Neither test races and hopes. Each drives one thread to a known point and then
proves the other cannot proceed past it:

* the store's injected clock is used as an interleaving hook, so the exact
  lost-update ordering is reproduced on demand;
* "the second caller is blocked" is asserted as a fact the lock guarantees —
  if the lock works, the second caller cannot finish no matter how the machine
  is loaded, so the assertion cannot flake under load.
"""

from __future__ import annotations

import threading
from datetime import datetime

from issue_orchestrator.domain.run_ledger import (
    RunLedgerRequest,
    RunLedgerRequestKind,
    RunLedgerStatus,
)
from issue_orchestrator.domain.tech_lead_run import (
    GlobalHealthReviewScope,
    IssueInvestigationScope,
    TechLeadRunRequest,
    TechLeadRunTrigger,
)
from issue_orchestrator.ports.run_ledger_store import SingleInstanceRunLedgerStore

from ..threading_helpers import join_or_fail, run_in_thread, wait_for_event

HEALTH = GlobalHealthReviewScope()
FOCUS = IssueInvestigationScope(42)
# Long enough that a real blocking failure is unambiguous, short enough that a
# regression fails fast. The ASSERTION is "still blocked", which the lock makes
# true independently of how long we wait.
BLOCKED_PROBE_SECONDS = 0.25
JOIN_SECONDS = 5.0


class _InterleavingClock:
    """The store's injected clock, doubling as a deterministic control point.

    The first caller to reach it announces that it is INSIDE the store's
    critical section and then waits to be released, which is what lets a test
    ask the precise question that matters: can a second caller get in?
    """

    def __init__(self, moment: datetime) -> None:
        self._moment = moment
        self.inside = threading.Event()
        self.may_continue = threading.Event()
        self._armed = True

    def __call__(self) -> datetime:
        if self._armed:
            self._armed = False
            self.inside.set()
            self.may_continue.wait(timeout=JOIN_SECONDS)
        return self._moment


def _reserve(scope) -> RunLedgerRequest:
    return RunLedgerRequest(
        kind=RunLedgerRequestKind.RESERVE,
        run_key=scope.run_key,
        scope_kind=scope.kind,
    )


def test_a_second_submission_cannot_enter_while_one_is_resolving():
    """The lost update, reproduced on demand rather than raced for.

    Without serialization the second caller resolves from the ledger the first
    caller has not written back yet, and its write ERASES the first's entry —
    leaving one hold where there should be two, i.e. a repository that looks
    freer than it is.
    """
    clock = _InterleavingClock(datetime(2026, 8, 7, 12, 0, 0))
    store = SingleInstanceRunLedgerStore(lease_seconds=900, now=clock)

    first, first_result = run_in_thread(store.submit, _reserve(HEALTH))
    wait_for_event(clock.inside, JOIN_SECONDS, label="first submission entered")

    second, second_result = run_in_thread(store.submit, _reserve(FOCUS))
    entered_too = threading.Event()

    def _watch() -> None:
        second.join()
        entered_too.set()

    watcher = threading.Thread(target=_watch)
    watcher.start()
    try:
        assert not entered_too.wait(timeout=BLOCKED_PROBE_SECONDS), (
            "a second submission entered the store while the first was resolving"
        )
    finally:
        clock.may_continue.set()

    join_or_fail(first, JOIN_SECONDS, label="first submission")
    join_or_fail(second, JOIN_SECONDS, label="second submission")
    join_or_fail(watcher, JOIN_SECONDS, label="watcher")

    assert first_result.unwrap().status is RunLedgerStatus.GRANTED
    assert second_result.unwrap().status is RunLedgerStatus.GRANTED
    ledger = store.read()
    assert ledger is not None
    assert sorted(entry.run_key for entry in ledger.entries) == [
        HEALTH.run_key,
        FOCUS.run_key,
    ], "one caller's hold was overwritten by the other"


def test_every_contending_thread_gets_a_coherent_answer():
    """No torn reads: each key ends up held exactly once."""
    store = SingleInstanceRunLedgerStore(lease_seconds=900)
    scopes = [IssueInvestigationScope(n) for n in range(1, 9)]
    barrier = threading.Barrier(len(scopes), timeout=JOIN_SECONDS)

    def reserve(scope) -> RunLedgerStatus:
        barrier.wait()
        return store.submit(_reserve(scope)).status

    threads = [run_in_thread(reserve, scope) for scope in scopes]
    for index, (thread, _result) in enumerate(threads):
        join_or_fail(thread, JOIN_SECONDS, label=f"reserver {index}")

    assert [result.unwrap() for _t, result in threads] == [
        RunLedgerStatus.GRANTED
    ] * len(scopes)
    ledger = store.read()
    assert ledger is not None
    assert len(ledger.entries) == len(scopes)


# ---------------------------------------------------------------------------
# Dashboard admission vs an in-flight tick
# ---------------------------------------------------------------------------


class _BlockingHealthCheck:
    """Blocks INSIDE the tick, which is inside the orchestrator's state lock."""

    def __init__(self) -> None:
        self.inside = threading.Event()
        self.may_continue = threading.Event()

    def __call__(self) -> None:
        self.inside.set()
        self.may_continue.wait(timeout=JOIN_SECONDS)


def _tech_lead_orchestrator(sample_config):
    from tests.unit.test_orchestrator import create_test_orchestrator

    sample_config.tech_lead_review_agent = "agent:tech-lead"
    return create_test_orchestrator(sample_config)


def test_dashboard_admission_waits_for_an_in_flight_tick(sample_config):
    """Admission reads the pending queue and then mutates it (#6994 R2 F8).

    Interleaving that with the tick — which is doing the same thing — is how a
    request is admitted against a queue that no longer exists by the time it is
    appended, so admission takes the SAME state lock the tick holds.
    """
    orchestrator = _tech_lead_orchestrator(sample_config)
    blocker = _BlockingHealthCheck()
    object.__setattr__(orchestrator.deps.services, "state_health_check", blocker)

    ticking, tick_result = run_in_thread(orchestrator.tick)
    wait_for_event(blocker.inside, JOIN_SECONDS, label="tick entered")

    request = TechLeadRunRequest(
        scope=FOCUS, trigger=TechLeadRunTrigger.DASHBOARD
    )
    admitting, admission_result = run_in_thread(
        orchestrator.request_tech_lead_run, request
    )
    admitted = threading.Event()

    def _watch() -> None:
        admitting.join()
        admitted.set()

    watcher = threading.Thread(target=_watch)
    watcher.start()
    try:
        assert not admitted.wait(timeout=BLOCKED_PROBE_SECONDS), (
            "the dashboard admitted a run while a tick held the state lock"
        )
    finally:
        blocker.may_continue.set()

    join_or_fail(ticking, JOIN_SECONDS, label="tick")
    join_or_fail(admitting, JOIN_SECONDS, label="admission")
    join_or_fail(watcher, JOIN_SECONDS, label="watcher")

    tick_result.unwrap()
    admission = admission_result.unwrap()
    assert admission.run_key == FOCUS.run_key


def test_a_tech_lead_launch_also_serializes_against_the_tick(sample_config):
    """The launch authority reads live state and then mutates it, too."""
    orchestrator = _tech_lead_orchestrator(sample_config)
    blocker = _BlockingHealthCheck()
    object.__setattr__(orchestrator.deps.services, "state_health_check", blocker)

    ticking, tick_result = run_in_thread(orchestrator.tick)
    wait_for_event(blocker.inside, JOIN_SECONDS, label="tick entered")

    launching, launch_result = run_in_thread(
        orchestrator.launch_tech_lead_session, _pending_investigation()
    )
    launched = threading.Event()

    def _watch() -> None:
        launching.join()
        launched.set()

    watcher = threading.Thread(target=_watch)
    watcher.start()
    try:
        assert not launched.wait(timeout=BLOCKED_PROBE_SECONDS), (
            "a tech-lead launch ran while a tick held the state lock"
        )
    finally:
        blocker.may_continue.set()

    join_or_fail(ticking, JOIN_SECONDS, label="tick")
    join_or_fail(launching, JOIN_SECONDS, label="launch")
    join_or_fail(watcher, JOIN_SECONDS, label="watcher")

    tick_result.unwrap()
    launch_result.unwrap()


def _pending_investigation(number: int = 42):
    from issue_orchestrator.domain.models import (
        DiscoveredFailure,
        PendingTechLeadReview,
    )
    from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

    return PendingTechLeadReview(
        number,
        f"Investigate #{number}",
        flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        failure=DiscoveredFailure(
            issue_number=number,
            issue_title=f"Investigate #{number}",
            failure_reason="timed_out",
        ),
    )
