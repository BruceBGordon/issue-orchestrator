from __future__ import annotations

from pathlib import Path
import re

import pytest

import asyncio

from issue_orchestrator.entrypoints.mcp_server import (
    McpApp,
    McpSettings,
    OrchestratorHttpClient,
    _mcp_repos_allowlist,
    _validate_repo_start_path,
)
from issue_orchestrator.infra import supervisor


def _settings(*, host: str = "127.0.0.1") -> McpSettings:
    return McpSettings(
        repo_root=Path("/tmp/repo"),
        config_path=Path("/tmp/repo/.issue-orchestrator/config/default.yaml"),
        instance_id=None,
        host=host,
        auto_start=False,
    )


def test_http_client_keeps_internal_api_base_url_local() -> None:
    client = OrchestratorHttpClient(_settings(host="0.0.0.0"))
    client.update_port(55543)

    assert client.api_base_url() == "http://0.0.0.0:55543"


def test_http_client_resolves_client_base_url_for_codespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODESPACE_NAME", "octo-space")
    monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
    client = OrchestratorHttpClient(_settings())
    client.update_port(55543)

    assert client.client_base_url() == "https://octo-space-55543.app.github.dev"
    assert client.doctor_url() == "https://octo-space-55543.app.github.dev/api/doctor"


def test_mcp_urls_use_client_facing_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODESPACE_NAME", "octo-space")
    monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
    app = McpApp(_settings())
    app.override_port(55543)

    assert app.urls() == {
        "base_url": "https://octo-space-55543.app.github.dev",
        "dashboard_url": "https://octo-space-55543.app.github.dev/",
        "events_url": "https://octo-space-55543.app.github.dev/api/events",
        "config_url": "https://octo-space-55543.app.github.dev/api/config",
    }


def test_client_base_url_uses_supervisor_status_when_port_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OrchestratorHttpClient(_settings(host="0.0.0.0"))
    running = supervisor.SupervisorStatus(
        state="running",
        pid=123,
        port=19080,
        started_at=None,
        instance_id=None,
    )
    monkeypatch.setattr(
        "issue_orchestrator.entrypoints.mcp_server.supervisor.status",
        lambda repo_root, instance_id=None: running,
    )

    assert client.client_base_url() == "http://localhost:19080"


# ---------------------------------------------------------------------------
# Security hardening tests — see #5987 (F4).
# ---------------------------------------------------------------------------


class _FakeMcpServer:
    """Captures tool registrations so we can assert on the exposed surface."""

    def __init__(self) -> None:
        self.registered: list[str] = []

    def tool(self, name: str):
        def decorator(fn):
            self.registered.append(name)
            return fn

        return decorator


def test_register_omits_session_send_tool() -> None:
    """orchestrator.session.send is the prompt-injection tool we removed."""
    app = McpApp(_settings())
    fake = _FakeMcpServer()

    app.register(fake)  # type: ignore[arg-type]

    assert "orchestrator.session.kill" in fake.registered
    assert "orchestrator.session.send" not in fake.registered


def test_shutdown_force_requires_confirm() -> None:
    app = McpApp(_settings())

    result = asyncio.run(app.tool_shutdown(force=True, confirm=False))

    assert "error" in result
    assert result["error"]["type"] == "ConfirmationRequired"


def test_shutdown_graceful_does_not_require_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-forced shutdown runs without the confirm gate."""
    app = McpApp(_settings())

    async def fake_shutdown(force: bool, *, reason: str = "") -> dict:
        return {"ok": True, "force": force, "reason": reason}

    # Replace the inner shutdown coroutine so we do not have to stand up
    # a real HTTP client for this unit.
    monkeypatch.setattr(app, "shutdown", fake_shutdown)

    result = asyncio.run(app.tool_shutdown(force=False))

    assert result == {"ok": True, "force": False, "reason": "mcp.tool_shutdown"}


def test_validate_repo_start_path_rejects_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    error = _validate_repo_start_path(str(missing))

    assert error is not None
    assert "not found" in error


def test_validate_repo_start_path_rejects_non_git(tmp_path: Path) -> None:
    plain = tmp_path / "plain-dir"
    plain.mkdir()

    error = _validate_repo_start_path(str(plain))

    assert error is not None
    assert "not a git checkout" in error


def test_validate_repo_start_path_accepts_git_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    assert _validate_repo_start_path(str(repo)) is None


def test_validate_repo_start_path_rejects_outside_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").mkdir()
    monkeypatch.setenv(
        "ISSUE_ORCHESTRATOR_MCP_REPOS_ALLOWLIST", str(allowed_root)
    )

    error = _validate_repo_start_path(str(other))

    assert error is not None
    assert "ALLOWLIST" in error


def test_validate_repo_start_path_accepts_under_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    repo = allowed_root / "child" / "repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.setenv(
        "ISSUE_ORCHESTRATOR_MCP_REPOS_ALLOWLIST", str(allowed_root)
    )

    assert _validate_repo_start_path(str(repo)) is None


def test_mcp_repos_allowlist_empty_forbids_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_MCP_REPOS_ALLOWLIST", "   ")

    assert _mcp_repos_allowlist() == []


# ---------------------------------------------------------------------------
# Documentation drift guard — see #6463.
#
# docs/user/mcp.md is the public tool reference for the MCP server. It is the
# only place a client author can learn what the server exposes, so a tool
# added to ``McpApp.register`` without a matching doc entry is a real defect,
# not a formatting nit.
# ---------------------------------------------------------------------------

MCP_DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "user" / "mcp.md"

# Tools the doc names deliberately even though they are not registered. Each
# entry needs a documented reason for its absence; ``session.send`` is the
# prompt-injection primitive removed in #5987 (F4).
INTENTIONALLY_UNREGISTERED_TOOLS = {"orchestrator.session.send"}

_DOCUMENTED_TOOL_PATTERN = re.compile(r"`(orchestrator\.[A-Za-z0-9_.]+)`")


def _documented_tool_names() -> set[str]:
    text = MCP_DOC_PATH.read_text(encoding="utf-8")
    return {
        match.group(1).rstrip(".")
        for match in _DOCUMENTED_TOOL_PATTERN.finditer(text)
    }


def _registered_tool_names() -> set[str]:
    app = McpApp(_settings())
    fake = _FakeMcpServer()
    app.register(fake)  # type: ignore[arg-type]
    return set(fake.registered)


def test_every_registered_tool_is_documented() -> None:
    undocumented = sorted(_registered_tool_names() - _documented_tool_names())

    assert not undocumented, (
        "MCP tools registered but missing from docs/user/mcp.md: "
        f"{undocumented}. Add a row (purpose, arguments, return shape) to the "
        "tool reference."
    )


def test_doc_does_not_advertise_unregistered_tools() -> None:
    registered = _registered_tool_names()
    phantom = sorted(
        _documented_tool_names() - registered - INTENTIONALLY_UNREGISTERED_TOOLS
    )

    assert not phantom, (
        "docs/user/mcp.md documents tools the server does not register: "
        f"{phantom}."
    )
    assert not (registered & INTENTIONALLY_UNREGISTERED_TOOLS), (
        "A tool documented as intentionally omitted is now registered. "
        "Re-review the security posture in the mcp_server module docstring "
        "and docs/user/mcp.md before allowing this."
    )


def test_doc_records_why_session_send_is_absent() -> None:
    """The omission is a security decision; it must stay explained in the doc."""
    text = MCP_DOC_PATH.read_text(encoding="utf-8")

    assert "orchestrator.session.send" in text
    assert "prompt-injection" in text
