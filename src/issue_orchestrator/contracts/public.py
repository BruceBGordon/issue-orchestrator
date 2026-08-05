"""Public contracts for UI-facing payloads.

These contracts are intentionally minimal and stable:
- Require only fields used by the UI.
- Allow extra fields to avoid brittleness.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ContractBase(BaseModel):
    """Base contract with permissive extra fields."""

    model_config = ConfigDict(extra="allow")


class ProviderCircuitEntryContract(ContractBase):
    """One provider's circuit row in the health panel (issue #5980)."""

    provider: str
    is_open: bool
    status_label: str
    cooldown_remaining_label: Optional[str] = None
    next_retry_at: Optional[str] = None
    consecutive_outages: int
    last_error_summary: Optional[str] = None


class ProviderCircuitStatusContract(ContractBase):
    """Provider circuit-breaker status powering the outage banner + panel.

    ``any_open`` is the single flag the banner gates on. ``summary_text`` is a
    colour-independent one-liner (provider names + next retry) so the outage is
    legible without relying on the banner's colour alone.
    """

    any_open: bool
    open_count: int
    open_providers: list[str] = Field(default_factory=list)
    summary_text: str
    next_retry_at: Optional[str] = None
    entries: list[ProviderCircuitEntryContract] = Field(default_factory=list)
    # True when the circuit state could not be read/projected. The banner shows
    # a health warning instead of hiding, so a broken read never masquerades as
    # "no outage" (issue #5980). Defaults to ``False`` (readable / healthy).
    status_unavailable: bool = False


class DashboardDataContract(ContractBase):
    startupComplete: bool
    paused: bool
    e2eRunning: bool
    queueRefreshSeconds: int
    repo: Optional[str]
    repoRoot: Optional[str]
    githubOwner: Optional[str]
    githubRepo: Optional[str]
    e2eLastRun: Optional[dict[str, Any]] = None
    agents: list[str]
    # False when no validation command is configured, so the UI can warn that
    # agent output is pushed without any automated safety net (issue #4109).
    # Required (no default): the producer always emits it, and a missing flag
    # must fail the contract loudly rather than silently defaulting to ``True``
    # and suppressing the warning.
    validationConfigured: bool
    # Provider circuit-breaker status (issue #5980). Required (no default): a
    # dropped producer value must fail the contract loudly rather than silently
    # reading as "no outage" and hiding a real provider outage from operators.
    providerCircuit: ProviderCircuitStatusContract


class DashboardViewModelContract(ContractBase):
    dashboard_data: DashboardDataContract
    paused: bool
    startup_status: str
    active_tab: str
    shutdown_requested: bool


class SessionStartedPayload(ContractBase):
    issue_number: int


class SessionCompletedPayload(ContractBase):
    issue_number: int


class OrchestratorPausedPayload(ContractBase):
    pass


class OrchestratorResumedPayload(ContractBase):
    pass


class QueueChangedPayload(ContractBase):
    added: list[dict[str, Any]]
    removed: list[dict[str, Any]]
    total: int


class DependencyBlockedPayload(ContractBase):
    issue_number: int
    summary: str


class DependencyUnblockedPayload(ContractBase):
    issue_number: int


class StaleDetectedPayload(ContractBase):
    issue_number: int


class StaleClearedPayload(ContractBase):
    issue_number: int


class PersistentStalePayload(ContractBase):
    issue_number: int
    consecutive_ticks: int
    threshold: int


class HistoryReconciledPayload(ContractBase):
    issue_number: int
    issue_key: str
    pr_number: int
    pr_url: str
    previous_status: Literal["completed"]
    status: Literal["merged", "closed"]
    status_reason: str
    source: Literal["pull_request", "issue"]


class StartupCompletePayload(ContractBase):
    elapsed_seconds: float


class ShutdownRequestedPayload(ContractBase):
    force: bool
    # ``reason`` is the calling-site's "why" string. Required on the
    # API contract (see ``/api/shutdown`` in ``web_operator_routes``)
    # so each shutdown is traceable in the orchestrator log.
    reason: str | None = None
    # ``actor`` identifies the source (cc, dashboard, cli, mcp, …)
    # for log-aggregation grouping. Optional on the wire because
    # legacy clients haven't been updated yet.
    actor: str | None = None
    active_sessions: int | None = None


class TimelineArtifactContract(ContractBase):
    type: str
    label: str
    value: str
    render_mode: Optional[str] = None


class TimelineEventContract(ContractBase):
    event_id: str
    timestamp: str
    event: str
    issue_number: int
    phase: str
    step: str
    status: str
    level: str
    summary: Optional[str] = None
    parent_key: str
    artifacts: list[TimelineArtifactContract] = Field(default_factory=list)
    round_index: Optional[int] = None
    attempt_index: Optional[int] = None
    role: Optional[str] = None
    # Per-role verdict carried by ``review_exchange.role_feedback`` events
    # (the raw ``response_type`` the agent reported: ok / changes_requested /
    # disagree / …).  Distinct from the round-level ``reviewer_response_type`` /
    # ``coder_response_type`` recorded on ``round_completed``; the in-round
    # Story progress projection reads this field, so it is part of the durable
    # timeline contract (issue #6428).
    response_type: Optional[str] = None
    reviewer_response_type: Optional[str] = None
    reviewer_response_text: Optional[str] = None
    review_decision_verdict: Optional[str] = None
    review_nit_policy: Optional[str] = None
    review_abstraction_status: Optional[str] = None
    coder_response_type: Optional[str] = None
    coder_response_text: Optional[str] = None


class TimelineIssueContract(ContractBase):
    issue_number: int
    events: list[TimelineEventContract]


class StackGateStatusView(ContractBase):
    """One lifecycle gate (work/review/publish/merge) in the gate report.

    ``reason_codes`` are stable, machine-readable ``GateBlockReason`` values so
    the UI can branch on *why* a gate is closed without parsing human text;
    ``reasons`` carries the human phrasing rendered in the drawer.
    """

    gate: str
    open: bool
    reason_codes: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class StackPredecessorEdgeView(ContractBase):
    """A predecessor dependency edge this issue is gated on."""

    ref: str
    mode: str
    state: str
    problem: Optional[str] = None


class StackSuccessorEdgeView(ContractBase):
    """An issue that depends on / stacks after this issue (chain context)."""

    issue_number: int
    ref: str
    mode: str


class StackChipView(ContractBase):
    """Precomputed compact stack-chip display fields.

    The server template (first paint) and the client rebuild render the chip from
    these identical fields, so the tone / label / status / title logic lives in
    one place (the projection) rather than being duplicated between Jinja and JS.
    ``tone`` is a presentation bucket (``ok`` / ``blocked`` / ``stale``);
    ``status_text`` is the visible accessible-name text; ``title`` is the fuller
    hover sentence including chain context.
    """

    tone: str
    mode_label: str
    status_text: str
    title: str


class StackDependencyGateView(ContractBase):
    """Producer-provided projection of the dependency gate report for one issue.

    The dashboard and issue detail render stack state from this contract without
    recomputing dependency policy in the UI. ``mode`` distinguishes normal
    dependency edges from stack predecessor edges; ``gates`` carries the
    work/review/publish/merge decisions; ``stale`` marks a successor invalidated
    by a predecessor branch change or a stale own-approval.
    """

    issue_number: int
    mode: str
    has_stack_edges: bool
    gates: list[StackGateStatusView] = Field(default_factory=list)
    predecessors: list[StackPredecessorEdgeView] = Field(default_factory=list)
    successors: list[StackSuccessorEdgeView] = Field(default_factory=list)
    blocked_gates: list[str] = Field(default_factory=list)
    blocked_reason_codes: list[str] = Field(default_factory=list)
    stale: bool = False
    stale_reason_codes: list[str] = Field(default_factory=list)
    stack_base_branch: Optional[str] = None
    # Reviewed-commit freshness of the slice's own agent-review approval:
    # "fresh", "stale", or "unknown". "unknown" is surfaced explicitly (rather
    # than implying "fresh") when no approval-freshness source answered — so the
    # merge gate is never rendered verified-fresh on a guess (ADR-0029).
    approval_freshness: str = "unknown"


PUBLIC_CONTRACTS: dict[str, type[BaseModel]] = {
    "dashboard.view_model": DashboardViewModelContract,
    "sse.session.started": SessionStartedPayload,
    "sse.session.completed": SessionCompletedPayload,
    "sse.orchestrator.paused": OrchestratorPausedPayload,
    "sse.orchestrator.resumed": OrchestratorResumedPayload,
    "sse.queue.changed": QueueChangedPayload,
    "sse.dependency.blocked": DependencyBlockedPayload,
    "sse.dependency.unblocked": DependencyUnblockedPayload,
    "sse.stale.in_progress_detected": StaleDetectedPayload,
    "sse.stale.in_progress_cleared": StaleClearedPayload,
    "sse.stale.persistent_detected": PersistentStalePayload,
    "sse.history.reconciled": HistoryReconciledPayload,
    "sse.startup_complete": StartupCompletePayload,
    "sse.shutdown_requested": ShutdownRequestedPayload,
    "timeline.issue": TimelineIssueContract,
    "stack.dependency_gate_view": StackDependencyGateView,
}


def generate_public_schemas() -> dict[str, dict[str, Any]]:
    """Generate JSON schemas for public contracts."""
    return {name: model.model_json_schema() for name, model in PUBLIC_CONTRACTS.items()}
