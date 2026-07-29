"""Guardrails for UI OpenAPI generated artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.contracts.ui_openapi_generator import (
    ComponentSchema,
    generate_artifacts,
    is_optional,
    render_dts_types,
    render_python_models,
    resolve_type,
)


def test_ui_openapi_artifacts_match_generated(tmp_path: Path) -> None:
    python_out = tmp_path / "ui_openapi_models.py"
    dts_out = tmp_path / "ui-contracts.d.ts"

    generate_artifacts(python_out=python_out, dts_out=dts_out)

    assert python_out.read_text() == Path("src/issue_orchestrator/contracts/ui_openapi_models.py").read_text()
    assert dts_out.read_text() == Path("src/issue_orchestrator/static/js/ui-contracts.d.ts").read_text()


def _python_class_body(source: str, class_name: str) -> str:
    marker = f"class {class_name}(BaseModel):"
    start = source.index(marker)
    rest = source[start + len(marker) :]
    end = rest.find("\nclass ")
    return rest if end == -1 else rest[:end]


def _dts_interface_body(source: str, interface_name: str) -> str:
    marker = f"export interface {interface_name} {{"
    start = source.index(marker)
    rest = source[start + len(marker) :]
    return rest[: rest.index("}")]


def test_response_payload_view_fields_reference_timeline_view() -> None:
    """Issue #5976 F1: the two response payloads that echo the rendered lens
    back to the client (``IssueDetailPayload`` / ``E2ERunDetailPayload``) carry
    the shared ``TimelineView`` type in the generated artifacts, not a free
    ``str`` / ``string``.

    A plain ``str`` here would let a generated client accept or propagate an
    arbitrary response ``view`` value the runtime normalizer never produces —
    the exact looseness this contract hardening closes.
    """
    python_models = Path(
        "src/issue_orchestrator/contracts/ui_openapi_models.py"
    ).read_text()
    dts_types = Path("src/issue_orchestrator/static/js/ui-contracts.d.ts").read_text()

    for class_name in ("IssueDetailPayload", "E2ERunDetailPayload"):
        py_body = _python_class_body(python_models, class_name)
        assert "view: TimelineView | None = None" in py_body, class_name
        assert "view: str" not in py_body, class_name

        ts_body = _dts_interface_body(dts_types, class_name)
        assert "view?: TimelineView;" in ts_body, class_name
        assert "view?: string;" not in ts_body, class_name


def test_ui_openapi_generator_renders_const_enum_and_union_shapes() -> None:
    components = [
        ComponentSchema(
            "ConstEnumPayload",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "state", "maybe_text"],
                "properties": {
                    "kind": {"const": "const_enum"},
                    "state": {"enum": ["queued", "done"]},
                    "maybe_text": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
            },
        ),
        ComponentSchema(
            "UnionPayload",
            {
                "oneOf": [
                    {"$ref": "#/components/schemas/ConstEnumPayload"},
                    {"type": "null"},
                ],
            },
        ),
    ]

    python_models = render_python_models(components)
    dts_types = render_dts_types(components)

    assert "kind: Literal['const_enum']" in python_models
    assert "state: Literal['queued', 'done']" in python_models
    assert "maybe_text: str | None" in python_models
    assert "UnionPayload: TypeAlias = ConstEnumPayload | None" in python_models
    assert 'kind: "const_enum";' in dts_types
    assert 'state: "queued" | "done";' in dts_types
    assert "maybe_text: string | null;" in dts_types
    assert "export type UnionPayload = ConstEnumPayload | null;" in dts_types


def test_ui_openapi_generator_preserves_python_regex_patterns() -> None:
    components = [
        ComponentSchema(
            "PatternPayload",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["worker_agent_label"],
                "properties": {
                    "worker_agent_label": {
                        "type": "string",
                        "pattern": r"^agent:(?!tech-lead$).+",
                    },
                },
            },
        ),
    ]

    python_models = render_python_models(components)

    assert "@field_validator('worker_agent_label')" in python_models
    assert "re.search('^agent:(?!tech-lead$).+', value)" in python_models


def test_ui_openapi_generator_renders_bare_enum_component_as_reusable_alias() -> None:
    """A top-level ``enum`` component (e.g. ``TimelineView``) must render as
    a reusable ``Literal``/``type`` alias, not an empty Pydantic model, and
    ``$ref`` sites must resolve to the alias name.

    The alias carries no forward references, so it is emitted *before* any
    model that references it — that keeps the generated Pydantic module
    importable without a deferred ``model_rebuild``.
    """
    components = [
        ComponentSchema(
            "ViewEnum",
            {"type": "string", "enum": ["user", "ops", "debug", "raw"]},
        ),
        ComponentSchema(
            "UsesEnumPayload",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["view"],
                "properties": {"view": {"$ref": "#/components/schemas/ViewEnum"}},
            },
        ),
    ]

    python_models = render_python_models(components)
    dts_types = render_dts_types(components)

    assert "ViewEnum: TypeAlias = Literal['user', 'ops', 'debug', 'raw']" in python_models
    # No empty model was emitted for the enum component.
    assert "class ViewEnum(BaseModel)" not in python_models
    # The referencing field resolves to the alias name.
    assert "view: ViewEnum" in python_models
    # Alias precedes the model that references it.
    assert python_models.index("ViewEnum: TypeAlias") < python_models.index(
        "class UsesEnumPayload"
    )

    assert 'export type ViewEnum = "user" | "ops" | "debug" | "raw";' in dts_types
    assert "export interface ViewEnum" not in dts_types
    assert "view: ViewEnum;" in dts_types


def test_ui_openapi_generator_rejects_mixed_enum_object_schema() -> None:
    components = [
        ComponentSchema(
            "MixedEnumPayload",
            {
                "enum": ["a", "b"],
                "properties": {"kind": {"const": "mixed"}},
            },
        ),
    ]

    with pytest.raises(ValueError, match="must not mix enum with properties"):
        render_python_models(components)


def test_ui_openapi_generator_detects_nullable_schema_variants() -> None:
    assert is_optional(
        {
            "oneOf": [
                {"$ref": "#/components/schemas/IssueDetailPayload"},
                {"type": "null"},
            ],
        }
    )
    assert is_optional({"type": ["integer", "null"]})
    assert is_optional({"type": "string", "nullable": True})
    assert is_optional({"anyOf": [{"type": "string"}, {"type": "null"}]})
    assert resolve_type({"type": ["integer", "null"]}) == "int | None"
    assert (
        resolve_type(
            {
                "type": ["string", "null"],
                "enum": ["validation_artifacts", "session_evidence", "diagnostics", None],
            }
        )
        == "Literal['validation_artifacts', 'session_evidence', 'diagnostics'] | None"
    )


def test_ui_openapi_generator_rejects_mixed_union_object_schema() -> None:
    components = [
        ComponentSchema(
            "MixedPayload",
            {
                "oneOf": [{"type": "string"}, {"type": "null"}],
                "properties": {"kind": {"const": "mixed"}},
            },
        ),
    ]

    with pytest.raises(ValueError, match="must not mix oneOf/anyOf with properties"):
        render_python_models(components)
