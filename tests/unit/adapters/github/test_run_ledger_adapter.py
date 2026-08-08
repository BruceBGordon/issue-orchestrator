"""The production run-ledger adapter, at its real boundary (#6994 round 2 F11).

Cross-engine exclusivity depends on this adapter's atomicity and on its failure
semantics, and neither is observable from the pure resolver: the matrix can be
perfect while the adapter writes on a refusal, gives up silently on a lost
compare-and-swap, or reads a corrupt ledger as an empty one. So these tests
drive the adapter against an in-memory Git Database that enforces fast-forward
ref updates — the same fake the issue claim adapter uses — and assert on the
WRITES it makes, not only on the verdicts it returns.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from issue_orchestrator.adapters.github.errors import GitHubHttpError
from issue_orchestrator.adapters.github.ref_claim_adapter import (
    CLAIM_REF_PREFIX,
    MAX_CAS_ATTEMPTS,
    RUN_LEDGER_REF_KEY,
    GitHubRefRunLedgerAdapter,
)
from issue_orchestrator.domain.lease_config import LeaseConfig
from issue_orchestrator.domain.run_ledger import (
    RunLedger,
    RunLedgerEntry,
    RunLedgerRequest,
    RunLedgerRequestKind,
    RunLedgerStatus,
    RunLifecycle,
    format_run_ledger,
)
from issue_orchestrator.domain.tech_lead_run import (
    GlobalHealthReviewScope,
    IssueInvestigationScope,
    TechLeadRunScopeKind,
)

from .test_ref_claim_adapter import FakeGitHubRefClient

HEALTH = GlobalHealthReviewScope()
FOCUS = IssueInvestigationScope(42)
LEDGER_REF = f"{CLAIM_REF_PREFIX}/{RUN_LEDGER_REF_KEY}"
NOW = datetime(2026, 8, 7, 12, 0, 0)


def _adapter(
    client: FakeGitHubRefClient, claimant: str = "engine-a"
) -> GitHubRefRunLedgerAdapter:
    return GitHubRefRunLedgerAdapter(
        client=client,  # type: ignore[arg-type]
        claimant_id=claimant,
        config=LeaseConfig(lease_seconds=900),
    )


def _reserve(run_key: str = HEALTH.run_key, kind=None) -> RunLedgerRequest:
    return RunLedgerRequest(
        kind=RunLedgerRequestKind.RESERVE,
        run_key=run_key,
        scope_kind=kind or TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW,
    )


def _seed(client: FakeGitHubRefClient, message: str) -> None:
    """Point the ledger ref at a commit carrying ``message``."""
    commit = client.create_git_commit(
        message=message, tree_sha="tree-base", parents=["base"]
    )
    client.create_git_ref(ref=LEDGER_REF, sha=commit["sha"])


def _peer_entry(
    *,
    lifecycle: RunLifecycle = RunLifecycle.QUEUED,
    claimant: str = "engine-b",
    run_key: str = HEALTH.run_key,
    kind: TechLeadRunScopeKind = TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW,
) -> RunLedgerEntry:
    return RunLedgerEntry(
        run_key=run_key,
        scope_kind=kind,
        lifecycle=lifecycle,
        claimant=claimant,
        lease_id="peer-lease",
        started_at=NOW,
        expires_at=datetime(2999, 1, 1),
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def test_the_first_reservation_CREATES_the_shared_ledger_ref():
    client = FakeGitHubRefClient()

    outcome = _adapter(client).submit(_reserve())

    assert outcome.status is RunLedgerStatus.GRANTED
    assert outcome.lease_id
    assert [ref for ref, _sha in client.created_refs] == [LEDGER_REF]
    assert LEDGER_REF in client.refs


def test_a_second_run_UPDATES_the_existing_ref_rather_than_recreating_it():
    client = FakeGitHubRefClient()
    adapter = _adapter(client)
    adapter.submit(_reserve())

    outcome = adapter.submit(_reserve(FOCUS.run_key, TechLeadRunScopeKind.ISSUE))

    assert outcome.status is RunLedgerStatus.GRANTED
    assert len(client.created_refs) == 1
    assert [ref for ref, _sha, _force in client.updated_refs] == [LEDGER_REF]
    ledger = adapter.read()
    assert ledger is not None
    assert sorted(entry.run_key for entry in ledger.entries) == [
        HEALTH.run_key,
        FOCUS.run_key,
    ]


def test_a_lost_compare_and_swap_RE_READS_and_re_decides():
    """A concurrent writer must make us look again, never assume an outcome."""
    client = FakeGitHubRefClient()
    adapter = _adapter(client)
    adapter.submit(_reserve())
    client.conflict_updates_remaining = 1

    outcome = adapter.submit(_reserve(FOCUS.run_key, TechLeadRunScopeKind.ISSUE))

    assert outcome.status is RunLedgerStatus.GRANTED
    # One rejected update, then a successful one from a fresh read.
    assert len(client.updated_refs) == 1


def test_a_ledger_that_keeps_changing_gives_up_as_UNAVAILABLE_not_as_free():
    client = FakeGitHubRefClient()
    adapter = _adapter(client)
    adapter.submit(_reserve())
    client.conflict_updates_remaining = MAX_CAS_ATTEMPTS

    outcome = adapter.submit(_reserve(FOCUS.run_key, TechLeadRunScopeKind.ISSUE))

    assert outcome.status is RunLedgerStatus.UNAVAILABLE
    assert client.updated_refs == []


# ---------------------------------------------------------------------------
# Refusals cost nothing
# ---------------------------------------------------------------------------


def test_a_peer_held_run_is_refused_WITHOUT_any_write():
    client = FakeGitHubRefClient()
    _seed(client, format_run_ledger(RunLedger((_peer_entry(),))))
    before = len(client.commits)

    outcome = _adapter(client).submit(_reserve())

    assert outcome.status is RunLedgerStatus.HELD_BY_PEER
    assert outcome.holder == "engine-b"
    assert client.updated_refs == []
    assert len(client.commits) == before, "a refusal must not author a commit"


def test_a_barrier_is_refused_without_a_write_too():
    client = FakeGitHubRefClient()
    _seed(
        client,
        format_run_ledger(RunLedger((_peer_entry(lifecycle=RunLifecycle.RUNNING),))),
    )
    adapter = _adapter(client)
    reserved = adapter.submit(_reserve(FOCUS.run_key, TechLeadRunScopeKind.ISSUE))
    writes_before = len(client.updated_refs)

    promotion = adapter.submit(
        RunLedgerRequest(
            kind=RunLedgerRequestKind.PROMOTE,
            run_key=FOCUS.run_key,
            scope_kind=TechLeadRunScopeKind.ISSUE,
            lease_id=reserved.lease_id,
        )
    )

    assert promotion.status is RunLedgerStatus.BARRIER
    assert len(client.updated_refs) == writes_before


# ---------------------------------------------------------------------------
# Renew / release
# ---------------------------------------------------------------------------


def test_renew_extends_our_own_hold_and_release_removes_it():
    client = FakeGitHubRefClient()
    adapter = _adapter(client)
    reserved = adapter.submit(_reserve())

    renewed = adapter.submit(
        RunLedgerRequest(
            kind=RunLedgerRequestKind.RENEW,
            run_key=HEALTH.run_key,
            scope_kind=HEALTH.kind,
            lease_id=reserved.lease_id,
        )
    )
    assert renewed.status is RunLedgerStatus.GRANTED

    released = adapter.submit(
        RunLedgerRequest(
            kind=RunLedgerRequestKind.RELEASE,
            run_key=HEALTH.run_key,
            scope_kind=HEALTH.kind,
            lease_id=reserved.lease_id,
        )
    )

    assert released.status is RunLedgerStatus.GRANTED
    ledger = adapter.read()
    assert ledger is not None and ledger.entries == ()


def test_renewing_a_hold_a_peer_took_reports_definitive_LOSS():
    client = FakeGitHubRefClient()
    _seed(client, format_run_ledger(RunLedger((_peer_entry(),))))

    outcome = _adapter(client).submit(
        RunLedgerRequest(
            kind=RunLedgerRequestKind.RENEW,
            run_key=HEALTH.run_key,
            scope_kind=HEALTH.kind,
            lease_id="ours",
        )
    )

    assert outcome.status is RunLedgerStatus.LOST
    assert outcome.holder == "engine-b"


# ---------------------------------------------------------------------------
# Failure semantics — the whole point of the boundary
# ---------------------------------------------------------------------------


def test_a_transport_error_is_UNAVAILABLE_and_writes_nothing():
    class UnreachableClient(FakeGitHubRefClient):
        def get_git_ref(self, ref: str):
            raise GitHubHttpError("gateway timeout", status_code=504)

    client = UnreachableClient()

    outcome = _adapter(client).submit(_reserve())

    assert outcome.status is RunLedgerStatus.UNAVAILABLE
    assert client.created_refs == []
    assert client.updated_refs == []
    assert _adapter(client).read() is None


@pytest.mark.parametrize(
    "message,why",
    [
        ("no ledger block here at all", "an unrecognised record"),
        ("<io-run-ledger>\nnot json\n</io-run-ledger>", "a malformed payload"),
        (
            '<io-run-ledger>\n{"version": 99, "entries": []}\n</io-run-ledger>',
            "a forward schema version",
        ),
        (
            '<io-run-ledger>\n{"version": 1, "entries": [{"run_key":'
            ' "global:health_review", "scope_kind": "global_time_travel",'
            ' "lifecycle": "queued", "claimant": "engine-b", "lease_id": "x",'
            ' "started_at": "2026-08-07T12:00:00",'
            ' "expires_at": "2999-01-01T00:00:00"}]}\n</io-run-ledger>',
            "a live row this build cannot classify",
        ),
        (
            '<io-run-ledger>\n{"version": 1, "entries": [{"run_key":'
            ' "global:health_review", "scope_kind": "global_health_review",'
            ' "lifecycle": "running", "claimant": "engine-b", "lease_id": "x",'
            ' "started_at": "2026-08-07T12:00:00",'
            ' "expires_at": "2999-01-01T00:00:00"}], "entries": []}'
            "\n</io-run-ledger>",
            "a duplicate member hiding a live global run",
        ),
        (
            '<io-run-ledger>\n{"version": 1, "entries": [], "extra": 1}'
            "\n</io-run-ledger>",
            "an unexpected top-level member",
        ),
    ],
)
def test_an_undecodable_ledger_refuses_the_request_and_writes_NOTHING(
    message: str, why: str
):
    """A partial read of an exclusivity ledger makes a BUSY repo look free (F7)."""
    client = FakeGitHubRefClient()
    _seed(client, message)
    adapter = _adapter(client)

    outcome = adapter.submit(_reserve())

    assert outcome.status is RunLedgerStatus.UNAVAILABLE, why
    assert outcome.detail
    assert client.updated_refs == []
    assert adapter.read() is None, "an undecodable ledger is not a readable one"


def test_an_absent_ref_is_an_EMPTY_ledger_not_an_unreadable_one():
    """Absent and corrupt are different facts and must not be conflated."""
    adapter = _adapter(FakeGitHubRefClient())

    assert adapter.read() == RunLedger()


def test_an_expired_peer_hold_is_reclaimable_through_the_real_adapter():
    client = FakeGitHubRefClient()
    expired = RunLedgerEntry(
        run_key=HEALTH.run_key,
        scope_kind=HEALTH.kind,
        lifecycle=RunLifecycle.QUEUED,
        claimant="engine-b",
        lease_id="peer-lease",
        started_at=NOW - timedelta(days=1),
        expires_at=NOW - timedelta(hours=1),
    )
    _seed(client, format_run_ledger(RunLedger((expired,))))

    outcome = _adapter(client).submit(_reserve())

    assert outcome.status is RunLedgerStatus.GRANTED
    assert client.updated_refs, "reclaiming an expired hold is a real write"
