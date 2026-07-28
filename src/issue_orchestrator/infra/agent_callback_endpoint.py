"""Where agents can actually reach this orchestrator process.

One runtime owner for the *bound* Control API port, because the
configured port is not a usable answer. ``control_api_port: 0`` means
"bind any free port", and the supervised engine never writes the real
port back into ``Config`` — so every consumer that read
``config.control_api_port`` handed agents a ``0`` they could not dial
(#6913, #6924).

The bound port is only knowable after uvicorn binds, so the server
records it here via ``on_server_started`` and consumers resolve through
:func:`resolve_agent_callback_port`. Both agent-environment builders —
normal session launch and the review-exchange pair — must go through
this owner, or they drift apart again.

Process-global by nature: there is exactly one server per process, and
the CLI tools that consume the resulting environment are separate
processes. :func:`reset_bound_callback_port` exists for tests.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# ``0`` is the auto-assign request in configuration, never a destination.
AUTO_ASSIGN_PORT = 0

_lock = threading.Lock()
_bound_port: int | None = None


def record_bound_callback_port(port: int) -> None:
    """Publish the port uvicorn actually bound.

    Called from the server-started hook, including — especially — when
    the configured port was the ``0`` auto-assign sentinel, which is the
    case this owner exists for.
    """
    if port <= AUTO_ASSIGN_PORT:
        raise ValueError(
            f"bound callback port must be a real port, got {port!r}"
        )
    global _bound_port
    with _lock:
        _bound_port = port
    logger.info("Agent callback endpoint bound on port %d", port)


def bound_callback_port() -> int | None:
    """The bound port, or ``None`` before the server has started."""
    with _lock:
        return _bound_port


def reset_bound_callback_port() -> None:
    """Clear the recorded port. Tests only."""
    global _bound_port
    with _lock:
        _bound_port = None


def resolve_agent_callback_port(configured_port: int) -> int | None:
    """The port to hand agents, or ``None`` when there is nothing to say.

    The bound port wins: it is what is actually listening. A non-zero
    configured port is the fallback for callers that run before the
    server binds. A configured ``0`` with nothing bound yields ``None``
    rather than a sentinel, so downstream fails honestly instead of
    dialling ``localhost:0``.
    """
    bound = bound_callback_port()
    if bound is not None:
        return bound
    if configured_port != AUTO_ASSIGN_PORT:
        return configured_port
    return None
