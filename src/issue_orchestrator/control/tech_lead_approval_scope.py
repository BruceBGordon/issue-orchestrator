"""The approval backlog's own observation scope (#7014).

Split from ``tech_lead_proposals`` because it is one subject with one rule:
the backlog is defined by a LABEL, so only a query for that label observes it,
and only that query may decide who is in it. Everything else a tick holds is a
query for something else that merely overlaps.

Kept out of the fact gatherer for the same reason it is kept out of the
proposal lifecycle: deciding WHEN to re-observe and WHAT counts as observed is
policy about the approval scope, not about gathering facts or reconciling ops.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..domain.tech_lead_session import (
    GatedTechLeadProposal,
    PROPOSED_TECH_LEAD_LABEL,
)
from .tech_lead_proposals import (
    TECH_LEAD_PROPOSAL_SCAN_LIMIT,
    observe_gated_tech_lead_proposals,
)

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from ..ports import RepositoryHost
    from ..ports.issue import Issue

# How often the approval scope is re-observed when nothing else arms the tick.
# One labelled, exhaustive read per interval rather than per tick, while still
# guaranteeing a repo with only hidden approvals discovers them.
APPROVAL_SCAN_INTERVAL_SECONDS = 300.0

logger = logging.getLogger(__name__)


def approval_refresh_due(
    config: "Config", state: "OrchestratorState", now: float, authority: object
) -> bool:
    """Whether the approval scope must be re-observed on this tick.

    Deliberately independent of every other trigger. Each of those is either
    unrelated to approvals or reads the worker board — the very fetch whose
    blind spot the gate query exists to cover — so arming on them infers
    "quiet" from the source that cannot see the thing being looked for. A repo
    whose only tech-lead activity is worker-labelled proposals outside that
    board therefore never armed, never queried, and never showed them.

    ``tech_lead_approval_scan_at`` is 0.0 on a fresh state, so the first tick
    after startup is always due. Gated on an authority store because without
    one there is no proposal lifecycle at all, and a repo in that shape keeps
    costing nothing.
    """
    if not config.tech_lead_enabled or authority is None:
        return False
    return now - state.tech_lead_approval_scan_at >= APPROVAL_SCAN_INTERVAL_SECONDS


def discover_open_gated_proposals(
    repository_host: "RepositoryHost", config: "Config"
) -> list["Issue"]:
    """AUTHORITATIVE observation of the approval backlog, in its own scope.

    The backlog is defined by a LABEL, so the only complete observation of it
    is a query for that label. Everything the tick already holds is a query
    for something else that merely overlaps:

    - the worker board is narrowed by configured agents, milestones, exclusion
      filters and a fetch limit — it fetches runnable work, not approvals;
    - the anchor scan queries the TECH-LEAD agent label, while a promoted
      finding carries the TARGET'S worker agent label so it is
      "DISCOVERABLE the moment the gate comes off"
      (:func:`~.tech_lead_finding_promotion.promotion_issue_labels`) — and is
      therefore structurally invisible to an agent-scoped scan.

    Joining those two does not produce a complete set; it produces two
    incomplete ones. This costs one labelled query on ticks that already do
    tech-lead work, and it is what lets the board be written straight from
    the facts: a complete observation needs no retention, and retention is
    what would let an approved proposal linger (the warning in
    ``_build_view``'s own docstring).

    ``exhaustive`` for the same reason the anchor scan is (#6779 R17): a
    dropped page must RAISE rather than return a silently partial set a caller
    would read as "fewer approvals pending".
    """
    from .health_review_trigger import _scoped_issues

    issues = repository_host.list_issues(
        labels=[
            value
            for value in (PROPOSED_TECH_LEAD_LABEL, config.filtering.label)
            if value
        ],
        state="open",
        limit=TECH_LEAD_PROPOSAL_SCAN_LIMIT,
        exhaustive=True,
    )
    return _scoped_issues(issues, config.filtering.label)


def observe_approval_backlog(
    repository_host: "RepositoryHost",
    config: "Config",
    *partial: Sequence["Issue"],
) -> tuple[GatedTechLeadProposal, ...]:
    """The backlog as the board should publish it: complete, and this tick's.

    Composes the two halves so no caller has to remember to do both. The sets
    a tick already holds go in first (free, and the freshest evidence about
    the issues they cover), and the authoritative gate-label query goes last
    so it decides the ones only it can see.

    The exhaustive query decides membership; the free sets only enrich it.
    Complete on purpose, because the board is written straight from the
    result. The alternative — publishing a partial observation and retaining
    what it missed — trades erasing a pending approval for advertising one
    the operator already approved, which is the failure ``_build_view``'s
    docstring warns about and #7014's own symptom.
    """
    authoritative = discover_open_gated_proposals(repository_host, config)
    # MEMBERSHIP comes from the authoritative query; the partial sets may only
    # enrich what it already contains.
    #
    # Appending it to a union does not make it authoritative, because its
    # verdict on a retired proposal is ABSENCE, and absence supersedes nothing.
    # If the worker board saw gated #9000 and the operator approves it before
    # this query runs, the query returns no #9000 at all — so a plain union
    # keeps advertising it on the strength of the older, narrower observation,
    # against the complete later one. (That is distinct from an explicit
    # ungated or closed OBJECT arriving later, which the observer already
    # handles by resolving the latest observation per issue.)
    in_scope = {issue.number for issue in authoritative}
    observed = observe_gated_tech_lead_proposals(*partial, authoritative)
    return tuple(
        proposal for proposal in observed if proposal.issue_number in in_scope
    )


def observe_approval_backlog_or_none(
    repository_host: "RepositoryHost",
    config: "Config",
    *partial: Sequence["Issue"],
    decline_on_failure: bool = True,
) -> tuple[GatedTechLeadProposal, ...] | None:
    """The backlog, or None when this tick could not observe its scope.

    ``decline_on_failure`` must be False whenever the tick has ALREADY gathered
    facts of its own — an anchor scan, approved ops, promotion updates. None
    reaches the planner as "nothing armed", which is not merely a missing
    board: with a problem storm underway it discards the very
    ``existing_health_review_issue`` this tick observed, and the storm planner
    mints a DUPLICATE anchor. Declining to publish is a statement about the
    approval display only; it must never be a statement about facts that were
    successfully observed (F5). Where those exist, the failure propagates so
    the snapshot cannot be planned at all, which is how the anchor scan has
    always behaved.

    None means DECLINE TO PUBLISH. The board is rewritten wholesale from the
    facts a tick produces, so publishing a backlog we could not observe is
    exactly the erasure this work exists to prevent — an operator would watch
    approvals vanish because GitHub was briefly unreachable. Leaving the last
    good board in place costs nothing the outage was not already costing, and
    cannot strand: the caller does not advance its refresh timestamp, so the
    next tick retries immediately.
    """
    from ..ports.repository_host import RepositoryHostError

    try:
        return observe_approval_backlog(repository_host, config, *partial)
    except RepositoryHostError as error:
        if not decline_on_failure:
            raise
        logger.warning(
            "[tech_lead] approval scope unobservable this tick, board left as "
            "published: %s",
            error,
        )
        return None
