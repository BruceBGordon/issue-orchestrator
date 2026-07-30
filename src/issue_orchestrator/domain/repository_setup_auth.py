"""Typed GitHub authorization selected during repository setup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

GitHubAuthorizationKind = Literal["detected", "personal", "github_app"]


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


@dataclass(frozen=True, slots=True)
class RepositorySetupGitHubAuthorization:
    """One durable, non-ambiguous GitHub authorization choice.

    ``detected`` intentionally carries no repository-scoped reference and uses
    the normal global resolution chain. ``personal`` pins either an environment
    variable or an OS-keychain entry. ``github_app`` pins an installation and
    a private-key reference. The legacy ``token`` field exists only so the CLI
    can preserve an already-configured inline token; browser setup never accepts
    or returns it.
    """

    kind: GitHubAuthorizationKind
    token: str | None = None
    token_env: str | None = None
    keyring_service: str | None = None
    keyring_username: str | None = None
    app_client_id: str | None = None
    app_id: str | None = None
    app_installation_id: str | None = None
    app_private_key_path: str | None = None
    app_private_key_env: str | None = None
    api_url: str = "https://api.github.com"
    http_timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        for name in _OPTIONAL_TEXT_FIELDS:
            object.__setattr__(self, name, _clean(getattr(self, name)))
        object.__setattr__(self, "api_url", self.api_url.strip())

        if not self.api_url:
            raise ValueError("GitHub API URL is required")
        if self.http_timeout_seconds <= 0:
            raise ValueError("GitHub HTTP timeout must be positive")
        self._validate_kind()

    def _validate_kind(self) -> None:
        personal_values = self._personal_values()
        app_values = self._app_values()
        if self.kind == "detected":
            if any((*personal_values, *app_values)):
                raise ValueError("Detected GitHub auth cannot carry explicit sources")
        elif self.kind == "personal":
            self._validate_personal(personal_values, app_values)
        else:
            self._validate_github_app(personal_values)

    def _personal_values(self) -> tuple[str | None, ...]:
        return (
            self.token,
            self.token_env,
            self.keyring_service,
            self.keyring_username,
        )

    def _app_values(self) -> tuple[str | None, ...]:
        return (
            self.app_client_id,
            self.app_id,
            self.app_installation_id,
            self.app_private_key_path,
            self.app_private_key_env,
        )

    @staticmethod
    def _validate_personal(
        personal_values: tuple[str | None, ...],
        app_values: tuple[str | None, ...],
    ) -> None:
        if any(app_values):
            raise ValueError("Personal GitHub auth cannot carry GitHub App fields")
        if not any(personal_values):
            raise ValueError(
                "Personal GitHub auth requires a token, environment variable, "
                "or keyring reference"
            )

    def _validate_github_app(
        self,
        personal_values: tuple[str | None, ...],
    ) -> None:
        if any(personal_values):
            raise ValueError("GitHub App auth cannot carry personal-token fields")
        if not (self.app_client_id or self.app_id):
            raise ValueError("GitHub App auth requires a client ID or app ID")
        if not self.app_installation_id:
            raise ValueError("GitHub App auth requires an installation ID")
        if bool(self.app_private_key_path) == bool(self.app_private_key_env):
            raise ValueError(
                "GitHub App auth requires exactly one private-key path or "
                "environment variable"
            )

    @property
    def contains_inline_token(self) -> bool:
        """Whether this choice would expose a secret in rendered YAML."""
        return self.token is not None


_OPTIONAL_TEXT_FIELDS = (
    "token",
    "token_env",
    "keyring_service",
    "keyring_username",
    "app_client_id",
    "app_id",
    "app_installation_id",
    "app_private_key_path",
    "app_private_key_env",
)
__all__ = [
    "GitHubAuthorizationKind",
    "RepositorySetupGitHubAuthorization",
]
