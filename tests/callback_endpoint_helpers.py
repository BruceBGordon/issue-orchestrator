"""Shared helpers: callback endpoints in each *resolved* state.

The session launcher refuses to spawn an agent until the endpoint
question has been answered, because launching in that window hands the
agent an environment with no way to call back (#6924 F7).

There are two honest answers, and tests must pick the one matching what
they actually run:

- :func:`ready_callback_endpoint` — "this deployment serves no Control
  API". Ready, resolves to no port. Correct for the vast majority of
  tests, and the same state a real ``issue-orchestrator start`` without
  ``--api-port`` reports.
- :func:`published_callback_endpoint` — "a server is listening on this
  port". Correct for tests that actually stand one up, such as the
  mailbox-backed review exchange.

Using the first where the second is meant silently strips the port from
the agent environment, and the exchange fails with
``reviewer_no_completion`` — the original bug, reproduced in a test.
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


def published_callback_endpoint(port: int) -> RuntimeAgentCallbackEndpoint:
    """A real endpoint reporting a server listening on ``port``."""
    endpoint = RuntimeAgentCallbackEndpoint()
    endpoint.publish_bound_port(port)
    return endpoint
