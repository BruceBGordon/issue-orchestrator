"""Keep the declared public API surface in sync with the code.

``docs/user/stability.md`` declares which surfaces are public and what stability
they carry during ``0.x``. A stability doc nobody re-reads is worse than none at
all, so the inventory is enforced: adding a console script, an MCP tool, or a
CLI command must classify it in the doc, and removing one must remove it there.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.cli_parser import CLI_COMMANDS
from issue_orchestrator.entrypoints.mcp_server import MCP_TOOL_NAMES, McpApp, McpSettings

REPO_ROOT = Path(__file__).resolve().parents[2]
STABILITY_DOC = REPO_ROOT / "docs" / "user" / "stability.md"
README = REPO_ROOT / "README.md"


def _stability_doc_text() -> str:
    return STABILITY_DOC.read_text(encoding="utf-8")


def _console_scripts() -> set[str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        data = tomllib.load(file)
    return set(data["project"]["scripts"])


def test_stability_doc_declares_every_console_script() -> None:
    text = _stability_doc_text()
    missing = sorted(script for script in _console_scripts() if f"`{script}`" not in text)

    assert not missing, (
        f"Console scripts missing a stability tier in {STABILITY_DOC.name}: {missing}. "
        "Declare each installed entry point as supported, experimental, or internal."
    )


def test_stability_doc_declares_every_mcp_tool() -> None:
    text = _stability_doc_text()
    missing = sorted(name for name in MCP_TOOL_NAMES if f"`{name}`" not in text)

    assert not missing, (
        f"MCP tools missing from the surface inventory in {STABILITY_DOC.name}: {missing}."
    )


def test_stability_doc_does_not_document_removed_mcp_tools() -> None:
    """The doc must not advertise a tool the server no longer registers."""
    text = _stability_doc_text()
    stale = sorted(
        name
        for name in ("orchestrator.session.send",)
        if f"`{name}`" in text and name not in MCP_TOOL_NAMES
    )

    assert not stale, (
        f"{STABILITY_DOC.name} documents MCP tools that are not registered: {stale}."
    )


def test_stability_doc_classifies_every_cli_command() -> None:
    text = _stability_doc_text()
    missing = sorted(command for command in CLI_COMMANDS if f"`{command}`" not in text)

    assert not missing, (
        f"CLI commands missing from the surface inventory in {STABILITY_DOC.name}: "
        f"{missing}. Public commands go under the CLI surface; test-only commands "
        "must be listed as internal."
    )


def test_registered_mcp_tools_match_declared_surface() -> None:
    """``MCP_TOOLS`` is the surface: registration must not add or drop names."""

    class _RecordingServer:
        def __init__(self) -> None:
            self.registered: list[str] = []

        def tool(self, name: str):
            def decorator(fn):
                self.registered.append(name)
                return fn

            return decorator

    app = McpApp(
        McpSettings(
            repo_root=Path("/tmp/repo"),
            config_path=Path("/tmp/repo/.issue-orchestrator/config/default.yaml"),
            instance_id=None,
            host="127.0.0.1",
            auto_start=False,
        )
    )
    server = _RecordingServer()

    app.register(server)  # type: ignore[arg-type]

    assert server.registered == list(MCP_TOOL_NAMES)


@pytest.mark.parametrize(
    "expected",
    [
        "## Stability & API surface",
        "docs/user/stability.md",
    ],
)
def test_readme_hosts_the_stability_section(expected: str) -> None:
    assert expected in README.read_text(encoding="utf-8")


def test_stability_doc_states_the_0x_semver_policy() -> None:
    text = _stability_doc_text()

    assert "semver.org" in text
    assert "pre-release" in text
    assert "Path to 1.0" in text
