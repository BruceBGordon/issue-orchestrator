"""The agent-callback endpoint (#6924).

``control_api_port: 0`` is a request to the OS, not an address. Only the
running server knows the real port, and the supervised engine never
writes it back into ``Config`` — so consumers reading the configured
value handed agents an undialable ``0``.

Instance state, not process state: two orchestrators in one process must
not leak a port into each other, and no test should need a production
reset API to undo a global.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.infra.agent_callback_endpoint import (
    RuntimeAgentCallbackEndpoint,
)
from issue_orchestrator.ports.agent_callback_endpoint import AgentCallbackEndpoint


@pytest.fixture
def endpoint() -> RuntimeAgentCallbackEndpoint:
    return RuntimeAgentCallbackEndpoint()


def test_runtime_endpoint_satisfies_the_port() -> None:
    assert isinstance(RuntimeAgentCallbackEndpoint(), AgentCallbackEndpoint)


class TestPublishing:
    def test_records_the_bound_port(
        self, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        endpoint.publish_bound_port(59957)
        assert endpoint.bound_port() == 59957

    def test_last_bind_wins(self, endpoint: RuntimeAgentCallbackEndpoint) -> None:
        endpoint.publish_bound_port(59957)
        endpoint.publish_bound_port(60001)
        assert endpoint.bound_port() == 60001

    @pytest.mark.parametrize("bad", [0, -1])
    def test_rejects_a_non_port(
        self, endpoint: RuntimeAgentCallbackEndpoint, bad: int
    ) -> None:
        """The sentinel is what this owner exists to eliminate.

        Accepting it would let the original bug back in through the
        publisher instead of the consumer.
        """
        with pytest.raises(ValueError, match="real port"):
            endpoint.publish_bound_port(bad)

    def test_nothing_bound_before_the_server_starts(
        self, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        assert endpoint.bound_port() is None


class TestResolution:
    def test_bound_port_wins_over_configured(
        self, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        endpoint.publish_bound_port(59957)
        assert endpoint.resolve_port(8080) == 59957

    def test_auto_assign_before_bind_is_none(
        self, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        """None, not 0 — callers must fail honestly rather than dial it."""
        assert endpoint.resolve_port(0) is None

    def test_explicit_configured_port_used_before_bind(
        self, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        assert endpoint.resolve_port(8080) == 8080

    def test_auto_assign_after_bind_is_the_bound_port(
        self, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        """The production shape this whole owner exists for."""
        endpoint.publish_bound_port(59957)
        assert endpoint.resolve_port(0) == 59957


class TestInstanceIsolation:
    """Why this is not a module global."""

    def test_instances_do_not_share_a_port(self) -> None:
        first = RuntimeAgentCallbackEndpoint()
        second = RuntimeAgentCallbackEndpoint()
        first.publish_bound_port(59957)
        assert second.bound_port() is None
        assert second.resolve_port(0) is None


class TestReadinessLifecycle:
    """Readiness is what gates agent launch (#6924 F7).

    An agent spawned before the endpoint question is answered gets an
    environment with no way to call back. But "no Control API in this
    deployment" is a legitimate answer — ``issue-orchestrator start``
    without ``--api-port`` binds no server — so readiness must
    distinguish "answered: none" from "not answered yet". Conflating
    them would either deadlock those deployments or reopen the race.
    """

    def test_not_ready_before_anything_is_declared(
        self, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        assert endpoint.is_ready() is False

    def test_ready_after_binding(
        self, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        endpoint.publish_bound_port(59957)
        assert endpoint.is_ready() is True

    def test_ready_after_declaring_unavailable(
        self, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        endpoint.declare_unavailable()
        assert endpoint.is_ready() is True

    def test_unavailable_still_resolves_to_no_port(
        self, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        """Ready, but with nothing to hand the agent — and that is correct."""
        endpoint.declare_unavailable()
        assert endpoint.resolve_port(0) is None

    def test_binding_after_unavailable_wins(
        self, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        """A late-binding server must override an earlier declaration."""
        endpoint.declare_unavailable()
        endpoint.publish_bound_port(59957)
        assert endpoint.resolve_port(0) == 59957
        assert endpoint.is_ready() is True
