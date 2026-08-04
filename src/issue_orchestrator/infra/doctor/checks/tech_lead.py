"""Tech Lead workflow label + promotion-route checks for doctor.

When tech_lead is configured, act-level proposals are filed as GitHub issues
carrying the ``proposed-tech-lead`` gate (#6779 R3). A fresh install that never
provisioned that label would create ungated (schedulable) proposal issues, so
surface the missing gate here in addition to the applier's fail-before-create
guard.

The finding-promotion lane (#6957) adds a second precondition: every configured
``tech_lead.findings.route`` target must be a repo this token can actually file
that route's promotions in — the whole filing contract, labels included, since
``file_issue`` provisions any missing label before it creates the issue. That is
verified here rather than at promotion time, because a promotion fires on the
tick a pattern finally crosses its evidence threshold — discovering a misrouted
target then means losing the actuation the lane exists to provide.
"""

from typing import TYPE_CHECKING

from ..types import Check
from ...tech_lead_promotion_activation import promotion_lane_readiness

if TYPE_CHECKING:
    from ....ports.promotion_target import PromotionTargetHost
    from ...config import Config


def check_tech_lead_labels(config: "Config | None" = None) -> list[Check]:
    if config is None or not config.tech_lead_review_agent or not config.repo:
        return []  # tech_lead/repo not configured -> nothing to verify

    from ....domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL

    try:
        from ....execution.providers import create_repository_host

        host = create_repository_host(repo=config.repo, config=config)
        existing = {
            name.casefold()
            for entry in host.list_labels()
            if isinstance(entry, dict) and isinstance((name := entry.get("name")), str)
        }
    except Exception as exc:
        # Advisory only: a GitHub read failure must not fail doctor outright
        # (auth/connectivity are covered by their own checks).
        return [
            Check(
                name="Tech Lead Labels",
                status="warning",
                detail=f"Could not verify the '{PROPOSED_TECH_LEAD_LABEL}' gate label: {exc}",
            )
        ]

    gate_present = PROPOSED_TECH_LEAD_LABEL.casefold() in existing
    if gate_present:
        return [
            Check(
                name="Tech Lead Labels",
                status="ok",
                detail=f"Gate label '{PROPOSED_TECH_LEAD_LABEL}' provisioned",
            )
        ]
    return [
        Check(
            name="Tech Lead Labels",
            status="error",
            detail=(
                f"Gate label '{PROPOSED_TECH_LEAD_LABEL}' is missing — tech_lead"
                " proposals would be ungated. Run `issue-orchestrator init`."
            ),
        )
    ]


def check_tech_lead_finding_routes(
    config: "Config | None" = None,
    *,
    target_host: "PromotionTargetHost | None" = None,
) -> list[Check]:
    """Verify every finding-promotion route can actually be FILED into (#6957).

    Only NON-``self`` targets are probed: a ``self`` route lands in the managed
    repo, whose writability and label provisioning are already covered by the
    auth/repo/label checks. One probe per distinct target, and none at all when
    the lane is inactive or every route is ``self`` — GitHub API discipline.

    Neither half of the question is decided here. Activation comes from
    :func:`promotion_lane_readiness`, the same owner configuration validation
    and fact gathering consume — deciding it locally is what let doctor skip
    these probes while the runtime went on promoting anyway (round-2 review F9).
    WHAT must be proven comes from ``promotion_filing_contracts``, built on the
    lane's one route resolver — deriving a weaker permission check locally is
    what let doctor approve a route whose first promotion would die provisioning
    a label (round-6 review F2/A1).

    ``target_host`` is injectable so this check is testable without a live
    GitHub; production leaves it None and the host is built from config.
    """
    if config is None:
        return []
    readiness = promotion_lane_readiness(config)
    if not readiness.active:
        return []
    if readiness.problems:
        # An active-but-unready lane cannot even be routed, so there is nothing
        # to probe yet. Report the same strings startup validation reports.
        return [
            Check(
                name="Tech Lead Finding Routes",
                status="error",
                detail=(
                    "tech_lead.findings is configured but not startable: "
                    + "; ".join(readiness.problems)
                ),
            )
        ]
    try:
        from ....control.tech_lead_finding_promotion import promotion_filing_contracts

        contracts = promotion_filing_contracts(config)
    except Exception as exc:
        # A route that cannot even be RESOLVED (e.g. no follow-up worker agent
        # for a route that inherits one) is a startup error, reported here
        # rather than raised on the tick a pattern crosses its threshold.
        return [
            Check(
                name="Tech Lead Finding Routes",
                status="error",
                detail=f"Promotion route(s) could not be resolved: {exc}",
            )
        ]
    if not contracts:
        return [
            Check(
                name="Tech Lead Finding Routes",
                status="ok",
                detail="All promotion routes resolve to this repository",
            )
        ]
    if target_host is None:
        try:
            from ....execution.providers import (
                create_promotion_target_host,
                create_repository_host,
            )

            # An ACTIVE lane always has a configured repo (the readiness owner
            # makes that part of activation), so this narrowing cannot fail.
            assert config.repo is not None
            target_host = create_promotion_target_host(
                create_repository_host(repo=config.repo, config=config)
            )
        except Exception as exc:
            return [
                Check(
                    name="Tech Lead Finding Routes",
                    status="error",
                    detail=f"Could not verify promotion route targets: {exc}",
                )
            ]
    if target_host is None:
        return [
            Check(
                name="Tech Lead Finding Routes",
                status="error",
                detail=(
                    "Promotion routes cannot be verified for this repository host;"
                    " issue-write access must be proven before startup"
                ),
            )
        ]
    problems = [
        reason
        for contract in contracts
        if (reason := target_host.check_filing_ready(contract)) is not None
    ]
    if problems:
        return [
            Check(
                name="Tech Lead Finding Routes",
                status="error",
                detail=(
                    "tech_lead.findings.route target(s) cannot be filed into: "
                    + "; ".join(problems)
                ),
            )
        ]
    return [
        Check(
            name="Tech Lead Finding Routes",
            status="ok",
            detail=(
                "Promotion route target(s) ready to file: "
                + ", ".join(contract.repo for contract in contracts)
            ),
        )
    ]
