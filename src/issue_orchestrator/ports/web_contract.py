"""Contract defining what the web layer requires from the orchestrator.

This Protocol ensures test mocks and the real Orchestrator
stay in sync. The type checker catches drift, and runtime_checkable
allows isinstance() verification in tests.
"""

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from typing import Any

if TYPE_CHECKING:
    from issue_orchestrator.infra.config import Config
    from issue_orchestrator.domain.models import OrchestratorState
    from issue_orchestrator.events import EventHub
    from issue_orchestrator.ports.provider_resilience import (
        ProviderCircuitStatusReader,
    )
    from issue_orchestrator.ports.tech_lead_run_record_store import (
        TechLeadRunHistoryReader,
    )


@runtime_checkable
class OrchestratorForWeb(Protocol):
    """What the web layer needs from an orchestrator instance.

    The read-model facades (``provider_circuit``, ``tech_lead_run_history``)
    belong here for the same reason the lifecycle methods do. ``GET /`` reads
    them through required properties so a composition error fails loudly rather
    than rendering an empty-looking panel — which means a double that lacks one
    makes the dashboard raise. Declaring them keeps that drift a contract-test
    failure instead of a browser test discovering it several suites later.
    """

    state: "OrchestratorState"
    config: "Config"
    _shutdown_requested: bool

    def pause(self) -> None:
        """Pause the orchestrator."""
        ...

    def resume(self) -> None:
        """Resume the orchestrator."""
        ...

    def request_shutdown(self, force: bool = False) -> None:
        """Request graceful or forced shutdown."""
        ...

    def request_refresh(self, inflight_stable_ids: set[str] | None = None) -> None:
        """Request immediate issue refresh."""
        ...

    @property
    def event_hub(self) -> "EventHub":
        """Access to event hub for SSE subscriptions."""
        ...

    @property
    def provider_circuit(self) -> "ProviderCircuitStatusReader":
        """Read-only provider circuit status for the dashboard (#5980)."""
        ...

    @property
    def tech_lead_run_history(self) -> "TechLeadRunHistoryReader":
        """Read-only local tech-lead run history (ADR-0033 / #6858)."""
        ...

    def get_failure_diagnosis(self, issue_number: int) -> dict[str, Any]:
        """Get failure diagnosis for a session.

        Returns diagnostic info for debugging failed sessions as a dict
        ready for JSON serialization.
        """
        ...
