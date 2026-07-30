"""Round-trip tests for the setup GitHub authorization codec."""

from __future__ import annotations

from unittest.mock import call, patch

from issue_orchestrator.domain.repository_setup_auth import (
    RepositorySetupGitHubAuthorization,
)
from issue_orchestrator.execution.providers import (
    store_repository_setup_github_token,
)
from issue_orchestrator.execution.repository_setup_github_authorization import (
    repository_setup_github_authorization_codec,
)


def test_personal_authorization_round_trips_config_public_and_adapter() -> None:
    config = {
        "repo": {
            "name": "owner/repo",
            "github": {
                "token_env": "GHES_TOKEN",
                "api_url": "https://github.example/api/v3",
                "http_timeout_seconds": 47,
            },
        }
    }

    authorization = repository_setup_github_authorization_codec.from_config(config)
    public = repository_setup_github_authorization_codec.to_public(authorization)

    assert repository_setup_github_authorization_codec.to_config(
        authorization
    ) == config["repo"]["github"]
    assert repository_setup_github_authorization_codec.from_public(
        public
    ) == authorization
    assert repository_setup_github_authorization_codec.adapter_kwargs(
        authorization
    ) == {
        "configured_token": None,
        "configured_env": "GHES_TOKEN",
        "configured_keyring_service": None,
        "configured_keyring_username": None,
        "configured_app_client_id": None,
        "configured_app_id": None,
        "configured_app_installation_id": None,
        "configured_app_private_key_path": None,
        "configured_app_private_key_env": None,
    }


def test_inline_token_redaction_preserves_ghes_transport() -> None:
    authorization = RepositorySetupGitHubAuthorization(
        kind="personal",
        token="ghp_secret",
        api_url="https://github.example/api/v3",
        http_timeout_seconds=47,
    )

    public = repository_setup_github_authorization_codec.to_public(
        authorization,
        redact_inline_token=True,
    )

    assert public == {
        "kind": "detected",
        "api_url": "https://github.example/api/v3",
        "http_timeout_seconds": 47,
    }
    assert "ghp_secret" not in repr(public)


def test_token_store_scopes_equal_repo_names_by_canonical_github_host() -> None:
    public = RepositorySetupGitHubAuthorization(
        kind="personal",
        token="github-token",
    )
    enterprise = RepositorySetupGitHubAuthorization(
        kind="personal",
        token="ghes-token",
        api_url="https://GitHub.Example:443/api/v3",
    )

    with patch(
        "issue_orchestrator.adapters.github.tokens.store_keyring_token_for"
    ) as store:
        stored_public = store_repository_setup_github_token(
            public,
            repo="owner/repo",
        )
        stored_enterprise = store_repository_setup_github_token(
            enterprise,
            repo="owner/repo",
        )

    assert stored_public.keyring_username == "github-token:api.github.com:owner/repo"
    assert (
        stored_enterprise.keyring_username
        == "github-token:github.example:owner/repo"
    )
    assert stored_public.keyring_username != stored_enterprise.keyring_username
    assert store.call_args_list == [
        call(
            "github-token",
            service="issue-orchestrator",
            username="github-token:api.github.com:owner/repo",
        ),
        call(
            "ghes-token",
            service="issue-orchestrator",
            username="github-token:github.example:owner/repo",
        ),
    ]
