"""GitHub authentication modes for API and git transport."""

from __future__ import annotations

import re
import time
from base64 import b64encode
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt

from ... import __version__
from .errors import GitHubAuthError
from .tokens import (
    GitHubAppAuthConfig,
    GitHubAuthSource,
    GitHubTokenProvider,
    StaticGitHubTokenProvider,
    TokenValidationResult,
    _mask_token,
    describe_github_token_sources,
    has_github_app_auth_config,
    resolve_github_token_with_source,
)

_APP_JWT_LIFETIME_SECONDS = 540
_APP_TOKEN_REFRESH_SKEW_SECONDS = 300


def _git_basic_auth_header(token: str) -> str:
    credential = f"x-access-token:{token}".encode("utf-8")
    encoded = b64encode(credential).decode("ascii")
    return f"Authorization: Basic {encoded}"


class GitHubAppInstallationTokenProvider:
    """Mint and cache GitHub App installation access tokens."""

    def __init__(
        self,
        config: GitHubAppAuthConfig,
        *,
        clock: Callable[[], float] = time.time,
        post: Callable[..., httpx.Response] = httpx.post,
    ) -> None:
        self._config = config
        self._clock = clock
        self._post = post
        self._cached_token: str | None = None
        self._expires_at_epoch: float = 0.0

    @property
    def auth_kind(self) -> str:
        return "github_app"

    def get_token(self) -> str:
        if self._cached_token and self._clock() < (
            self._expires_at_epoch - _APP_TOKEN_REFRESH_SKEW_SECONDS
        ):
            return self._cached_token
        return self._refresh()

    def _refresh(self) -> str:
        private_key = self._config.read_private_key()
        now = int(self._clock())
        jwt_payload = {
            "iat": now - 60,
            "exp": now + _APP_JWT_LIFETIME_SECONDS,
            "iss": self._config.jwt_issuer,
        }
        encoded_jwt = jwt.encode(jwt_payload, private_key, algorithm="RS256")
        if isinstance(encoded_jwt, bytes):
            encoded_jwt = encoded_jwt.decode("ascii")

        url = (
            f"{self._config.api_url.rstrip('/')}/app/installations/"
            f"{self._config.installation_id}/access_tokens"
        )
        try:
            response = self._post(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {encoded_jwt}",
                    "User-Agent": f"issue-orchestrator/{__version__}",
                },
                timeout=self._config.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.HTTPError, OSError) as exc:
            raise GitHubAuthError(
                f"Failed to request GitHub App installation token: {exc}"
            ) from exc

        if response.status_code >= 400:
            detail = _summarize_auth_error(response.text)
            suffix = f" - {detail}" if detail else ""
            raise GitHubAuthError(
                "GitHub App installation token request failed: "
                f"HTTP {response.status_code}{suffix}"
            )

        payload = response.json()
        token = payload.get("token") if isinstance(payload, dict) else None
        expires_at = payload.get("expires_at") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise GitHubAuthError(
                "GitHub App installation token response did not include a token."
            )
        if not isinstance(expires_at, str) or not expires_at:
            raise GitHubAuthError(
                "GitHub App installation token response did not include expires_at."
            )
        self._cached_token = token
        self._expires_at_epoch = _parse_github_timestamp(expires_at)
        return token


@dataclass(frozen=True)
class GitHubAuth:
    """Central GitHub authentication object used by HTTP, doctor, and git push."""

    token_provider: GitHubTokenProvider
    source_descriptions: tuple[str, ...]
    api_url: str = "https://api.github.com"
    repo: str | None = None
    enable_git_push_auth: bool = False
    resolved_source: GitHubAuthSource | None = None

    @property
    def auth_kind(self) -> str:
        return self.token_provider.auth_kind

    def authorization_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token_provider.get_token()}"}

    def describe_sources(self) -> list[str]:
        return list(self.source_descriptions)

    def git_env_overrides(self, *, remote: str) -> dict[str, str] | None:
        if not self.enable_git_push_auth:
            return None
        if not self.repo:
            raise GitHubAuthError("Authenticated git push requires a configured repo.")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", remote):
            raise GitHubAuthError(
                f"Invalid git remote name for authenticated push: {remote!r}"
            )
        base_url = _github_git_base_url(self.api_url)
        remote_url = f"{base_url}{self.repo}.git"
        return {
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": f"http.{base_url}.extraheader",
            "GIT_CONFIG_VALUE_0": _git_basic_auth_header(
                self.token_provider.get_token()
            ),
            "GIT_CONFIG_KEY_1": f"remote.{remote}.url",
            "GIT_CONFIG_VALUE_1": remote_url,
            "GIT_CONFIG_KEY_2": f"remote.{remote}.pushurl",
            "GIT_CONFIG_VALUE_2": remote_url,
        }

    def validate(
        self,
        *,
        repo: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> TokenValidationResult:
        base_url = self.api_url.rstrip("/")
        try:
            headers = self.authorization_headers()
            if self.auth_kind == "github_app":
                return self._validate_installation_token(
                    base_url=base_url,
                    headers=headers,
                    repo=repo,
                    timeout_seconds=timeout_seconds,
                )
            return self._validate_personal_token(
                base_url=base_url,
                headers=headers,
                repo=repo,
                timeout_seconds=timeout_seconds,
            )
        except GitHubAuthError as exc:
            return TokenValidationResult(valid=False, error=str(exc))
        except Exception as exc:
            return TokenValidationResult(valid=False, error=str(exc))

    def _validate_installation_token(
        self,
        *,
        base_url: str,
        headers: dict[str, str],
        repo: str | None,
        timeout_seconds: float,
    ) -> TokenValidationResult:
        username = self._installation_identity()
        if repo:
            repo_resp = httpx.get(
                f"{base_url}/repos/{repo}",
                headers=headers,
                timeout=timeout_seconds,
            )
            if repo_resp.status_code == 200:
                return TokenValidationResult(valid=True, username=username)
            return TokenValidationResult(
                valid=False,
                error=(
                    f"GitHub App installation cannot access repo {repo} "
                    f"(HTTP {repo_resp.status_code})"
                ),
            )
        installations_resp = httpx.get(
            f"{base_url}/installation/repositories",
            headers=headers,
            timeout=timeout_seconds,
        )
        if installations_resp.status_code == 200:
            return TokenValidationResult(valid=True, username=username)
        return TokenValidationResult(
            valid=False,
            error=(
                "GitHub App installation token invalid "
                f"(HTTP {installations_resp.status_code})"
            ),
        )

    def _validate_personal_token(
        self,
        *,
        base_url: str,
        headers: dict[str, str],
        repo: str | None,
        timeout_seconds: float,
    ) -> TokenValidationResult:
        resp = httpx.get(
            f"{base_url}/user",
            headers=headers,
            timeout=timeout_seconds,
        )
        if resp.status_code != 200:
            return TokenValidationResult(
                valid=False,
                error=f"Token invalid (HTTP {resp.status_code})",
            )
        user_info = resp.json()
        username = user_info.get("login")
        if repo:
            repo_resp = httpx.get(
                f"{base_url}/repos/{repo}",
                headers=headers,
                timeout=timeout_seconds,
            )
            if repo_resp.status_code != 200:
                return TokenValidationResult(
                    valid=False,
                    username=username,
                    error=(
                        f"Token cannot access repo {repo} "
                        f"(HTTP {repo_resp.status_code})"
                    ),
                )
        return TokenValidationResult(valid=True, username=username)

    def _installation_identity(self) -> str:
        for source in self.source_descriptions:
            match = re.search(r"GitHub App installation ([^ ]+)", source)
            if match:
                return f"GitHub App installation {match.group(1)}"
        return "GitHub App installation"


def build_github_auth(
    *,
    configured_token: str | None = None,
    configured_env: str | None = None,
    configured_keyring_service: str | None = None,
    configured_keyring_username: str | None = None,
    configured_app_client_id: str | None = None,
    configured_app_id: str | None = None,
    configured_app_installation_id: str | None = None,
    configured_app_private_key_path: str | None = None,
    configured_app_private_key_env: str | None = None,
    repo: str | None = None,
    api_url: str = "https://api.github.com",
    timeout_seconds: float = 20.0,
) -> GitHubAuth:
    """Build the configured GitHub auth object."""
    if has_github_app_auth_config(
        configured_app_client_id=configured_app_client_id,
        configured_app_id=configured_app_id,
        configured_app_installation_id=configured_app_installation_id,
        configured_app_private_key_path=configured_app_private_key_path,
        configured_app_private_key_env=configured_app_private_key_env,
    ):
        app_config = GitHubAppAuthConfig.from_values(
            client_id=configured_app_client_id,
            app_id=configured_app_id,
            installation_id=configured_app_installation_id,
            private_key_path=configured_app_private_key_path,
            private_key_env=configured_app_private_key_env,
            api_url=api_url,
            timeout_seconds=timeout_seconds,
        )
        return GitHubAuth(
            token_provider=GitHubAppInstallationTokenProvider(app_config),
            source_descriptions=(app_config.describe_source(),),
            api_url=api_url,
            repo=repo,
            enable_git_push_auth=True,
            resolved_source=GitHubAuthSource(
                kind="github_app",
                description=app_config.describe_source(),
            ),
        )

    resolution = resolve_github_token_with_source(
        configured_token=configured_token,
        configured_env=configured_env,
        configured_keyring_service=configured_keyring_service,
        configured_keyring_username=configured_keyring_username,
        api_url=api_url,
    )
    source_descriptions: list[str] = []
    if configured_token:
        source_descriptions.append(f"configured token: {_mask_token(configured_token)}")
    else:
        source_descriptions.extend(
            describe_github_token_sources(
                configured_env=configured_env,
                configured_keyring_service=configured_keyring_service,
                configured_keyring_username=configured_keyring_username,
                api_url=api_url,
            )
        )
    return GitHubAuth(
        token_provider=StaticGitHubTokenProvider(token=resolution.token),
        source_descriptions=tuple(source_descriptions),
        api_url=api_url,
        repo=repo,
        enable_git_push_auth=False,
        resolved_source=resolution.source,
    )


def build_github_token_provider(
    **kwargs: Any,
) -> GitHubTokenProvider:
    """Build only the token provider for legacy callers."""
    return build_github_auth(**kwargs).token_provider


def _github_git_base_url(api_url: str) -> str:
    parsed = urlparse(api_url)
    scheme = parsed.scheme or "https"
    host = parsed.hostname or "api.github.com"
    if host == "api.github.com":
        host = "github.com"
    return f"{scheme}://{host}/"


def _parse_github_timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitHubAuthError(
            f"GitHub App installation token had invalid expires_at {value!r}."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _summarize_auth_error(response_text: str, max_len: int = 280) -> str:
    return response_text.strip().replace("\n", " ")[:max_len]


__all__ = [
    "GitHubAppInstallationTokenProvider",
    "GitHubAuth",
    "build_github_auth",
    "build_github_token_provider",
]
