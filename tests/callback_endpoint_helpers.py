"""Shared helper: a callback endpoint that is *ready* for tests.

The session launcher refuses to spawn an agent until the endpoint
question has been answered — bound to a port, or explicitly declared
unavailable — because launching in that window hands the agent an
environment with no way to call back (#6924 F7).

Tests almost never bind a Control API, so the honest answer for them is
"unavailable": ready, with no port. That matches what a real
``issue-orchestrator start`` without ``--api-port`` reports.
"""

from __future__ import annotations

from issue_orchestrator.infra.agent_callback_endpoint import (
    RuntimeAgentCallbackEndpoint,
)


def ready_callback_endpoint() -> RuntimeAgentCallbackEndpoint:
    """A real endpoint that has answered the question: no Control API."""
    endpoint = RuntimeAgentCallbackEndpoint()
    endpoint.declare_unavailable()
    return endpoint
