"""Claim and logical-run ownership composition.

Issue claims and tech-lead logical-run claims are one deployment decision:
when cross-instance claims are enabled both use GitHub refs, and when disabled
both use their single-instance owners.  Keeping that choice in one bootstrap
phase prevents the two coordination key spaces from drifting apart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from ..control.claim_gate import ClaimGate
from ..control.lease_renewer import LeaseRenewer
from ..control.tech_lead_run_ownership import TechLeadRunOwnership
from ..domain.lease_config import LeaseConfig
from ..ports.claim_manager import ClaimManager
from ..ports.run_ledger_store import TechLeadRunLedgerStore

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.event_sink import EventSink


class ClaimComponents(NamedTuple):
    """The issue-claim and logical-run owners created from one policy choice."""

    claim_gate: ClaimGate
    lease_renewer: LeaseRenewer
    lease_config: LeaseConfig
    claim_manager: ClaimManager
    run_ownership: TechLeadRunOwnership


def lease_config_from(config: "Config") -> LeaseConfig:
    """Translate claim settings once for both coordination key spaces."""
    return LeaseConfig(
        lease_seconds=config.claims.lease_seconds,
        renew_interval_seconds=config.claims.renew_before_expiry_seconds,
        convergence_timeout_seconds=config.claims.convergence_timeout_seconds,
        convergence_poll_min_ms=config.claims.convergence_poll_min_ms,
        convergence_poll_max_ms=config.claims.convergence_poll_max_ms,
    )


def assemble_claim_components(
    claim_manager: ClaimManager,
    run_ledger_store: TechLeadRunLedgerStore,
    lease_config: LeaseConfig,
    events: "EventSink",
) -> ClaimComponents:
    """Assemble issue and logical-run coordination from selected ports."""
    claim_gate = ClaimGate(claim_manager=claim_manager, events=events)
    lease_renewer = LeaseRenewer(
        claim_manager=claim_manager,
        events=events,
        config=lease_config,
    )
    run_ownership = TechLeadRunOwnership(
        run_ledger_store,
        lease_seconds=lease_config.lease_seconds,
        renew_before_expiry_seconds=lease_config.renew_interval_seconds,
    )
    return ClaimComponents(
        claim_gate=claim_gate,
        lease_renewer=lease_renewer,
        lease_config=lease_config,
        claim_manager=claim_manager,
        run_ownership=run_ownership,
    )


__all__ = ["ClaimComponents", "assemble_claim_components", "lease_config_from"]
