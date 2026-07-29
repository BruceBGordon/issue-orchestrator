"""Typed GitHub authorization selected during repository setup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

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

    def github_config(self) -> dict[str, Any]:
        """Return the YAML-safe ``repo.github`` mapping for this choice."""
        github = self._auth_config()
        if self.api_url != "https://api.github.com":
            github["api_url"] = self.api_url
        if self.http_timeout_seconds != 20.0:
            github["http_timeout_seconds"] = self.http_timeout_seconds
        return github

    def _auth_config(self) -> dict[str, Any]:
        if self.kind == "personal":
            return _present_values({
                "token": self.token,
                "token_env": self.token_env,
                "keyring_service": self.keyring_service,
                "keyring_username": self.keyring_username,
            })
        if self.kind == "github_app":
            return {"app": self._github_app_config()}
        return {}

    def _github_app_config(self) -> dict[str, str]:
        app = _present_values({
            "client_id": self.app_client_id,
            "app_id": self.app_id,
            "installation_id": self.app_installation_id,
            "private_key_path": self.app_private_key_path,
            "private_key_env": self.app_private_key_env,
        })
        return app

    def auth_kwargs(self) -> dict[str, str | None]:
        """Return keyword arguments accepted by the GitHub auth adapter."""
        return {
            "configured_token": self.token,
            "configured_env": self.token_env,
            "configured_keyring_service": self.keyring_service,
            "configured_keyring_username": self.keyring_username,
            "configured_app_client_id": self.app_client_id,
            "configured_app_id": self.app_id,
            "configured_app_installation_id": self.app_installation_id,
            "configured_app_private_key_path": self.app_private_key_path,
            "configured_app_private_key_env": self.app_private_key_env,
        }


def repository_setup_github_authorization_from_config(
    config: Mapping[str, Any],
) -> RepositorySetupGitHubAuthorization:
    """Parse the auth-relevant portion of a YAML-shaped repository config."""
    repo = config.get("repo")
    github = repo.get("github") if isinstance(repo, Mapping) else None
    if not isinstance(github, Mapping) or not github:
        return RepositorySetupGitHubAuthorization(kind="detected")

    app = github.get("app")
    if isinstance(app, Mapping) and app:
        return RepositorySetupGitHubAuthorization(
            kind="github_app",
            app_client_id=_mapping_text(app, "client_id"),
            app_id=_mapping_text(app, "app_id"),
            app_installation_id=_mapping_text(app, "installation_id"),
            app_private_key_path=_mapping_text(app, "private_key_path"),
            app_private_key_env=_mapping_text(app, "private_key_env"),
            api_url=_mapping_text(github, "api_url") or "https://api.github.com",
            http_timeout_seconds=float(github.get("http_timeout_seconds", 20.0)),
        )

    personal_fields = {
        "token": _mapping_text(github, "token"),
        "token_env": _mapping_text(github, "token_env"),
        "keyring_service": _mapping_text(github, "keyring_service"),
        "keyring_username": _mapping_text(github, "keyring_username"),
    }
    return RepositorySetupGitHubAuthorization(
        kind="personal" if any(personal_fields.values()) else "detected",
        **personal_fields,
        api_url=_mapping_text(github, "api_url") or "https://api.github.com",
        http_timeout_seconds=float(github.get("http_timeout_seconds", 20.0)),
    )


def _mapping_text(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"repo.github.{key} must be a string")
    return value


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


def _present_values(values: Mapping[str, str | None]) -> dict[str, str]:
    return {key: value for key, value in values.items() if value is not None}


__all__ = [
    "GitHubAuthorizationKind",
    "RepositorySetupGitHubAuthorization",
    "repository_setup_github_authorization_from_config",
]
