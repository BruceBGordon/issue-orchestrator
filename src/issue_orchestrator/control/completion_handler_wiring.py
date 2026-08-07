"""Assembly of the completion handler from live orchestrator state.

Its own module because the collaborators are a mix of injected dependencies
and lookups into mutable runtime state: that is composition, not facade work,
and it does not belong in either the orchestrator facade or the already
oversized support class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator
    from .completion_handler import CompletionHandler


def build_completion_handler(orch: "Orchestrator") -> "CompletionHandler":
    """Assemble the completion handler around live orchestrator state.

    Lives here rather than in the facade because every collaborator is either a
    dependency or a lookup into mutable runtime state, and the facade's job is
    to delegate, not to wire.
    """
    from .active_sessions import active_session_run_id
    from .completion_handler import CompletionHandler
    from .provider_availability import ProviderAvailabilityPolicy

    smm = orch.deps.state_machine_manager
    return CompletionHandler(
        orch.config,
        orch.deps.events,
        orch.deps.repository_host,
        lambda issue: smm.issue_machines.get(issue.number),
        lambda name: smm.session_machines.get(name),
        lambda pr_number: smm.review_machines.get(pr_number),
        orch.deps.session_output,
        orch.deps.tech_lead_authority,
        orch.deps.open_issue_corpus,
        lambda n: active_session_run_id(orch.state.active_sessions, n),
        # Completion never applies the provider-blocked label itself; it asks
        # this owner for the transition that carries the durable issue-scoped
        # record with it (#6999 F5/A2).
        ProviderAvailabilityPolicy(
            orch.config, orch.deps.provider_resilience, orch.deps.label_manager
        ),
        remove_session_machine_fn=smm.remove_session_machine,
        label_manager=orch.deps.label_manager,
    )


__all__ = ["build_completion_handler"]
