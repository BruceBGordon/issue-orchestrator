"""Runtime implementation of the agent-callback endpoint port.

Instance state, not module state: each orchestrator instance owns its
own endpoint, so two instances in one process (tests, multi-instance
deployments) cannot leak a port into each other, and no test needs a
production reset API to undo a global.

See ``ports.agent_callback_endpoint`` for why this is runtime state at
all.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# ``0`` is the auto-assign request in configuration, never a destination.
AUTO_ASSIGN_PORT = 0


class NullAgentCallbackEndpoint:
    """Test-only default that refuses to answer.

    Production always injects a real endpoint via the composition root.
    This exists so the twelve-odd test fixtures that never spawn an
    agent do not have to wire one — but it *raises* rather than
    returning ``None``, so a test that genuinely reaches the callback
    path fails immediately instead of silently reproducing the original
    bug (an agent told to dial nowhere).

    Mirrors ``NullReviewExchangeRunner``, which guards the same seam for
    the same reason.
    """

    def publish_bound_port(self, port: int) -> None:
        raise NotImplementedError(
            "No agent callback endpoint was injected. Production wires one "
            "in bootstrap; a test reaching this must inject "
            "RuntimeAgentCallbackEndpoint."
        )

    def declare_unavailable(self) -> None:
        raise NotImplementedError(
            "No agent callback endpoint was injected. Production wires one "
            "in bootstrap."
        )

    def is_ready(self) -> bool:
        raise NotImplementedError(
            "No agent callback endpoint was injected, so readiness is "
            "unknowable. Production wires one in bootstrap."
        )

    def resolve_port(self, configured_port: int) -> int | None:
        raise NotImplementedError(
            "No agent callback endpoint was injected, so there is no port to "
            "hand an agent. Production wires one in bootstrap; a test "
            "reaching this must inject RuntimeAgentCallbackEndpoint."
        )


class RuntimeAgentCallbackEndpoint:
    """Holds the bound Control API port for the process serving it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bound_port: int | None = None
        # Distinct from "no port": tells us the question has been
        # answered, so readiness is not just "port is None".
        self._unavailable = False

    def publish_bound_port(self, port: int) -> None:
        """Publish the port uvicorn actually bound.

        Called from the server-started hook, including — especially —
        when the configured port was the auto-assign sentinel, which is
        the case this owner exists for.
        """
        if port <= AUTO_ASSIGN_PORT:
            raise ValueError(
                f"bound callback port must be a real port, got {port!r}"
            )
        with self._lock:
            self._bound_port = port
            self._unavailable = False
        logger.info("Agent callback endpoint bound on port %d", port)

    def declare_unavailable(self) -> None:
        """Record that this deployment serves no Control API."""
        with self._lock:
            self._unavailable = True
        logger.info(
            "No Control API in this deployment; agents will have no callback "
            "endpoint"
        )

    def is_ready(self) -> bool:
        """True once the endpoint is bound or explicitly unavailable."""
        with self._lock:
            return self._bound_port is not None or self._unavailable

    def bound_port(self) -> int | None:
        """The bound port, or ``None`` before the server has started."""
        with self._lock:
            return self._bound_port

    def resolve_port(self, configured_port: int) -> int | None:
        """The port to hand agents, or ``None`` when there is none.

        See the port docstring for the precedence rule.
        """
        bound = self.bound_port()
        if bound is not None:
            return bound
        if configured_port != AUTO_ASSIGN_PORT:
            return configured_port
        return None
