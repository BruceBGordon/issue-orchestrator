"""External representation codec for repository-setup GitHub authorization."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from ..domain.repository_setup_auth import (
    GitHubAuthorizationKind,
    RepositorySetupGitHubAuthorization,
)

_DEFAULT_API_URL = "https://api.github.com"
_DEFAULT_HTTP_TIMEOUT_SECONDS = 20.0


class RepositorySetupGitHubAuthorizationCodecAdapter:
    """Own YAML, browser, redaction, and GitHub-adapter authorization mappings."""

    def from_config(
        self,
        config: Mapping[str, Any],
    ) -> RepositorySetupGitHubAuthorization:
        """Decode the auth-relevant portion of a YAML-shaped config."""
        repo = config.get("repo")
        github = repo.get("github") if isinstance(repo, Mapping) else None
        if not isinstance(github, Mapping) or not github:
            return RepositorySetupGitHubAuthorization(kind="detected")

        transport = self._transport_from(github, prefix="repo.github")
        app = github.get("app")
        if isinstance(app, Mapping) and app:
            return RepositorySetupGitHubAuthorization(
                kind="github_app",
                app_client_id=self._text(app, "client_id", prefix="repo.github.app"),
                app_id=self._text(app, "app_id", prefix="repo.github.app"),
                app_installation_id=self._text(
                    app,
                    "installation_id",
                    prefix="repo.github.app",
                ),
                app_private_key_path=self._text(
                    app,
                    "private_key_path",
                    prefix="repo.github.app",
                ),
                app_private_key_env=self._text(
                    app,
                    "private_key_env",
                    prefix="repo.github.app",
                ),
                **transport,
            )

        personal = {
            "token": self._text(github, "token", prefix="repo.github"),
            "token_env": self._text(github, "token_env", prefix="repo.github"),
            "keyring_service": self._text(
                github,
                "keyring_service",
                prefix="repo.github",
            ),
            "keyring_username": self._text(
                github,
                "keyring_username",
                prefix="repo.github",
            ),
        }
        return RepositorySetupGitHubAuthorization(
            kind="personal" if any(personal.values()) else "detected",
            **personal,
            **transport,
        )

    def to_config(
        self,
        authorization: RepositorySetupGitHubAuthorization,
    ) -> dict[str, Any]:
        """Encode the canonical ``repo.github`` YAML mapping."""
        github: dict[str, Any] = {}
        if authorization.kind == "personal":
            github.update(
                self._present(
                    {
                        "token": authorization.token,
                        "token_env": authorization.token_env,
                        "keyring_service": authorization.keyring_service,
                        "keyring_username": authorization.keyring_username,
                    }
                )
            )
        elif authorization.kind == "github_app":
            github["app"] = self._present(
                {
                    "client_id": authorization.app_client_id,
                    "app_id": authorization.app_id,
                    "installation_id": authorization.app_installation_id,
                    "private_key_path": authorization.app_private_key_path,
                    "private_key_env": authorization.app_private_key_env,
                }
            )

        if authorization.api_url != _DEFAULT_API_URL:
            github["api_url"] = authorization.api_url
        if authorization.http_timeout_seconds != _DEFAULT_HTTP_TIMEOUT_SECONDS:
            github["http_timeout_seconds"] = authorization.http_timeout_seconds
        return github

    def from_public(
        self,
        payload: Mapping[str, Any],
    ) -> RepositorySetupGitHubAuthorization:
        """Decode the canonical browser payload, including setup-only token input."""
        kind = payload.get("kind")
        if kind not in {"detected", "personal", "github_app"}:
            raise ValueError(f"Unsupported GitHub authorization kind: {kind!r}")
        transport = self._transport_from(payload, prefix="github_authorization")
        return RepositorySetupGitHubAuthorization(
            kind=kind,
            token=self._text(payload, "token", prefix="github_authorization"),
            token_env=self._text(payload, "token_env", prefix="github_authorization"),
            keyring_service=self._text(
                payload,
                "keyring_service",
                prefix="github_authorization",
            ),
            keyring_username=self._text(
                payload,
                "keyring_username",
                prefix="github_authorization",
            ),
            app_client_id=self._text(
                payload,
                "app_client_id",
                prefix="github_authorization",
            ),
            app_id=self._text(payload, "app_id", prefix="github_authorization"),
            app_installation_id=self._text(
                payload,
                "app_installation_id",
                prefix="github_authorization",
            ),
            app_private_key_path=self._text(
                payload,
                "app_private_key_path",
                prefix="github_authorization",
            ),
            app_private_key_env=self._text(
                payload,
                "app_private_key_env",
                prefix="github_authorization",
            ),
            **transport,
        )

    def to_public(
        self,
        authorization: RepositorySetupGitHubAuthorization,
        *,
        redact_inline_token: bool = False,
    ) -> dict[str, Any]:
        """Encode a non-secret browser payload, optionally migrating inline tokens."""
        public_authorization = authorization
        if authorization.contains_inline_token:
            if not redact_inline_token:
                raise ValueError(
                    "Browser setup cannot expose repo.github.token; store it in the "
                    "OS keychain or an environment variable first"
                )
            public_authorization = self._without_inline_token(authorization)

        payload: dict[str, Any] = {
            "kind": public_authorization.kind,
            "api_url": public_authorization.api_url,
            "http_timeout_seconds": public_authorization.http_timeout_seconds,
        }
        payload.update(
            self._present(
                {
                    "token_env": public_authorization.token_env,
                    "keyring_service": public_authorization.keyring_service,
                    "keyring_username": public_authorization.keyring_username,
                    "app_client_id": public_authorization.app_client_id,
                    "app_id": public_authorization.app_id,
                    "app_installation_id": (
                        public_authorization.app_installation_id
                    ),
                    "app_private_key_path": public_authorization.app_private_key_path,
                    "app_private_key_env": public_authorization.app_private_key_env,
                }
            )
        )
        return payload

    def adapter_kwargs(
        self,
        authorization: RepositorySetupGitHubAuthorization,
    ) -> dict[str, str | None]:
        """Translate the value object to the GitHub auth adapter call contract."""
        return {
            "configured_token": authorization.token,
            "configured_env": authorization.token_env,
            "configured_keyring_service": authorization.keyring_service,
            "configured_keyring_username": authorization.keyring_username,
            "configured_app_client_id": authorization.app_client_id,
            "configured_app_id": authorization.app_id,
            "configured_app_installation_id": authorization.app_installation_id,
            "configured_app_private_key_path": authorization.app_private_key_path,
            "configured_app_private_key_env": authorization.app_private_key_env,
        }

    @staticmethod
    def _without_inline_token(
        authorization: RepositorySetupGitHubAuthorization,
    ) -> RepositorySetupGitHubAuthorization:
        remaining_personal_source = any(
            (
                authorization.token_env,
                authorization.keyring_service,
                authorization.keyring_username,
            )
        )
        kind: GitHubAuthorizationKind = (
            "personal" if remaining_personal_source else "detected"
        )
        return replace(authorization, kind=kind, token=None)

    @staticmethod
    def _transport_from(
        mapping: Mapping[str, Any],
        *,
        prefix: str,
    ) -> dict[str, Any]:
        api_url = (
            RepositorySetupGitHubAuthorizationCodecAdapter._text(
                mapping,
                "api_url",
                prefix=prefix,
            )
            or _DEFAULT_API_URL
        )
        raw_timeout = mapping.get(
            "http_timeout_seconds",
            _DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            raise ValueError(f"{prefix}.http_timeout_seconds must be a number")
        return {
            "api_url": api_url,
            "http_timeout_seconds": float(raw_timeout),
        }

    @staticmethod
    def _text(
        mapping: Mapping[str, Any],
        key: str,
        *,
        prefix: str,
    ) -> str | None:
        value = mapping.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{prefix}.{key} must be a string")
        return value

    @staticmethod
    def _present(values: Mapping[str, str | None]) -> dict[str, str]:
        return {key: value for key, value in values.items() if value is not None}


repository_setup_github_authorization_codec = (
    RepositorySetupGitHubAuthorizationCodecAdapter()
)


__all__ = [
    "RepositorySetupGitHubAuthorizationCodecAdapter",
    "repository_setup_github_authorization_codec",
]
