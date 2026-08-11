"""Schema-driven doctor checks.

Reads doctor_check annotations from settings_schema.py and runs checks
generically. Adding a new check = annotating the schema field, not editing
doctor code.

Check types:
- path_exists: field value is a repo-relative path that should exist
- path_is_file: field value is a repo-relative path to a regular file
- first_arg_path_exists: first space-separated arg in a list field is a path
- references_agent: field value must be a key in config.agents (or None)

Summary generation:
- Reads summary annotations to build human-readable status lines like
  "Enabled (auto=30m, retry=on, tests: tests/e2e)"
"""

from pathlib import Path
from typing import Any

from ..types import Check
from ...config import Config
from ...settings_schema import (
    DOCTOR_CHECK_FIRST_ARG_PATH_EXISTS,
    DOCTOR_CHECK_PATH_EXISTS,
    DOCTOR_CHECK_PATH_IS_FILE,
    DOCTOR_CHECK_REFERENCES_AGENT,
    SUMMARY_BOOLEAN_FLAG,
    SUMMARY_ENABLED_FLAG,
    SUMMARY_INTERVAL,
    SUMMARY_KEY_VALUE,
    get_doctor_check_fields,
    get_summary_fields,
)


def _get_nested_attr(obj: Any, path: str) -> Any:
    """Get obj.a.b.c from dotted path 'a.b.c'."""
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _condition_met(config: Config, condition: str | None) -> bool:
    """Check if a doctor_check_condition is satisfied."""
    if condition is None:
        return True
    try:
        return bool(_get_nested_attr(config, condition))
    except AttributeError:
        return False


def _resolve_field_value(config: Config, field: dict[str, Any]) -> Any:
    """Read the config value for a doctor check field."""
    raw = _get_nested_attr(config, field["config_attr"])
    # For list fields displayed as space-separated, the config stores a list
    if field.get("ui_transform") == "space_separated_list" and isinstance(raw, list):
        return raw
    return raw


# ---------------------------------------------------------------------------
# Check handlers
# ---------------------------------------------------------------------------

def _check_path_exists(
    field: dict[str, Any], value: Any, repo_root: Path,
) -> list[Check]:
    """Check that a repo-relative path exists."""
    if not value:
        return []
    path = repo_root / str(value)
    if path.exists():
        return []
    return [Check(
        name=field["title"],
        status=field["doctor_severity"],
        detail=f"Path '{value}' not found (field: {field['name']})",
    )]


def _check_path_is_file(
    field: dict[str, Any], value: Any, repo_root: Path,
) -> list[Check]:
    """Check that a contained repo-relative path identifies a regular file."""
    if not value:
        return []
    repo_root_resolved = repo_root.resolve()
    path = (repo_root_resolved / str(value)).resolve()
    try:
        path.relative_to(repo_root_resolved)
    except ValueError:
        return [Check(
            name=field["title"],
            status=field["doctor_severity"],
            detail=(
                f"File path '{value}' resolves outside the repository "
                f"(field: {field['name']})"
            ),
        )]
    if path.is_file():
        return []
    return [Check(
        name=field["title"],
        status=field["doctor_severity"],
        detail=(
            f"File '{value}' is missing or not a regular file "
            f"(field: {field['name']})"
        ),
    )]


def _check_first_arg_path(
    field: dict[str, Any], value: Any, repo_root: Path,
) -> list[Check]:
    """Check that the first element of a list/space-separated field is a valid path."""
    if not value:
        return []
    # Value is a list (from config) or a string (from schema)
    if isinstance(value, list):
        first_arg = value[0] if value else None
    else:
        first_arg = str(value).split()[0] if str(value).strip() else None
    if not first_arg:
        return []
    path = repo_root / first_arg
    if path.exists():
        return []
    return [Check(
        name=field["title"],
        status=field["doctor_severity"],
        detail=f"Test path '{first_arg}' not found",
    )]


def _check_references_agent(
    field: dict[str, Any], value: Any, config: Config,
) -> list[Check]:
    """Check that a field value references a valid agent key in config.agents."""
    if not value:
        # None/empty means "not configured" — different checks handle "enabled but missing"
        return []
    if value in config.agents:
        return []
    return [Check(
        name=field["title"],
        status=field["doctor_severity"],
        detail=f"'{value}' not in configured agents",
    )]


# Dispatch table: doctor_check string -> handler function
_CHECK_HANDLERS = {
    DOCTOR_CHECK_PATH_EXISTS: _check_path_exists,
    DOCTOR_CHECK_PATH_IS_FILE: _check_path_is_file,
    DOCTOR_CHECK_FIRST_ARG_PATH_EXISTS: _check_first_arg_path,
    DOCTOR_CHECK_REFERENCES_AGENT: _check_references_agent,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_schema_checks(config: Config) -> list[Check]:
    """Run all doctor checks driven by schema field metadata.

    Iterates over all fields with doctor_check annotations, evaluates
    conditions, and dispatches to the appropriate check handler.
    """
    checks: list[Check] = []
    repo_root = config.repo_root

    for field in get_doctor_check_fields():
        # Evaluate condition (e.g., "e2e.enabled" must be truthy)
        if not _condition_met(config, field["doctor_check_condition"]):
            continue

        value = _resolve_field_value(config, field)
        check_type = field["doctor_check"]
        handler = _CHECK_HANDLERS.get(check_type)
        if handler is None:
            checks.append(Check(
                name=field["title"],
                status="error",
                detail=f"Unknown doctor_check type: {check_type}",
            ))
            continue

        # Dispatch — path checks need repo_root, reference checks need config
        if check_type == DOCTOR_CHECK_REFERENCES_AGENT:
            checks.extend(handler(field, value, config))
        else:
            checks.extend(handler(field, value, repo_root))

    return checks


def _format_interval(field: dict[str, Any], label: str, value: Any) -> str:
    if value and value > 0:
        unit = field.get("unit", "")
        return f"{label}={value}{unit}"
    return field.get("zero_label", "disabled")


def _format_key_value(field: dict[str, Any], label: str, value: Any) -> str | None:
    idx = field.get("value_index")
    if idx is not None and isinstance(value, (list, tuple)):
        value = value[idx] if len(value) > idx else None
    return f"{label}: {value}" if value is not None else None


def _format_boolean_flag(field: dict[str, Any], label: str, value: Any) -> str:
    true_val = field.get("true_value", "on")
    false_val = field.get("false_value", "off")
    return f"{label}={true_val if value else false_val}"


_SUMMARY_FORMATTERS = {
    SUMMARY_INTERVAL: _format_interval,
    SUMMARY_KEY_VALUE: _format_key_value,
    SUMMARY_BOOLEAN_FLAG: _format_boolean_flag,
}


def format_summary(section: str, config: Config) -> str | None:
    """Build a human-readable status summary from schema summary annotations.

    Returns a string like "Enabled (auto=30m, retry=on, tests: tests/e2e)"
    or None if the section has no summary fields.
    """
    fields = get_summary_fields(section)
    if not fields:
        return None

    parts: list[str] = []
    for field in fields:
        value = _get_nested_attr(config, field["config_attr"])
        if isinstance(value, list):
            value = value[0] if value else None

        fmt: str | None = field.get("format")
        if fmt is None or fmt == SUMMARY_ENABLED_FLAG:
            continue

        label = field.get("label", field["name"])
        formatter = _SUMMARY_FORMATTERS.get(fmt)
        if formatter:
            result = formatter(field, label, value)
            if result is not None:
                parts.append(result)

    return ", ".join(parts) if parts else None
