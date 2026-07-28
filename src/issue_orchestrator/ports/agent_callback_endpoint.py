"""Port: where a spawned agent can reach this orchestrator.

``control_api_port: 0`` means "bind any free port", so the configured
value is a request, not an address. Only the running server knows the
real port, and it learns it after binding — so the endpoint is runtime
state with a lifecycle, not configuration.

Two collaborators sit either side of this port:

- the **server**, which publishes the port it bound;
- the **agent-environment builders** (session launch and the
  review-exchange pair), which resolve what to hand an agent.

They must agree, or agents are told to dial somewhere nothing is
listening — which is how every review-exchange verdict became
undeliverable (#6913, #6924).

Modelled as a port so the control layer depends on an interface rather
than mutable process state, and so the single shared instance is wired
through the composition root like every other collaborator.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AgentCallbackEndpoint(Protocol):
    """The reachable Control API endpoint for agent callbacks."""

    def publish_bound_port(self, port: int) -> None:
        """Record the port the server actually bound.

        Called once the server is listening. Implementations must reject
        a non-port (``0`` or negative): the auto-assign sentinel is what
        this port exists to eliminate, so accepting it here would let the
        original defect back in through the publisher.
        """
        ...

    def resolve_port(self, configured_port: int) -> int | None:
        """The port to hand an agent, or ``None`` if there is none yet.

        The bound port wins — it is what is actually listening. A
        non-zero configured port is the fallback for callers running
        before the server binds. Auto-assign with nothing bound resolves
        to ``None`` rather than ``0``, so callers fail honestly instead
        of dialling an unreachable address.
        """
        ...
