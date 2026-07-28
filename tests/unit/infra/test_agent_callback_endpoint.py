"""The bound agent-callback endpoint owner (#6924).

``control_api_port: 0`` is a request to the OS, not an address. Only the
running server knows the real port, and the supervised engine never
writes it back into ``Config`` — so consumers reading the configured
value handed agents an undialable ``0``.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.infra.agent_callback_endpoint import (
    bound_callback_port,
    record_bound_callback_port,
    reset_bound_callback_port,
    resolve_agent_callback_port,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_bound_callback_port()
    yield
    reset_bound_callback_port()


class TestRecording:
    def test_records_the_bound_port(self) -> None:
        record_bound_callback_port(59957)
        assert bound_callback_port() == 59957

    def test_last_bind_wins(self) -> None:
        record_bound_callback_port(59957)
        record_bound_callback_port(60001)
        assert bound_callback_port() == 60001

    @pytest.mark.parametrize("bad", [0, -1])
    def test_rejects_a_non_port(self, bad: int) -> None:
        """The sentinel is what this owner exists to eliminate.

        Accepting it would let the original bug back in through the
        publisher instead of the consumer.
        """
        with pytest.raises(ValueError, match="real port"):
            record_bound_callback_port(bad)

    def test_nothing_bound_before_the_server_starts(self) -> None:
        assert bound_callback_port() is None


class TestResolution:
    def test_bound_port_wins_over_configured(self) -> None:
        record_bound_callback_port(59957)
        assert resolve_agent_callback_port(8080) == 59957

    def test_auto_assign_before_bind_is_none(self) -> None:
        """None, not 0 — callers must fail honestly rather than dial it."""
        assert resolve_agent_callback_port(0) is None

    def test_explicit_configured_port_used_before_bind(self) -> None:
        assert resolve_agent_callback_port(8080) == 8080

    def test_auto_assign_after_bind_is_the_bound_port(self) -> None:
        """The production shape this whole owner exists for."""
        record_bound_callback_port(59957)
        assert resolve_agent_callback_port(0) == 59957


class TestServerStartedHook:
    """``build_bound_port_recorder`` — what the engine wires to uvicorn.

    Closes the loop: the owner being correct is useless if nothing ever
    populates it, which is how the first attempt at this fix left the
    callback silently undeliverable.
    """

    @staticmethod
    def _recorder(monkeypatch: pytest.MonkeyPatch, requested_port: int, tmp_path):
        from issue_orchestrator.entrypoints import run_orchestrator

        lock_writes: list[int] = []
        monkeypatch.setattr(
            run_orchestrator,
            "set_lock_http_port",
            lambda _root, port, instance_id=None: lock_writes.append(port),
        )
        recorder = run_orchestrator.build_bound_port_recorder(
            repo_root=tmp_path, requested_port=requested_port, instance_id="test"
        )
        return recorder, lock_writes

    def test_auto_assign_publishes_the_bound_port(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        recorder, lock_writes = self._recorder(monkeypatch, 0, tmp_path)
        recorder(59957)
        assert bound_callback_port() == 59957
        assert lock_writes == [59957]

    def test_publishes_even_when_the_requested_port_was_bound_exactly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """The lock write is skipped here; the callback publish must not be.

        The lock already records the requested port, so it needs no
        update — but the agent still needs an endpoint. Inheriting that
        guard would leave explicit-port deployments with no callback.
        """
        recorder, lock_writes = self._recorder(monkeypatch, 8080, tmp_path)
        recorder(8080)
        assert bound_callback_port() == 8080
        assert lock_writes == [], "lock should not be rewritten with the same port"

    def test_agents_can_resolve_the_port_after_the_hook_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """The consumer-visible effect the whole chain depends on."""
        recorder, _ = self._recorder(monkeypatch, 0, tmp_path)
        assert resolve_agent_callback_port(0) is None
        recorder(59957)
        assert resolve_agent_callback_port(0) == 59957
