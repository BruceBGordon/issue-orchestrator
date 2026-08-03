from __future__ import annotations

from pathlib import Path
import re

import pytest

import asyncio

from issue_orchestrator.entrypoints.mcp_server import (
    LAUNCH_FAILURE_ERROR_TYPES,
    McpApp,
    McpSettings,
    OrchestratorHttpClient,
    launch_failure_error,
    _mcp_repos_allowlist,
    _validate_repo_start_path,
)
from issue_orchestrator.infra import supervisor
from issue_orchestrator.infra.doctor.types import Check, DoctorResult
from issue_orchestrator.infra.launcher import (
    LaunchResult,
    LaunchStatus,
    UnknownLaunchStatusError,
)


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


# --- LaunchResult mapping ---------------------------------------------------
#
# A doctor/launch failure is an ordinary return value, not an exception, so it
# never reaches ``_safe``. These pin the full status mapping against real
# ``LaunchResult`` values.


def _launch_result(
    status: LaunchStatus | str,
    *,
    launched: bool,
    error: str | None = None,
    checks: list[Check] | None = None,
    supervisor: dict | None = None,
) -> LaunchResult:
    return LaunchResult(
        doctor=DoctorResult(checks=checks or []),
        launched=launched,
        status=status,  # type: ignore[arg-type]  # raw strings exercise the guard
        error=error,
        supervisor=supervisor,
    )


@pytest.mark.parametrize(
    ("status", "launched"),
    [
        (LaunchStatus.OK, True),
        (LaunchStatus.DOCTOR_WARNING, True),
        (LaunchStatus.ALREADY_RUNNING, False),
    ],
)
def test_launch_failure_error_is_none_for_successful_outcomes(
    status: LaunchStatus, launched: bool
) -> None:
    """Warnings and a lost start race mean the orchestrator is running."""
    result = _launch_result(
        status,
        launched=launched,
        error=(
            "Orchestrator already running"
            if status is LaunchStatus.ALREADY_RUNNING
            else None
        ),
        supervisor={"pid": 1, "port": 19080},
    )

    assert launch_failure_error(result) is None


def test_launch_failure_error_builds_a_message_from_failing_doctor_checks() -> None:
    """``doctor_error`` carries no ``error`` string, so the checks supply it."""
    result = _launch_result(
        LaunchStatus.DOCTOR_ERROR,
        launched=False,
        checks=[
            Check(name="github_auth", status="error", detail="token expired"),
            Check(name="worktrees", status="ok", detail="fine"),
            Check(name="hooks", status="error", detail=""),
        ],
    )

    error = launch_failure_error(result)

    assert error == {
        "message": "Doctor checks failed — github_auth: token expired; hooks",
        "type": "DoctorError",
    }


def test_launch_failure_error_uses_the_launcher_message_for_launch_error() -> None:
    result = _launch_result(
        LaunchStatus.LAUNCH_ERROR, launched=False, error="port already bound"
    )

    assert launch_failure_error(result) == {
        "message": "port already bound",
        "type": "LaunchError",
    }


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_launch_failure_error_never_produces_a_blank_message(blank: str | None) -> None:
    """A blank message would read as success to clients that test for one."""
    result = _launch_result(LaunchStatus.LAUNCH_ERROR, launched=False, error=blank)

    error = launch_failure_error(result)

    assert error is not None
    assert error["message"] == "Orchestrator failed to start (launch_error)"


def test_the_failure_type_map_covers_exactly_the_failure_statuses() -> None:
    """A new failure status must not silently fall through as a success."""
    assert set(LAUNCH_FAILURE_ERROR_TYPES) == {
        status for status in LaunchStatus if status.is_failure
    }


@pytest.mark.parametrize("unknown", ["doctor_eror", "supervisor_error", "", "OK"])
def test_an_unknown_status_fails_loudly_instead_of_reading_as_success(
    unknown: str,
) -> None:
    """The classification is exhaustive: no default-to-success branch exists.

    ``LaunchResult`` is a plain dataclass, so a misspelled or newly added
    status can reach this mapping at runtime even though the enum types it.
    """
    result = _launch_result(unknown, launched=False)

    with pytest.raises(UnknownLaunchStatusError) as excinfo:
        launch_failure_error(result)

    assert repr(unknown) in str(excinfo.value)


def test_tool_start_reports_an_unknown_status_as_an_error_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: an unrecognised status reaches the client as a failure."""
    app = McpApp(_settings())
    app.override_port(19080)
    _patch_launch(monkeypatch, _launch_result("supervisor_error", launched=False))

    result = asyncio.run(app.tool_start())

    assert result["error"]["type"] == "UnknownLaunchStatusError"
    assert "supervisor_error" in result["error"]["message"]
    assert result["ui_hint"]["kind"] == "doctor"


def _patch_launch(monkeypatch: pytest.MonkeyPatch, result: LaunchResult) -> None:
    """Make ``McpApp.start`` observe ``result`` from the real launcher seam."""
    monkeypatch.setattr(
        "issue_orchestrator.entrypoints.mcp_server.supervisor.status",
        lambda repo_root, instance_id=None: supervisor.SupervisorStatus(
            state="stopped", pid=None, port=None, started_at=None, instance_id=None
        ),
    )
    monkeypatch.setattr(
        "issue_orchestrator.infra.config.Config.load", staticmethod(lambda path: None)
    )
    monkeypatch.setattr(
        "issue_orchestrator.infra.launcher.launch_subprocess",
        lambda **kwargs: result,
    )


def test_tool_start_surfaces_doctor_error_with_a_ui_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure path a client actually hits: a returned LaunchResult."""
    app = McpApp(_settings())
    app.override_port(19080)
    _patch_launch(
        monkeypatch,
        _launch_result(
            LaunchStatus.DOCTOR_ERROR,
            launched=False,
            checks=[Check(name="github_auth", status="error", detail="token expired")],
        ),
    )

    result = asyncio.run(app.tool_start())

    assert result["error"] == {
        "message": "Doctor checks failed — github_auth: token expired",
        "type": "DoctorError",
    }
    assert result["ui_hint"] == {
        "kind": "doctor",
        "url": "http://127.0.0.1:19080/api/doctor",
    }
    # The nested launch payload is still returned for detail.
    assert result["launch"]["status"] == "doctor_error"
    assert result["launch"]["launched"] is False


def test_tool_start_surfaces_launch_error_with_a_ui_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = McpApp(_settings())
    app.override_port(19080)
    _patch_launch(
        monkeypatch,
        _launch_result(
        LaunchStatus.LAUNCH_ERROR, launched=False, error="port already bound"
    ),
    )

    result = asyncio.run(app.tool_start())

    assert result["error"] == {
        "message": "port already bound",
        "type": "LaunchError",
    }
    assert result["ui_hint"]["kind"] == "doctor"


@pytest.mark.parametrize(
    "status",
    [LaunchStatus.OK, LaunchStatus.DOCTOR_WARNING, LaunchStatus.ALREADY_RUNNING],
)
def test_tool_start_reports_no_error_for_successful_launches(
    monkeypatch: pytest.MonkeyPatch, status: LaunchStatus
) -> None:
    app = McpApp(_settings())
    _patch_launch(
        monkeypatch,
        _launch_result(
            status,
            launched=status is not LaunchStatus.ALREADY_RUNNING,
            error=(
                "Orchestrator already running"
                if status is LaunchStatus.ALREADY_RUNNING
                else None
            ),
            supervisor={"pid": 4242, "port": 19081},
        ),
    )

    result = asyncio.run(app.tool_start())

    assert "error" not in result
    assert "ui_hint" not in result
    assert result["launch"]["status"] == status.value


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
    """A failed launch is a plain string too, not the structured error object."""
    app = McpApp(_settings())
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    def boom(path, config_name):
        raise RuntimeError("port already bound")

    monkeypatch.setattr(
        "issue_orchestrator.entrypoints.mcp_server.supervisor.start", boom
    )

    result = asyncio.run(app.tool_repos_start(str(repo)))

    assert result == {"error": "port already bound"}


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


def test_doc_records_why_session_send_is_absent() -> None:
    """The omission is a security decision; it must stay explained in the doc."""
    text = MCP_DOC_PATH.read_text(encoding="utf-8")

    assert "orchestrator.session.send" in text
    assert "prompt-injection" in text
    # ...and it must be explained in prose, never handed a reference row.
    assert "orchestrator.session.send" not in _tool_reference_rows(text)
