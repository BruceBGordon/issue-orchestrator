"""The Control API port rule shared by every agent callback (#6913).

``control_api_port: 0`` means "bind any free port", so a literal ``"0"``
in the environment is a request, never a reachable destination. It is
also a *truthy* string, so the naive ``a or b`` chains these call sites
used returned it and shadowed the live port the review exchange injects
as ``ORCHESTRATOR_API_PORT``. Agent callbacks dialled ``localhost:0``,
verdicts were undeliverable, and the round runner misread the silence
as an unresponsive agent — SIGKILLing healthy coders and stranding
completed, validated work.

All three callback paths must answer identically, so they share one
owner. These tests pin the owner *and* each caller, because a caller
quietly reverting to its own ``or`` chain is the regression.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.entrypoints.cli_tools.orchestrator_resume import (
    ResumeTarget,
    resolve_control_api_port,
)


class TestResolveControlApiPort:
    def test_sentinel_zero_falls_through_to_live_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "0")
        monkeypatch.setenv("ORCHESTRATOR_API_PORT", "59957")
        assert resolve_control_api_port() == "59957"

    def test_sentinel_zero_alone_is_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "0")
        monkeypatch.delenv("ORCHESTRATOR_API_PORT", raising=False)
        assert resolve_control_api_port() is None

    def test_both_sentinels_is_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "0")
        monkeypatch.setenv("ORCHESTRATOR_API_PORT", "0")
        assert resolve_control_api_port() is None

    def test_real_prefixed_port_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "8080")
        monkeypatch.setenv("ORCHESTRATOR_API_PORT", "59957")
        assert resolve_control_api_port() == "8080"

    def test_whitespace_only_is_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "   ")
        monkeypatch.delenv("ORCHESTRATOR_API_PORT", raising=False)
        assert resolve_control_api_port() is None


class TestResumeTargetUsesSharedRule:
    """``/api/issues/{n}/resume`` is an allowlisted agent-callback route.

    It read the port with its own ``or`` chain and so accepted the 0
    sentinel as a real port, building ``http://localhost:0/...``.
    """

    def test_sentinel_zero_falls_through_to_live_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "0")
        monkeypatch.setenv("ORCHESTRATOR_API_PORT", "59957")
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_ISSUE_NUMBER", "6410")
        target = ResumeTarget.from_agent_environment()
        assert target.port == "59957"
        assert target.url() == "http://localhost:59957/api/issues/6410/resume"

    def test_sentinel_zero_alone_raises_rather_than_dialling_port_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "0")
        monkeypatch.delenv("ORCHESTRATOR_API_PORT", raising=False)
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_ISSUE_NUMBER", "6410")
        with pytest.raises(ValueError, match="ISSUE_ORCHESTRATOR_API_PORT"):
            ResumeTarget.from_agent_environment()


def test_exchange_respond_delegates_to_shared_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``exchange-respond`` must not reintroduce its own port chain."""
    from issue_orchestrator.entrypoints.cli_tools.exchange_respond import _api_port

    monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "0")
    monkeypatch.setenv("ORCHESTRATOR_API_PORT", "59957")
    assert _api_port() == "59957"
