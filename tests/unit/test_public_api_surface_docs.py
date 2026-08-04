"""Keep the declared public API surface in sync with the code.

``docs/user/stability.md`` declares which surfaces are public and what stability
they carry during ``0.x``. A stability doc nobody re-reads is worse than none at
all, so the inventory is enforced: adding a console script, an MCP tool, a CLI
command, or a contracted HTTP route must classify it in the doc, and removing
one must remove it there.

Enforcement is deliberately *table-scoped*, not text-scoped. Each inventory
table is preceded by an ``<!-- inventory:name -->`` anchor and only rows in that
table count as a classification, so a name that merely appears in prose does not
satisfy the contract. Comparisons are exact set equality in both directions, so
a stale row fails just as loudly as a missing one.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.cli_parser import CLI_COMMAND_SURFACE
from issue_orchestrator.entrypoints.mcp_server import MCP_TOOL_NAMES, McpApp, McpSettings

REPO_ROOT = Path(__file__).resolve().parents[2]
STABILITY_DOC = REPO_ROOT / "docs" / "user" / "stability.md"
README = REPO_ROOT / "README.md"
UI_OPENAPI = REPO_ROOT / "docs" / "api" / "ui-openapi.json"

_BACKTICKED = re.compile(r"^`([^`]+)`$")
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _stability_doc_text() -> str:
    return STABILITY_DOC.read_text(encoding="utf-8")


def _console_scripts() -> set[str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        data = tomllib.load(file)
    return set(data["project"]["scripts"])


def _openapi_paths() -> set[str]:
    return set(json.loads(UI_OPENAPI.read_text(encoding="utf-8"))["paths"])


def _inventory_table(text: str, anchor: str) -> dict[str, tuple[str, ...]]:
    """Return the anchored inventory table, keyed by its backticked first cell.

    Only rows of the table immediately following ``<!-- inventory:{anchor} -->``
    are returned. That scoping is the point: it is what makes "mentioned in the
    doc" and "classified in the inventory" different things.
    """
    marker = f"<!-- inventory:{anchor} -->"
    _, separator, remainder = text.partition(marker)
    assert separator, f"{STABILITY_DOC.name} has no {marker} anchor"

    rows: dict[str, tuple[str, ...]] = {}
    started = False
    for line in remainder.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if started:
                break
            continue
        started = True
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= {"-", ":"} and cell for cell in cells):
            continue  # header separator
        name_match = _BACKTICKED.match(cells[0])
        if name_match is None:
            continue  # header row
        name = name_match.group(1)
        assert name not in rows, f"{marker} lists {name!r} twice"
        rows[name] = tuple(cells[1:])

    assert rows, f"{marker} is followed by no table rows"
    return rows


def _assert_same_set(documented: set[str], actual: set[str], *, anchor: str) -> None:
    """Assert exact equality both ways, naming which direction drifted."""
    undocumented = sorted(actual - documented)
    stale = sorted(documented - actual)
    assert not undocumented, (
        f"Present in the code but missing from the inventory:{anchor} table in "
        f"{STABILITY_DOC.name}: {undocumented}. Classify each one with a tier."
    )
    assert not stale, (
        f"Documented in the inventory:{anchor} table in {STABILITY_DOC.name} but "
        f"no longer present in the code: {stale}. Remove the stale rows."
    )


def _declared_tiers() -> set[str]:
    return set(_inventory_table(_stability_doc_text(), "tiers"))


# --------------------------------------------------------------------------
# Console scripts
# --------------------------------------------------------------------------


def test_console_script_inventory_matches_pyproject() -> None:
    documented = _inventory_table(_stability_doc_text(), "console-scripts")

    _assert_same_set(set(documented), _console_scripts(), anchor="console-scripts")


def test_console_script_inventory_uses_defined_tiers() -> None:
    documented = _inventory_table(_stability_doc_text(), "console-scripts")
    tiers = _declared_tiers()

    undefined = sorted(
        {row[-1] for row in documented.values()} - tiers
    )
    assert not undefined, (
        f"Console-script rows use tiers that the tiers table does not define: {undefined}."
    )


# --------------------------------------------------------------------------
# CLI commands
# --------------------------------------------------------------------------


def test_cli_command_inventory_matches_declared_surface() -> None:
    documented = _inventory_table(_stability_doc_text(), "cli-commands")

    _assert_same_set(
        set(documented),
        {spec.name for spec in CLI_COMMAND_SURFACE},
        anchor="cli-commands",
    )


def test_cli_command_inventory_publishes_the_declared_group_and_tier() -> None:
    """Being listed is not enough - the published tier must be the real one.

    In particular the development-only commands must be published as ``Internal``
    rather than silently inheriting the section's "supported" heading.
    """
    documented = _inventory_table(_stability_doc_text(), "cli-commands")

    mismatched = sorted(
        f"{spec.name}: doc says {documented[spec.name]}, code says "
        f"({spec.group.value!r}, {spec.stability.value!r})"
        for spec in CLI_COMMAND_SURFACE
        if documented[spec.name] != (spec.group.value, spec.stability.value)
    )

    assert not mismatched, (
        "CLI commands published with the wrong group or tier in "
        f"{STABILITY_DOC.name}:\n" + "\n".join(mismatched)
    )


def test_development_only_cli_commands_are_published_as_internal() -> None:
    documented = _inventory_table(_stability_doc_text(), "cli-commands")

    assert documented["test-reset"][-1] == "Internal"
    assert documented["e2e-reset"][-1] == "Internal"


def test_cli_command_inventory_uses_defined_tiers() -> None:
    documented = _inventory_table(_stability_doc_text(), "cli-commands")
    tiers = _declared_tiers()

    undefined = sorted({row[-1] for row in documented.values()} - tiers)
    assert not undefined, (
        f"CLI rows use tiers that the tiers table does not define: {undefined}."
    )


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------


def test_mcp_tool_inventory_matches_registered_tools() -> None:
    """``orchestrator.session.send`` was removed as a prompt-injection primitive.

    A doc that still promised it would be worse than no doc at all, so the
    comparison runs in both directions.
    """
    documented = _inventory_table(_stability_doc_text(), "mcp-tools")

    _assert_same_set(set(documented), set(MCP_TOOL_NAMES), anchor="mcp-tools")


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


# --------------------------------------------------------------------------
# HTTP routes
# --------------------------------------------------------------------------


def test_http_route_inventory_matches_the_openapi_path_set() -> None:
    """The contracted HTTP surface is the OpenAPI path set, and only that.

    Without this, contracting a new route leaves it covered by the prose that
    calls every other ``/api/*`` and ``/control/*`` route internal - and a
    consumer cannot tell which claim applies.
    """
    documented = _inventory_table(_stability_doc_text(), "http-routes")

    _assert_same_set(set(documented), _openapi_paths(), anchor="http-routes")


def test_http_route_inventory_assigns_each_route_the_right_scope() -> None:
    """Control Center and Repository Engine are different lifecycle scopes."""
    documented = _inventory_table(_stability_doc_text(), "http-routes")

    expected_scope = {
        path: "Control Center" if path.startswith("/control/") else "Repository Engine"
        for path in documented
    }
    mismatched = sorted(
        f"{path}: doc says {row[0]!r}, prefix implies {expected_scope[path]!r}"
        for path, row in documented.items()
        if row[0] != expected_scope[path]
    )

    assert not mismatched, "HTTP routes published under the wrong scope:\n" + "\n".join(
        mismatched
    )


def test_every_contracted_route_is_under_a_classified_prefix() -> None:
    """The internal remainder is defined as "other /api/* and /control/*".

    A contracted route outside those prefixes would fall through both the
    contracted table and the internal remainder, so it must fail here.
    """
    unclassified = sorted(
        path
        for path in _openapi_paths()
        if not path.startswith(("/api/", "/control/"))
    )

    assert not unclassified, (
        f"Contracted routes outside the classified prefixes: {unclassified}. "
        f"Add the new prefix to the HTTP section of {STABILITY_DOC.name}."
    )


# --------------------------------------------------------------------------
# Doc-format regressions
#
# The parser and comparison above are the contract; these prove they actually
# reject the failure modes they claim to, using synthetic doc text.
# --------------------------------------------------------------------------


def _fake_doc(anchor: str, rows: list[tuple[str, ...]], *, preamble: str = "") -> str:
    body = "\n".join(f"| `{name}` | " + " | ".join(rest) + " |" for name, *rest in rows)
    return (
        f"{preamble}\n"
        f"<!-- inventory:{anchor} -->\n\n"
        "| Name | Tier |\n|---|---|\n"
        f"{body}\n\nTrailing prose.\n"
    )


def test_a_removed_but_still_documented_console_script_fails() -> None:
    documented = _inventory_table(
        _fake_doc("console-scripts", [("coding-done", "Supported"), ("gone", "Supported")]),
        "console-scripts",
    )

    with pytest.raises(AssertionError, match="no longer present in the code"):
        _assert_same_set(set(documented), {"coding-done"}, anchor="console-scripts")


def test_a_removed_but_still_documented_cli_command_fails() -> None:
    documented = _inventory_table(
        _fake_doc("cli-commands", [("start", "Supported"), ("teleport", "Supported")]),
        "cli-commands",
    )

    with pytest.raises(AssertionError, match="no longer present in the code"):
        _assert_same_set(set(documented), {"start"}, anchor="cli-commands")


def test_a_new_undocumented_surface_fails() -> None:
    documented = _inventory_table(
        _fake_doc("cli-commands", [("start", "Supported")]), "cli-commands"
    )

    with pytest.raises(AssertionError, match="missing from the inventory"):
        _assert_same_set(set(documented), {"start", "status"}, anchor="cli-commands")


def test_mentioning_a_name_outside_the_inventory_table_does_not_classify_it() -> None:
    """Prose is not a classification; only a row in the anchored table counts."""
    text = _fake_doc(
        "cli-commands",
        [("start", "Supported")],
        preamble="Run `health-review` to walk the board. See `e2e-reset` too.",
    )

    documented = _inventory_table(text, "cli-commands")

    assert set(documented) == {"start"}
    with pytest.raises(AssertionError, match="missing from the inventory"):
        _assert_same_set(
            set(documented), {"start", "health-review"}, anchor="cli-commands"
        )


def test_a_missing_anchor_fails_rather_than_silently_passing() -> None:
    with pytest.raises(AssertionError, match="anchor"):
        _inventory_table("no tables here", "cli-commands")


def test_an_anchor_with_no_rows_fails_rather_than_silently_passing() -> None:
    text = "<!-- inventory:cli-commands -->\n\n| Name | Tier |\n|---|---|\n\nProse.\n"

    with pytest.raises(AssertionError, match="no table rows"):
        _inventory_table(text, "cli-commands")


def test_table_parsing_stops_at_the_end_of_the_anchored_table() -> None:
    """A later, unrelated table must not leak rows into this inventory."""
    text = (
        "<!-- inventory:cli-commands -->\n\n"
        "| Command | Group | Tier |\n|---|---|---|\n"
        "| `start` | Runtime | Supported |\n\n"
        "Some prose.\n\n"
        "| Other | Table |\n|---|---|\n| `not-a-command` | nope |\n"
    )

    assert set(_inventory_table(text, "cli-commands")) == {"start"}


# --------------------------------------------------------------------------
# Page-level promises
# --------------------------------------------------------------------------


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


def test_versioned_tier_is_backed_by_a_real_runtime_version_field() -> None:
    """The page may only call a surface ``Versioned`` if it truly carries one."""
    from uuid import UUID

    from issue_orchestrator.events.catalog import EVENT_SCHEMA_VERSION
    from issue_orchestrator.events.context import EventContext

    run_id = UUID("00000000-0000-0000-0000-0000000000ab")
    enriched = EventContext(run_id=run_id, tick_id=7).enrich({"issue_number": 1})

    assert enriched["schema"] == EVENT_SCHEMA_VERSION
    assert enriched["run_id"] == str(run_id)
    assert enriched["tick_id"] == 7


def test_contracted_http_payloads_do_not_advertise_a_runtime_version() -> None:
    """``Contracted`` is the honest tier only while responses carry no version.

    If a version field is ever added to the HTTP responses, this fails and the
    surface should be promoted to ``Versioned`` rather than quietly under-sold.
    """
    document = json.loads(UI_OPENAPI.read_text(encoding="utf-8"))
    schemas = document.get("components", {}).get("schemas", {})

    versioned_models = sorted(
        name
        for name, schema in schemas.items()
        if "schema_version" in schema.get("properties", {})
    )

    assert not versioned_models, (
        "UI OpenAPI response models now carry a version field: "
        f"{versioned_models}. Promote the HTTP surface to Versioned in "
        f"{STABILITY_DOC.name} instead of leaving it Contracted."
    )


def test_stability_doc_relative_links_resolve() -> None:
    """A moved file must not leave a dangling promise on this page."""
    broken = []
    for target in _MARKDOWN_LINK.findall(_stability_doc_text()):
        if target.startswith(("http://", "https://", "#")):
            continue
        path = target.partition("#")[0]
        if not path:
            continue
        if not (STABILITY_DOC.parent / path).exists():
            broken.append(target)

    assert not broken, f"{STABILITY_DOC.name} has dangling relative links: {broken}"
