from __future__ import annotations

from pathlib import Path
import re
from unittest.mock import MagicMock

import pytest

import asyncio

from issue_orchestrator.entrypoints.mcp_server import (
    McpApp,
    McpSettings,
    OrchestratorHttpClient,
    _mcp_repos_allowlist,
    _validate_repo_start_path,
)
from issue_orchestrator.execution.repository_engine_start import (
    RepositoryEngineStartResult,
)
from issue_orchestrator.infra import supervisor
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.doctor.types import DoctorResult
from issue_orchestrator.infra.launcher import LaunchResult, LaunchStatus


def _settings(*, host: str = "127.0.0.1") -> McpSettings:
    return McpSettings(
        repo_root=Path("/tmp/repo"),
        config_path=Path(
            "/tmp/repo/.issue-orchestrator/config/modes/default/default.yaml"
        ),
        instance_id=None,
        host=host,
        auto_start=False,
    )


def _repo_settings(
    tmp_path: Path,
    *,
    mode: str = "codex",
    auto_start: bool = False,
) -> tuple[McpSettings, Config]:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    config_path = repo / f".issue-orchestrator/config/modes/{mode}/main.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("agents: {}\n", encoding="utf-8")
    settings = McpSettings(
        repo_root=repo,
        config_path=config_path,
        instance_id=None,
        host="127.0.0.1",
        auto_start=auto_start,
    )
    return settings, Config.load(config_path)


def test_http_client_keeps_internal_api_base_url_local() -> None:
    client = OrchestratorHttpClient(_settings(host="0.0.0.0"))
    client.update_port(55543)

    assert client.api_base_url() == "http://0.0.0.0:55543"


def test_http_client_resolves_client_base_url_for_codespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_client_base_url_uses_supervisor_status_when_port_not_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, config = _repo_settings(tmp_path, mode="default")
    settings = McpSettings(**{**settings.__dict__, "host": "0.0.0.0"})
    client = OrchestratorHttpClient(settings)
    running = supervisor.SupervisorStatus(
        state="running",
        pid=123,
        port=19080,
        started_at=None,
        instance_id=None,
        configuration_mode=config.configuration_mode,
        config_name=config.config_name,
        config_fingerprint=config.config_fingerprint,
    )
    monkeypatch.setattr(
        "issue_orchestrator.entrypoints.mcp_server.supervisor.status",
        lambda repo_root, instance_id=None: running,
    )

    assert client.client_base_url() == "http://localhost:19080"


def test_auto_start_rejects_identity_mismatch_even_when_conflict_has_port(
    tmp_path: Path,
) -> None:
    settings, _config = _repo_settings(tmp_path, auto_start=True)

    class ConflictCommand:
        def execute(self, _request: object) -> RepositoryEngineStartResult:
            return RepositoryEngineStartResult(
                {
                    "error": "engine_identity_mismatch",
                    "detail": "active engine differs",
                    "port": 19080,
                },
                409,
            )

    client = OrchestratorHttpClient(settings, ConflictCommand())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="active engine differs"):
        client.start()


def test_client_rejects_running_engine_with_different_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, config = _repo_settings(tmp_path)
    client = OrchestratorHttpClient(settings)
    monkeypatch.setattr(
        client,
        "status",
        lambda: supervisor.SupervisorStatus(
            state="running",
            port=19080,
            configuration_mode="default",
            config_name=config.config_name,
            config_fingerprint=config.config_fingerprint,
        ),
    )

    with pytest.raises(RuntimeError, match="configuration identity mismatch"):
        client.api_base_url()


def test_cached_port_is_revalidated_after_engine_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, config = _repo_settings(tmp_path)
    client = OrchestratorHttpClient(settings)
    statuses = iter(
        [
            supervisor.SupervisorStatus(
                state="running",
                port=19080,
                configuration_mode=config.configuration_mode,
                config_name=config.config_name,
                config_fingerprint=config.config_fingerprint,
            ),
            supervisor.SupervisorStatus(
                state="running",
                port=19080,
                configuration_mode=config.configuration_mode,
                config_name=config.config_name,
                config_fingerprint="different-bytes",
            ),
        ]
    )
    monkeypatch.setattr(client, "status", lambda: next(statuses))

    assert client.api_base_url() == "http://127.0.0.1:19080"
    with pytest.raises(RuntimeError, match="configuration identity mismatch"):
        client.api_base_url()


def test_mcp_start_rejects_existing_engine_with_different_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, config = _repo_settings(tmp_path)
    app = McpApp(settings)
    monkeypatch.setattr(
        app._client,
        "status",
        lambda: supervisor.SupervisorStatus(
            state="running",
            port=19080,
            configuration_mode=config.configuration_mode,
            config_name=config.config_name,
            config_fingerprint="different-bytes",
        ),
    )

    with pytest.raises(RuntimeError, match="configuration identity mismatch"):
        app.start()


def test_mcp_stop_rejects_existing_engine_with_different_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, config = _repo_settings(tmp_path)
    app = McpApp(settings)
    monkeypatch.setattr(
        app._client,
        "status",
        lambda: supervisor.SupervisorStatus(
            state="running",
            port=19080,
            configuration_mode="default",
            config_name=config.config_name,
            config_fingerprint=config.config_fingerprint,
        ),
    )
    stop = MagicMock()
    monkeypatch.setattr(
        "issue_orchestrator.entrypoints.mcp_server.supervisor.stop",
        stop,
    )

    with pytest.raises(RuntimeError, match="configuration identity mismatch"):
        app.stop()
    stop.assert_not_called()


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


def test_shutdown_graceful_does_not_require_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-forced shutdown runs without the confirm gate."""
    app = McpApp(_settings())

    async def fake_shutdown(force: bool, *, reason: str = "") -> dict:
        return {"ok": True, "force": force, "reason": reason}

    # Replace the inner shutdown coroutine so we do not have to stand up
    # a real HTTP client for this unit.
    monkeypatch.setattr(app, "shutdown", fake_shutdown)

    result = asyncio.run(app.tool_shutdown(force=False))

    assert result == {"ok": True, "force": False, "reason": "mcp.tool_shutdown"}


# ---------------------------------------------------------------------------
# Return-shape contracts published in docs/user/mcp.md — see #6463.
# ---------------------------------------------------------------------------


def test_start_failure_returns_error_with_doctor_ui_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed start must carry the doctor ``ui_hint`` the VS Code panel reads."""
    app = McpApp(_settings())
    app.override_port(19080)

    def failing_start() -> dict:
        raise RuntimeError("launcher exploded")

    monkeypatch.setattr(app, "start", failing_start)

    result = asyncio.run(app.tool_start())

    assert result["error"] == {
        "message": "launcher exploded",
        "type": "RuntimeError",
    }
    assert result["ui_hint"] == {
        "kind": "doctor",
        "url": "http://127.0.0.1:19080/api/doctor",
    }


def test_start_success_carries_no_ui_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    app = McpApp(_settings())

    monkeypatch.setattr(app, "start", lambda: {"supervisor": {"state": "running"}})

    result = asyncio.run(app.tool_start())

    assert result == {"supervisor": {"state": "running"}}
    assert "ui_hint" not in result


def _tool_start_for_command_result(
    monkeypatch: pytest.MonkeyPatch,
    result: RepositoryEngineStartResult,
) -> dict:
    """Run the public MCP tool against one exact shared-command outcome."""
    app = McpApp(_settings())
    app.override_port(19080)
    monkeypatch.setattr(
        "issue_orchestrator.entrypoints.mcp_server.supervisor.status",
        lambda repo_root, instance_id=None: supervisor.SupervisorStatus(
            state="stopped", pid=None, port=None, started_at=None, instance_id=None
        ),
    )
    config = Config()
    monkeypatch.setattr(
        "issue_orchestrator.infra.config.Config.load",
        staticmethod(lambda path: config),
    )
    monkeypatch.setattr(app._repo_start_command, "execute", lambda _request: result)
    return asyncio.run(app.tool_start())


@pytest.mark.parametrize("launch_status", ["ok", "doctor_warning"])
def test_tool_start_preserves_successful_command_status(
    monkeypatch: pytest.MonkeyPatch,
    launch_status: str,
) -> None:
    result = _tool_start_for_command_result(
        monkeypatch,
        RepositoryEngineStartResult(
            {"status": "started", "launch_status": launch_status}
        ),
    )

    assert result["launch"]["status"] == launch_status
    assert result["launch"]["launched"] is True
    assert "error" not in result
    assert "ui_hint" not in result


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "started", "launch_status": "future_success"},
        {"status": "started"},
    ],
)
def test_tool_start_reports_invalid_success_status_as_error(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, str],
) -> None:
    result = _tool_start_for_command_result(
        monkeypatch,
        RepositoryEngineStartResult(payload),
    )

    assert result["error"]["type"] == "UnknownLaunchStatusError"
    assert str(payload.get("launch_status")) in result["error"]["message"]
    assert result["ui_hint"]["kind"] == "doctor"


def test_tool_start_survives_a_doctor_url_lookup_that_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable state directory fails the start *and* the hint lookup.

    Resolving the doctor URL re-reads supervisor state, so the read that broke
    the start breaks the hint too — and it runs after ``_safe`` has already
    built the structured error. If it were allowed to raise, the exception
    would cross the protocol boundary and the client would lose the error
    entirely. No ``override_port`` here on purpose: a cached port would let
    ``doctor_url`` answer without touching supervisor state and the second
    failure would never happen.
    """
    app = McpApp(_settings())

    def unreadable_state(repo_root, instance_id=None):
        raise OSError(13, "Permission denied: .issue-orchestrator/state")

    monkeypatch.setattr(
        "issue_orchestrator.entrypoints.mcp_server.supervisor.status", unreadable_state
    )

    result = asyncio.run(app.tool_start())

    assert result["error"]["type"] == "PermissionError"
    assert "Permission denied" in result["error"]["message"]
    # The hint still routes the operator to the doctor panel; only the
    # optional URL is missing.
    assert result["ui_hint"] == {"kind": "doctor"}


def test_tool_start_surfaces_doctor_error_with_a_ui_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure path a client actually hits: a returned start-command result."""
    result = _tool_start_for_command_result(
        monkeypatch,
        RepositoryEngineStartResult(
            {
                "error": "doctor_failed",
                "detail": "Doctor checks failed — github_auth: token expired",
            },
            422,
        ),
    )

    assert result["error"] == {
        "message": "Doctor checks failed — github_auth: token expired",
        "type": "DoctorError",
    }
    assert result["ui_hint"] == {
        "kind": "doctor",
        "url": "http://127.0.0.1:19080/api/doctor",
    }
    # The nested command payload is still returned for detail.
    assert result["launch"]["error_code"] == "doctor_failed"
    assert result["launch"]["error"] == (
        "Doctor checks failed — github_auth: token expired"
    )
    assert result["launch"]["status"] == "doctor_error"


def test_tool_start_surfaces_launch_error_with_a_ui_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _tool_start_for_command_result(
        monkeypatch,
        RepositoryEngineStartResult(
            {"error": "launch_failed", "detail": "port already bound"},
            500,
        ),
    )

    assert result["error"] == {
        "message": "port already bound",
        "type": "LaunchError",
    }
    assert result["ui_hint"]["kind"] == "doctor"


def test_tool_start_treats_already_running_command_result_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _tool_start_for_command_result(
        monkeypatch,
        RepositoryEngineStartResult(
            {"error": "already_running", "detail": "Orchestrator already running"},
            409,
        ),
    )

    assert "error" not in result
    assert "ui_hint" not in result
    assert result["launch"]["status"] == "already_running"
    assert result["launch"]["launched"] is False


def test_tool_start_surfaces_configuration_conflict_with_identity_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _tool_start_for_command_result(
        monkeypatch,
        RepositoryEngineStartResult(
            {
                "error": "configuration_conflict",
                "detail": "codex/main is already running",
            },
            409,
        ),
    )

    assert result["error"] == {
        "message": "codex/main is already running",
        "type": "ConfigurationConflict",
    }
    assert result["launch"]["status"] == "configuration_conflict"
    assert result["launch"]["launched"] is False
    assert result["ui_hint"]["kind"] == "doctor"


def test_repos_start_returns_plain_string_error_for_invalid_path(
    tmp_path: Path,
) -> None:
    """``repos.start`` validation failures are a plain string, not the error object."""
    app = McpApp(_settings())
    plain = tmp_path / "not-a-checkout"
    plain.mkdir()

    result = asyncio.run(app.tool_repos_start(str(plain)))

    assert isinstance(result["error"], str)
    assert "not a git checkout" in result["error"]


def test_repos_start_returns_plain_string_error_for_launch_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A command failure preserves the typed repository-start payload."""
    app = McpApp(_settings())
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    monkeypatch.setattr(
        app._repo_start_command,
        "execute",
        lambda _request: RepositoryEngineStartResult(
            {"error": "launch_failed", "detail": "port already bound"},
            500,
        ),
    )

    result = asyncio.run(app.tool_repos_start(str(repo)))

    assert result == {"error": "launch_failed", "detail": "port already bound"}


def test_repos_stop_reports_status_not_a_plain_string_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``repos.stop`` has no plain-string error path — it reports a status."""
    app = McpApp(_settings())
    monkeypatch.setattr(
        "issue_orchestrator.entrypoints.mcp_server.supervisor.stop",
        lambda path, force=False, reason="", actor="": False,
    )

    result = asyncio.run(app.tool_repos_stop(str(tmp_path)))

    assert result == {"status": "failed"}


def test_repos_stop_failure_uses_the_structured_error_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exceptions from ``repos.stop`` come back through the shared error shape."""
    app = McpApp(_settings())

    def boom(path, force=False, reason="", actor="") -> bool:
        raise RuntimeError("supervisor unavailable")

    monkeypatch.setattr(
        "issue_orchestrator.entrypoints.mcp_server.supervisor.stop", boom
    )

    result = asyncio.run(app.tool_repos_stop(str(tmp_path)))

    assert result == {
        "error": {"message": "supervisor unavailable", "type": "RuntimeError"}
    }


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
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_MCP_REPOS_ALLOWLIST", str(allowed_root))

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
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_MCP_REPOS_ALLOWLIST", str(allowed_root))

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

_TOOL_REFERENCE_HEADING = "## Tool Reference"
_TOP_LEVEL_HEADING = re.compile(r"^## ")
_TOOL_CELL = re.compile(r"^`(orchestrator\.[A-Za-z0-9_.]+)`$")


def _tool_reference_section(text: str) -> list[str]:
    """Return the lines under ``## Tool Reference`` (its ``###`` tables included)."""
    lines = text.splitlines()
    try:
        start = lines.index(_TOOL_REFERENCE_HEADING)
    except ValueError:  # pragma: no cover - guarded by the tests below
        raise AssertionError(
            f"docs/user/mcp.md is missing its '{_TOOL_REFERENCE_HEADING}' section"
        ) from None
    section: list[str] = []
    for line in lines[start + 1 :]:
        if _TOP_LEVEL_HEADING.match(line):
            break
        section.append(line)
    return section


def _tool_reference_rows(text: str) -> set[str]:
    """Tools that have a real Tool Reference *table row*, not a prose mention.

    A row only counts when it names the tool in the first cell and fills in
    the arguments and returns cells — which is exactly what the drift guard's
    failure message promises a documented tool has. Scanning the whole file
    for inline-code mentions instead would let a tool stay "documented" by an
    incidental reference in a security note after its row was deleted.
    """
    documented: set[str] = set()
    for line in _tool_reference_section(text):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        match = _TOOL_CELL.match(cells[0])
        if match is None:
            continue
        if not cells[1] or not cells[2]:
            continue
        documented.add(match.group(1))
    return documented


def _documented_tool_names() -> set[str]:
    return _tool_reference_rows(MCP_DOC_PATH.read_text(encoding="utf-8"))


def _registered_tool_names() -> set[str]:
    app = McpApp(_settings())
    fake = _FakeMcpServer()
    app.register(fake)  # type: ignore[arg-type]
    return set(fake.registered)


def test_every_registered_tool_is_documented() -> None:
    undocumented = sorted(_registered_tool_names() - _documented_tool_names())

    assert not undocumented, (
        "MCP tools registered but missing from the docs/user/mcp.md Tool "
        f"Reference tables: {undocumented}. Add a row (purpose, arguments, "
        "return shape); a prose mention elsewhere does not count."
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


def test_prose_mention_does_not_count_as_a_tool_reference_row() -> None:
    """The regression this guard exists for: a name in prose is not a row.

    ``orchestrator.shutdown`` is discussed at length in the security notes.
    If its reference row were deleted, an inline-code scan of the whole file
    would still call it documented.
    """
    doc = "\n".join(
        [
            "## Tool Reference",
            "",
            "| Tool | Arguments | Returns |",
            "|------|-----------|---------|",
            "| `orchestrator.status` | *(none)* | `{...}` |",
            "",
            "## Security and Operational Notes",
            "",
            "**Destructive shutdown is confirm-gated.** "
            "`orchestrator.shutdown(force=true)` needs `confirm=true`.",
        ]
    )

    assert _tool_reference_rows(doc) == {"orchestrator.status"}


def test_tool_reference_row_needs_arguments_and_returns() -> None:
    """An empty row is not documentation either."""
    doc = "\n".join(
        [
            "## Tool Reference",
            "",
            "| Tool | Arguments | Returns |",
            "|------|-----------|---------|",
            "| `orchestrator.status` | *(none)* | `{...}` |",
            "| `orchestrator.pause` |  |  |",
        ]
    )

    assert _tool_reference_rows(doc) == {"orchestrator.status"}


def test_tool_reference_rows_span_every_subsection_table() -> None:
    """``###`` subsection tables inside Tool Reference all count."""
    doc = "\n".join(
        [
            "## Tool Reference",
            "",
            "### Lifecycle",
            "",
            "| Tool | Arguments | Returns |",
            "|------|-----------|---------|",
            "| `orchestrator.status` | *(none)* | `{...}` |",
            "",
            "### Sessions",
            "",
            "| Tool | Arguments | Returns |",
            "|------|-----------|---------|",
            "| `orchestrator.session.kill` | `issue_number` | `{...}` |",
            "",
            "## Troubleshooting",
            "",
            "| Tool | Arguments | Returns |",
            "|------|-----------|---------|",
            "| `orchestrator.ghost` | `x` | `{...}` |",
        ]
    )

    assert _tool_reference_rows(doc) == {
        "orchestrator.status",
        "orchestrator.session.kill",
    }


# Claims the repository tools cannot support. They read the persistent registry
# plus the server's own working directory — there is no process enumeration and
# no lock-file scan — so any of these phrases is a factual overclaim about what
# a token-less client can see, wherever in the page it appears.
_HOST_WIDE_DISCOVERY_CLAIMS = (
    "every repository on the host",
    "all repositories on the host",
    "repositories across the host",
    "enumerate every repository",
    "scans the host",
    "every git checkout",
)

_REPO_SCOPE_ANCHOR = "#scope-of-the-repository-tools"


def _heading_depths(text: str) -> list[tuple[int, str]]:
    """``(line index, heading)`` for every real heading.

    Fence-aware: a shell comment inside a ``` block starts with ``#`` but is not
    a heading, and treating one as a section boundary silently truncates the
    section under inspection.
    """
    headings: list[tuple[int, str]] = []
    in_fence = False
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and stripped.startswith("#"):
            headings.append((index, stripped))
    return headings


def _doc_section(text: str, heading: str) -> str:
    """The body of ``heading``, up to the next heading of the same or higher level."""
    lines = text.splitlines()
    headings = _heading_depths(text)
    start = next(index for index, found in headings if found == heading)
    depth = len(heading) - len(heading.lstrip("#"))
    for index, found in headings:
        if index > start and len(found) - len(found.lstrip("#")) <= depth:
            return "\n".join(lines[start:index])
    return "\n".join(lines[start:])


def test_doc_never_claims_host_wide_repository_discovery() -> None:
    """The summaries must not outgrow the scope section they summarise.

    The page states the real scope in one place; a security or authentication
    summary that promises host-wide enumeration contradicts it and overstates
    what a client without a bearer token can actually see.
    """
    text = MCP_DOC_PATH.read_text(encoding="utf-8").lower()

    overclaims = [claim for claim in _HOST_WIDE_DISCOVERY_CLAIMS if claim in text]

    assert not overclaims, (
        f"docs/user/mcp.md claims host-wide repository discovery: {overclaims}. "
        "The repository tools read the registry plus the server's working "
        "directory; see the 'Scope of the repository tools' section."
    )


@pytest.mark.parametrize(
    "heading",
    ["### Authentication to the Control API", "## Security and Operational Notes"],
)
def test_doc_summaries_point_at_the_repository_scope(heading: str) -> None:
    """Both summaries must route the reader to the authoritative scope.

    They describe what a token-less client can see, so they cannot be read
    safely without the limits that section sets out.
    """
    section = _doc_section(MCP_DOC_PATH.read_text(encoding="utf-8"), heading)

    assert _REPO_SCOPE_ANCHOR in section, (
        f"{heading!r} describes token-free disclosure without linking "
        f"{_REPO_SCOPE_ANCHOR}"
    )


def test_the_repository_scope_anchor_resolves() -> None:
    """A link the summaries depend on must not silently rot."""
    text = MCP_DOC_PATH.read_text(encoding="utf-8")
    anchors = {
        "#" + heading.lstrip("#").strip().lower().replace(" ", "-").replace("`", "")
        for _, heading in _heading_depths(text)
    }

    assert _REPO_SCOPE_ANCHOR in anchors


def test_doc_records_why_session_send_is_absent() -> None:
    """The omission is a security decision; it must stay explained in the doc."""
    text = MCP_DOC_PATH.read_text(encoding="utf-8")

    assert "orchestrator.session.send" in text
    assert "prompt-injection" in text
    # ...and it must be explained in prose, never handed a reference row.
    assert "orchestrator.session.send" not in _tool_reference_rows(text)


def test_repo_start_uses_shared_preflight_and_returns_effective_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    config_path = repo / ".issue-orchestrator/config/modes/codex/main.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("agents: {}\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_launch_subprocess(**kwargs: object) -> LaunchResult:
        captured.update(kwargs)
        return LaunchResult(
            doctor=DoctorResult(checks=[]),
            launched=True,
            status=LaunchStatus.OK,
            supervisor={
                "pid": 42,
                "port": 19080,
                "configuration_mode": "codex",
                "config_name": "main.yaml",
                "config_fingerprint": "abc123",
            },
        )

    selections: list[object] = []
    monkeypatch.delenv("ISSUE_ORCHESTRATOR_MCP_REPOS_ALLOWLIST", raising=False)
    monkeypatch.setattr(
        "issue_orchestrator.infra.launcher.launch_subprocess",
        fake_launch_subprocess,
    )
    monkeypatch.setattr(
        "issue_orchestrator.infra.repo_registry.record_launched_selection",
        lambda _repo, selection: selections.append(selection),
    )
    app = McpApp(
        McpSettings(
            repo_root=repo,
            config_path=config_path,
            instance_id=None,
            host="127.0.0.1",
            auto_start=False,
        )
    )

    result = app.start_repo(str(repo), "main", "codex")

    assert result["status"] == "started"
    assert result["pid"] == 42
    assert result["port"] == 19080
    assert result["mode"] == "codex"
    assert result["config_name"] == "main.yaml"
    assert result["config_fingerprint"] == captured["config"].config_fingerprint
    assert captured["mode"] == "codex"
    assert captured["config_name"] == "main.yaml"
    assert captured["config"].configuration_mode == "codex"
    assert selections[0].to_dict() == {
        "mode": "codex",
        "config_name": "main.yaml",
    }
