"""Read-only repository-host authorization inspection port."""

from __future__ import annotations

from typing import Protocol


class RepositoryAuthorizationInspector(Protocol):
    """Authorization facts needed by startup policy checks."""

    @property
    def auth_kind(self) -> str:
        """Return the configured repository authorization mechanism."""
        ...

    def get_token_scopes(self) -> list[str]:
        """Return OAuth token scopes reported by the repository host."""
        ...
