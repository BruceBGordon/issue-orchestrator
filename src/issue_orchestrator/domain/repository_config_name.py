"""Validated repository-local configuration file names."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Self


def _require_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid config_name")
    return value


@dataclass(frozen=True, slots=True)
class RepositoryConfigName:
    """A YAML config name that cannot escape the repository config directory."""

    value: str

    def __post_init__(self) -> None:
        """Normalize and enforce the invariant for every construction path."""
        candidate = _require_string(self.value)
        if (
            not candidate
            or candidate != candidate.strip()
            or "/" in candidate
            or "\\" in candidate
        ):
            raise ValueError("Invalid config_name")

        normalized = (
            candidate if candidate.endswith(".yaml") else f"{candidate}.yaml"
        )
        config_path = Path(normalized)
        if (
            config_path.is_absolute()
            or config_path.name != normalized
            or normalized == ".yaml"
        ):
            raise ValueError("Invalid config_name")
        object.__setattr__(self, "value", normalized)

    @classmethod
    def parse(
        cls,
        raw: object,
        *,
        default: str | None = None,
    ) -> Self:
        """Normalize one config name or raise for empty/path-like input."""
        candidate = default if raw is None else raw
        return cls(_require_string(candidate))

    @classmethod
    def default(cls) -> Self:
        """Return the canonical default config name."""
        return cls("default.yaml")

    def __str__(self) -> str:
        return self.value


__all__ = ["RepositoryConfigName"]
