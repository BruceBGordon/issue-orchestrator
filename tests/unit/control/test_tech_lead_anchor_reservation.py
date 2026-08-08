"""The global run is reserved BEFORE its anchor is created (#6994 R2 F3 / A3).

The periodic/storm health-review path creates its anchor through the planner's
action pipeline. Round 1 claimed the run in the POST-apply handler, which is a
scan-then-create gap wearing a claim's clothes: two engines both scan, both find
no open anchor, both create one, and only afterwards does one of them discover it
lost. A claim cannot un-create a GitHub issue.

The wiring tests that existed began AFTER the side effect and asserted only
whether the local queue changed, so they could not have detected the duplicate
anchor at all. These start BEFORE it, interleave the two engines explicitly, and
assert on the number of ``create_issue`` calls — the fact that actually matters.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import CreateTechLeadIssueAction
from issue_orchestrator.control.health_review_trigger import (
    HEALTH_REVIEW_MARKER_LABEL,
)
from issue_orchestrator.domain.tech_lead_run import GlobalHealthReviewScope
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadCreationOrigin,
    TechLeadSessionFlavor,
)

from .run_ledger_doubles import SharedRunLedger

HEALTH = GlobalHealthReviewScope()


class CountingRepositoryHost:
    """A GitHub host that records every issue it was actually asked to create."""

    def __init__(self, start: int = 900) -> None:
        self.created: list[str] = []
        self._next = start

    def create_issue(self, *args, **kwargs) -> dict:
        title = kwargs.get("title") or (args[0] if args else "")
        self.created.append(str(title))
        number = self._next
        self._next += 1
        return {"number": number, "html_url": f"https://example.test/{number}"}

    def add_comment(self, *_args, **_kwargs) -> None:
        return None

    def __getattr__(self, name):  # pragma: no cover - unrelated host surface
        return MagicMock()


def _anchor_action(
    flavor: TechLeadSessionFlavor = TechLeadSessionFlavor.HEALTH_REVIEW,
) -> CreateTechLeadIssueAction:
    return CreateTechLeadIssueAction(
        title="Repository health review",
        body="Walk the board",
        labels=("tech-lead-agent", HEALTH_REVIEW_MARKER_LABEL),
        pr_count=0,
        flavor=flavor,
        origin=TechLeadCreationOrigin.authors_anchor(),
    )


def _engine(shared: SharedRunLedger, claimant: str, host: CountingRepositoryHost):
    return ActionApplier(
        labels=MagicMock(),
        sessions=MagicMock(),
        events=MagicMock(),
        repository_host=host,  # type: ignore[arg-type]
        reconcile=False,
        run_ownership=shared.ownership(claimant),
    )


def test_two_engines_racing_the_same_anchor_create_it_exactly_ONCE():
    """The race, interleaved by hand rather than raced for."""
    shared = SharedRunLedger()
    host = CountingRepositoryHost()
    engine_a = _engine(shared, "engine-a", host)
    engine_b = _engine(shared, "engine-b", host)

    won = engine_a.apply(_anchor_action())
    lost = engine_b.apply(_anchor_action())

    assert won.success
    assert not lost.success
    assert len(host.created) == 1, "exactly one anchor issue may exist"
    assert "another orchestrator" in lost.error.lower()


def test_the_loser_makes_no_github_write_at_all():
    """A refusal must not burn a create and then apologise."""
    shared = SharedRunLedger()
    assert shared.ownership("engine-b").claim(HEALTH).owned
    host = CountingRepositoryHost()

    result = _engine(shared, "engine-a", host).apply(_anchor_action())

    assert not result.success
    assert host.created == []


def test_a_failed_create_hands_the_reserved_run_straight_back():
    """Compensation: a reserved-but-uncreated run must not block the repository."""
    shared = SharedRunLedger()
    host = CountingRepositoryHost()
    host.create_issue = MagicMock(side_effect=RuntimeError("GitHub said no"))

    result = _engine(shared, "engine-a", host).apply(_anchor_action())

    assert not result.success
    assert shared.live_keys() == (), "the hold must not survive a failed create"


def test_a_batch_anchor_reserves_its_OWN_global_identity():
    """Health and batch are distinct runs; one must not suppress the other."""
    shared = SharedRunLedger()
    assert shared.ownership("engine-b").claim(HEALTH).owned
    host = CountingRepositoryHost()

    result = _engine(shared, "engine-a", host).apply(
        _anchor_action(TechLeadSessionFlavor.BATCH_REVIEW)
    )

    assert result.success
    assert len(host.created) == 1
    assert set(shared.live_keys()) == {HEALTH.run_key, "global:batch_review"}


def test_anchor_creation_refuses_to_race_when_no_coordination_is_wired():
    """Fail loudly rather than create an anchor nothing could coordinate."""
    host = CountingRepositoryHost()
    applier = ActionApplier(
        labels=MagicMock(),
        sessions=MagicMock(),
        events=MagicMock(),
        repository_host=host,  # type: ignore[arg-type]
        reconcile=False,
    )

    result = applier.apply(_anchor_action())

    assert not result.success
    assert host.created == []
