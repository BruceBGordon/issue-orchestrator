"""Field-local YAML config persistence."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Protocol

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode
from yaml.tokens import (
    FlowEntryToken,
    FlowMappingEndToken,
    FlowMappingStartToken,
    FlowSequenceEndToken,
    FlowSequenceStartToken,
    ScalarToken as YAMLScalarToken,
)


class ConfigDocumentPatchTarget(Protocol):
    """Config-like object that identifies its source YAML path."""

    config_path: Path | None


class ConfigDocumentPatchEntry(Protocol):
    """One dotted YAML path and value owned by the settings save plan."""

    @property
    def yaml_path(self) -> str:
        """Dotted mapping path to update."""
        ...

    @property
    def value(self) -> Any:
        """Stored YAML value for the path."""
        ...


@dataclass(frozen=True)
class _FlowMappingLayout:
    """Token-derived boundary details for one flow mapping."""

    closing_index: int
    direct_commas: int


def save_config_document_patch(
    config: ConfigDocumentPatchTarget,
    entries: Iterable[ConfigDocumentPatchEntry],
    path: Path | None = None,
) -> Path:
    """Persist settings entries without serializing the surrounding document.

    Existing values are replaced at their parser-reported source spans. Missing
    paths are inserted into the nearest existing mapping. In both cases, bytes
    outside the changed field stay untouched, including comments, indentation,
    quote and collection style, directives, document markers, and final-newline
    choice. The document is parsed without ``${VAR}`` expansion so referenced
    secrets are never materialized.
    """
    save_path = path or config.config_path
    if save_path is None:
        raise ValueError("No path specified and config_path is not set")

    patch_entries = tuple(entries)
    if not patch_entries:
        return save_path

    original = save_path.read_bytes().decode("utf-8") if save_path.exists() else ""
    updated = original
    for entry in patch_entries:
        updated = _patch_yaml_path(updated, entry.yaml_path, entry.value)
    _validate_patched_entries(updated, save_path, patch_entries)
    save_path.write_bytes(updated.encode("utf-8"))
    return save_path


def _patch_yaml_path(text: str, dotted_path: str, value: Any) -> str:
    """Replace or insert one path while retaining every unrelated byte."""
    parts = tuple(part for part in dotted_path.split(".") if part)
    if not parts or len(parts) != len(dotted_path.split(".")):
        raise ValueError(f"Invalid empty component in YAML path {dotted_path!r}")

    root = yaml.compose(text)
    if root is None:
        return _append_to_empty_document(text, parts, value)
    if isinstance(root, ScalarNode) and root.tag == "tag:yaml.org,2002:null":
        return _replace_empty_document(text, root, parts, value)
    if not isinstance(root, MappingNode):
        raise ValueError("Config document root is not a mapping")

    current: Node = root
    for index, part in enumerate(parts):
        if not isinstance(current, MappingNode):
            prefix = ".".join(parts[:index])
            raise ValueError(
                f"Cannot write YAML path {dotted_path!r}: {prefix!r} is not a mapping"
            )
        child = _mapping_child(current, part)
        if child is None:
            return _insert_missing_path(text, current, parts[index:], value)
        if index == len(parts) - 1:
            return _replace_node_source(text, child, value)
        current = child

    raise AssertionError("non-empty YAML path traversal must return")


def _mapping_child(mapping: MappingNode, key: str) -> Node | None:
    """Return one mapping child, failing fast on an ambiguous duplicate key."""
    matches = [
        (key_node, value_node)
        for key_node, value_node in mapping.value
        if isinstance(key_node, ScalarNode) and key_node.value == key
    ]
    if len(matches) > 1:
        raise ValueError(f"Cannot patch duplicate YAML key {key!r}")
    if not matches:
        return None
    key_node, value_node = matches[0]
    if value_node.start_mark.index < key_node.end_mark.index:
        raise ValueError(
            f"Cannot patch YAML alias at key {key!r} without changing its anchor"
        )
    return value_node


def _replace_node_source(text: str, node: Node, value: Any) -> str:
    """Replace exactly one existing value node's source span."""
    start = node.start_mark.index
    end = node.end_mark.index
    old_fragment = text[start:end]
    content_end = len(old_fragment.rstrip("\r\n"))
    trailing_newlines = old_fragment[content_end:]
    property_prefix = _scalar_property_prefix(text, node)
    implicit_null_separator = (
        " " if node.start_mark.index == node.end_mark.index else ""
    )
    replacement = (
        property_prefix
        + implicit_null_separator
        + _render_yaml_value(value)
        + trailing_newlines
    )
    return text[:start] + replacement + text[end:]


def _scalar_property_prefix(text: str, node: Node) -> str:
    """Retain a scalar's tag/anchor properties in either legal order."""
    if not isinstance(node, ScalarNode):
        return ""
    if node.start_mark.index == node.end_mark.index:
        return ""
    for token in yaml.scan(text):
        if (
            isinstance(token, YAMLScalarToken)
            and node.start_mark.index <= token.start_mark.index
            and token.end_mark.index <= node.end_mark.index
        ):
            return text[node.start_mark.index : token.start_mark.index]
    property_prefix = text[node.start_mark.index : node.end_mark.index]
    separator = "" if property_prefix.endswith((" ", "\t", "\r", "\n")) else " "
    return property_prefix + separator


def _insert_missing_path(
    text: str,
    parent: MappingNode,
    missing_parts: tuple[str, ...],
    value: Any,
) -> str:
    """Insert a missing descendant into an existing block or flow mapping."""
    if parent.flow_style:
        return _insert_into_flow_mapping(text, parent, missing_parts, value)

    insertion_index = parent.end_mark.index
    indent = _mapping_indent(parent)
    newline = _line_separator(text)
    rendered = _render_block_path(missing_parts, value, indent, newline)
    prefix = (
        ""
        if insertion_index == 0 or text[:insertion_index].endswith(("\n", "\r"))
        else newline
    )
    suffix = (
        newline if insertion_index < len(text) or text.endswith(("\n", "\r")) else ""
    )
    return text[:insertion_index] + prefix + rendered + suffix + text[insertion_index:]


def _insert_into_flow_mapping(
    text: str,
    parent: MappingNode,
    missing_parts: tuple[str, ...],
    value: Any,
) -> str:
    """Insert a missing descendant at the flow mapping's closing token."""
    closing_index, has_trailing_comma = _flow_mapping_end(text, parent)
    nested_value = value
    for part in reversed(missing_parts[1:]):
        nested_value = {part: nested_value}
    separator = ", " if parent.value and not has_trailing_comma else ""
    addition = (
        separator
        + json.dumps(missing_parts[0], ensure_ascii=False)
        + ": "
        + _render_yaml_value(nested_value)
    )
    return text[:closing_index] + addition + text[closing_index:]


def _flow_mapping_end(text: str, parent: MappingNode) -> tuple[int, bool]:
    """Locate the parent's closing token and detect a direct trailing comma."""
    layout = _scan_flow_mapping_layout(text, parent)
    if layout.closing_index != parent.end_mark.index - 1:
        raise ValueError("Cannot locate closing token for YAML flow mapping")

    entry_count = len(parent.value)
    expected_commas = max(entry_count - 1, 0)
    if layout.direct_commas not in {expected_commas, entry_count}:
        raise ValueError("Unexpected YAML flow mapping separator layout")
    return layout.closing_index, bool(
        entry_count and layout.direct_commas == entry_count
    )


def _scan_flow_mapping_layout(text: str, parent: MappingNode) -> _FlowMappingLayout:
    """Scan collection tokens to find the selected mapping's direct separators."""
    depth = 0
    direct_commas = 0
    started = False

    for token in yaml.scan(text):
        if not started:
            if (
                isinstance(token, FlowMappingStartToken)
                and token.start_mark.index == parent.start_mark.index
            ):
                started = True
                depth = 1
            continue

        if isinstance(token, (FlowMappingStartToken, FlowSequenceStartToken)):
            depth += 1
        elif isinstance(token, (FlowMappingEndToken, FlowSequenceEndToken)):
            if depth == 1:
                if not isinstance(token, FlowMappingEndToken):
                    raise ValueError("Flow mapping ended with a sequence token")
                return _FlowMappingLayout(token.start_mark.index, direct_commas)
            depth -= 1
        elif isinstance(token, FlowEntryToken) and depth == 1:
            direct_commas += 1

    raise ValueError("Cannot locate closing token for YAML flow mapping")


def _append_to_empty_document(
    text: str,
    parts: tuple[str, ...],
    value: Any,
) -> str:
    """Add the first mapping path while retaining a comment-only preamble."""
    newline = _line_separator(text)
    rendered = _render_block_path(parts, value, 0, newline)
    if not text:
        return rendered + newline
    if text.endswith(("\n", "\r")):
        return text + rendered + newline
    return text + newline + rendered


def _replace_empty_document(
    text: str,
    root: ScalarNode,
    parts: tuple[str, ...],
    value: Any,
) -> str:
    """Replace an explicit YAML null document while retaining its framing."""
    newline = _line_separator(text)
    rendered = _render_block_path(parts, value, 0, newline)
    start = root.start_mark.index
    end = root.end_mark.index
    if start == end:
        prefix = "" if start == 0 or text[:start].endswith(("\n", "\r")) else newline
        suffix = newline if end < len(text) or text.endswith(("\n", "\r")) else ""
        return text[:start] + prefix + rendered + suffix + text[end:]
    return text[:start] + rendered + text[end:]


def _mapping_indent(mapping: MappingNode) -> int:
    """Return the indentation column used by a block mapping's children."""
    if not mapping.value:
        raise ValueError("Cannot insert into an empty non-flow YAML mapping")
    return mapping.value[0][0].start_mark.column


def _render_block_path(
    parts: tuple[str, ...],
    value: Any,
    indent: int,
    newline: str,
) -> str:
    """Render a newly inserted nested mapping path."""
    lines: list[str] = []
    for offset, part in enumerate(parts):
        prefix = " " * (indent + 2 * offset) + _render_mapping_key(part) + ":"
        if offset == len(parts) - 1:
            prefix += " " + _render_yaml_value(value)
        lines.append(prefix)
    return newline.join(lines)


def _render_mapping_key(key: str) -> str:
    """Render registry-owned keys plainly when YAML-safe, otherwise quoted."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
        return key
    return json.dumps(key, ensure_ascii=False)


def _render_yaml_value(value: Any) -> str:
    """Render a settings value as the JSON subset of YAML."""
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Settings value is not YAML-serializable: {value!r}") from exc


def _line_separator(text: str) -> str:
    """Keep the document's existing newline convention for inserted paths."""
    return "\r\n" if "\r\n" in text else "\n"


def _validate_patched_entries(
    text: str,
    path: Path,
    entries: tuple[ConfigDocumentPatchEntry, ...],
) -> None:
    """Fail before writing unless every requested path has its requested value."""
    node = yaml.compose(text)
    if not isinstance(node, MappingNode):
        raise ValueError(f"Config document at {path} is not a mapping")
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise ValueError(f"Config document at {path} is not a mapping")

    for entry in entries:
        cursor: Any = document
        for part in entry.yaml_path.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                raise ValueError(
                    f"Config patch did not persist requested path {entry.yaml_path!r}"
                )
            cursor = cursor[part]
        expected = json.loads(_render_yaml_value(entry.value))
        if type(cursor) is not type(expected) or cursor != expected:
            raise ValueError(
                f"Config patch persisted {entry.yaml_path!r} as {cursor!r}, "
                f"expected {expected!r}"
            )
