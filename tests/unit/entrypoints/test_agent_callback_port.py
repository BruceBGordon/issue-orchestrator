"""The Control API port rule shared by every agent callback (#6913, #6924).

``control_api_port: 0`` means "bind any free port", so a literal ``"0"``
in the environment is a request, never a reachable destination. It is
also a *truthy* string, so the naive ``a or b`` chains these call sites
used returned it and shadowed the live port the review exchange injects
as ``ORCHESTRATOR_API_PORT``. Agent callbacks dialled ``localhost:0``,
verdicts were undeliverable, and the round runner misread the silence
as an unresponsive agent — SIGKILLing healthy coders and stranding
completed, validated work.

All three callback commands must answer identically, so they share one
owner. Each caller is pinned *behaviourally* — through its public entry
point, asserting the URL actually dialled — because a caller quietly
reintroducing its own ``or`` chain is the regression, and asserting on
the owner alone would not catch it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from issue_orchestrator.entrypoints.cli_tools.agent_callback import (
    resolve_control_api_port,
)
from issue_orchestrator.entrypoints.cli_tools.orchestrator_resume import ResumeTarget


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _CapturedRequest:
    """Records the URL a command dialled, and answers with a canned body."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def __call__(self, request: Any, timeout: float | None = None) -> _FakeResponse:
        self.urls.append(request.full_url)
        return _FakeResponse(self.payload)

    @property
    def url(self) -> str:
        assert len(self.urls) == 1, f"expected exactly one request, got {self.urls}"
        return self.urls[0]


def _must_not_dial(*args: object, **kwargs: object) -> None:
    raise AssertionError("must not attempt a request without an endpoint")


@pytest.fixture
def prefixed_sentinel_with_live_legacy_port(monkeypatch: pytest.MonkeyPatch) -> str:
    """The exact production shape that broke: prefixed 0, live legacy port.

    Session launch exported ``ISSUE_ORCHESTRATOR_API_PORT`` and the
    review exchange injected the live ``ORCHESTRATOR_API_PORT``. The
    sentinel won, so the live port was never used.
    """
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "0")
    monkeypatch.setenv("ORCHESTRATOR_API_PORT", "59957")
    return "59957"


class TestResolveControlApiPort:
    def test_sentinel_falls_through_to_live_port(
        self, prefixed_sentinel_with_live_legacy_port: str
    ) -> None:
        assert resolve_control_api_port() == "59957"

    def test_sentinel_alone_is_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "0")
        monkeypatch.delenv("ORCHESTRATOR_API_PORT", raising=False)
        assert resolve_control_api_port() is None

    def test_both_sentinels_is_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "0")
        monkeypatch.setenv("ORCHESTRATOR_API_PORT", "0")
        assert resolve_control_api_port() is None

    def test_real_prefixed_port_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "8080")
        monkeypatch.setenv("ORCHESTRATOR_API_PORT", "59957")
        assert resolve_control_api_port() == "8080"

    def test_whitespace_only_is_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "   ")
        monkeypatch.delenv("ORCHESTRATOR_API_PORT", raising=False)
        assert resolve_control_api_port() is None

    def test_unset_everywhere_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ISSUE_ORCHESTRATOR_API_PORT", raising=False)
        monkeypatch.delenv("ORCHESTRATOR_API_PORT", raising=False)
        assert resolve_control_api_port() is None


class TestExchangeRespondDialsTheLivePort:
    """``exchange-respond``, driven through its ``main`` entry point."""

    def test_verdict_is_posted_to_the_live_port(
        self,
        monkeypatch: pytest.MonkeyPatch,
        prefixed_sentinel_with_live_legacy_port: str,
    ) -> None:
        from issue_orchestrator.entrypoints.cli_tools import exchange_respond

        monkeypatch.setenv(
            "ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE", "/tmp/review-response.json"
        )
        captured = _CapturedRequest({"status": "accepted"})
        monkeypatch.setattr(exchange_respond.urllib.request, "urlopen", captured)

        exit_code = exchange_respond.main(["ok", "--text", "Applied the fixes."])

        assert exit_code == 0
        assert captured.url == "http://localhost:59957/api/review-exchange/respond"

    def test_fails_loudly_when_there_is_no_endpoint(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No endpoint must report an error, not dial ``localhost:0``."""
        from issue_orchestrator.entrypoints.cli_tools import exchange_respond

        monkeypatch.setenv(
            "ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE", "/tmp/review-response.json"
        )
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "0")
        monkeypatch.delenv("ORCHESTRATOR_API_PORT", raising=False)
        monkeypatch.setattr(exchange_respond.urllib.request, "urlopen", _must_not_dial)

        assert exchange_respond.main(["ok", "--text", "Applied."]) == 1
        assert "API_PORT" in capsys.readouterr().err


class TestPreflightPushDialsTheLivePort:
    """``coding-done`` / ``reviewer-done`` preflight-push."""

    def test_preflight_calls_the_live_port(
        self,
        monkeypatch: pytest.MonkeyPatch,
        prefixed_sentinel_with_live_legacy_port: str,
        tmp_path: Path,
    ) -> None:
        import urllib.request

        from issue_orchestrator.entrypoints.cli_tools import agent_done

        # ``agent_done`` imports urllib inside the function, so patch the
        # module it resolves at call time.
        captured = _CapturedRequest({"would_succeed": True})
        monkeypatch.setattr(urllib.request, "urlopen", captured)

        would_succeed, error, _hint = agent_done.run_preflight_push_check(tmp_path)

        assert captured.url == "http://localhost:59957/api/preflight-push"
        assert would_succeed is True
        assert error is None

    def test_skips_without_dialling_when_there_is_no_endpoint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import urllib.request

        from issue_orchestrator.entrypoints.cli_tools import agent_done

        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "0")
        monkeypatch.delenv("ORCHESTRATOR_API_PORT", raising=False)
        monkeypatch.setattr(urllib.request, "urlopen", _must_not_dial)

        assert agent_done.run_preflight_push_check(tmp_path) == (True, None, None)


class TestResumeTargetUsesTheSharedRule:
    """``/api/issues/{n}/resume`` is an allowlisted agent-callback route."""

    def test_sentinel_falls_through_to_live_port(
        self,
        monkeypatch: pytest.MonkeyPatch,
        prefixed_sentinel_with_live_legacy_port: str,
    ) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_ISSUE_NUMBER", "6410")
        target = ResumeTarget.from_agent_environment()
        assert target.url() == "http://localhost:59957/api/issues/6410/resume"

    def test_sentinel_alone_raises_rather_than_dialling_port_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "0")
        monkeypatch.delenv("ORCHESTRATOR_API_PORT", raising=False)
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_ISSUE_NUMBER", "6410")
        with pytest.raises(ValueError, match="ISSUE_ORCHESTRATOR_API_PORT"):
            ResumeTarget.from_agent_environment()
