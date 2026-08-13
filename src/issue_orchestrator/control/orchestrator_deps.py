"""OrchestratorDeps - All dependencies required by the Orchestrator.

This module defines a frozen dataclass containing all required collaborators
for the Orchestrator. No Optional fields, no Null defaults - the Orchestrator
must be constructed in a fully-wired, valid state.

Principle: "No Nulls in Orchestrator"
- Bootstrap is the single source of truth for choosing implementations
- Tests explicitly pass fakes/nulls (never via defaults)
- Makes wiring readable and type-safe
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..events import EventHub
    from ..ports.agent_callback_endpoint import AgentCallbackEndpoint
    from ..ports.completion_handler_factory import CompletionHandlerFactory
    from ..ports.operator_issue_commands import OperatorIssueCommandFactory
    from ..ports.session_launcher_factory import SessionLauncherFactory
    from ..ports.label_store import LabelStore
    from ..ports.pending_work_claim_store import PendingWorkClaimStore
    from .claim_quarantine import ClaimQuarantineOwner
    from .needs_human_block import SharedNeedsHumanBlock
    from ..ports.queue_cache_store import QueueCacheStore
    from ..ports import (
        EventSink,
        SessionRunner,
        RepositoryHost,
        CommandRunner,
        SessionOutput,
        ManifestDownloader,
    )
    from ..ports.timeline_reader import TimelineReader
    from ..ports.timeline_store import TimelineStore
    from ..ports.timeline_writer import TimelineWriter
    from ..ports.timeline_evidence import TimelineEvidence
    from ..ports.e2e_issue_tracker import E2EIssueTracker
    from ..ports.goal_pilot_store import GoalPilotStore
    from ..ports.attempt_store import AttemptStore
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .open_issue_corpus import OpenIssueCorpusManager
    from ..ports.fresh_issue_reader import FreshIssueReader
    from ..ports.worktree_manager import WorktreeManager
    from ..ports.working_copy import WorkingCopy
    from ..ports.claim_manager import ClaimManager
    from .tech_lead_run_ownership import TechLeadRunOwnership
    from .infra_services import InfraServices
    from .label_manager import LabelManager
    from .planner import Planner
    from .session_manager import SessionManager
    from .label_sync import LabelSync
    from .action_applier import ActionApplier
    from .fact_gatherer import FactGatherer
    from .pr_scanner import PRScanner
    from .session_restorer import SessionRestorer
    from .state_machine_manager import StateMachineManager
    from .completion_processor import CompletionProcessor
    from .completion_dispatcher import CompletionDispatcher
    from .publish_recovery import PublishRecoveryService
    from .session_controller import SessionController
    from .health_gate import HealthGate
    from .claim_gate import ClaimGate
    from .lease_renewer import LeaseRenewer
    from .provider_resilience import ProviderResilienceManager
    from .board_snapshot_builder import BoardSnapshotBuilder
    from ..ports.provider_readiness import ProviderReadinessProbe


@dataclass(frozen=True)
class OrchestratorDeps:
    """All dependencies required by the Orchestrator.

    This is a frozen (immutable) container for all collaborators.
    No Optional fields - all must be provided at construction time.

    The Orchestrator receives this bundle instead of many individual parameters,
    making the wiring explicit and type-checked.

    Cross-cutting infrastructure services (label management, persistence,
    provider resilience, timeline) are bundled in ``services: InfraServices``.
    Backward-compat properties delegate to the bundle so existing callers
    (e.g. ``deps.provider_resilience``) continue to work unchanged.
    """

    # Core event/runtime ports
    events: "EventSink"
    runner: "SessionRunner"

    # Repository adapter
    repository_host: "RepositoryHost"
    e2e_issue_tracker: "E2EIssueTracker"
    fresh_issue_reader: "FreshIssueReader"

    # Event distribution
    event_hub: "EventHub"

    # Control plane components
    planner: "Planner"
    session_manager: "SessionManager"
    label_sync: "LabelSync"
    action_applier: "ActionApplier"
    fact_gatherer: "FactGatherer"
    pr_scanner: "PRScanner"
    session_restorer: "SessionRestorer"
    state_machine_manager: "StateMachineManager"
    completion_processor: "CompletionProcessor"
    session_controller: "SessionController"
    # Runs a terminated session's completion decision (publish gate + push + PR);
    # the background impl keeps that work off the tick thread.
    completion_dispatcher: "CompletionDispatcher"
    health_gate: "HealthGate"
    # Board-snapshot fact assembly (ADR-0031 §3); the orchestrator binds it to
    # live state when wiring the session launcher's snapshot provider.
    board_snapshot_builder: "BoardSnapshotBuilder"
    # Where spawned agents can reach this orchestrator. Runtime state
    # with a lifecycle (the port is only known after the server binds),
    # so it is a port injected here rather than read from Config —
    # ``control_api_port: 0`` is a request, not an address (#6924).
    agent_callback_endpoint: "AgentCallbackEndpoint"
    # Builds the session launcher. Assembly lives at the composition
    # root; the facade supplies only its own callbacks (#6924 A3-R2).
    session_launcher_factory: "SessionLauncherFactory"
    # Same split for the completion handler: the facade supplies only its own
    # runtime state, never the dependency-container layout (#6999 A4).
    completion_handler_factory: "CompletionHandlerFactory"
    # Operator "retry"/"dismiss": one settled transition across GitHub labels
    # and the local retry/queue state, in that order (#6999 F5). A factory for
    # the same reason as the two above - the live state and its lock belong to
    # the facade, everything else to this container.
    operator_issue_command_factory: "OperatorIssueCommandFactory"

    # IO adapters
    worktree_manager: "WorktreeManager"
    working_copy: "WorkingCopy"
    command_runner: "CommandRunner"
    session_output: "SessionOutput"
    manifest_downloader: "ManifestDownloader"
    # Orchestrator-owned, OUTSIDE every agent-writable worktree (#6999 F7): it
    # records which queued request each running session is carrying, and
    # restoration accepts it as authority.
    pending_work_claims: "PendingWorkClaimStore"
    # Owns what an unreadable claim means: its own durable per-run marker, its
    # own labels/comment, and the event only after those commit (#6999 F12/A5).
    claim_quarantine: "ClaimQuarantineOwner"
    # The one owner of the shared needs-human block: every acquisition, cause
    # release and operator force-clear of that label routes through it, so a
    # block can never exist without a discoverable cause (#6999 F2 round 3).
    needs_human_block: "SharedNeedsHumanBlock"

    # Claim/lease management (multi-orchestrator coordination)
    claim_manager: "ClaimManager"
    claim_gate: "ClaimGate"
    lease_renewer: "LeaseRenewer"
    # Cross-instance ownership of LOGICAL tech-lead runs (#6994). Long-lived on
    # purpose: a run is owned from admission until its session ends, which is
    # many ticks, so the holder cannot be rebuilt per request.
    run_ownership: "TechLeadRunOwnership"

    # Manual publish recovery ("Retry publish"): off-thread republish + reconcile
    publish_recovery: "PublishRecoveryService"

    # Cross-cutting infrastructure services (label mgmt, persistence, etc.)
    services: "InfraServices"

    # ------------------------------------------------------------------
    # Backward-compat properties — delegate to services bundle
    # ------------------------------------------------------------------

    @property
    def label_manager(self) -> "LabelManager":
        return self.services.label_manager

    @property
    def label_store(self) -> "LabelStore":
        return self.services.label_store

    @property
    def goal_pilot_store(self) -> "GoalPilotStore":
        return self.services.goal_pilot_store

    @property
    def attempt_store(self) -> "AttemptStore":
        return self.services.attempt_store

    @property
    def tech_lead_authority(self) -> "TechLeadAuthorityStore":
        return self.services.tech_lead_authority

    @property
    def open_issue_corpus(self) -> "OpenIssueCorpusManager":
        return self.services.open_issue_corpus

    @property
    def provider_resilience(self) -> "ProviderResilienceManager":
        return self.services.provider_resilience

    @property
    def provider_readiness_probe(self) -> "ProviderReadinessProbe":
        return self.services.provider_readiness_probe

    @property
    def queue_cache_store(self) -> "QueueCacheStore":
        return self.services.queue_cache_store

    @property
    def timeline_reader(self) -> "TimelineReader":
        return self.services.timeline_reader

    @property
    def timeline_store(self) -> "TimelineStore":
        return self.services.timeline_store

    @property
    def timeline_writer(self) -> "TimelineWriter":
        return self.services.timeline_writer

    @property
    def timeline_evidence(self) -> "TimelineEvidence":
        return self.services.timeline_evidence

    @property
    def pair_registry(self):  # noqa: ANN201 — return type is the protocol
        """The persistent exchange pair registry, or ``None`` in test deps.

        Production bootstrap always provides one; legacy test fixtures
        that build deps without going through ``InfraServices``'s
        ``pair_registry`` field will see ``None``. ``Orchestrator.close``
        and other lifecycle owners must guard accordingly.
        """
        return self.services.pair_registry
