"""Runtime identity for the running orchestrator process."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeIdentity:
    """Version/build identity of the running orchestrator code."""

    package_version: str
    source_commit_sha: str | None = None

    @property
    def source_commit_short(self) -> str | None:
        """Return a short source SHA for human-facing displays."""
        if self.source_commit_sha is None:
            return None
        return self.source_commit_sha[:7]
