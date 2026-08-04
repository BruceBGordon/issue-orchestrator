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

import ast
import json
import re
import tomllib
from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.cli_parser import CLI_COMMAND_SURFACE
from issue_orchestrator.entrypoints.mcp_server import MCP_TOOL_NAMES, McpApp, McpSettings
from issue_orchestrator.events.sse_envelope import SSE_SCHEMA_FIELD

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


def _contracted_response_schema_names(document: dict) -> set[str]:
    """Component schema names referenced by a contracted 200 JSON response."""
    referenced: set[str] = set()
    for operations in document["paths"].values():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            content = (
                operation.get("responses", {})
                .get("200", {})
                .get("content", {})
                .get("application/json", {})
            )
            ref = content.get("schema", {}).get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                referenced.add(ref.rsplit("/", 1)[-1])
    return referenced


def _is_contract_version_field(name: str) -> bool:
    """Whether a response property would function as a contract version.

    Substring matching on "version" alone is not enough: this repository's own
    versioned surface names the field ``schema`` (``SSE_SCHEMA_FIELD``), so a
    check that missed it would go green on exactly the change that should
    promote the HTTP surface to ``Versioned``.
    """
    lowered = name.lower()
    return lowered == SSE_SCHEMA_FIELD or "version" in lowered


def _surface_wide_version_fields(
    schemas: dict, response_models: set[str]
) -> list[str]:
    """Version fields carried by *every* response model, i.e. by the surface.

    A version on one payload is a per-payload detail. A version on all of them
    is a surface-wide mechanism a client could rely on - which is what separates
    ``Contracted`` from ``Versioned``.
    """
    if not response_models:
        return []
    property_sets = [
        set(schemas.get(name, {}).get("properties", {})) for name in response_models
    ]
    common_properties = set.intersection(*property_sets)
    return sorted(name for name in common_properties if _is_contract_version_field(name))


def test_http_surface_has_no_surface_wide_response_version() -> None:
    """``Contracted`` is honest only while no *surface-wide* version exists.

    The distinction that matters is not "no field whose name contains version" -
    ``E2ETimelineEventPayload`` already carries ``timeline_schema_version``, and
    a per-payload version is not a surface guarantee. What would make this
    surface ``Versioned`` is a version field on *every* contracted response, the
    way ``schema`` rides on every SSE event. Assert that no such field exists;
    when one appears, promote the tier instead of under-selling it.
    """
    document = json.loads(UI_OPENAPI.read_text(encoding="utf-8"))
    schemas = document.get("components", {}).get("schemas", {})
    response_models = _contracted_response_schema_names(document)

    assert response_models, "no contracted JSON responses found - format changed?"

    surface_wide_version = _surface_wide_version_fields(schemas, response_models)

    assert not surface_wide_version, (
        "Every contracted HTTP response now carries "
        f"{surface_wide_version}, which is a surface-wide version. Promote the "
        f"HTTP surface to Versioned in {STABILITY_DOC.name} instead of leaving "
        "it Contracted."
    )


@pytest.mark.parametrize(
    ("case", "schemas", "expected"),
    [
        (
            "a common `schema` field - this repo's canonical version name",
            {
                "A": {"properties": {"schema": {}, "rows": {}}},
                "B": {"properties": {"schema": {}, "total": {}}},
            },
            ["schema"],
        ),
        (
            "a common explicit *_version field",
            {
                "A": {"properties": {"schema_version": {}, "rows": {}}},
                "B": {"properties": {"schema_version": {}, "total": {}}},
            },
            ["schema_version"],
        ),
        (
            "a version present on only one payload is not surface-wide",
            {
                "A": {"properties": {"timeline_schema_version": {}, "rows": {}}},
                "B": {"properties": {"total": {}}},
            },
            [],
        ),
        (
            "a common field that is not a version",
            {
                "A": {"properties": {"issue_number": {}}},
                "B": {"properties": {"issue_number": {}}},
            },
            [],
        ),
    ],
)
def test_surface_wide_version_detection(case: str, schemas: dict, expected: list) -> None:
    """Prove both sides of the Contracted-versus-Versioned distinction."""
    assert _surface_wide_version_fields(schemas, set(schemas)) == expected, case


def test_per_payload_versions_do_not_make_the_http_surface_versioned() -> None:
    """Guard the distinction itself, using the real per-payload version field."""
    document = json.loads(UI_OPENAPI.read_text(encoding="utf-8"))
    schemas = document.get("components", {}).get("schemas", {})
    timeline = schemas.get("E2ETimelineEventPayload", {}).get("properties", {})

    assert "timeline_schema_version" in timeline, (
        "E2ETimelineEventPayload lost timeline_schema_version; the stability doc "
        "cites it as the example of a per-payload (not surface-wide) version."
    )
    assert "timeline_schema_version" in _stability_doc_text(), (
        f"{STABILITY_DOC.name} must keep naming the per-payload version as the "
        "counter-example, or the Contracted-vs-Versioned distinction reads as an "
        "oversight rather than a decision."
    )


ENTRYPOINTS_DIR = REPO_ROOT / "src" / "issue_orchestrator" / "entrypoints"


def _discovered_sse_endpoints() -> dict[str, str]:
    """Every SSE endpoint in the source, as ``dotted.handler`` -> route path.

    Found structurally (a function returning ``EventSourceResponse``) rather
    than from a hand-maintained list, so a newly added stream shows up here and
    forces a classification instead of quietly inheriting a public promise.
    """
    endpoints: dict[str, str] = {}
    for path in sorted(ENTRYPOINTS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            returns_sse = any(
                isinstance(inner, ast.Return)
                and isinstance(inner.value, ast.Call)
                and isinstance(inner.value.func, ast.Name)
                and inner.value.func.id == "EventSourceResponse"
                for inner in ast.walk(node)
            )
            if not returns_sse:
                continue
            route = _decorator_route(node)
            assert route is not None, (
                f"{path.name}:{node.name} returns EventSourceResponse but its "
                "route path is not a string literal, so it cannot be classified"
            )
            dotted = f"issue_orchestrator.entrypoints.{path.stem}.{node.name}"
            endpoints[dotted] = route
    return endpoints


def _decorator_route(node) -> str | None:
    """The literal path from a ``@router.get("/x")``-style decorator."""
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not decorator.args:
            continue
        first = decorator.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def test_sse_stream_inventory_matches_the_endpoints_in_the_source() -> None:
    """Every SSE endpoint must be classified - none may be silently public."""
    documented = _inventory_table(_stability_doc_text(), "sse-streams")

    _assert_same_set(
        set(documented), set(_discovered_sse_endpoints()), anchor="sse-streams"
    )


def test_sse_stream_inventory_publishes_the_real_route_for_each_endpoint() -> None:
    documented = _inventory_table(_stability_doc_text(), "sse-streams")
    discovered = _discovered_sse_endpoints()

    mismatched = sorted(
        f"{handler}: doc says {row[0]}, source says `{discovered[handler]}`"
        for handler, row in documented.items()
        if row[0] != f"`{discovered[handler]}`"
    )

    assert not mismatched, "SSE streams published under the wrong route:\n" + "\n".join(
        mismatched
    )


def test_exactly_one_sse_stream_is_versioned_and_it_owns_the_envelope() -> None:
    """The public row must name the endpoint that actually applies the envelope.

    Two of the three SSE endpoints serialize their own wire objects without a
    version. If the ``Versioned`` row ever pointed at one of those, the page
    would promise runtime version detection on a stream that has none.
    """
    documented = _inventory_table(_stability_doc_text(), "sse-streams")
    versioned = sorted(
        handler for handler, row in documented.items() if row[-1] == "Versioned"
    )

    assert len(versioned) == 1, (
        f"expected exactly one Versioned SSE stream, found {versioned}"
    )

    module_name = versioned[0].rsplit(".", 1)[0]
    module_path = ENTRYPOINTS_DIR / f"{module_name.rsplit('.', 1)[-1]}.py"
    assert "apply_sse_envelope" in module_path.read_text(encoding="utf-8"), (
        f"{versioned[0]} is published Versioned but {module_path.name} does not "
        "apply the SSE envelope."
    )


def test_internal_sse_streams_do_not_apply_the_public_envelope() -> None:
    """The other direction: an Internal row must not silently be versioned.

    If one of these starts applying the envelope it has become part of the
    public promise and needs re-tiering, not an unnoticed upgrade.
    """
    documented = _inventory_table(_stability_doc_text(), "sse-streams")

    misclassified = []
    for handler, row in documented.items():
        if row[-1] != "Internal":
            continue
        module_file = ENTRYPOINTS_DIR / f"{handler.rsplit('.', 2)[-2]}.py"
        if "apply_sse_envelope" in module_file.read_text(encoding="utf-8"):
            misclassified.append(handler)

    assert not misclassified, (
        f"Streams tiered Internal now apply the public envelope: {misclassified}. "
        f"Re-tier them in {STABILITY_DOC.name} or stop enveloping them."
    )


def test_sse_payload_inventory_matches_the_contracted_subset() -> None:
    """The Contracted claim must cover exactly the schema-backed SSE events.

    ``PUBLIC_CONTRACTS`` covers a selected subset of a 160+ event catalog, so a
    blanket "the data inside the envelope is schema-owned" claim would be false.
    """
    from issue_orchestrator.contracts.public import PUBLIC_CONTRACTS

    documented = _inventory_table(_stability_doc_text(), "sse-payloads")
    contracted = {
        key[len("sse.") :] for key in PUBLIC_CONTRACTS if key.startswith("sse.")
    }

    _assert_same_set(set(documented), contracted, anchor="sse-payloads")


def test_sse_payload_inventory_points_at_committed_artifacts() -> None:
    documented = _inventory_table(_stability_doc_text(), "sse-payloads")

    missing = sorted(
        row[0].strip("`")
        for row in documented.values()
        if not (REPO_ROOT / row[0].strip("`")).exists()
    )

    assert not missing, f"SSE payload rows cite missing schema artifacts: {missing}"


def test_uncontracted_sse_events_are_not_claimed_as_contracted() -> None:
    """Sanity: the catalog really is much larger than the contracted subset."""
    from issue_orchestrator.contracts.public import PUBLIC_CONTRACTS
    from issue_orchestrator.events.catalog import EventName

    contracted = {
        key[len("sse.") :] for key in PUBLIC_CONTRACTS if key.startswith("sse.")
    }
    uncontracted = {event.value for event in EventName} - contracted

    assert uncontracted, "every event is contracted - update the doc's claim"
    assert "Every other event on the stream is `Experimental`." in _stability_doc_text(), (
        f"{len(uncontracted)} of {len(EventName)} events have no committed payload "
        f"schema, so {STABILITY_DOC.name} must classify the remainder explicitly "
        "instead of implying the whole stream is Contracted."
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


def _heading_slug(heading: str) -> str:
    """GitHub's anchor slug for a markdown heading."""
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"\s", "-", slug)


def test_stability_doc_internal_anchors_resolve() -> None:
    """Cross-references between sections must not rot when a heading is retitled.

    The tier carve-outs point at each other by anchor; a stale one sends a
    reader looking for the exception straight past it.
    """
    text = _stability_doc_text()
    slugs = {
        _heading_slug(line.lstrip("#").strip())
        for line in text.splitlines()
        if line.startswith("#")
    }
    broken = sorted(
        target
        for target in _MARKDOWN_LINK.findall(text)
        if target.startswith("#") and target[1:] not in slugs
    )

    assert not broken, (
        f"{STABILITY_DOC.name} has dangling section anchors: {broken}. "
        f"Available: {sorted(slugs)}"
    )
