"""Patch-preserving YAML config persistence."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap


class ConfigDocumentPatchTarget(Protocol):
    """Config-like object that identifies its source YAML path."""

    config_path: Path | None


def save_config_document_patch(
    config: ConfigDocumentPatchTarget,
    patch: Callable[[dict[str, Any]], Any],
    path: Path | None = None,
) -> Path:
    """Persist config by patching the existing on-disk YAML document.

    This reads the current YAML file, lets ``patch`` mutate the parsed mapping
    in place, and writes it back. Only the keys ``patch`` touches change; every
    unrelated section, key, comment, quote style, anchor, and ordering choice is
    preserved by the round-trip YAML representation. The document is parsed
    without ``${VAR}`` expansion so referenced secrets are never materialized.
    """
    save_path = path or config.config_path
    if save_path is None:
        raise ValueError("No path specified and config_path is not set")

    round_trip_yaml = _build_round_trip_yaml()
    document = _read_yaml_document(save_path, round_trip_yaml)
    patch(document)

    with open(save_path, "w", encoding="utf-8") as f:
        round_trip_yaml.dump(document, f)

    return save_path


def _build_round_trip_yaml() -> YAML:
    """Build the comment- and style-preserving YAML document codec."""
    round_trip_yaml = YAML(typ="rt")
    round_trip_yaml.preserve_quotes = True
    # Avoid wrapping an untouched long scalar merely because another field was
    # edited. Hand-authored line breaks already present in the document remain.
    round_trip_yaml.width = 4096
    return round_trip_yaml


def _read_yaml_document(path: Path, round_trip_yaml: YAML) -> dict[str, Any]:
    """Read a YAML mapping without discarding its round-trip presentation."""
    if not path.exists():
        return CommentedMap()
    with open(path, encoding="utf-8") as f:
        document = round_trip_yaml.load(f)
    if document is None:
        document = CommentedMap()
    if not isinstance(document, dict):
        raise ValueError(f"Config document at {path} is not a mapping")
    return document
