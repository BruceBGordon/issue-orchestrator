"""Scope exclusivity holds ACROSS engines, not just within one (#6994 R2 F1).

The round-1 design owned each run key independently, which is correct about one
key and silent about the relationship between keys: engine A could own and run
``global:health_review`` while engine B independently owned and ran
``issue:42``, each perfectly within its rights. The invariant the feature exists
to enforce was therefore never enforced at all once a second Repository Engine
existed.

These tests drive TWO engines over ONE shared ledger cell and pin the whole
matrix. They interleave the two engines' calls explicitly rather than racing
them, so every ordering is reproduced exactly and deterministically — no
threads, no sleeps, and a hand-advanced clock.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from issue_orchestrator.control.tech_lead_run_ownership import (
    RunExecutionVerdict,
    RunOwnershipVerdict,
)
from issue_orchestrator.domain.run_ledger import (
    RUN_LEDGER_VERSION,
    BARRIER_GLOBAL_AWAITING_DRAIN,
    BARRIER_GLOBAL_RUN_ACTIVE,
    BARRIER_GLOBAL_RUN_QUEUED,
    RunLedger,
    RunLedgerDecodeError,
    RunLedgerRequest,
    RunLedgerRequestKind,
    RunLifecycle,
    format_run_ledger,
    parse_run_ledger,
)
from issue_orchestrator.domain.tech_lead_run import (
    GlobalBatchReviewScope,
    GlobalHealthReviewScope,
    IssueInvestigationScope,
)

from .run_ledger_doubles import LEASE_SECONDS, FrozenClock, SharedRunLedger

HEALTH = GlobalHealthReviewScope()
BATCH = GlobalBatchReviewScope()


def _two_engines(clock: FrozenClock | None = None):
    """Two Repository Engines coordinating through one shared ledger."""
    shared = SharedRunLedger(clock)
    return shared, shared.ownership("engine-a"), shared.ownership("engine-b")


def _start(ownership, scope) -> None:
    """Take a run all the way to RUNNING, asserting each step succeeded."""
    assert ownership.claim(scope).owned
    assert ownership.begin_run(scope).started


# ---------------------------------------------------------------------------
# The matrix, one cell per test
# ---------------------------------------------------------------------------


def test_a_peers_running_global_blocks_this_engines_targeted_run():
    """The exact gap round 1 left open: two engines, two keys, one violation."""
    _shared, engine_a, engine_b = _two_engines()
    _start(engine_a, HEALTH)

    assert engine_b.claim(IssueInvestigationScope(42)).owned
    admission = engine_b.begin_run(IssueInvestigationScope(42))

    assert admission.verdict is RunExecutionVerdict.BARRIER
    assert admission.barrier_reason == BARRIER_GLOBAL_RUN_ACTIVE
    assert admission.holder == "engine-a"


def test_a_peers_QUEUED_global_is_a_barrier_too_not_merely_first_in_line():
    """A queued global holds targeted work back on every engine, not just its own."""
    _shared, engine_a, engine_b = _two_engines()
    assert engine_a.claim(HEALTH).owned  # queued, never started

    assert engine_b.claim(IssueInvestigationScope(42)).owned
    admission = engine_b.begin_run(IssueInvestigationScope(42))

    assert admission.verdict is RunExecutionVerdict.BARRIER
    assert admission.barrier_reason == BARRIER_GLOBAL_RUN_QUEUED


def test_a_global_run_waits_for_a_peers_targeted_run_to_DRAIN():
    """Exclusive means exclusive: the global waits, then goes."""
    _shared, engine_a, engine_b = _two_engines()
    _start(engine_a, IssueInvestigationScope(42))

    assert engine_b.claim(HEALTH).owned
    held = engine_b.begin_run(HEALTH)

    assert held.verdict is RunExecutionVerdict.BARRIER
    assert held.barrier_reason == BARRIER_GLOBAL_AWAITING_DRAIN

    engine_a.end_run(IssueInvestigationScope(42).run_key)

    assert engine_b.begin_run(HEALTH).started


def test_a_running_health_review_makes_a_peers_batch_review_wait():
    """Health -> batch: two global identities that SERIALIZE across engines."""
    _shared, engine_a, engine_b = _two_engines()
    _start(engine_a, HEALTH)

    assert engine_b.claim(BATCH).owned
    admission = engine_b.begin_run(BATCH)

    assert admission.verdict is RunExecutionVerdict.BARRIER
    assert admission.barrier_reason == BARRIER_GLOBAL_AWAITING_DRAIN


def test_a_running_batch_review_makes_a_peers_health_review_wait():
    """Batch -> health: the same rule, the other way round."""
    _shared, engine_a, engine_b = _two_engines()
    _start(engine_a, BATCH)

    assert engine_b.claim(HEALTH).owned
    admission = engine_b.begin_run(HEALTH)

    assert admission.verdict is RunExecutionVerdict.BARRIER
    assert admission.barrier_reason == BARRIER_GLOBAL_AWAITING_DRAIN


def test_distinct_targeted_investigations_run_concurrently_on_two_engines():
    """Different issues are different work; exclusivity would be a deadlock."""
    _shared, engine_a, engine_b = _two_engines()

    _start(engine_a, IssueInvestigationScope(42))
    _start(engine_b, IssueInvestigationScope(73))


def test_two_queued_globals_pick_the_SAME_winner_on_both_engines():
    """Deterministic ordering, or both engines conclude they are next.

    The turn order is ``global_run_precedence`` — a pure function of run
    IDENTITY, evaluated over the shared cell — so two engines cannot disagree
    and neither can a local planner that has not read the cell (round 2 F1,
    round 5 F16).
    """
    clock = FrozenClock()
    _shared, engine_a, engine_b = _two_engines(clock)
    assert engine_a.claim(HEALTH).owned
    clock.advance(1)
    assert engine_b.claim(BATCH).owned

    second = engine_b.begin_run(BATCH)
    first = engine_a.begin_run(HEALTH)

    assert second.verdict is RunExecutionVerdict.BARRIER
    assert second.barrier_reason == BARRIER_GLOBAL_RUN_QUEUED
    assert first.started


def test_reserving_the_LATER_turn_first_does_not_move_it_ahead():
    """Reservation time is not the authority — two engines' clocks disagree.

    ``started_at`` is stamped by whichever engine wrote the entry, from its own
    wall clock, so ordering by it is not a total order any peer can reproduce.
    """
    clock = FrozenClock()
    _shared, engine_a, engine_b = _two_engines(clock)
    assert engine_a.claim(BATCH).owned  # reserved FIRST...
    clock.advance(60)
    assert engine_b.claim(HEALTH).owned  # ...but health still has the turn

    batch = engine_a.begin_run(BATCH)
    health = engine_b.begin_run(HEALTH)

    assert batch.verdict is RunExecutionVerdict.BARRIER
    assert batch.barrier_reason == BARRIER_GLOBAL_RUN_QUEUED
    assert health.started


def test_the_same_run_identity_cannot_be_owned_by_two_engines():
    """Dedup across engines: the loser is told WHO won, not "queued"."""
    _shared, engine_a, engine_b = _two_engines()
    assert engine_a.claim(HEALTH).owned

    ownership = engine_b.claim(HEALTH)

    assert ownership.verdict is RunOwnershipVerdict.HELD_BY_PEER
    assert ownership.holder == "engine-a"


def test_an_expired_hold_is_free_so_a_dead_engine_cannot_strand_a_run():
    """An unrenewed lease is not a lease; otherwise a crash blocks forever."""
    clock = FrozenClock()
    _shared, engine_a, engine_b = _two_engines(clock)
    assert engine_a.claim(HEALTH).owned

    clock.advance(LEASE_SECONDS + 1)

    assert engine_b.claim(HEALTH).owned


def test_releasing_a_global_immediately_frees_targeted_work_on_a_peer():
    """A finished exclusive run must not make peers wait out its whole lease."""
    _shared, engine_a, engine_b = _two_engines()
    _start(engine_a, HEALTH)
    assert engine_b.claim(IssueInvestigationScope(42)).owned
    assert not engine_b.begin_run(IssueInvestigationScope(42)).started

    engine_a.end_run(HEALTH.run_key)

    assert engine_b.begin_run(IssueInvestigationScope(42)).started


def test_a_launch_that_never_happened_gives_the_exclusive_hold_straight_back():
    """``end_run`` is the compensation the launch authority applies on failure."""
    _shared, engine_a, engine_b = _two_engines()
    _start(engine_a, HEALTH)

    engine_a.end_run(HEALTH.run_key)

    assert engine_b.claim(HEALTH).owned


# ---------------------------------------------------------------------------
# Wire format — two engines must read each other's bytes
# ---------------------------------------------------------------------------


def test_the_ledger_survives_a_round_trip_through_its_wire_format():
    shared, engine_a, _engine_b = _two_engines()
    _start(engine_a, HEALTH)
    assert engine_a.claim(IssueInvestigationScope(42)).owned

    restored = parse_run_ledger(format_run_ledger(shared.ledger))

    assert restored == shared.ledger
    health = restored.find(HEALTH.run_key, shared.clock())
    assert health is not None and health.lifecycle is RunLifecycle.RUNNING


def _encoded(**overrides) -> str:
    """One live global row, encoded, with fields overridden for a defect case."""
    row = {
        "run_key": HEALTH.run_key,
        "scope_kind": HEALTH.kind.value,
        "lifecycle": "queued",
        "claimant": "engine-b",
        "lease_id": "peer-1",
        "started_at": "2026-08-07T12:00:00",
        "expires_at": "2999-01-01T00:00:00",
    }
    row.update(overrides)
    payload = {"version": RUN_LEDGER_VERSION, "entries": [row]}
    return f"<io-run-ledger>\n{json.dumps(payload)}\n</io-run-ledger>"


@pytest.mark.parametrize(
    "text,why",
    [
        (_encoded(scope_kind="global_time_travel"), "a forward-version scope kind"),
        (_encoded(lifecycle="paused"), "an unknown lifecycle"),
        (_encoded(started_at="whenever"), "an unparseable timestamp"),
        (_encoded(run_key="issue:7"), "a run key that contradicts its scope"),
        (_encoded(run_key=""), "an empty run key"),
        ("<io-run-ledger>\nnot json\n</io-run-ledger>", "a malformed payload"),
        ("no ledger block at all", "a missing block"),
        (
            "<io-run-ledger>\n"
            + json.dumps({"version": RUN_LEDGER_VERSION, "entries": [], "extra": 1})
            + "\n</io-run-ledger>",
            "an unexpected top-level member",
        ),
        (
            f"<io-run-ledger>\n{json.dumps({'version': 2, 'entries': []})}\n"
            "</io-run-ledger>",
            "an unsupported schema version",
        ),
    ],
)
def test_a_ledger_this_build_cannot_read_EXACTLY_is_refused_not_partially_read(
    text: str, why: str
):
    """A dropped row makes a BUSY repository look free (#6994 round 2 F7).

    The dropped row could be the live global that makes everything else wait,
    so decoding is all-or-nothing and the caller fails closed.
    """
    with pytest.raises(RunLedgerDecodeError):
        parse_run_ledger(text)
    assert why  # names the defect this case pins


def test_a_duplicate_run_key_is_refused_rather_than_silently_deduplicated():
    row = {
        "run_key": HEALTH.run_key,
        "scope_kind": HEALTH.kind.value,
        "lifecycle": "queued",
        "claimant": "engine-b",
        "lease_id": "peer-1",
        "started_at": "2026-08-07T12:00:00",
        "expires_at": "2999-01-01T00:00:00",
    }
    payload = {"version": RUN_LEDGER_VERSION, "entries": [row, dict(row)]}
    text = f"<io-run-ledger>\n{json.dumps(payload)}\n</io-run-ledger>"

    with pytest.raises(RunLedgerDecodeError):
        parse_run_ledger(text)


def test_field_values_containing_the_delimiter_survive_a_round_trip():
    """Token splitting could not round-trip these; the codec must."""
    shared, engine_a, _engine_b = _two_engines()
    assert engine_a.claim(HEALTH).owned
    entry = shared.ledger.entries[0]
    awkward = replace(
        entry, claimant="engine a=b c", lease_id="lease with spaces\tand tabs"
    )

    restored = parse_run_ledger(format_run_ledger(RunLedger((awkward,))))

    assert restored.entries == (awkward,)


def test_an_unreadable_store_answers_unavailable_rather_than_free():
    """Fail CLOSED: guessing is what produces the duplicate run."""
    shared, engine_a, _engine_b = _two_engines()
    shared.unavailable = True

    ownership = engine_a.claim(HEALTH)

    assert ownership.verdict is RunOwnershipVerdict.UNAVAILABLE
    assert not ownership.owned


def test_a_promote_request_must_name_the_lease_it_holds():
    """The request type refuses to be constructed without its evidence."""
    try:
        RunLedgerRequest(
            kind=RunLedgerRequestKind.PROMOTE,
            run_key=HEALTH.run_key,
            scope_kind=HEALTH.kind,
        )
    except ValueError as exc:
        assert "lease" in str(exc)
    else:  # pragma: no cover - the guard is the point of the test
        raise AssertionError("a promote with no lease must fail fast")


def test_a_DUPLICATE_top_level_member_cannot_hide_a_live_run():
    """json.loads is last-value-wins; that would read a busy repo as free (F13).

    A record carrying a live global run in one ``entries`` member and ``[]`` in
    a second would otherwise decode as an empty ledger, and the adapter would
    grant a conflicting reservation and overwrite the shared ref.
    """
    live = {
        "run_key": HEALTH.run_key,
        "scope_kind": HEALTH.kind.value,
        "lifecycle": "running",
        "claimant": "engine-b",
        "lease_id": "peer-1",
        "started_at": "2026-08-07T12:00:00",
        "expires_at": "2999-01-01T00:00:00",
    }
    body = (
        f'{{"version": {RUN_LEDGER_VERSION},'
        f' "entries": [{json.dumps(live)}],'
        f' "entries": []}}'
    )

    with pytest.raises(RunLedgerDecodeError):
        parse_run_ledger(f"<io-run-ledger>\n{body}\n</io-run-ledger>")


def test_a_DUPLICATE_member_inside_an_entry_is_refused_too():
    """The rule is recursive: a shadowed scope kind is the same fail-open."""
    body = (
        f'{{"version": {RUN_LEDGER_VERSION}, "entries": [{{'
        f'"run_key": "{HEALTH.run_key}",'
        f' "scope_kind": "{HEALTH.kind.value}",'
        f' "scope_kind": "issue",'
        ' "lifecycle": "queued", "claimant": "engine-b", "lease_id": "peer-1",'
        ' "started_at": "2026-08-07T12:00:00",'
        ' "expires_at": "2999-01-01T00:00:00"}]}'
    )

    with pytest.raises(RunLedgerDecodeError):
        parse_run_ledger(f"<io-run-ledger>\n{body}\n</io-run-ledger>")
