"""Identity helpers for one fully resolved engine configuration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..domain.repository_launch_selection import RepositoryLaunchSelection
from ..domain.runtime_config import RuntimeConfigReference
from .config_paths import (
    require_engine_launch_config_path,
    selection_from_config_path,
)

EXPECTED_CONFIG_FINGERPRINT_ENV = "ISSUE_ORCHESTRATOR_EXPECTED_CONFIG_FINGERPRINT"


class ConfigurationFingerprintMismatch(RuntimeError):
    """Raised when config bytes changed after startup preflight."""



def effective_config_fingerprint(data: dict[str, Any]) -> str:
    """Hash canonical effective YAML after environment and CLI overrides."""
    encoded = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def assert_expected_config_fingerprint(
    observed: str,
    expected: str | None,
) -> None:
    """Fail startup when observed config bytes differ from the preflight snapshot."""
    if expected is not None and observed != expected:
        raise ConfigurationFingerprintMismatch(
            "Configuration changed after preflight: "
            f"expected={expected} observed={observed}"
        )


@dataclass
class ConfigLaunchIdentity:
    """Reusable typed view of the launch identity carried by Config."""

    launch_selection: RepositoryLaunchSelection = field(
        default_factory=RepositoryLaunchSelection.default
    )
    config_fingerprint: str = ""

    @property
    def configuration_mode(self) -> str:
        """Return the selected mode name for diagnostics and child processes."""
        return self.launch_selection.mode.value

    @property
    def config_name(self) -> str:
        """Return the selected config filename."""
        return self.launch_selection.config.value

    def launch_identity_dict(self) -> dict[str, str]:
        """Return public mode/config/fingerprint attribution."""
        return {
            **self.launch_selection.to_dict(),
            "config_fingerprint": self.config_fingerprint,
        }

    def refresh_config_fingerprint(self) -> str:
        """Hash effective dataclass state without identity fields."""
        snapshot = asdict(self)
        for identity_field in ("launch_selection", "config_fingerprint", "config_path"):
            snapshot.pop(identity_field, None)
        self.config_fingerprint = effective_config_fingerprint(snapshot)
        return self.config_fingerprint


class RuntimeConfigReferenceOwner:
    """Construct a verified runtime reference from loaded configuration state."""

    config_path: Path | None
    launch_selection: RepositoryLaunchSelection

    def runtime_config_reference(self) -> RuntimeConfigReference:
        """Validate file existence and storage-derived mode at the infra boundary."""
        if self.config_path is None:
            raise ValueError("Runtime config reference requires config_path")
        config_path = require_engine_launch_config_path(self.config_path)
        if not config_path.is_file():
            raise ValueError(
                f"config_path must point to an existing file: {config_path}"
            )
        path_selection = selection_from_config_path(config_path)
        if path_selection != self.launch_selection:
            raise ValueError("config_path and launch selection must match")
        return RuntimeConfigReference(
            config_path=config_path,
            selection=self.launch_selection,
        )


__all__ = [
    "ConfigurationFingerprintMismatch",
    "ConfigLaunchIdentity",
    "EXPECTED_CONFIG_FINGERPRINT_ENV",
    "RuntimeConfigReferenceOwner",
    "assert_expected_config_fingerprint",
    "effective_config_fingerprint",
]
