"""GitHub token resolution, validation, and keyring storage."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from base64 import b64decode
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse

import yaml

from .errors import GitHubAuthError

logger = logging.getLogger(__name__)


# Keyring service/username constants for token storage
KEYRING_SERVICE = "issue-orchestrator"
KEYRING_USERNAME = "github-token"
_GO_KEYRING_B64_PREFIX = "go-keyring-base64:"


class GitHubTokenProvider(Protocol):
    """Supplies GitHub bearer tokens for HTTP and git transport auth."""

    @property
    def auth_kind(self) -> str:
        """Return a stable identifier for the auth mode."""
        ...

    def get_token(self) -> str:
        """Return a current GitHub bearer token."""
        ...


@dataclass(frozen=True)
class StaticGitHubTokenProvider:
    """Token provider for personal access tokens and other static tokens."""

    token: str

    @property
    def auth_kind(self) -> str:
        return "token"

    def get_token(self) -> str:
        return self.token


@dataclass(frozen=True, slots=True)
class GitHubAuthSource:
    """Non-secret identity of the credential source selected by resolution."""

    kind: Literal["configured", "environment", "github_cli", "keyring", "github_app"]
    description: str
    environment_variable: str | None = None
    keyring_service: str | None = None
    keyring_username: str | None = None


@dataclass(frozen=True, slots=True)
class GitHubTokenResolution:
    """Resolved personal token plus its separately safe source metadata."""

    token: str
    source: GitHubAuthSource


@dataclass(frozen=True)
class GitHubAppAuthConfig:
    """GitHub App installation auth settings.

    The private key may come from a file or an environment variable. This value
    object deliberately does not perform HTTP; the GitHub HTTP adapter owns the
    installation-token exchange.
    """

    client_id: str | None
    app_id: str | None
    installation_id: str
    private_key_path: str | None
    private_key_env: str | None
    api_url: str = "https://api.github.com"
    timeout_seconds: float = 20.0

    @classmethod
    def from_values(
        cls,
        *,
        client_id: str | None = None,
        app_id: str | None = None,
        installation_id: str | None = None,
        private_key_path: str | None = None,
        private_key_env: str | None = None,
        api_url: str = "https://api.github.com",
        timeout_seconds: float = 20.0,
    ) -> "GitHubAppAuthConfig":
        normalized_client_id = _normalize_optional_text(client_id)
        normalized_app_id = _normalize_optional_text(app_id)
        normalized_installation_id = _normalize_optional_text(installation_id)
        normalized_private_key_path = _normalize_optional_text(private_key_path)
        normalized_private_key_env = _normalize_optional_text(private_key_env)
        if not (normalized_client_id or normalized_app_id):
            raise GitHubAuthError(
                "GitHub App auth requires repo.github.app.client_id or app_id."
            )
        if not normalized_installation_id:
            raise GitHubAuthError(
                "GitHub App auth requires repo.github.app.installation_id."
            )
        if not (normalized_private_key_path or normalized_private_key_env):
            raise GitHubAuthError(
                "GitHub App auth requires repo.github.app.private_key_path "
                "or private_key_env."
            )
        return cls(
            client_id=normalized_client_id,
            app_id=normalized_app_id,
            installation_id=normalized_installation_id,
            private_key_path=normalized_private_key_path,
            private_key_env=normalized_private_key_env,
            api_url=api_url,
            timeout_seconds=timeout_seconds,
        )

    @property
    def jwt_issuer(self) -> str:
        """Issuer for GitHub App JWTs. GitHub recommends Client ID when present."""
        return self.client_id or self.app_id or ""

    def read_private_key(self) -> str:
        """Read the configured private key without logging its contents."""
        if self.private_key_env:
            value = os.environ.get(self.private_key_env)
            if not value:
                raise GitHubAuthError(
                    f"GitHub App private key env var {self.private_key_env} is not set."
                )
            return value
        if not self.private_key_path:
            raise GitHubAuthError(
                "GitHub App auth requires private_key_path or private_key_env."
            )
        path = Path(self.private_key_path).expanduser()
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GitHubAuthError(
                f"Could not read GitHub App private key at {path}: {exc}"
            ) from exc

    def describe_source(self) -> str:
        issuer = (
            f"client_id {self.client_id}" if self.client_id else f"app_id {self.app_id}"
        )
        key_source = (
            f"env:{self.private_key_env}"
            if self.private_key_env
            else f"path:{self.private_key_path}"
        )
        return (
            "GitHub App installation "
            f"{self.installation_id} ({issuer}, private key {key_source})"
        )


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def has_github_app_auth_config(
    *,
    configured_app_client_id: str | None = None,
    configured_app_id: str | None = None,
    configured_app_installation_id: str | None = None,
    configured_app_private_key_path: str | None = None,
    configured_app_private_key_env: str | None = None,
) -> bool:
    """Return whether any GitHub App auth field was configured."""
    return any(
        _normalize_optional_text(value)
        for value in (
            configured_app_client_id,
            configured_app_id,
            configured_app_installation_id,
            configured_app_private_key_path,
            configured_app_private_key_env,
        )
    )


def resolve_github_token(
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
    api_url: str = "https://api.github.com",
) -> str:
    """Resolve GitHub token from multiple sources.

    Priority order (per ADR-0014):
    1. Explicitly configured token (from config file)
    2. Repo-specific auth sources declared in config:
       - Custom env var (repo.github.token_env)
       - Custom keyring entry (repo.github.keyring_service / keyring_username)
    3. ISSUE_ORCH_GITHUB_TOKEN env var (primary)
    4. GITHUB_TOKEN env var (fallback)
    5. GH_TOKEN env var (fallback)
    6. GitHub CLI hosts.yml auth
    7. Default OS keychain entry created by issue-orchestrator auth store

    When a repo declares repo-specific auth sources, those sources are
    authoritative. Missing repo-scoped auth is treated as an error instead of
    silently falling back to a different token that may not have access to the
    configured repository.
    """
    return resolve_github_token_with_source(
        configured_token=configured_token,
        configured_env=configured_env,
        configured_keyring_service=configured_keyring_service,
        configured_keyring_username=configured_keyring_username,
        configured_app_client_id=configured_app_client_id,
        configured_app_id=configured_app_id,
        configured_app_installation_id=configured_app_installation_id,
        configured_app_private_key_path=configured_app_private_key_path,
        configured_app_private_key_env=configured_app_private_key_env,
        api_url=api_url,
    ).token


def resolve_github_token_with_source(
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
    api_url: str = "https://api.github.com",
) -> GitHubTokenResolution:
    """Resolve a personal token and identify the exact non-secret source used."""
    _reject_github_app_token_resolution(
        configured_app_client_id=configured_app_client_id,
        configured_app_id=configured_app_id,
        configured_app_installation_id=configured_app_installation_id,
        configured_app_private_key_path=configured_app_private_key_path,
        configured_app_private_key_env=configured_app_private_key_env,
    )

    if configured_token:
        return GitHubTokenResolution(
            token=configured_token,
            source=GitHubAuthSource(
                kind="configured",
                description="Inline token from repo.github.token",
            ),
        )

    configured_resolution = _resolve_configured_personal_source(
        configured_env=configured_env,
        configured_keyring_service=configured_keyring_service,
        configured_keyring_username=configured_keyring_username,
    )
    if configured_resolution is not None:
        return configured_resolution

    return _resolve_default_personal_source(api_url=api_url)


def _reject_github_app_token_resolution(
    *,
    configured_app_client_id: str | None,
    configured_app_id: str | None,
    configured_app_installation_id: str | None,
    configured_app_private_key_path: str | None,
    configured_app_private_key_env: str | None,
) -> None:
    if not has_github_app_auth_config(
        configured_app_client_id=configured_app_client_id,
        configured_app_id=configured_app_id,
        configured_app_installation_id=configured_app_installation_id,
        configured_app_private_key_path=configured_app_private_key_path,
        configured_app_private_key_env=configured_app_private_key_env,
    ):
        return
    raise GitHubAuthError(
        "GitHub App auth is configured; use the GitHub HTTP token provider "
        "to mint an installation token."
    )


def _resolve_configured_personal_source(
    *,
    configured_env: str | None,
    configured_keyring_service: str | None,
    configured_keyring_username: str | None,
) -> GitHubTokenResolution | None:
    if not any(
        (configured_env, configured_keyring_service, configured_keyring_username)
    ):
        return None

    if configured_env and (token := os.environ.get(configured_env)):
        return GitHubTokenResolution(
            token=token,
            source=GitHubAuthSource(
                kind="environment",
                description=f"Environment variable {configured_env}",
                environment_variable=configured_env,
            ),
        )

    service = configured_keyring_service or KEYRING_SERVICE
    username = configured_keyring_username or KEYRING_USERNAME
    if (configured_keyring_service or configured_keyring_username) and (
        token := _read_keyring_token(service=service, username=username)
    ):
        return GitHubTokenResolution(
            token=token,
            source=GitHubAuthSource(
                kind="keyring",
                description=f"Keyring ({service}/{username})",
                keyring_service=service,
                keyring_username=username,
            ),
        )

    expected_sources = []
    if configured_env:
        expected_sources.append(f"env:{configured_env}")
    if configured_keyring_service or configured_keyring_username:
        expected_sources.append(f"keyring:{service}/{username}")
    raise GitHubAuthError(
        "GitHub token not configured for repo-specific auth. "
        f"Checked {', '.join(expected_sources)}."
    )


def _resolve_default_personal_source(*, api_url: str) -> GitHubTokenResolution:
    """Resolve the configured global source chain from ADR-0014."""
    for env_name in ("ISSUE_ORCH_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(env_name)
        if token:
            return GitHubTokenResolution(
                token=token,
                source=GitHubAuthSource(
                    kind="environment",
                    description=f"Environment variable {env_name}",
                    environment_variable=env_name,
                ),
            )
    host = _github_host_for_api_url(api_url)
    token = _read_gh_cli_token(host=host)
    if token:
        return GitHubTokenResolution(
            token=token,
            source=GitHubAuthSource(
                kind="github_cli",
                description=f"GitHub CLI auth ({host})",
            ),
        )
    # Optional keychain via keyring library
    token = _read_keyring_token(service=KEYRING_SERVICE, username=KEYRING_USERNAME)
    if token:
        return GitHubTokenResolution(
            token=token,
            source=GitHubAuthSource(
                kind="keyring",
                description=f"Keyring ({KEYRING_SERVICE}/{KEYRING_USERNAME})",
                keyring_service=KEYRING_SERVICE,
                keyring_username=KEYRING_USERNAME,
            ),
        )
    raise GitHubAuthError(
        "GitHub token not configured. Set ISSUE_ORCH_GITHUB_TOKEN or run: "
        "issue-orchestrator auth store"
    )


@dataclass
class TokenValidationResult:
    """Result of validating a GitHub token."""

    valid: bool
    username: str | None = None
    error: str | None = None


def _mask_token(token: str) -> str:
    """Mask a token for logs and diagnostics."""
    return token[:4] + "..." + token[-4:] if len(token) > 12 else "***"


def describe_github_token_sources(
    *,
    configured_env: str | None = None,
    configured_keyring_service: str | None = None,
    configured_keyring_username: str | None = None,
    configured_app_client_id: str | None = None,
    configured_app_id: str | None = None,
    configured_app_installation_id: str | None = None,
    configured_app_private_key_path: str | None = None,
    configured_app_private_key_env: str | None = None,
    api_url: str = "https://api.github.com",
) -> list[str]:
    """Describe visible token sources for diagnostics.

    Repo-scoped auth sources are authoritative. When a repo declares a custom
    env var or keyring entry, diagnostics only report those sources and do not
    surface unrelated generic fallback tokens.
    """
    app_sources = _describe_github_app_sources(
        configured_app_client_id=configured_app_client_id,
        configured_app_id=configured_app_id,
        configured_app_installation_id=configured_app_installation_id,
        configured_app_private_key_path=configured_app_private_key_path,
        configured_app_private_key_env=configured_app_private_key_env,
        api_url=api_url,
    )
    if app_sources is not None:
        return app_sources

    repo_scoped_auth = any(
        (configured_env, configured_keyring_service, configured_keyring_username)
    )
    token_sources = _describe_repo_scoped_token_sources(
        configured_env=configured_env,
        configured_keyring_service=configured_keyring_service,
        configured_keyring_username=configured_keyring_username,
    )
    if repo_scoped_auth:
        return token_sources
    token_sources.extend(_describe_default_token_sources(api_url=api_url))
    return token_sources


def _describe_repo_scoped_token_sources(
    *,
    configured_env: str | None,
    configured_keyring_service: str | None,
    configured_keyring_username: str | None,
) -> list[str]:
    token_sources: list[str] = []
    if configured_env:
        value = os.environ.get(configured_env)
        if value:
            token_sources.append(f"{configured_env}: {_mask_token(value)}")
    if configured_keyring_service or configured_keyring_username:
        keyring_service = configured_keyring_service or KEYRING_SERVICE
        keyring_username = configured_keyring_username or KEYRING_USERNAME
        value = _read_keyring_token(service=keyring_service, username=keyring_username)
        if value:
            token_sources.append(
                f"Keyring ({keyring_service}/{keyring_username}): {_mask_token(value)}"
            )
    return token_sources


def _describe_default_token_sources(*, api_url: str) -> list[str]:
    token_sources: list[str] = []
    for env_name in ("ISSUE_ORCH_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(env_name)
        if value:
            token_sources.append(f"{env_name}: {_mask_token(value)}")
    gh_cli_token = _read_gh_cli_token(host=_github_host_for_api_url(api_url))
    if gh_cli_token:
        token_sources.append(
            f"GitHub CLI auth ({_github_host_for_api_url(api_url)}): {_mask_token(gh_cli_token)}"
        )
    value = _read_keyring_token(service=KEYRING_SERVICE, username=KEYRING_USERNAME)
    if value:
        token_sources.append(
            f"Keyring ({KEYRING_SERVICE}/{KEYRING_USERNAME}): {_mask_token(value)}"
        )
    return token_sources


def _describe_github_app_sources(
    *,
    configured_app_client_id: str | None,
    configured_app_id: str | None,
    configured_app_installation_id: str | None,
    configured_app_private_key_path: str | None,
    configured_app_private_key_env: str | None,
    api_url: str,
) -> list[str] | None:
    if not has_github_app_auth_config(
        configured_app_client_id=configured_app_client_id,
        configured_app_id=configured_app_id,
        configured_app_installation_id=configured_app_installation_id,
        configured_app_private_key_path=configured_app_private_key_path,
        configured_app_private_key_env=configured_app_private_key_env,
    ):
        return None
    try:
        app_config = GitHubAppAuthConfig.from_values(
            client_id=configured_app_client_id,
            app_id=configured_app_id,
            installation_id=configured_app_installation_id,
            private_key_path=configured_app_private_key_path,
            private_key_env=configured_app_private_key_env,
            api_url=api_url,
        )
    except GitHubAuthError:
        return []
    return [app_config.describe_source()]


def _github_host_for_api_url(api_url: str) -> str:
    """Map an API base URL to the corresponding GitHub host entry in hosts.yml."""
    hostname = urlparse(api_url).hostname or "api.github.com"
    if hostname == "api.github.com":
        return "github.com"
    return hostname


def _gh_hosts_paths() -> list[Path]:
    """Return candidate GitHub CLI hosts.yml locations in lookup order."""
    candidates: list[Path] = []
    seen: set[Path] = set()

    def _append(path: Path | None) -> None:
        if path is None:
            return
        normalized = path.expanduser()
        if normalized in seen:
            return
        seen.add(normalized)
        candidates.append(normalized)

    gh_config_dir = os.environ.get("GH_CONFIG_DIR")
    if gh_config_dir:
        _append(Path(gh_config_dir) / "hosts.yml")
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        _append(Path(xdg_config_home) / "gh" / "hosts.yml")
    _append(Path.home() / ".config" / "gh" / "hosts.yml")
    appdata = os.environ.get("APPDATA")
    if appdata:
        _append(Path(appdata) / "GitHub CLI" / "hosts.yml")
    _append(Path("/etc/gh/hosts.yml"))
    return candidates


def _read_gh_hosts_record(*, host: str) -> dict[str, object] | None:
    """Read a single host record from GitHub CLI's hosts.yml if available."""
    for hosts_path in _gh_hosts_paths():
        try:
            if not hosts_path.exists():
                continue
            raw_text = hosts_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug(
                "Could not read GitHub CLI hosts.yml at %s: %s", hosts_path, exc
            )
            continue
        try:
            payload = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            logger.warning(
                "Ignoring malformed GitHub CLI hosts.yml at %s: %s",
                hosts_path,
                exc,
            )
            continue
        if not isinstance(payload, dict):
            continue
        host_data = payload.get(host)
        if not isinstance(host_data, dict):
            continue
        return host_data
    return None


def _gh_cli_account_for_host(record: dict[str, object]) -> str | None:
    """Return the GitHub CLI account name for a host record."""
    user = record.get("user")
    if isinstance(user, str) and user.strip():
        return user.strip()
    users = record.get("users")
    if isinstance(users, dict):
        for account_name in users:
            if isinstance(account_name, str) and account_name.strip():
                return account_name.strip()
    return None


def _read_gh_cli_token(*, host: str) -> str | None:
    """Read GitHub CLI auth from hosts.yml or its paired keychain entry."""
    record = _read_gh_hosts_record(host=host)
    if record is None:
        return None
    token = record.get("oauth_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    account = _gh_cli_account_for_host(record)
    if account:
        return _read_keyring_token(service=f"gh:{host}", username=account)
    return None


def _resolve_repo_scoped_github_token(
    *,
    configured_env: str | None,
    configured_keyring_service: str | None,
    configured_keyring_username: str | None,
) -> str:
    """Resolve a token from repo-configured auth sources only."""
    return resolve_github_token_with_source(
        configured_env=configured_env,
        configured_keyring_service=configured_keyring_service,
        configured_keyring_username=configured_keyring_username,
    ).token


def _read_keyring_token(
    *,
    service: str = KEYRING_SERVICE,
    username: str = KEYRING_USERNAME,
) -> str | None:
    """Read GitHub token from OS keychain via keyring library.

    Uses the cross-platform keyring library which supports:
    - macOS Keychain
    - Windows Credential Locker
    - Linux Secret Service (GNOME Keyring, KWallet)

    Returns None if keyring is not available or no token is stored.
    """
    token: str | None = None
    try:
        import keyring
    except ImportError:
        keyring = None
    if keyring is not None:
        try:
            token = keyring.get_password(service, username)
            if token:
                return _normalize_keyring_secret(token)
        except Exception as exc:  # noqa: BLE001
            # Keyring can fail for a variety of legitimate reasons — no
            # backend on headless Linux, a locked keyring, a remote
            # daemon that timed out. We still have to fall through to
            # the macOS ``security`` CLI fallback for those cases, so
            # catch broadly here but log at DEBUG so a misconfigured
            # keyring is not totally invisible when someone is
            # troubleshooting auth.
            logger.debug(
                "keyring.get_password failed for service=%s user=%s: %s",
                service,
                username,
                exc,
            )
    return _read_macos_security_keychain_token(service=service, username=username)


def _read_macos_security_keychain_token(*, service: str, username: str) -> str | None:
    """Read a generic password directly from the macOS login keychain.

    SECURITY NOTE: this is a command-injection boundary. ``service`` and
    ``username`` flow into the argv of the ``security`` binary, which is
    safe because we pass a fixed ``list`` to ``subprocess.run`` (no
    ``shell=True``, no string interpolation). Do NOT switch this call
    to ``shell=True`` or build the command with an f-string — that
    would let an attacker-controlled service name inject arbitrary
    shell metacharacters.
    """
    if sys.platform != "darwin":
        return None
    if not shutil.which("security"):
        return None
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                username,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Keep this catch narrow — an unknown exception here may be a
        # signal of tampering or a broken environment and should not be
        # silently swallowed without a trace. DEBUG so it only surfaces
        # under active troubleshooting.
        logger.debug(
            "macOS security find-generic-password failed for service=%s user=%s: %s",
            service,
            username,
            exc,
        )
        return None
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    normalized = _normalize_keyring_secret(token)
    return normalized or None


def _normalize_keyring_secret(secret: str) -> str:
    """Normalize secrets read from OS keychains.

    GitHub CLI stores secure-storage tokens through go-keyring using the
    ``go-keyring-base64:<base64-token>`` envelope. issue-orchestrator's own
    keyring entries are stored as raw tokens, so only unwrap when the prefix is
    present.
    """
    if not secret.startswith(_GO_KEYRING_B64_PREFIX):
        return secret
    encoded = secret.removeprefix(_GO_KEYRING_B64_PREFIX)
    try:
        decoded = b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
    except Exception:
        return secret
    return decoded or secret


def store_keyring_token(token: str) -> None:
    """Store GitHub token in OS keychain via keyring library.

    Args:
        token: The GitHub token to store

    Raises:
        ImportError: If keyring library is not installed
        Exception: If keyring storage fails
    """
    import keyring

    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, token)


def store_keyring_token_for(
    token: str,
    *,
    service: str,
    username: str,
) -> None:
    """Store a token at one explicit repo-scoped keychain reference."""
    if not token.strip():
        raise ValueError("GitHub token is required")
    if not service.strip() or not username.strip():
        raise ValueError("GitHub keyring service and username are required")
    import keyring

    keyring.set_password(service, username, token)


def clear_keyring_token() -> bool:
    """Remove GitHub token from OS keychain.

    Returns:
        True if token was deleted, False if no token was stored
    """
    try:
        import keyring
    except ImportError:
        return False
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
        return True
    except keyring.errors.PasswordDeleteError:  # type: ignore[attr-defined]
        return False
    except Exception:
        return False


__all__ = [
    "KEYRING_SERVICE",
    "KEYRING_USERNAME",
    "GitHubAppAuthConfig",
    "GitHubAuthSource",
    "GitHubTokenResolution",
    "GitHubTokenProvider",
    "StaticGitHubTokenProvider",
    "TokenValidationResult",
    "_mask_token",
    "_gh_cli_account_for_host",
    "_gh_hosts_paths",
    "_github_host_for_api_url",
    "_normalize_keyring_secret",
    "_read_gh_cli_token",
    "_read_gh_hosts_record",
    "_read_keyring_token",
    "_read_macos_security_keychain_token",
    "_resolve_repo_scoped_github_token",
    "clear_keyring_token",
    "describe_github_token_sources",
    "has_github_app_auth_config",
    "resolve_github_token",
    "resolve_github_token_with_source",
    "store_keyring_token",
    "store_keyring_token_for",
]
