"""Field-local YAML config persistence."""

from __future__ import annotations

from collections.abc import Iterable
import json
from pathlib import Path
import re
from typing import Any, Protocol

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode


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
    _validate_mapping_document(updated, save_path)
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
    anchor = re.match(r"(&[^\s,\[\]{}]+\s+)", old_fragment)
    anchor_prefix = anchor.group(1) if anchor else ""
    replacement = anchor_prefix + _render_yaml_value(value) + trailing_newlines
    return text[:start] + replacement + text[end:]


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
    """Insert a missing descendant before a flow mapping's closing brace."""
    closing_index = parent.end_mark.index - 1
    if closing_index < 0 or text[closing_index] != "}":
        raise ValueError("Cannot locate closing brace for YAML flow mapping")
    nested_value = value
    for part in reversed(missing_parts[1:]):
        nested_value = {part: nested_value}
    insertion_index = closing_index
    while (
        insertion_index > parent.start_mark.index + 1
        and text[insertion_index - 1] in " \t\r\n"
    ):
        insertion_index -= 1
    inner = text[parent.start_mark.index + 1 : insertion_index]
    separator = "" if not inner.strip() else ", "
    addition = (
        separator
        + json.dumps(missing_parts[0], ensure_ascii=False)
        + ": "
        + _render_yaml_value(nested_value)
    )
    return text[:insertion_index] + addition + text[insertion_index:]


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


def _validate_mapping_document(text: str, path: Path) -> None:
    """Fail before writing if source patching produced an invalid root shape."""
    document = yaml.compose(text)
    if not isinstance(document, MappingNode):
        raise ValueError(f"Config document at {path} is not a mapping")
