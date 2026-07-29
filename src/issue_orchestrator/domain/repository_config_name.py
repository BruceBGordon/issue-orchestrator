"""Validated repository-local configuration file names."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Self


@dataclass(frozen=True, slots=True)
class RepositoryConfigName:
    """A YAML config name that cannot escape the repository config directory."""

    value: str

    @classmethod
    def parse(
        cls,
        raw: object,
        *,
        default: str | None = None,
    ) -> Self:
        """Normalize one config name or raise for empty/path-like input."""
        candidate = default if raw is None else raw
        if (
            not isinstance(candidate, str)
            or not candidate
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
        return cls(normalized)

    @classmethod
    def default(cls) -> Self:
        """Return the canonical default config name."""
        return cls("default.yaml")

    def __str__(self) -> str:
        return self.value


__all__ = ["RepositoryConfigName"]
