"""Admission and the shared ledger are atomic ACROSS THREADS (#6994 R2 F8/A5).

One process is not one thread. The tick runs in a worker thread and holds the
orchestrator's state lock; the dashboard command surface answers on another
thread and, before this fix, went straight to the admission owner without
taking that lock. Two callers could therefore resolve from the same ledger
snapshot, both be granted, and overwrite one another — in the DEFAULT
claims-disabled deployment, where the in-memory store was the only coordination
there is.

Nothing here infers serialization from elapsed time (round 3 F14). Both the
store and the orchestrator take their lock as an INJECTED collaborator, so these
tests substitute :class:`RecordingLock`, which:

* announces, from inside the acquisition path, that a thread has PARKED — at
  which point the parked thread provably cannot acquire until the owner
  releases, no matter how the machine is scheduled;
* records the acquire/release timeline, so "B went after A" is read off the
  order rather than deduced from a clock.

Every wait is a bounded guard around an expected POSITIVE signal, which is
exactly what fails loudly when the lock is missing: a second caller that never
parks never signals.
"""

from __future__ import annotations

import threading
from datetime import datetime
from types import TracebackType
from typing import Callable, Optional

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

from ..threading_helpers import ThreadResult, join_or_fail, wait_for_event

HEALTH = GlobalHealthReviewScope()
FOCUS = IssueInvestigationScope(42)
# A failure guard around POSITIVE signals only. If the lock is missing the
# second caller never parks, so this expires and the test fails loudly; if the
# lock is present the signal arrives immediately regardless of load.
SIGNAL_SECONDS = 5.0


class RecordingLock:
    """A reentrant lock that says when a thread has parked, and in what order.

    The seam the round-3 review asked for: instead of asking "did the other
    thread finish within N milliseconds?", a test waits for the POSITIVE
    ``parked`` signal — which can only be emitted from inside the acquisition
    path, once the thread is provably unable to proceed until the owner
    releases — and then reads the acquisition order off ``timeline``.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._owner: Optional[int] = None
        self._depth = 0
        self.timeline: list[str] = []
        self.parked = threading.Event()

    def __enter__(self) -> "RecordingLock":
        me = threading.current_thread().name
        ident = threading.get_ident()
        with self._cond:
            self.timeline.append(f"attempt:{me}")
            while self._owner is not None and self._owner != ident:
                # Parked: `_owner` is another thread, so this one cannot leave
                # the loop until that thread releases. Signalling HERE is what
                # makes the test's question answerable without a clock.
                self.parked.set()
                self._cond.wait(timeout=SIGNAL_SECONDS)
            self._owner = ident
            self._depth += 1
            self.timeline.append(f"acquire:{me}")
        return self

    def __exit__(
        self,
        _type: Optional[type[BaseException]],
        _value: Optional[BaseException],
        _traceback: Optional[TracebackType],
    ) -> None:
        me = threading.current_thread().name
        with self._cond:
            self._depth -= 1
            self.timeline.append(f"release:{me}")
            if self._depth == 0:
                self._owner = None
                self._cond.notify_all()

    def went_after(self, later: str, earlier: str) -> bool:
        """True when ``later`` acquired only after ``earlier`` released."""
        return self.timeline.index(f"acquire:{later}") > self.timeline.index(
            f"release:{earlier}"
        )


class _ParkingHook:
    """Parks the FIRST caller inside a critical section until released."""

    def __init__(self) -> None:
        self.inside = threading.Event()
        self.may_continue = threading.Event()
        self._armed = True

    def _park(self) -> None:
        if not self._armed:
            return
        self._armed = False
        self.inside.set()
        self.may_continue.wait(timeout=SIGNAL_SECONDS)


class ParkingClock(_ParkingHook):
    """The store's injected clock, parking its first caller mid-resolve."""

    def __init__(self, moment: datetime) -> None:
        super().__init__()
        self._moment = moment

    def __call__(self) -> datetime:
        self._park()
        return self._moment


class ParkingHealthCheck(_ParkingHook):
    """Parks INSIDE the tick, which is inside the orchestrator's state lock."""

    def __call__(self) -> None:
        self._park()


def named_thread(
    name: str, fn: Callable[..., object], *args: object
) -> tuple[threading.Thread, ThreadResult]:
    """A named worker, so the lock timeline reads as roles rather than ids."""
    result = ThreadResult()

    def target() -> None:
        try:
            result.result = fn(*args)
        except BaseException as exc:  # pragma: no cover - surfaced by unwrap()
            result.error = exc

    thread = threading.Thread(target=target, name=name)
    thread.start()
    return thread, result


def _reserve(scope) -> RunLedgerRequest:
    return RunLedgerRequest(
        kind=RunLedgerRequestKind.RESERVE,
        run_key=scope.run_key,
        scope_kind=scope.kind,
    )


# ---------------------------------------------------------------------------
# The shared store
# ---------------------------------------------------------------------------


def test_a_second_submission_parks_until_the_first_has_written_back():
    """The lost update, reproduced on demand rather than raced for.

    Without serialization the second caller resolves from the ledger the first
    has not written back yet, and its write ERASES the first's entry — leaving
    one hold where there should be two, i.e. a repository that looks freer than
    it is.
    """
    lock = RecordingLock()
    clock = ParkingClock(datetime(2026, 8, 7, 12, 0, 0))
    store = SingleInstanceRunLedgerStore(lease_seconds=900, now=clock, lock=lock)

    first, first_result = named_thread("first", store.submit, _reserve(HEALTH))
    wait_for_event(clock.inside, SIGNAL_SECONDS, label="first submission entered")

    second, second_result = named_thread("second", store.submit, _reserve(FOCUS))
    # POSITIVE proof the store is serialized: the second caller parked, and a
    # parked caller cannot acquire until the first releases.
    wait_for_event(lock.parked, SIGNAL_SECONDS, label="second submission parked")

    clock.may_continue.set()
    join_or_fail(first, SIGNAL_SECONDS, label="first submission")
    join_or_fail(second, SIGNAL_SECONDS, label="second submission")

    assert lock.went_after("second", "first")
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
    barrier = threading.Barrier(len(scopes), timeout=SIGNAL_SECONDS)

    def reserve(scope) -> RunLedgerStatus:
        barrier.wait()
        return store.submit(_reserve(scope)).status

    threads = [
        named_thread(f"reserver-{index}", reserve, scope)
        for index, scope in enumerate(scopes)
    ]
    for index, (thread, _result) in enumerate(threads):
        join_or_fail(thread, SIGNAL_SECONDS, label=f"reserver {index}")

    assert [result.unwrap() for _t, result in threads] == [
        RunLedgerStatus.GRANTED
    ] * len(scopes)
    ledger = store.read()
    assert ledger is not None
    assert len(ledger.entries) == len(scopes)


# ---------------------------------------------------------------------------
# Dashboard admission and launch vs an in-flight tick
# ---------------------------------------------------------------------------


def _parked_tick(sample_config):
    """An orchestrator whose tick is parked inside an instrumented state lock."""
    from tests.unit.test_orchestrator import create_test_orchestrator

    sample_config.tech_lead_review_agent = "agent:tech-lead"
    orchestrator = create_test_orchestrator(sample_config)
    lock = RecordingLock()
    orchestrator.state_lock = lock
    parker = ParkingHealthCheck()
    object.__setattr__(orchestrator.deps.services, "state_health_check", parker)

    ticking, tick_result = named_thread("tick", orchestrator.tick)
    wait_for_event(parker.inside, SIGNAL_SECONDS, label="tick entered")
    return orchestrator, lock, parker, ticking, tick_result


def test_dashboard_admission_waits_for_an_in_flight_tick(sample_config):
    """Admission reads the pending queue and then mutates it (#6994 R2 F8).

    Interleaving that with the tick — which is doing the same thing — is how a
    request is admitted against a queue that no longer exists by the time it is
    appended, so admission takes the SAME state lock the tick holds.
    """
    orchestrator, lock, parker, ticking, tick_result = _parked_tick(sample_config)

    request = TechLeadRunRequest(scope=FOCUS, trigger=TechLeadRunTrigger.DASHBOARD)
    admitting, admission_result = named_thread(
        "admission", orchestrator.request_tech_lead_run, request
    )
    wait_for_event(lock.parked, SIGNAL_SECONDS, label="admission parked")

    parker.may_continue.set()
    join_or_fail(ticking, SIGNAL_SECONDS, label="tick")
    join_or_fail(admitting, SIGNAL_SECONDS, label="admission")

    assert lock.went_after("admission", "tick")
    tick_result.unwrap()
    assert admission_result.unwrap().run_key == FOCUS.run_key


def test_a_tech_lead_launch_also_serializes_against_the_tick(sample_config):
    """The launch authority reads live state and then mutates it, too."""
    orchestrator, lock, parker, ticking, tick_result = _parked_tick(sample_config)

    launching, launch_result = named_thread(
        "launch", orchestrator.launch_tech_lead_session, _pending_investigation()
    )
    wait_for_event(lock.parked, SIGNAL_SECONDS, label="launch parked")

    parker.may_continue.set()
    join_or_fail(ticking, SIGNAL_SECONDS, label="tick")
    join_or_fail(launching, SIGNAL_SECONDS, label="launch")

    assert lock.went_after("launch", "tick")
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
