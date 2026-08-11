"""Support utilities for the data-driven settings schema."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .config_paths import resolve_relative_path


# Doctor check type constants - used in json_schema_extra["doctor_check"]
DOCTOR_CHECK_PATH_EXISTS = "path_exists"
DOCTOR_CHECK_PATH_IS_FILE = "path_is_file"
DOCTOR_CHECK_FIRST_ARG_PATH_EXISTS = "first_arg_path_exists"
DOCTOR_CHECK_REFERENCES_AGENT = "references_agent"

# Doctor severity levels
DOCTOR_SEVERITY_ERROR = "error"
DOCTOR_SEVERITY_WARNING = "warning"

# Summary format constants - used in json_schema_extra["summary"]
SUMMARY_ENABLED_FLAG = "enabled_flag"
SUMMARY_KEY_VALUE = "key_value"
SUMMARY_INTERVAL = "interval"
SUMMARY_BOOLEAN_FLAG = "boolean_flag"

# Config value type constants - used in json_schema_extra["config_value_type"]
CONFIG_VALUE_TYPE_PATH = "path"

# Form control kinds - the CLOSED set of control types the settings form can
# faithfully render and round-trip. classify_form_control() below is the
# single owner of the JSON-schema -> form-control mapping. The settings
# template and static/js/settings_form_controls.js dispatch on the kind token
# it produces and must never re-interpret the schema themselves.
FORM_CONTROL_BOOLEAN = "boolean"
FORM_CONTROL_ENUM = "enum"
FORM_CONTROL_INTEGER = "integer"
FORM_CONTROL_NUMBER = "number"
FORM_CONTROL_STRING = "string"
FORM_CONTROL_OPTIONAL_STRING = "optional_string"
FORM_CONTROL_OPTIONAL_INTEGER = "optional_integer"
FORM_CONTROL_DICT_ENUM = "dict_enum"

FORM_CONTROL_KINDS = frozenset(
    {
        FORM_CONTROL_BOOLEAN,
        FORM_CONTROL_ENUM,
        FORM_CONTROL_INTEGER,
        FORM_CONTROL_NUMBER,
        FORM_CONTROL_STRING,
        FORM_CONTROL_OPTIONAL_STRING,
        FORM_CONTROL_OPTIONAL_INTEGER,
        FORM_CONTROL_DICT_ENUM,
    }
)


class UnsupportedSettingsFieldError(Exception):
    """A settings-schema field has no faithful form-control projection.

    Raised at schema-build time so an unsupported field type fails loudly
    in CI and at page render, instead of silently degrading to a text
    input whose posted string the strict POST validation then rejects.
    """


_SCALAR_FORM_KINDS = {
    "boolean": FORM_CONTROL_BOOLEAN,
    "integer": FORM_CONTROL_INTEGER,
    "number": FORM_CONTROL_NUMBER,
    "string": FORM_CONTROL_STRING,
}


def classify_form_control(field_name: str, prop: dict[str, Any]) -> dict[str, Any]:
    """Classify a JSON-schema property into a form-control descriptor.

    Returns ``{"kind": <FORM_CONTROL_*>}`` plus kind-specific keys:
    ``enum`` carries ``options``; ``dict_enum`` carries ``value_options``.

    Raises UnsupportedSettingsFieldError for any property shape outside the
    closed set - extend this function AND both dispatches (template + JS)
    together when the registry grows a new field shape.
    """
    if prop.get("enum") is not None:
        return {"kind": FORM_CONTROL_ENUM, "options": list(prop["enum"])}
    if prop.get("const") is not None:
        # Single-value Literal emits const, not enum; classifying it as a
        # plain string would silently drop the value constraint.
        raise UnsupportedSettingsFieldError(
            f"Settings field '{field_name}' uses a single-value const schema "
            f"{prop!r}; the form would degrade it to free text. Extend "
            "classify_form_control() and the form dispatches together."
        )
    prop_type = prop.get("type")
    scalar_kind = _SCALAR_FORM_KINDS.get(prop_type) if isinstance(prop_type, str) else None
    if scalar_kind is not None:
        return {"kind": scalar_kind}
    if prop_type == "object":
        return _classify_object_control(field_name, prop)
    any_of = prop.get("anyOf")
    if isinstance(any_of, list):
        return _classify_optional_control(field_name, prop, any_of)
    raise UnsupportedSettingsFieldError(
        f"Settings field '{field_name}' has no form-control projection for "
        f"JSON schema {prop!r}. Extend classify_form_control() and the form "
        "dispatches together."
    )


def _classify_object_control(field_name: str, prop: dict[str, Any]) -> dict[str, Any]:
    """Classify an object-typed property (dict fields)."""
    additional = prop.get("additionalProperties")
    if isinstance(additional, dict) and additional.get("enum"):
        return {
            "kind": FORM_CONTROL_DICT_ENUM,
            "value_options": list(additional["enum"]),
        }
    raise UnsupportedSettingsFieldError(
        f"Settings field '{field_name}' is an object without an enum "
        "additionalProperties value schema; the settings form cannot "
        "project it. Extend classify_form_control() and the form "
        "dispatches together."
    )


def _classify_optional_control(
    field_name: str, prop: dict[str, Any], any_of: list[Any]
) -> dict[str, Any]:
    """Classify an anyOf property (Optional[...] fields)."""
    entries = [entry for entry in any_of if isinstance(entry, dict)]
    if any(
        entry.get("enum") is not None or entry.get("const") is not None
        for entry in entries
    ):
        # Optional[Literal[...]] emits anyOf with an enum/const entry;
        # projecting it as optional_string would silently drop the
        # value constraint into a free-text input.
        raise UnsupportedSettingsFieldError(
            f"Settings field '{field_name}' is an optional enum/const "
            f"({prop!r}); the form would drop the value constraint. Add "
            "an optional_enum kind to classify_form_control() and the "
            "form dispatches before using this shape."
        )
    types = sorted(entry.get("type", "") for entry in entries)
    if types == ["null", "string"]:
        return {"kind": FORM_CONTROL_OPTIONAL_STRING}
    if types == ["integer", "null"]:
        return {"kind": FORM_CONTROL_OPTIONAL_INTEGER}
    raise UnsupportedSettingsFieldError(
        f"Settings field '{field_name}' has no form-control projection for "
        f"JSON schema {prop!r}. Extend classify_form_control() and the form "
        "dispatches together."
    )


def reverse_ui_transform(transform: str | None, value: Any) -> Any:
    """Reverse a display-form ``ui_transform`` back to its stored (list) form.

    Mirrors ``build_tabs_from_config``'s forward transform. Shared by
    ``apply_tabs_to_config`` (display value -> live Config attribute) and
    ``build_settings_save_plan`` (display value -> persisted YAML value) so both
    directions stay in lock-step from a single definition.
    """
    if transform == "comma_separated_list":
        return [s.strip() for s in value.split(",") if s.strip()] if value else []
    if transform == "space_separated_list":
        return value.split() if value else []
    if transform == "newline_separated_list":
        return [s.strip() for s in value.split("\n") if s.strip()] if value else []
    return value


def _get_nested_attr(obj: Any, path: str) -> Any:
    """Get obj.a.b.c from dotted path 'a.b.c'."""
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _set_nested_attr(obj: Any, path: str, value: Any) -> None:
    """Set obj.a.b.c = value from dotted path 'a.b.c'."""
    parts = path.split(".")
    for part in parts[:-1]:
        obj = getattr(obj, part)
    setattr(obj, parts[-1], value)


def _coerce_config_value(extra: dict[str, Any], value: Any, config: Any) -> Any:
    """Convert schema form values into the config field's runtime type."""
    if extra.get("config_value_type") != CONFIG_VALUE_TYPE_PATH:
        return value
    if value is None:
        return None
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        return resolve_relative_path(value, config.repo_root)
    return value


def build_tabs_from_config(tab_definitions: list[dict[str, Any]], config: Any) -> dict[str, BaseModel]:
    """Build all tab models from a Config object.

    Returns a dict mapping tab key -> Pydantic model instance with current values.
    """
    result: dict[str, BaseModel] = {}
    for tab in tab_definitions:
        model_cls = tab["model"]
        values: dict[str, Any] = {}
        for field_name, field_info in model_cls.model_fields.items():
            extra = field_info.json_schema_extra
            assert isinstance(extra, dict), f"Missing json_schema_extra on {field_name}"

            # Use config_read_method if specified (e.g., "filtering.get_milestones"),
            # otherwise fall back to config_attr for simple attribute reads.
            read_method = extra.get("config_read_method")
            if read_method:
                raw = _get_nested_attr(config, read_method)()
            else:
                config_attr = extra["config_attr"]
                raw = _get_nested_attr(config, config_attr)

            # Handle UI transforms (list -> string for display).
            transform = extra.get("ui_transform")
            if transform == "comma_separated_list":
                raw = ", ".join(raw) if raw else ""
            elif transform == "space_separated_list":
                raw = " ".join(raw) if raw else ""
            elif transform == "newline_separated_list":
                raw = "\n".join(raw) if raw else ""
            elif isinstance(raw, Path):
                raw = str(raw)

            values[field_name] = raw
        result[tab["key"]] = model_cls(**values)
    return result


def apply_tabs_to_config(tab_definitions: list[dict[str, Any]], tabs: dict[str, BaseModel], config: Any) -> bool:
    """Apply all tab models to a Config object.

    Returns True if any field marked restart_required changed.
    """
    restart = False
    for tab in tab_definitions:
        model = tabs.get(tab["key"])
        if model is None:
            continue
        model_cls = tab["model"]
        for field_name, field_info in model_cls.model_fields.items():
            extra = field_info.json_schema_extra
            assert isinstance(extra, dict), f"Missing json_schema_extra on {field_name}"
            config_attr = extra["config_attr"]
            value = getattr(model, field_name)

            # Handle transforms (string -> list for storage).
            value = reverse_ui_transform(extra.get("ui_transform"), value)

            old = _get_nested_attr(config, config_attr)
            value = _coerce_config_value(extra, value, config)
            if extra.get("restart_required"):
                old_for_comparison = _coerce_config_value(extra, old, config)
                if str(old_for_comparison) != str(value):
                    restart = True

            _set_nested_attr(config, config_attr, value)
    return restart


@dataclass(frozen=True)
class SettingsSavePatchEntry:
    """One settings-owned YAML field to write during a save."""

    yaml_path: str
    value: Any


@dataclass(frozen=True)
class SettingsSavePlan:
    """A field-granular patch plan for a settings save.

    Produced by :func:`build_settings_save_plan`. Carries exactly the
    settings-owned ``yaml_path`` entries whose submitted value differs from the
    current-config snapshot -- never whole tabs. Persistence consumes
    :attr:`entries` directly so every unedited field (even a sibling field in
    the same tab as the edit) keeps its raw on-disk bytes: a ``${SECRET}``
    reference is not rewritten with its ``Config.load``-expanded value, and
    hand-authored comments, quoting, anchors, and layout survive.

    :attr:`is_empty` is the explicit no-op outcome: when nothing changed the
    caller must skip the file write entirely so an otherwise-unchanged file
    remains byte-for-byte identical without an unnecessary round trip.
    """

    entries: tuple[SettingsSavePatchEntry, ...]

    @property
    def is_empty(self) -> bool:
        """True when no settings-owned field changed (a no-op save)."""
        return not self.entries

    @property
    def changed_yaml_paths(self) -> tuple[str, ...]:
        """The ``yaml_path`` of each field this plan will write (for logging)."""
        return tuple(entry.yaml_path for entry in self.entries)


def build_settings_save_plan(
    tab_definitions: list[dict[str, Any]],
    snapshot: dict[str, BaseModel],
    submitted: dict[str, BaseModel],
) -> SettingsSavePlan:
    """Build the field-granular patch plan for a settings save.

    Settings-save persistence-policy owner. Compares each submitted field value
    against the ``build_tabs_from_config`` snapshot and emits a
    ``(yaml_path, value)`` entry ONLY for fields whose value actually changed,
    with the reverse UI transform applied to those changed entries alone.

    Working at field / ``yaml_path`` granularity -- not whole tabs -- is what
    upholds this issue's invariant. The browser settings form posts *every* tab
    on every save (``settings_form_controls.collectForm`` collects all
    ``[data-tab][data-field]`` controls), and both the snapshot and submitted
    models come from ``from_config`` after ``Config.load`` has expanded every
    ``${VAR}`` string. A whole-tab projection would therefore rewrite unedited
    settings-owned values -- materializing defaults for untouched sections
    (``provider_resilience``, ``sqlite_backup``, ``goal_pilot``, ...) and
    replacing a sibling ``${SECRET}`` reference with its expanded value -- merely
    because one field in that tab changed. Emitting only changed fields keeps the
    persisted set correct regardless of what the client sends, and an empty plan
    (:attr:`SettingsSavePlan.is_empty`) marks a no-op save the caller must not
    write to disk.

    Both mappings hold Pydantic models of the same per-tab classes, so the
    per-field equality check is a structural value comparison in display form.
    """
    entries: list[SettingsSavePatchEntry] = []
    for tab in tab_definitions:
        submitted_model = submitted.get(tab["key"])
        if submitted_model is None:
            continue
        base_model = snapshot.get(tab["key"])
        model_cls = tab["model"]
        for field_name, field_info in model_cls.model_fields.items():
            new_value = getattr(submitted_model, field_name)
            if base_model is not None and new_value == getattr(base_model, field_name):
                continue
            extra = field_info.json_schema_extra
            assert isinstance(extra, dict), f"Missing json_schema_extra on {field_name}"
            yaml_path = extra.get("yaml_path")
            assert yaml_path, f"Missing yaml_path on {field_name}"
            value = reverse_ui_transform(extra.get("ui_transform"), new_value)
            if isinstance(value, Path):
                value = str(value)
            entries.append(SettingsSavePatchEntry(yaml_path=yaml_path, value=value))
    return SettingsSavePlan(entries=tuple(entries))


def collect_restart_fields(tab_definitions: list[dict[str, Any]]) -> set[str]:
    """Return field names that require restart when changed."""
    fields: set[str] = set()
    for tab in tab_definitions:
        model_cls = tab["model"]
        for field_name, field_info in model_cls.model_fields.items():
            extra = field_info.json_schema_extra
            if isinstance(extra, dict) and extra.get("restart_required"):
                fields.add(field_name)
    return fields


def build_settings_json_schema(tab_definitions: list[dict[str, Any]]) -> dict[str, Any]:
    """Generate per-tab JSON schemas for template rendering.

    Returns a dict mapping tab key -> JSON schema dict.
    The schema includes x_extra with section, restart_required, etc., and
    x_control with the form-control classification every renderer/collector
    must dispatch on. Raises UnsupportedSettingsFieldError when a field has
    no faithful form projection (fail-fast - no silent text-input fallback).
    """
    schemas: dict[str, Any] = {}
    for tab in tab_definitions:
        model_cls = tab["model"]
        schema = model_cls.model_json_schema()

        # Pydantic v2 puts json_schema_extra into the property dict.
        # We normalize it into an x_extra key for template access.
        for prop_name, prop in schema.get("properties", {}).items():
            field_info = model_cls.model_fields[prop_name]
            extra = field_info.json_schema_extra
            if isinstance(extra, dict):
                prop["x_extra"] = extra
            prop["x_control"] = classify_form_control(
                f"{tab['key']}.{prop_name}", prop
            )

        schemas[tab["key"]] = schema
    return schemas


def field_meta(tab_definitions: list[dict[str, Any]], tab_key: str, field_name: str) -> dict[str, Any]:
    """Get schema metadata for a specific field.

    Returns dict with 'title', 'description', 'default', and any json_schema_extra.
    """
    for tab in tab_definitions:
        if tab["key"] == tab_key:
            field_info = tab["model"].model_fields[field_name]
            extra = field_info.json_schema_extra or {}
            return {
                "title": field_info.title,
                "description": field_info.description,
                "default": field_info.default,
                **extra,
            }
    raise KeyError(f"Unknown tab '{tab_key}' or field '{field_name}'")


def setup_fields(tab_definitions: list[dict[str, Any]], section: str) -> list[dict[str, Any]]:
    """Get schema fields for a wizard section, sorted by order.

    Returns a list of field metadata dicts with keys:
        name, title, description, default, type, order, prompt, condition, tab_key
    """
    fields: list[dict[str, Any]] = []
    for tab in tab_definitions:
        for field_name, field_info in tab["model"].model_fields.items():
            extra = field_info.json_schema_extra or {}
            setup = extra.get("setup")
            if not setup or not setup.get("enabled"):
                continue
            if setup.get("section") != section:
                continue
            fields.append(
                {
                    "name": field_name,
                    "title": field_info.title,
                    "description": field_info.description,
                    "default": field_info.default,
                    "type": field_info.annotation,
                    "order": setup.get("order", 0),
                    "prompt": setup.get("prompt", field_info.title),
                    "condition": setup.get("condition"),
                    "tab_key": tab["key"],
                    "yaml_path": extra.get("yaml_path", field_name),
                }
            )
    fields.sort(key=lambda f: f["order"])
    return fields


def doctor_check_fields(tab_definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Get all schema fields that have doctor_check annotations.

    Returns a list of field metadata dicts with keys:
        name, doctor_check, doctor_check_condition, doctor_severity, config_attr,
        title, tab_key, ui_transform
    """
    fields: list[dict[str, Any]] = []
    for tab in tab_definitions:
        for field_name, field_info in tab["model"].model_fields.items():
            extra = field_info.json_schema_extra or {}
            doctor_check = extra.get("doctor_check")
            if not doctor_check:
                continue
            fields.append(
                {
                    "name": field_name,
                    "doctor_check": doctor_check,
                    "doctor_check_condition": extra.get("doctor_check_condition"),
                    "doctor_severity": extra.get("doctor_severity", DOCTOR_SEVERITY_ERROR),
                    "config_attr": extra["config_attr"],
                    "title": field_info.title,
                    "tab_key": tab["key"],
                    "ui_transform": extra.get("ui_transform"),
                }
            )
    return fields


def summary_fields(tab_definitions: list[dict[str, Any]], section: str) -> list[dict[str, Any]]:
    """Get schema fields that contribute to a doctor status summary.

    Returns a list of field metadata dicts with summary format info.
    """
    fields: list[dict[str, Any]] = []
    for tab in tab_definitions:
        for field_name, field_info in tab["model"].model_fields.items():
            extra = field_info.json_schema_extra or {}
            summary = extra.get("summary")
            if not summary or summary.get("section") != section:
                continue
            fields.append(
                {
                    "name": field_name,
                    "config_attr": extra["config_attr"],
                    "ui_transform": extra.get("ui_transform"),
                    **summary,
                }
            )
    return fields


def generate_reference_markdown(tab_definitions: list[dict[str, Any]]) -> str:
    """Generate markdown configuration reference from schema.

    Returns a markdown string with tables for each tab.
    """
    lines = ["# Settings Reference", "", "_Auto-generated from settings schema._", ""]
    for tab in tab_definitions:
        lines.append(f"## {tab['label']}")
        lines.append("")
        lines.append("| Field | Type | Default | Description | Examples | Notes |")
        lines.append("|-------|------|---------|-------------|----------|-------|")
        model_cls = tab["model"]
        schema = model_cls.model_json_schema()
        for prop_name, prop in schema.get("properties", {}).items():
            field_info = model_cls.model_fields[prop_name]
            extra = field_info.json_schema_extra or {}
            yaml_path = extra.get("yaml_path", prop_name) if isinstance(extra, dict) else prop_name
            type_str = prop.get("type", "string")
            # Handle anyOf (Optional types).
            if "anyOf" in prop:
                types = [t.get("type", "null") for t in prop["anyOf"]]
                type_str = " | ".join(t for t in types if t != "null")
                if "null" in types:
                    type_str += " (optional)"
            default = prop.get("default", "")
            desc = prop.get("description", "")
            examples = extra.get("doc_examples", [])
            if not isinstance(examples, list):
                examples = [str(examples)]
            examples_str = ", ".join(f"`{e}`" for e in examples if str(e) != "")
            notes = extra.get("doc_notes", "") or ""
            lines.append(
                f"| `{yaml_path}` | {type_str} | `{default}` | {desc} | {examples_str} | {notes} |"
            )
        lines.append("")
    return "\n".join(lines)
