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
        """Record that this deployment serves no Control API.

        Clears any previously-bound port: if a server has gone away, a
        stale port is worse than none, because agents would dial it and
        the failure would look like an unresponsive orchestrator rather
        than a missing endpoint.
        """
        with self._lock:
            self._unavailable = True
            self._bound_port = None
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

        Precedence, resolved under a single lock so a concurrent
        publish/declare cannot be observed half-applied:

        1. **Unavailable wins.** A mode that says it serves no Control
           API must not have that overridden by a configured fallback or
           a stale bound port — otherwise it hands agents a dead
           endpoint, which is the failure this owner exists to prevent.
        2. The bound port, which is what is actually listening.
        3. A non-zero configured port, for callers running before bind.
        4. Otherwise ``None``: auto-assign with nothing bound is not an
           address.
        """
        with self._lock:
            if self._unavailable:
                return None
            if self._bound_port is not None:
                return self._bound_port
        if configured_port != AUTO_ASSIGN_PORT:
            return configured_port
        return None
