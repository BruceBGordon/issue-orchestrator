"""Typed identity for a repository engine configuration selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Self

from .repository_config_name import RepositoryConfigName

_MODE_PATTERN = re.compile(r"[a-z][a-z0-9-]*")


@dataclass(frozen=True, slots=True)
class ConfigurationModeName:
    """A directory-safe, user-visible configuration mode name."""

    value: str

    def __post_init__(self) -> None:
        candidate = self.value
        if (
            type(candidate) is not str
            or candidate != candidate.strip()
            or _MODE_PATTERN.fullmatch(candidate) is None
        ):
            raise ValueError("Invalid configuration mode")

    @classmethod
    def parse(cls, raw: object, *, default: str = "default") -> Self:
        candidate = default if raw in (None, "") else raw
        if not isinstance(candidate, str):
            raise ValueError("Invalid configuration mode")
        return cls(candidate)

    @classmethod
    def default(cls) -> Self:
        return cls("default")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class RepositoryLaunchSelection:
    """The complete configuration identity selected for one engine launch."""

    mode: ConfigurationModeName
    config: RepositoryConfigName

    @classmethod
    def parse(
        cls,
        *,
        mode: object = None,
        config_name: object = None,
    ) -> Self:
        return cls(
            mode=(
                mode
                if isinstance(mode, ConfigurationModeName)
                else ConfigurationModeName.parse(mode)
            ),
            config=(
                config_name
                if isinstance(config_name, RepositoryConfigName)
                else RepositoryConfigName.parse(
                    config_name,
                    default=RepositoryConfigName.default().value,
                )
            ),
        )

    @classmethod
    def default(cls) -> Self:
        return cls(
            mode=ConfigurationModeName.default(),
            config=RepositoryConfigName.default(),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "mode": self.mode.value,
            "config_name": self.config.value,
        }


@dataclass(frozen=True, slots=True)
class RepositoryConfigurationIdentity:
    """A complete runtime configuration identity including effective bytes."""

    selection: RepositoryLaunchSelection
    fingerprint: str

    @classmethod
    def parse(
        cls,
        *,
        mode: object,
        config_name: object,
        fingerprint: object,
    ) -> Self:
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("Invalid configuration fingerprint")
        return cls(
            selection=RepositoryLaunchSelection.parse(
                mode=mode,
                config_name=config_name,
            ),
            fingerprint=fingerprint,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            **self.selection.to_dict(),
            "config_fingerprint": self.fingerprint,
        }

    def require_match(self, active: Self) -> None:
        """Fail when a live engine does not own this exact identity."""
        if active != self:
            raise RuntimeError(
                "Repository Engine configuration identity mismatch: "
                f"requested={self.to_dict()} active={active.to_dict()}"
            )


__all__ = [
    "ConfigurationModeName",
    "RepositoryLaunchSelection",
    "RepositoryConfigurationIdentity",
]
