"""Unit tests for GitHub HTTP client."""

from __future__ import annotations

from base64 import b64decode

import httpx

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_orchestrator.adapters.github.auth import (
    GitHubAppInstallationTokenProvider,
    GitHubAuth,
    build_github_auth,
)
from issue_orchestrator.adapters.github.http_client import (
    GitHubAuthError,
    GitHubHttpClient,
    GitHubHttpConfig,
    GitHubHttpError,
    GitHubTransportError,
    describe_github_token_sources,
    resolve_github_token,
    validate_github_token,
)
from issue_orchestrator.adapters.github.tokens import (
    GitHubAppAuthConfig,
    GitHubAuthSource,
    StaticGitHubTokenProvider,
    TokenValidationResult,
    _normalize_keyring_secret,
    _read_gh_hosts_record,
    _read_gh_cli_token,
    _read_keyring_token,
    resolve_github_token_with_source,
)
from issue_orchestrator.domain.repository_setup_auth import (
    RepositorySetupGitHubAuthorization,
)
from issue_orchestrator.execution.providers import (
    verify_repository_setup_github_authorization,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.infra import gh_audit


def _client_with_transport(transport: httpx.BaseTransport) -> GitHubHttpClient:
    client = GitHubHttpClient(
        GitHubHttpConfig(
            repo="owner/repo", token="token", base_url="https://api.github.com"
        )
    )
    client._client = httpx.Client(  # noqa: SLF001 - test transport injection
        transport=transport, base_url="https://api.github.com"
    )
    return client


def test_resolve_github_token_repo_scoped_env_is_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TIXMEUP_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("ISSUE_ORCH_GITHUB_TOKEN", "generic-token")

    with pytest.raises(GitHubAuthError, match="repo-specific auth"):
        resolve_github_token(
            configured_token=None,
            configured_env="TIXMEUP_GITHUB_TOKEN",
        )


def test_resolve_github_token_allows_default_sources_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ISSUE_ORCH_GITHUB_TOKEN", "generic-token")

    assert resolve_github_token() == "generic-token"


def test_resolve_github_token_reports_exact_environment_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PORCHPIN_GITHUB_TOKEN", "repo-token")
    monkeypatch.setenv("ISSUE_ORCH_GITHUB_TOKEN", "generic-token")

    resolution = resolve_github_token_with_source(
        configured_env="PORCHPIN_GITHUB_TOKEN",
    )

    assert resolution.token == "repo-token"
    assert resolution.source.kind == "environment"
    assert resolution.source.environment_variable == "PORCHPIN_GITHUB_TOKEN"
    assert resolution.source.description == (
        "Environment variable PORCHPIN_GITHUB_TOKEN"
    )


def test_resolve_github_token_reports_exact_keyring_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.tokens._read_keyring_token",
        lambda *, service, username: (
            "repo-token"
            if (service, username) == ("issue-orchestrator", "github-token:owner/repo")
            else None
        ),
    )

    resolution = resolve_github_token_with_source(
        configured_keyring_service="issue-orchestrator",
        configured_keyring_username="github-token:owner/repo",
    )

    assert resolution.token == "repo-token"
    assert resolution.source.kind == "keyring"
    assert resolution.source.keyring_service == "issue-orchestrator"
    assert resolution.source.keyring_username == "github-token:owner/repo"


def test_setup_verifier_normalizes_detected_environment_to_durable_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = SimpleNamespace(
        auth_kind="token",
        resolved_source=GitHubAuthSource(
            kind="environment",
            description="Environment variable PORCHPIN_GITHUB_TOKEN",
            environment_variable="PORCHPIN_GITHUB_TOKEN",
        ),
        validate=lambda **_kwargs: TokenValidationResult(
            valid=True,
            username="porchpin-owner",
        ),
    )
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.build_github_auth",
        lambda **_kwargs: auth,
    )

    verification = verify_repository_setup_github_authorization(
        "owner/repo",
        RepositorySetupGitHubAuthorization(kind="detected"),
    )

    assert verification.identity == "porchpin-owner"
    assert verification.normalized_authorization == (
        RepositorySetupGitHubAuthorization(
            kind="personal",
            token_env="PORCHPIN_GITHUB_TOKEN",
        )
    )


def test_setup_verifier_keeps_github_cli_as_explicitly_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = SimpleNamespace(
        auth_kind="token",
        resolved_source=GitHubAuthSource(
            kind="github_cli",
            description="GitHub CLI auth (github.com)",
        ),
        validate=lambda **_kwargs: TokenValidationResult(
            valid=True,
            username="porchpin-owner",
        ),
    )
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.build_github_auth",
        lambda **_kwargs: auth,
    )
    authorization = RepositorySetupGitHubAuthorization(kind="detected")

    verification = verify_repository_setup_github_authorization(
        "owner/repo",
        authorization,
    )

    assert verification.source == "GitHub CLI auth (github.com)"
    assert verification.normalized_authorization == authorization


def test_resolve_github_token_uses_gh_hosts_before_default_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ISSUE_ORCH_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.tokens._read_gh_cli_token",
        lambda *, host: "gh-hosts-token",
    )
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.tokens._read_keyring_token",
        lambda *, service=..., username=...: "stale-keyring-token",
    )

    assert resolve_github_token() == "gh-hosts-token"


def test_resolve_github_token_repo_scoped_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ISSUE_ORCH_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.tokens._read_keyring_token",
        lambda *, service=..., username=...: (
            "repo-keyring-token"
            if service == "tixmeup-github" and username == "bruce"
            else None
        ),
    )

    token = resolve_github_token(
        configured_token=None,
        configured_keyring_service="tixmeup-github",
        configured_keyring_username="bruce",
    )

    assert token == "repo-keyring-token"


def test_read_keyring_token_logs_debug_on_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keyring backend failures must leave a DEBUG trace.

    Related to security issue #6017 (F8 from #5987) — silently swallowing
    every exception hides misconfiguration. We still fall through to the
    macOS ``security`` CLI for the happy path, but the keyring failure
    must be logged so troubleshooting is possible.
    """

    class _ExplodingKeyring:
        @staticmethod
        def get_password(service: str, username: str) -> str | None:
            raise RuntimeError("keyring backend is locked")

    monkeypatch.setitem(sys.modules, "keyring", _ExplodingKeyring)
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.tokens._read_macos_security_keychain_token",
        lambda *, service, username: None,
    )

    with caplog.at_level("DEBUG", logger="issue_orchestrator.adapters.github.tokens"):
        result = _read_keyring_token(service="svc", username="user")

    assert result is None
    assert any(
        "keyring.get_password failed" in record.getMessage()
        for record in caplog.records
    )


def test_read_keyring_token_falls_back_to_macos_security(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeKeyring:
        @staticmethod
        def get_password(service: str, username: str) -> None:
            assert service == "tixmeup-github"
            assert username == "brucegordon"
            return None

    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring)
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.tokens.sys.platform",
        "darwin",
    )
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.tokens.shutil.which",
        lambda name: "/usr/bin/security" if name == "security" else None,
    )
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.tokens.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="repo-keyring-token\n",
            stderr="",
        ),
    )

    token = _read_keyring_token(service="tixmeup-github", username="brucegordon")

    assert token == "repo-keyring-token"


def test_normalize_keyring_secret_decodes_go_keyring_base64() -> None:
    secret = "go-keyring-base64:Z2hwX2V4YW1wbGU="

    assert _normalize_keyring_secret(secret) == "ghp_example"


def test_normalize_keyring_secret_returns_raw_secret_for_unknown_format() -> None:
    assert _normalize_keyring_secret("plain-token") == "plain-token"


def test_describe_github_token_sources_repo_scoped_ignores_generic_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ISSUE_ORCH_GITHUB_TOKEN", "generic-token")
    monkeypatch.delenv("TIXMEUP_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.tokens._read_keyring_token",
        lambda *, service=..., username=...: None,
    )

    sources = describe_github_token_sources(
        configured_env="TIXMEUP_GITHUB_TOKEN",
    )

    assert sources == []


def test_describe_github_token_sources_includes_gh_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ISSUE_ORCH_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.tokens._read_gh_cli_token",
        lambda *, host: "gh-hosts-token",
    )
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.tokens._read_keyring_token",
        lambda *, service=..., username=...: None,
    )

    sources = describe_github_token_sources()

    assert sources == ["GitHub CLI auth (github.com): gh-h...oken"]


def test_describe_github_token_sources_reports_app_installation() -> None:
    sources = describe_github_token_sources(
        configured_app_client_id="Iv23example",
        configured_app_installation_id="145305179",
        configured_app_private_key_path="~/.config/issue-orchestrator/github-apps/bot.pem",
    )

    assert sources == [
        "GitHub App installation 145305179 "
        "(client_id Iv23example, private key "
        "path:~/.config/issue-orchestrator/github-apps/bot.pem)"
    ]


def test_github_app_installation_provider_mints_and_caches_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    key_path = tmp_path / "bot.private-key.pem"
    key_path.write_text(
        "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n"
    )
    calls: list[str] = []

    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.auth.jwt.encode",
        lambda payload, key, algorithm: "jwt-token",
    )

    def _post(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        calls.append(url)
        assert url == "https://api.github.com/app/installations/145305179/access_tokens"
        assert headers["Authorization"] == "Bearer jwt-token"
        assert timeout == 20.0
        return httpx.Response(
            201,
            json={
                "token": "installation-token",
                "expires_at": "2026-07-08T12:00:00Z",
            },
        )

    provider = GitHubAppInstallationTokenProvider(
        GitHubAppAuthConfig.from_values(
            client_id="Iv23example",
            installation_id="145305179",
            private_key_path=str(key_path),
        ),
        clock=lambda: 1_000.0,
        post=_post,
    )

    assert provider.get_token() == "installation-token"
    assert provider.get_token() == "installation-token"
    assert calls == ["https://api.github.com/app/installations/145305179/access_tokens"]


def test_github_auth_builds_git_env_overrides_without_token_in_remote() -> None:
    auth = GitHubAuth(
        token_provider=StaticGitHubTokenProvider("installation-token"),
        source_descriptions=("GitHub App installation 145305179",),
        api_url="https://api.github.com",
        repo="owner/repo",
        enable_git_push_auth=True,
    )

    env = auth.git_env_overrides(remote="origin")

    assert env is not None
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    header = env["GIT_CONFIG_VALUE_0"]
    assert header.startswith("Authorization: Basic ")
    encoded_credential = header.removeprefix("Authorization: Basic ")
    assert b64decode(encoded_credential).decode("utf-8") == (
        "x-access-token:installation-token"
    )
    assert env["GIT_CONFIG_VALUE_1"] == "https://github.com/owner/repo.git"
    assert env["GIT_CONFIG_VALUE_2"] == "https://github.com/owner/repo.git"
    assert "installation-token" not in env["GIT_CONFIG_VALUE_1"]
    assert "installation-token" not in env["GIT_CONFIG_VALUE_2"]


def test_github_http_client_uses_fresh_auth_headers_per_request() -> None:
    class _Provider:
        auth_kind = "github_app"

        def __init__(self) -> None:
            self.count = 0

        def get_token(self) -> str:
            self.count += 1
            return f"token-{self.count}"

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"number": 1, "labels": []})

    auth = GitHubAuth(
        token_provider=_Provider(),
        source_descriptions=("GitHub App installation 145305179",),
        repo="owner/repo",
    )
    client = GitHubHttpClient(GitHubHttpConfig(repo="owner/repo", auth=auth))
    client._client = httpx.Client(  # noqa: SLF001
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )

    client.get_issue(1)
    client.get_issue_labels(1, use_cache=False)

    assert seen == ["Bearer token-1", "Bearer token-2"]


def test_github_auth_validates_app_installation_repo_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Provider:
        auth_kind = "github_app"

        def get_token(self) -> str:
            return "installation-token"

    def _get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        assert url == "https://api.github.com/repos/owner/repo"
        assert headers["Authorization"] == "Bearer installation-token"
        assert timeout == 10.0
        return httpx.Response(200, json={"full_name": "owner/repo"})

    monkeypatch.setattr("issue_orchestrator.adapters.github.auth.httpx.get", _get)
    auth = GitHubAuth(
        token_provider=_Provider(),
        source_descriptions=("GitHub App installation 145305179",),
        repo="owner/repo",
    )

    result = auth.validate(repo="owner/repo")

    assert result.valid is True
    assert result.username == "GitHub App installation 145305179"


def test_github_auth_validation_reports_token_provider_failure() -> None:
    class _Provider:
        auth_kind = "github_app"

        def get_token(self) -> str:
            raise GitHubAuthError("GitHub App private key is unavailable")

    auth = GitHubAuth(
        token_provider=_Provider(),
        source_descriptions=("GitHub App installation 145305179",),
        repo="owner/repo",
    )

    result = auth.validate(repo="owner/repo")

    assert result == TokenValidationResult(
        valid=False,
        error="GitHub App private key is unavailable",
    )


def test_read_gh_cli_token_from_hosts_oauth_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "gh-config"
    config_dir.mkdir()
    hosts_path = config_dir / "hosts.yml"
    hosts_path.write_text(
        "github.com:\n  oauth_token: gh-hosts-token\n  user: octocat\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)

    token = _read_gh_cli_token(host="github.com")

    assert token == "gh-hosts-token"


def test_read_gh_cli_token_from_hosts_keychain_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "gh-config"
    config_dir.mkdir()
    hosts_path = config_dir / "hosts.yml"
    hosts_path.write_text(
        "github.com:\n  user: octocat\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.tokens._read_keyring_token",
        lambda *, service=..., username=...: (
            "gh-keychain-token"
            if service == "gh:github.com" and username == "octocat"
            else None
        ),
    )

    token = _read_gh_cli_token(host="github.com")

    assert token == "gh-keychain-token"


def test_read_gh_hosts_record_logs_malformed_yaml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_dir = tmp_path / "gh-config"
    config_dir.mkdir()
    hosts_path = config_dir / "hosts.yml"
    hosts_path.write_text("github.com: [not valid yaml\n", encoding="utf-8")
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.tokens._gh_hosts_paths",
        lambda: [hosts_path],
    )

    with caplog.at_level("WARNING", logger="issue_orchestrator.adapters.github.tokens"):
        record = _read_gh_hosts_record(host="github.com")

    assert record is None
    assert any(
        "Ignoring malformed GitHub CLI hosts.yml" in entry.getMessage()
        and str(hosts_path) in entry.getMessage()
        for entry in caplog.records
    )


def test_validate_github_token_checks_repo_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _mock_get(
        url: str, *, headers: dict[str, str], timeout: float
    ) -> httpx.Response:
        assert headers["Authorization"] == "Bearer repo-token"
        assert timeout == 10.0
        if url == "https://api.github.com/user":
            return httpx.Response(200, json={"login": "octocat"})
        if url == "https://api.github.com/repos/BruceBGordon/tixmeup":
            return httpx.Response(404, json={"message": "Not Found"})
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.http_client.httpx.get", _mock_get
    )

    result = validate_github_token(token="repo-token", repo="BruceBGordon/tixmeup")

    assert result.valid is False
    assert result.username == "octocat"
    assert result.error == "Token cannot access repo BruceBGordon/tixmeup (HTTP 404)"


def test_get_issue_uses_etag_cache() -> None:
    seen = {"count": 0}
    payload = {"number": 1, "title": "Test"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["count"] += 1
        if seen["count"] == 1:
            return httpx.Response(200, json=payload, headers={"ETag": "W/etag"})
        return httpx.Response(304, text="")

    client = _client_with_transport(httpx.MockTransport(handler))
    first = client.get_issue(1)
    second = client.get_issue(1)

    assert first == payload
    assert second == payload


def test_create_label_force_updates_on_422() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(422, json={"message": "exists"})
        return httpx.Response(200, json={"name": "bug"})

    client = _client_with_transport(httpx.MockTransport(handler))
    client.create_label("bug", force=True)

    assert calls[0][0] == "POST"
    assert calls[1][0] == "PATCH"


def test_create_label_ignores_422_without_force() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "exists"})

    client = _client_with_transport(httpx.MockTransport(handler))
    client.create_label("bug", force=False)


def test_list_issues_filters_pull_requests() -> None:
    payload = [
        {"number": 1, "title": "Issue"},
        {
            "number": 2,
            "title": "PR",
            "pull_request": {"url": "https://api.github.com/pulls/2"},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = _client_with_transport(httpx.MockTransport(handler))
    issues = client.list_issues()

    assert len(issues) == 1
    assert issues[0]["number"] == 1


def test_list_issues_paginates_when_limit_exceeds_one_page() -> None:
    """R4: a limit above one page walks every page so a backlog of proposals
    can never hide an older op/anchor behind the 100-item cap."""
    page1 = [{"number": n, "title": f"I{n}"} for n in range(1, 101)]  # full page
    page2 = [{"number": n, "title": f"I{n}"} for n in range(101, 131)]  # short page

    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        requested_pages.append(page)
        return httpx.Response(200, json=page1 if page == 1 else page2)

    client = _client_with_transport(httpx.MockTransport(handler))
    issues = client.list_issues(labels=["tech-lead-agent"], limit=2000)

    assert [i["number"] for i in issues] == list(range(1, 131))
    assert requested_pages == [1, 2]  # short page 2 stops the walk


def test_list_issues_single_page_when_limit_within_one_page() -> None:
    """The common case (limit<=100) keeps the single cached page — no walk."""
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_pages.append(int(request.url.params.get("page", "1")))
        return httpx.Response(
            200, json=[{"number": n, "title": "x"} for n in range(1, 101)]
        )

    client = _client_with_transport(httpx.MockTransport(handler))
    issues = client.list_issues(limit=100)

    assert len(issues) == 100
    assert requested_pages == [1]  # never asked for page 2


def test_list_issues_exhaustive_happy_path_returns_full_set() -> None:
    """R17: the authoritative scan pages the complete set and stops on the true
    final (short) page — no raise when the read is genuinely complete."""
    page1 = [{"number": n, "title": f"I{n}"} for n in range(1, 101)]  # full
    page2 = [{"number": n, "title": f"I{n}"} for n in range(101, 131)]  # short
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        requested_pages.append(page)
        return httpx.Response(200, json=page1 if page == 1 else page2)

    client = _client_with_transport(httpx.MockTransport(handler))
    issues = client.list_issues(labels=["tech-lead-agent"], limit=2000, exhaustive=True)

    assert [i["number"] for i in issues] == list(range(1, 131))
    assert requested_pages == [1, 2]


def test_list_issues_exhaustive_later_page_non_200_fails_loud() -> None:
    """R17: an AUTHORITATIVE open-issue scan must NOT return a silently partial
    anchor set. A later-page non-200 RAISES so planning/recovery cannot proceed
    from a truncated list (a dropped page could hide an older anchor/approved
    op). Mirrors the all-labels fail-loud pager (#6779 R8)."""
    page1 = [{"number": n, "title": f"I{n}"} for n in range(1, 101)]  # full

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(200, json=page1)
        return httpx.Response(500, text="server error")  # later page fails

    client = _client_with_transport(httpx.MockTransport(handler))
    with pytest.raises(GitHubHttpError):
        client.list_issues(labels=["tech-lead-agent"], limit=2000, exhaustive=True)


def test_list_issues_exhaustive_cap_exhaustion_fails_loud() -> None:
    """R17: when every page is full past the 2,000-result cap, the scan cannot
    prove the open-issue list is complete, so it RAISES rather than truncating."""
    full_page = [{"number": n, "title": "x"} for n in range(1, 101)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=full_page)  # always a full page

    client = _client_with_transport(httpx.MockTransport(handler))
    with pytest.raises(GitHubHttpError):
        client.list_issues(labels=["tech-lead-agent"], limit=2000, exhaustive=True)


def test_list_issues_exhaustive_transport_failure_fails_loud() -> None:
    """R17: a later-page transport failure raises GitHubTransportError, never a
    partial set."""
    page1 = [{"number": n, "title": "x"} for n in range(1, 101)]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(200, json=page1)
        raise httpx.ConnectError("boom")  # later page transport failure

    client = _client_with_transport(httpx.MockTransport(handler))
    with pytest.raises(GitHubTransportError):
        client.list_issues(labels=["tech-lead-agent"], limit=2000, exhaustive=True)


def test_list_issues_non_exhaustive_later_page_stays_lenient() -> None:
    """The general (bounded) fetch is NOT authoritative: ``limit`` is a
    deliberate window (``fetch_limit``), so a later-page non-200 returns the
    rows gathered so far rather than raising. Only the exhaustive anchor scan
    fails loud — the two paths must not converge."""
    page1 = [{"number": n, "title": f"I{n}"} for n in range(1, 101)]  # full

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(200, json=page1)
        return httpx.Response(500, text="server error")

    client = _client_with_transport(httpx.MockTransport(handler))
    issues = client.list_issues(
        labels=["tech-lead-agent"], limit=2000
    )  # not exhaustive

    assert [i["number"] for i in issues] == list(range(1, 101))  # partial, no raise


def test_list_issues_since_returns_oldest_watermark_hint() -> None:
    payload = [
        {"number": 10, "title": "Newest", "updated_at": "2026-01-02T10:00:00Z"},
        {"number": 9, "title": "Older", "updated_at": "2026-01-02T09:30:00Z"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = _client_with_transport(httpx.MockTransport(handler))
    issues, watermark = client.list_issues_since(since="2026-01-01T00:00:00Z", limit=20)

    assert len(issues) == 2
    assert watermark == "2026-01-02T09:30:00Z"


def test_list_issues_since_paginates_and_respects_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(
                200,
                json=[
                    {"number": 100, "title": "A", "updated_at": "2026-01-02T10:00:00Z"},
                    {"number": 99, "title": "B", "updated_at": "2026-01-02T09:59:00Z"},
                    {"number": 98, "title": "C", "updated_at": "2026-01-02T09:58:00Z"},
                ],
            )
        return httpx.Response(
            200,
            json=[
                {"number": 97, "title": "D", "updated_at": "2026-01-02T09:57:00Z"},
            ],
        )

    client = _client_with_transport(httpx.MockTransport(handler))
    issues, watermark = client.list_issues_since(
        since="2026-01-01T00:00:00Z",
        limit=3,
    )

    assert [issue["number"] for issue in issues] == [100, 99, 98]
    assert watermark == "2026-01-02T09:58:00Z"


_TEST_MARKER = "<!-- io:test-marker -->"


def test_issue_comment_marker_present_finds_marker_beyond_first_page() -> None:
    """Regression: a marker comment sitting past the first 100 comments must
    still be detected. A first-page-only read would miss it and let the
    caller post a duplicate."""
    pages_requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        pages_requested.append(page)
        if page == 1:
            # A full page (100) of unrelated comments forces a second fetch.
            return httpx.Response(
                200, json=[{"body": f"chatter {i}"} for i in range(100)]
            )
        return httpx.Response(200, json=[{"body": f"{_TEST_MARKER}\nearlier feedback"}])

    client = _client_with_transport(httpx.MockTransport(handler))

    assert client.issue_comment_marker_present(318, _TEST_MARKER) is True
    # Pagination actually advanced past the first page to find the marker.
    assert pages_requested == [1, 2]


def test_issue_comment_marker_present_false_when_absent_across_pages() -> None:
    """The scan covers every page and only returns False once a short
    (final) page confirms the marker is nowhere on the issue/PR."""
    pages_requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        pages_requested.append(page)
        if page == 1:
            return httpx.Response(
                200, json=[{"body": f"chatter {i}"} for i in range(100)]
            )
        return httpx.Response(200, json=[{"body": "still nothing"}])

    client = _client_with_transport(httpx.MockTransport(handler))

    assert client.issue_comment_marker_present(318, _TEST_MARKER) is False
    assert pages_requested == [1, 2]


def test_issue_comment_marker_present_short_circuits_on_first_page() -> None:
    """When the marker is on the first page the scan stops immediately
    instead of paging further."""
    pages_requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages_requested.append(int(request.url.params.get("page", "1")))
        return httpx.Response(
            200,
            json=[
                {"body": "unrelated chatter"},
                {"body": f"{_TEST_MARKER}\nfeedback"},
            ],
        )

    client = _client_with_transport(httpx.MockTransport(handler))

    assert client.issue_comment_marker_present(318, _TEST_MARKER) is True
    assert pages_requested == [1]


def test_issue_comment_marker_present_propagates_read_error() -> None:
    """A failed comment read must raise (fail loud) so dedupe callers do not
    silently risk a duplicate comment."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _client_with_transport(httpx.MockTransport(handler))

    with pytest.raises(GitHubHttpError):
        client.issue_comment_marker_present(318, _TEST_MARKER)


def test_issue_comment_marker_present_fails_loud_at_page_cap() -> None:
    """Regression: a scan that never reaches a final (short) page must NOT
    report "marker absent" once the operational page cap is hit. A truncated
    scan is not evidence the marker is missing, so the cap raises rather than
    returning False -- otherwise a marker on page 21+ would let the dedupe
    caller post a duplicate comment."""
    pages_requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages_requested.append(int(request.url.params.get("page", "1")))
        # Always a full page of unrelated comments: GitHub never signals a
        # final page, so the loop runs until the cap.
        return httpx.Response(200, json=[{"body": f"chatter {i}"} for i in range(100)])

    client = _client_with_transport(httpx.MockTransport(handler))

    with pytest.raises(GitHubHttpError):
        client.issue_comment_marker_present(318, _TEST_MARKER)
    # The scan walked every page up to the cap before failing loud (it did not
    # bail out early treating a full page as the end).
    assert len(pages_requested) >= 20


def test_issue_comment_marker_present_fails_loud_on_non_list_payload() -> None:
    """Regression: a malformed (non-list) 2xx body is a contract violation, not
    evidence the marker is absent. It must raise rather than return False so a
    dedupe caller never posts a duplicate from a response it could not scan."""

    def handler(request: httpx.Request) -> httpx.Response:
        # GitHub's comments endpoint returns a JSON array; an object here means
        # an error envelope, proxy/mock drift, or schema change.
        return httpx.Response(200, json={"message": "Not Found"})

    client = _client_with_transport(httpx.MockTransport(handler))

    with pytest.raises(GitHubHttpError):
        client.issue_comment_marker_present(318, _TEST_MARKER)


def test_list_issues_since_default_bypasses_etag_cache() -> None:
    requests_seen: list[dict[str, str]] = []
    payload = [{"number": 1, "title": "Issue", "updated_at": "2026-01-02T10:00:00Z"}]

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(dict(request.headers))
        return httpx.Response(
            200, json=payload, headers={"ETag": "W/issues-since-etag"}
        )

    client = _client_with_transport(httpx.MockTransport(handler))

    client.list_issues_since(since="2026-01-01T00:00:00Z", limit=10)
    client.list_issues_since(since="2026-01-01T00:00:00Z", limit=10)

    assert len(requests_seen) == 2
    assert "if-none-match" not in requests_seen[0]
    assert "if-none-match" not in requests_seen[1]


def test_search_issues_by_title_includes_is_issue_qualifier() -> None:
    """Search must include `is:issue` and `in:title`; OR terms inside parens.

    Regression bait: GitHub's /search/issues rejects queries without an
    `is:` qualifier (422); fine-grained PATs are stricter. The OR must
    bind inside parens so it doesn't cross the qualifiers.
    """
    queries_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries_seen.append(request.url.params.get("q", ""))
        return httpx.Response(
            200,
            json={"items": [{"number": 42, "title": "[M9-006] Foo"}]},
        )

    client = _client_with_transport(httpx.MockTransport(handler))
    items = client.search_issues_by_title(["M9-006", "M9-007"])

    assert len(queries_seen) == 1
    q = queries_seen[0]
    assert "is:issue" in q
    assert "in:title" in q
    assert '("M9-006" OR "M9-007")' in q
    assert items == [{"number": 42, "title": "[M9-006] Foo"}]


def test_search_issues_by_title_empty_terms_skips_http() -> None:
    """No terms → no HTTP call (don't burn search quota on a noop)."""

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Unexpected HTTP request: {request.url}")
        return httpx.Response(500)

    client = _client_with_transport(httpx.MockTransport(handler))
    assert client.search_issues_by_title([]) == []


def test_get_token_scopes_from_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"login": "user"},
            headers={"X-OAuth-Scopes": "repo, read:org"},
        )

    client = _client_with_transport(httpx.MockTransport(handler))
    scopes = client.get_token_scopes()

    assert scopes == ["repo", "read:org"]


def test_list_issues_use_cache_false_bypasses_etag() -> None:
    """Verify that use_cache=False bypasses ETag caching.

    When use_cache=False:
    - First call should not send If-None-Match header
    - Server returning 304 should NOT happen (no ETag sent)
    - Fresh data should always be fetched
    """
    requests_seen: list[dict] = []
    payload = [{"number": 1, "title": "Fresh Issue"}]

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "headers": dict(request.headers),
            }
        )
        # Always return 200 with fresh data
        return httpx.Response(200, json=payload, headers={"ETag": "W/fresh-etag"})

    client = _client_with_transport(httpx.MockTransport(handler))

    # First call with cache - establishes ETag
    first_cached = client.list_issues(use_cache=True)
    assert len(first_cached) == 1
    assert len(requests_seen) == 1
    assert "If-None-Match" not in requests_seen[0]["headers"]

    # Second call with use_cache=False - should NOT send If-None-Match
    requests_seen.clear()
    second_uncached = client.list_issues(use_cache=False)
    assert len(second_uncached) == 1
    assert len(requests_seen) == 1
    # The key assertion: no If-None-Match header when use_cache=False
    # (httpx stores headers as lowercase)
    assert "if-none-match" not in requests_seen[0]["headers"]


def test_list_issues_use_cache_true_sends_etag() -> None:
    """Verify that use_cache=True sends If-None-Match for cached responses."""
    requests_seen: list[dict] = []
    payload = [{"number": 1, "title": "Cached Issue"}]
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        requests_seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "headers": dict(request.headers),
            }
        )
        if call_count == 1:
            # First call: return data with ETag
            return httpx.Response(200, json=payload, headers={"ETag": "W/cached-etag"})
        else:
            # Second call: should have If-None-Match, return 304
            return httpx.Response(304, text="")

    client = _client_with_transport(httpx.MockTransport(handler))

    # First call - establishes cache
    first = client.list_issues(use_cache=True)
    assert len(first) == 1

    # Second call with use_cache=True - should send If-None-Match
    second = client.list_issues(use_cache=True)
    assert len(second) == 1
    assert len(requests_seen) == 2
    # The key assertion: If-None-Match header sent when use_cache=True
    # (httpx stores headers as lowercase)
    assert "if-none-match" in requests_seen[1]["headers"]
    assert requests_seen[1]["headers"]["if-none-match"] == "W/cached-etag"


def test_get_issue_labels_use_cache_false_bypasses_etag() -> None:
    """Verify that use_cache=False bypasses ETag caching for label reads."""
    requests_seen: list[dict] = []
    payload = [{"name": "bug"}]

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "headers": dict(request.headers),
            }
        )
        return httpx.Response(200, json=payload, headers={"ETag": "W/labels-etag"})

    client = _client_with_transport(httpx.MockTransport(handler))

    first = client.get_issue_labels(1, use_cache=True)
    assert first == ["bug"]
    assert "if-none-match" not in requests_seen[0]["headers"]

    requests_seen.clear()
    second = client.get_issue_labels(1, use_cache=False)
    assert second == ["bug"]
    assert "if-none-match" not in requests_seen[0]["headers"]


def test_get_issue_labels_use_cache_true_sends_etag() -> None:
    """Verify that use_cache=True sends If-None-Match for label reads."""
    requests_seen: list[dict] = []
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        requests_seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "headers": dict(request.headers),
            }
        )
        if call_count == 1:
            return httpx.Response(
                200, json=[{"name": "bug"}], headers={"ETag": "W/labels-etag"}
            )
        return httpx.Response(304, text="")

    client = _client_with_transport(httpx.MockTransport(handler))

    first = client.get_issue_labels(1, use_cache=True)
    assert first == ["bug"]
    second = client.get_issue_labels(1, use_cache=True)
    assert second == ["bug"]
    assert "if-none-match" in requests_seen[1]["headers"]


def test_invalidate_labels_etag_clears_cache() -> None:
    """Verify that invalidate_labels_etag clears the ETag cache for labels.

    After a write operation on labels (POST/PATCH/DELETE), the ETag cache
    should be invalidated so subsequent list_labels() fetches fresh data
    instead of getting a stale 304.
    """
    requests_seen: list[dict] = []
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        requests_seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "headers": dict(request.headers),
            }
        )
        if "if-none-match" in request.headers:
            # If client sent If-None-Match, return 304
            return httpx.Response(304, text="")
        # Otherwise return fresh data with new ETag
        return httpx.Response(
            200,
            json=[{"name": f"label-{call_count}"}],
            headers={"ETag": f"W/etag-{call_count}"},
        )

    client = _client_with_transport(httpx.MockTransport(handler))

    # First call - establishes cache
    first = client.list_labels()
    assert len(first) == 1
    assert first[0]["name"] == "label-1"
    assert len(requests_seen) == 1
    assert "if-none-match" not in requests_seen[0]["headers"]

    # Second call without invalidation - should send If-None-Match, get 304
    requests_seen.clear()
    second = client.list_labels()
    assert len(second) == 1
    assert second[0]["name"] == "label-1"  # Cached value from 304
    assert "if-none-match" in requests_seen[0]["headers"]

    # Invalidate the cache
    client.invalidate_labels_etag()

    # Third call after invalidation - should NOT send If-None-Match
    requests_seen.clear()
    third = client.list_labels()
    assert len(third) == 1
    assert third[0]["name"] == "label-3"  # Fresh data, not cached
    assert "if-none-match" not in requests_seen[0]["headers"]


def test_list_labels_paginates_beyond_the_first_full_page() -> None:
    """R8: list_labels() promises ALL labels, so a full first page continues
    paging. A gate label sorted onto a later page (e.g. proposed-tech-lead in a
    repo with 100+ labels) must be returned, else valid proposal creation is
    falsely refused."""
    page1 = [{"name": f"label-{n}"} for n in range(100)]  # full page -> keep paging
    page2 = [{"name": "proposed-tech-lead"}]  # gate label lives on page 2 (short page)

    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        requested_pages.append(page)
        return httpx.Response(200, json=page1 if page == 1 else page2)

    client = _client_with_transport(httpx.MockTransport(handler))
    labels = client.list_labels()

    names = {entry["name"] for entry in labels}
    assert "proposed-tech-lead" in names  # later-page gate label is not missed
    assert len(labels) == 101
    assert requested_pages == [1, 2]  # walked both pages, stopped on the short one


def test_list_labels_single_page_makes_no_extra_request() -> None:
    """A short first page is exhaustive on its own — no later-page fetch."""
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_pages.append(int(request.url.params.get("page", "1")))
        return httpx.Response(
            200, json=[{"name": "bug"}, {"name": "proposed-tech-lead"}]
        )

    client = _client_with_transport(httpx.MockTransport(handler))
    labels = client.list_labels()

    assert {e["name"] for e in labels} == {"bug", "proposed-tech-lead"}
    assert requested_pages == [1]


def test_list_labels_later_page_non_200_fails_loud() -> None:
    """R8: a later-page non-200 must RAISE, not silently return the labels
    gathered so far. control/tech_lead_proposals.py makes a gate-ABSENT decision
    from this list; a truncated scan that misses proposed-tech-lead would falsely
    refuse valid proposals, so completeness cannot be assumed."""
    page1 = [{"name": f"label-{n}"} for n in range(100)]  # full page -> keep paging

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(200, json=page1)
        return httpx.Response(500, text="server error")  # later page fails

    client = _client_with_transport(httpx.MockTransport(handler))
    with pytest.raises(GitHubHttpError):
        client.list_labels()


def test_list_labels_page_cap_fails_loud_rather_than_truncating() -> None:
    """R8: when every page is full past the cap, the scan cannot prove the list
    is complete, so it RAISES rather than silently returning a truncated set."""
    full_page = [{"name": f"label-{n}"} for n in range(100)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=full_page)  # always a full page

    client = _client_with_transport(httpx.MockTransport(handler))
    with pytest.raises(GitHubHttpError):
        client.list_labels()


def test_list_all_labels_later_page_non_200_fails_loud() -> None:
    """R8: the cleanup all-labels path shares the same fail-loud pager, so a
    later-page non-200 raises here too (no cross-path completeness drift)."""
    page1 = [{"name": f"label-{n}"} for n in range(100)]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(200, json=page1)
        return httpx.Response(403, text="forbidden")

    client = _client_with_transport(httpx.MockTransport(handler))
    with pytest.raises(GitHubHttpError):
        client.list_all_labels()


def test_invalidate_pr_etag_clears_pr_cache() -> None:
    requests_seen: list[dict] = []
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        requests_seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "headers": dict(request.headers),
            }
        )
        if "if-none-match" in request.headers:
            return httpx.Response(304, text="")
        return httpx.Response(
            200,
            json={
                "number": 42,
                "title": "PR",
                "labels": [{"name": f"label-{call_count}"}],
            },
            headers={"ETag": f"W/pr-etag-{call_count}"},
        )

    client = _client_with_transport(httpx.MockTransport(handler))

    first = client.get_pr(42)
    assert first is not None
    assert first["labels"][0]["name"] == "label-1"

    requests_seen.clear()
    second = client.get_pr(42)
    assert second is not None
    assert second["labels"][0]["name"] == "label-1"
    assert "if-none-match" in requests_seen[0]["headers"]

    client.invalidate_pr_etag(42)

    requests_seen.clear()
    third = client.get_pr(42)
    assert third is not None
    assert third["labels"][0]["name"] == "label-3"
    assert "if-none-match" not in requests_seen[0]["headers"]


def test_get_prs_for_issue_query_includes_is_pr_qualifier() -> None:
    """GitHub search rejects queries without `is:` with 422.

    Regression: tixmeup orchestrator hit
    `422 — Query must include 'is:issue' or 'is:pull-request'` on
    reset-and-retry-from-scratch because the search query lacked `is:pr`.
    The OR must be parenthesized so it binds inside the disjunction.
    """
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["q"] = request.url.params.get("q", "")
        return httpx.Response(200, json={"items": []})

    client = _client_with_transport(httpx.MockTransport(handler))
    client.get_prs_for_issue(359)

    query = captured["q"]
    assert "is:pr" in query
    assert "(head:359 OR #359)" in query
    assert "repo:owner/repo" in query


def test_http_error_message_includes_response_body_summary() -> None:
    """`str(GitHubHttpError)` must surface GitHub's reason text.

    Without this, toasts/logs only show "GitHub request failed: 422"
    and the user has no idea what GitHub actually rejected.
    """
    error_body = {
        "message": "Validation Failed",
        "errors": [
            {"resource": "Search", "field": "q", "code": "invalid"},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json=error_body)

    client = _client_with_transport(httpx.MockTransport(handler))
    with pytest.raises(GitHubHttpError) as exc_info:
        client.get_prs_for_issue(359)

    rendered = str(exc_info.value)
    assert "422" in rendered
    assert "Validation Failed" in rendered
    assert "Search" in rendered
    assert exc_info.value.status_code == 422
    assert exc_info.value.response_text  # raw body still preserved


def test_get_prs_with_label_state_all_skips_malformed_items() -> None:
    class _Sink:
        def __init__(self) -> None:
            self.events: list[object] = []

        def publish(self, event) -> None:
            self.events.append(event)

    sink = _Sink()
    previous_sink = getattr(gh_audit, "_event_sink", None)
    gh_audit.set_event_sink(sink)

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("q", "")
        if "state:open" in query:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "number": 10,
                            "title": "Open PR",
                            "html_url": "https://example.com/open",
                        },
                        {
                            "title": "Malformed PR",
                            "html_url": "https://example.com/bad",
                        },
                    ],
                },
            )
        if "state:closed" in query:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "number": 10,
                            "title": "Duplicate PR",
                            "html_url": "https://example.com/open",
                        },
                        {
                            "number": 11,
                            "title": "Closed PR",
                            "html_url": "https://example.com/closed",
                        },
                    ],
                },
            )
        return httpx.Response(200, json={"items": []})

    client = _client_with_transport(httpx.MockTransport(handler))
    try:
        items = client.get_prs_with_label("test-label", state="all")
    finally:
        gh_audit.set_event_sink(previous_sink)

    numbers = sorted([item["number"] for item in items])
    assert numbers == [10, 11]
    assert any(
        getattr(event, "name", None) == EventName.GH_SEARCH_ITEM_MALFORMED
        for event in sink.events
    )


# -------------------- GraphQL tests --------------------


def test_graphql_successful_query() -> None:
    """Verify _graphql() makes POST to /graphql with query and variables."""
    requests_seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(
            200, json={"data": {"repository": {"pullRequest": {"id": "PR_123"}}}}
        )

    client = _client_with_transport(httpx.MockTransport(handler))
    # noqa: SLF001 - Testing internal GraphQL method error handling
    result = client._graphql(  # noqa: SLF001
        "query($owner: String!) { repository(owner: $owner) { id } }",
        {"owner": "test"},
    )

    assert len(requests_seen) == 1
    assert requests_seen[0]["method"] == "POST"
    assert requests_seen[0]["path"] == "/graphql"
    assert "query" in requests_seen[0]["body"]
    assert requests_seen[0]["body"]["variables"] == {"owner": "test"}
    assert result["data"]["repository"]["pullRequest"]["id"] == "PR_123"


def test_graphql_raises_on_graphql_errors() -> None:
    """Verify _graphql() raises GitHubHttpError on GraphQL-level errors."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": None, "errors": [{"message": "Field 'foo' not found"}]}
        )

    client = _client_with_transport(httpx.MockTransport(handler))

    with pytest.raises(GitHubHttpError) as exc_info:
        client._graphql("query { foo }")  # noqa: SLF001

    assert "Field 'foo' not found" in str(exc_info.value)


def test_graphql_raises_on_http_error() -> None:
    """Verify _graphql() raises GitHubHttpError on HTTP errors."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    client = _client_with_transport(httpx.MockTransport(handler))

    with pytest.raises(GitHubHttpError) as exc_info:
        client._graphql("query { viewer { login } }")  # noqa: SLF001

    assert exc_info.value.status_code == 401


# -------------------- set_pr_draft tests --------------------


def test_set_pr_draft_marks_ready_for_review() -> None:
    """Verify set_pr_draft(draft=False) uses markPullRequestReadyForReview mutation."""
    requests_seen: list[dict] = []
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        body = json.loads(request.content)
        requests_seen.append({"call": call_count, "body": body})

        if call_count == 1:
            # First call: query for node ID
            return httpx.Response(
                200,
                json={"data": {"repository": {"pullRequest": {"id": "PR_node_123"}}}},
            )
        else:
            # Second call: mutation
            return httpx.Response(
                200,
                json={
                    "data": {
                        "markPullRequestReadyForReview": {
                            "pullRequest": {
                                "id": "PR_node_123",
                                "number": 42,
                                "isDraft": False,
                            }
                        }
                    }
                },
            )

    client = _client_with_transport(httpx.MockTransport(handler))
    result = client.set_pr_draft(42, draft=False)

    assert len(requests_seen) == 2
    # First request: get node ID
    assert "pullRequest(number: $number)" in requests_seen[0]["body"]["query"]
    assert requests_seen[0]["body"]["variables"]["number"] == 42
    # Second request: mutation
    assert "markPullRequestReadyForReview" in requests_seen[1]["body"]["query"]
    assert requests_seen[1]["body"]["variables"]["pullRequestId"] == "PR_node_123"
    # Result
    assert result is not None
    assert result["isDraft"] is False
    assert result["number"] == 42


def test_set_pr_draft_converts_to_draft() -> None:
    """Verify set_pr_draft(draft=True) uses convertPullRequestToDraft mutation."""
    requests_seen: list[dict] = []
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        body = json.loads(request.content)
        requests_seen.append({"call": call_count, "body": body})

        if call_count == 1:
            return httpx.Response(
                200,
                json={"data": {"repository": {"pullRequest": {"id": "PR_node_456"}}}},
            )
        else:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "convertPullRequestToDraft": {
                            "pullRequest": {
                                "id": "PR_node_456",
                                "number": 99,
                                "isDraft": True,
                            }
                        }
                    }
                },
            )

    client = _client_with_transport(httpx.MockTransport(handler))
    result = client.set_pr_draft(99, draft=True)

    assert len(requests_seen) == 2
    # Second request should use convertPullRequestToDraft
    assert "convertPullRequestToDraft" in requests_seen[1]["body"]["query"]
    assert result is not None
    assert result["isDraft"] is True


def test_set_pr_draft_raises_on_pr_not_found() -> None:
    """Verify set_pr_draft() raises GitHubHttpError when PR doesn't exist."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"repository": {"pullRequest": None}}})

    client = _client_with_transport(httpx.MockTransport(handler))

    with pytest.raises(GitHubHttpError) as exc_info:
        client.set_pr_draft(9999, draft=False)

    assert "PR #9999 not found" in str(exc_info.value)


def test_get_pr_reviews_returns_list() -> None:
    """Verify get_pr_reviews() returns list of reviews."""
    reviews = [
        {
            "id": 1,
            "state": "CHANGES_REQUESTED",
            "body": "Please add tests",
            "user": {"login": "reviewer1"},
        },
        {
            "id": 2,
            "state": "APPROVED",
            "body": "LGTM",
            "user": {"login": "reviewer2"},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/pulls/123/reviews" in request.url.path
        return httpx.Response(200, json=reviews)

    client = _client_with_transport(httpx.MockTransport(handler))
    result = client.get_pr_reviews(123)

    assert len(result) == 2
    assert result[0]["state"] == "CHANGES_REQUESTED"
    assert result[0]["body"] == "Please add tests"
    assert result[1]["state"] == "APPROVED"


def test_get_pr_reviews_returns_empty_list_on_non_list_response() -> None:
    """Verify get_pr_reviews() returns empty list if response is not a list."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "unexpected"})

    client = _client_with_transport(httpx.MockTransport(handler))
    result = client.get_pr_reviews(123)

    assert result == []


# ---------------------------------------------------------------------------
# get_commit_check_rollup — REST fallback when GraphQL statusCheckRollup is
# inaccessible (see issue #6589).
# ---------------------------------------------------------------------------


def _commit_rollup_handler(
    *,
    check_runs: object,
    statuses: object = None,
    check_runs_status: int = 200,
    status_status: int = 200,
):
    """Route /check-runs and /status for one commit SHA to canned payloads."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/check-runs"):
            return httpx.Response(check_runs_status, json=check_runs)
        if path.endswith("/status"):
            payload = (
                statuses
                if statuses is not None
                else {"state": "pending", "statuses": []}
            )
            return httpx.Response(status_status, json=payload)
        return httpx.Response(404, json={"message": "not found"})

    return handler


def test_get_commit_check_rollup_reports_failure_for_failed_check_run() -> None:
    handler = _commit_rollup_handler(
        check_runs={
            "check_runs": [
                {"name": "validate", "status": "completed", "conclusion": "success"},
                {
                    "name": "system-verification",
                    "status": "completed",
                    "conclusion": "failure",
                },
            ]
        },
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "FAILURE"
    assert rollup.complete is True


def test_get_commit_check_rollup_reports_pending_for_incomplete_check_run() -> None:
    handler = _commit_rollup_handler(
        check_runs={
            "check_runs": [
                {"name": "validate", "status": "completed", "conclusion": "success"},
                {
                    "name": "system-verification",
                    "status": "in_progress",
                    "conclusion": None,
                },
            ]
        },
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "PENDING"
    assert rollup.complete is True


def test_get_commit_check_rollup_reports_success_when_all_complete() -> None:
    handler = _commit_rollup_handler(
        check_runs={
            "check_runs": [
                {"name": "validate", "status": "completed", "conclusion": "success"},
                {"name": "lint", "status": "completed", "conclusion": "skipped"},
            ]
        },
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "SUCCESS"
    assert rollup.complete is True


def test_get_commit_check_rollup_returns_none_when_no_checks_or_statuses() -> None:
    handler = _commit_rollup_handler(check_runs={"check_runs": []})
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state is None
    assert rollup.complete is True


def test_get_commit_check_rollup_incomplete_when_check_runs_inaccessible() -> None:
    """A 403 on the check-runs API while the combined-status source has nothing
    conclusive must NOT raise or claim 'no checks': the source is unreadable, so
    the rollup is reported incomplete (the caller maps this to unreadable)."""
    handler = _commit_rollup_handler(
        check_runs={"message": "Resource not accessible by personal access token"},
        check_runs_status=403,
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state is None
    assert rollup.complete is False


def test_get_commit_check_rollup_legacy_status_failure_survives_check_runs_403() -> (
    None
):
    """The core #6589 path: GraphQL inaccessible, /check-runs inaccessible, but
    /commits/{sha}/status is readable and reports failure. The readable legacy
    status failure is conclusive, so the rollup is a complete FAILURE and the PR
    routes to rework instead of waiting out the unreadable-checks timeout."""
    handler = _commit_rollup_handler(
        check_runs={"message": "Resource not accessible by personal access token"},
        check_runs_status=403,
        statuses={
            "state": "failure",
            "statuses": [{"context": "ci/external", "state": "failure"}],
        },
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "FAILURE"
    assert rollup.complete is True


def test_get_commit_check_rollup_pending_incomplete_when_check_runs_403() -> None:
    """A readable legacy PENDING status with check-runs inaccessible is NOT
    conclusive: the unread check-runs source could hold a failed required run,
    and FAILURE outranks PENDING. The rollup must report complete=False so the
    caller escalates as unreadable rather than waiting it out as checks-pending
    (which could mask a hidden failure as a pending-checks timeout)."""
    handler = _commit_rollup_handler(
        check_runs={"message": "Resource not accessible by personal access token"},
        check_runs_status=403,
        statuses={
            "state": "pending",
            "statuses": [{"context": "ci/external", "state": "pending"}],
        },
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "PENDING"
    assert rollup.complete is False


def test_get_commit_check_rollup_pending_incomplete_when_status_403() -> None:
    """The opposite direction: readable pending check-runs with the legacy
    combined-status source inaccessible is likewise inconclusive — the unread
    status source could hold a failed required legacy status that outranks
    pending — so the rollup is incomplete, not a complete PENDING."""
    handler = _commit_rollup_handler(
        check_runs={
            "check_runs": [
                {"name": "validate", "status": "in_progress", "conclusion": None},
            ]
        },
        status_status=403,
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "PENDING"
    assert rollup.complete is False


def test_get_commit_check_rollup_folds_in_legacy_commit_statuses() -> None:
    """External CI posting a legacy commit status (not a check-run) must still
    surface as a failure even when there are no check-runs."""
    handler = _commit_rollup_handler(
        check_runs={"check_runs": []},
        statuses={
            "state": "failure",
            "statuses": [{"context": "ci/external", "state": "failure"}],
        },
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "FAILURE"
    assert rollup.complete is True


def test_get_commit_check_rollup_failure_wins_even_when_status_api_fails() -> None:
    """A failed check-run is conclusive: an inaccessible legacy-status source
    cannot change a FAILURE, so the rollup stays complete and routes to rework."""
    handler = _commit_rollup_handler(
        check_runs={
            "check_runs": [
                {"name": "validate", "status": "completed", "conclusion": "failure"},
            ]
        },
        status_status=403,
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "FAILURE"
    assert rollup.complete is True


def test_get_commit_check_rollup_incomplete_when_status_api_fails_and_checks_pass() -> (
    None
):
    """A 403 on the legacy combined-status source while check-runs are all green
    is INCONCLUSIVE: a required legacy status could be failing unseen, so the
    rollup must report complete=False rather than a false SUCCESS (issue #6589)."""
    handler = _commit_rollup_handler(
        check_runs={
            "check_runs": [
                {"name": "validate", "status": "completed", "conclusion": "success"},
            ]
        },
        status_status=403,
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "SUCCESS"
    assert rollup.complete is False


def test_get_commit_check_rollup_incomplete_when_status_api_fails_and_no_checks() -> (
    None
):
    """A 403 on the legacy combined-status source with zero check-runs cannot
    honestly claim "no checks": the status source might hold a required status."""
    handler = _commit_rollup_handler(
        check_runs={"check_runs": []},
        status_status=403,
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state is None
    assert rollup.complete is False


def _paginated_check_runs_handler(
    pages: dict[int, object],
    *,
    statuses: object = None,
    status_status: int = 200,
):
    """Serve /check-runs from a {page: payload} map, honoring the ?page param.

    Pages absent from the map return an empty page. /status returns ``statuses``
    (or an empty combined status) with ``status_status``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/check-runs"):
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=pages.get(page, {"check_runs": []}))
        if path.endswith("/status"):
            payload = (
                statuses
                if statuses is not None
                else {"state": "pending", "statuses": []}
            )
            return httpx.Response(status_status, json=payload)
        return httpx.Response(404, json={"message": "not found"})

    return handler


def test_get_commit_check_rollup_paginates_to_failure_on_later_page() -> None:
    """A failure on a later check-runs page must not be missed: page 1 is a full
    page of passing runs, page 2 carries the failure (issue #6589 F1)."""
    page1 = {
        "check_runs": [
            {"name": f"shard-{i}", "status": "completed", "conclusion": "success"}
            for i in range(100)
        ]
    }
    page2 = {
        "check_runs": [
            {
                "name": "system-verification",
                "status": "completed",
                "conclusion": "failure",
            },
        ]
    }
    handler = _paginated_check_runs_handler({1: page1, 2: page2})
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "FAILURE"
    assert rollup.complete is True


def test_get_commit_check_rollup_failure_on_later_page_beats_earlier_pending() -> None:
    """A pending run must NOT short-circuit pagination: a completed failure on a
    later page outranks an earlier in-progress run, so the rollup is FAILURE and
    the PR routes to rework instead of timing out as checks-pending (#6589 F1)."""
    page1 = {
        "check_runs": (
            [
                {"name": f"shard-{i}", "status": "completed", "conclusion": "success"}
                for i in range(99)
            ]
            + [{"name": "slow-shard", "status": "in_progress", "conclusion": None}]
        )
    }
    page2 = {
        "check_runs": [
            {
                "name": "system-verification",
                "status": "completed",
                "conclusion": "failure",
            },
        ]
    }
    handler = _paginated_check_runs_handler({1: page1, 2: page2})
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "FAILURE"
    assert rollup.complete is True


def test_get_commit_check_rollup_paginates_all_pages_success() -> None:
    """All pages green still aggregates to SUCCESS after walking every page."""
    page1 = {
        "check_runs": [
            {"name": f"shard-{i}", "status": "completed", "conclusion": "success"}
            for i in range(100)
        ]
    }
    page2 = {
        "check_runs": [
            {"name": "lint", "status": "completed", "conclusion": "success"},
        ]
    }
    handler = _paginated_check_runs_handler({1: page1, 2: page2})
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "SUCCESS"
    assert rollup.complete is True


def test_get_commit_check_rollup_cap_hit_without_failure_is_incomplete() -> None:
    """The check-runs pagination safety cap must not mask a later-page failure:
    20 full pages of passing runs (cap reached on a full page, so page 21
    exists) with the failure sitting on the unread page 21. The cap-hit source
    is reported unreadable, so the rollup is INCOMPLETE rather than a false
    complete SUCCESS that would route to branch-protection escalation instead of
    rework / an unreadable-rollup escalation (issue #6589 F1)."""
    full_success_page = {
        "check_runs": [
            {"name": f"shard-{i}", "status": "completed", "conclusion": "success"}
            for i in range(100)
        ]
    }
    pages: dict[int, object] = {p: full_success_page for p in range(1, 21)}
    # Page 21 carries a failure the cap stops us from ever reading.
    pages[21] = {
        "check_runs": [
            {
                "name": "system-verification",
                "status": "completed",
                "conclusion": "failure",
            },
        ]
    }
    handler = _paginated_check_runs_handler(pages)
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.complete is False
    # A truncated read is NOT a permission failure: it must stay transient so
    # the gate retries/waits instead of arming the repo-wide permission backoff.
    assert rollup.capability == "transient_error"


def test_get_commit_check_rollup_cap_hit_stays_complete_on_readable_legacy_failure() -> (
    None
):
    """A cap-hit check-runs read is only inconclusive on its own: if the
    readable legacy combined-status source already reports FAILURE, the
    aggregate is a conclusive FAILURE and stays complete (an unread check-runs
    page cannot make it less severe), so the PR still routes to rework."""
    full_success_page = {
        "check_runs": [
            {"name": f"shard-{i}", "status": "completed", "conclusion": "success"}
            for i in range(100)
        ]
    }
    pages: dict[int, object] = {p: full_success_page for p in range(1, 21)}
    handler = _paginated_check_runs_handler(
        pages,
        statuses={
            "state": "failure",
            "statuses": [{"context": "ci/external", "state": "failure"}],
        },
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "FAILURE"
    assert rollup.complete is True


def test_get_commit_check_rollup_check_runs_5xx_is_transient() -> None:
    """A 500 on /check-runs (with nothing conclusive elsewhere) is a retryable
    blip, NOT a missing scope: the rollup capability is transient_error so the
    gate retries next tick rather than arming the permission backoff and
    escalating a bogus missing-scope diagnostic (issue #6589 F1/A1)."""
    handler = _commit_rollup_handler(
        check_runs={"message": "Server Error"},
        check_runs_status=500,
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.capability == "transient_error"
    assert rollup.complete is False


def test_get_commit_check_rollup_check_runs_rate_limit_is_transient() -> None:
    """GitHub returns HTTP 403 for secondary rate limits; the body names no
    scope, so it classifies as transient_error rather than permission_denied."""
    handler = _commit_rollup_handler(
        check_runs={
            "message": (
                "You have exceeded a secondary rate limit. "
                "Please wait a few minutes before you try again."
            )
        },
        check_runs_status=403,
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.capability == "transient_error"


def test_get_commit_check_rollup_status_5xx_is_transient() -> None:
    """A 500 on the legacy combined-status source (check-runs all green) is a
    retryable blip: the unread status could still hold a failure, so the rollup
    is incomplete, but as transient_error (retry), not permission_denied."""
    handler = _commit_rollup_handler(
        check_runs={
            "check_runs": [
                {"name": "validate", "status": "completed", "conclusion": "success"},
            ]
        },
        status_status=500,
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.capability == "transient_error"


def test_get_commit_check_rollup_check_runs_403_scope_is_permission_denied() -> None:
    """A genuine missing-scope 403 on /check-runs (body names the gap) stays
    permission_denied so the operator is told to fix the token scope."""
    handler = _commit_rollup_handler(
        check_runs={"message": "Resource not accessible by personal access token"},
        check_runs_status=403,
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.capability == "permission_denied"


def test_get_commit_check_rollup_cancelled_check_run_is_failure() -> None:
    """A completed `cancelled` required check is non-passing: GitHub does not
    treat it as acceptable, so the REST fallback must report FAILURE (route to
    rework), not a false SUCCESS (issue #6589 F2)."""
    handler = _commit_rollup_handler(
        check_runs={
            "check_runs": [
                {"name": "validate", "status": "completed", "conclusion": "success"},
                {
                    "name": "system-verification",
                    "status": "completed",
                    "conclusion": "cancelled",
                },
            ]
        },
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "FAILURE"
    assert rollup.complete is True


def test_get_commit_check_rollup_stale_check_run_is_failure() -> None:
    """A completed `stale` required check is likewise non-passing and must not
    be reported as SUCCESS (issue #6589 F2)."""
    handler = _commit_rollup_handler(
        check_runs={
            "check_runs": [
                {"name": "validate", "status": "completed", "conclusion": "stale"},
            ]
        },
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "FAILURE"
    assert rollup.complete is True


def test_get_commit_check_rollup_neutral_and_skipped_runs_are_passing() -> None:
    """`neutral` and `skipped` are acceptable conclusions: an all-neutral/skipped
    set must stay SUCCESS so it does not trigger spurious rework."""
    handler = _commit_rollup_handler(
        check_runs={
            "check_runs": [
                {"name": "advisory", "status": "completed", "conclusion": "neutral"},
                {"name": "optional", "status": "completed", "conclusion": "skipped"},
            ]
        },
    )
    client = _client_with_transport(httpx.MockTransport(handler))

    rollup = client.get_commit_check_rollup("deadbeef")
    assert rollup.state == "SUCCESS"
    assert rollup.complete is True


# ---------------------------------------------------------------------------
# list_all_milestones: authoritative all-state exhaustive read
# ---------------------------------------------------------------------------

def test_list_all_milestones_requests_all_states_and_walks_pages() -> None:
    """The doctor makes a NEGATIVE-existence decision from this list, so the
    read must cover every state and every page. A state filter would hide a
    closed foundation milestone; stopping at page 1 would hide a later one.
    Either omission turns a correct configuration into a false report."""
    seen: list[dict[str, str]] = []
    page1 = [{"title": f"M{n}", "number": n} for n in range(100)]
    page2 = [{"title": "M0 - Foundation", "number": 100, "state": "closed"}]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "path": request.url.path,
                "state": request.url.params.get("state", ""),
                "per_page": request.url.params.get("per_page", ""),
                "page": request.url.params.get("page", "1"),
            }
        )
        return httpx.Response(200, json=page1 if seen[-1]["page"] == "1" else page2)

    client = _client_with_transport(httpx.MockTransport(handler))
    milestones = client.list_all_milestones()

    assert [s["page"] for s in seen] == ["1", "2"]
    assert {s["state"] for s in seen} == {"all"}
    assert {s["per_page"] for s in seen} == {"100"}
    assert all(s["path"].endswith("/milestones") for s in seen)
    # The page-2 entry survives — this is the one the doctor would otherwise
    # report as missing.
    assert {m["title"] for m in milestones} == {m["title"] for m in page1} | {"M0 - Foundation"}


def test_list_all_milestones_short_first_page_stops() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"title": "M0", "number": 1}])

    client = _client_with_transport(httpx.MockTransport(handler))

    assert [m["title"] for m in client.list_all_milestones()] == ["M0"]


def test_list_all_milestones_empty_list_is_valid_exhaustion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = _client_with_transport(httpx.MockTransport(handler))

    assert client.list_all_milestones() == []


def test_list_all_milestones_non_list_body_fails_loud() -> None:
    """A 2xx carrying a non-list body is a contract violation, not exhaustion.

    Returning [] here would hand the doctor an empty snapshot, which it reads as
    "this repo has no milestones" and reports nothing at all — recreating the
    silent diagnostic failure the fail-loud pager exists to prevent.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "unexpected shape"})

    client = _client_with_transport(httpx.MockTransport(handler))
    with pytest.raises(GitHubHttpError):
        client.list_all_milestones()


def test_list_all_milestones_later_page_non_200_fails_loud() -> None:
    page1 = [{"title": f"M{n}", "number": n} for n in range(100)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page", "1") == "1":
            return httpx.Response(200, json=page1)
        return httpx.Response(502, text="bad gateway")

    client = _client_with_transport(httpx.MockTransport(handler))
    with pytest.raises(GitHubHttpError):
        client.list_all_milestones()


def test_issue_closed_on_or_after_true_at_exact_merge_timestamp() -> None:
    """GitHub's auto-close event lands at (or just after) the merge moment;
    the boundary must count as evidence the auto-close fired."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"event": "labeled", "created_at": "2026-08-03T13:00:00Z"},
            {"event": "closed", "created_at": "2026-08-03T13:52:09Z"},
        ])

    client = _client_with_transport(httpx.MockTransport(handler))

    assert client.issue_closed_on_or_after(45, "2026-08-03T13:52:09Z") is True


def test_issue_closed_on_or_after_ignores_pre_merge_closures() -> None:
    """A close that happened (and was reopened) BEFORE the merge is not
    evidence the merge's auto-close fired."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"event": "closed", "created_at": "2026-08-01T10:00:00Z"},
            {"event": "reopened", "created_at": "2026-08-01T11:00:00Z"},
        ])

    client = _client_with_transport(httpx.MockTransport(handler))

    assert client.issue_closed_on_or_after(45, "2026-08-03T13:52:09Z") is False


def test_issue_closed_on_or_after_finds_event_beyond_first_page() -> None:
    """The close event may sit past the first 100 timeline events."""
    pages_requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        pages_requested.append(page)
        if page == 1:
            return httpx.Response(
                200,
                json=[
                    {"event": "labeled", "created_at": "2026-08-03T12:00:00Z"}
                    for _ in range(100)
                ],
            )
        return httpx.Response(200, json=[
            {"event": "closed", "created_at": "2026-08-03T14:00:00Z"},
        ])

    client = _client_with_transport(httpx.MockTransport(handler))

    assert client.issue_closed_on_or_after(45, "2026-08-03T13:52:09Z") is True
    assert pages_requested == [1, 2]


def test_issue_closed_on_or_after_fails_loud_on_non_list_payload() -> None:
    """A malformed 2xx body is a read error, not evidence of absence — the
    caller decides a destructive close on this fact."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "unexpected"})

    client = _client_with_transport(httpx.MockTransport(handler))

    with pytest.raises(GitHubHttpError):
        client.issue_closed_on_or_after(45, "2026-08-03T13:52:09Z")


# -------------------- Closing-PR linkage (#6957) --------------------


def _closing_pr_client(nodes: list[dict]) -> tuple[GitHubHttpClient, list[dict]]:
    """A client whose GraphQL endpoint replays *nodes* as CLOSED_EVENTs."""
    requests_seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {"issue": {"timelineItems": {"nodes": nodes}}}
                }
            },
        )

    return _client_with_transport(httpx.MockTransport(handler)), requests_seen


def test_closing_pull_request_reports_the_merged_closer() -> None:
    client, _ = _closing_pr_client(
        [{"closer": {"number": 42, "url": "https://x/pull/42", "merged": True}}]
    )

    assert client.get_closing_pull_request(501) == {
        "number": 42,
        "url": "https://x/pull/42",
        "merged": True,
    }


def test_closing_pull_request_only_asks_for_the_newest_close() -> None:
    """The query itself is bounded to the close that left the issue closed."""
    client, requests_seen = _closing_pr_client([])

    client.get_closing_pull_request(501)

    assert "last: 1" in requests_seen[0]["query"]
    assert requests_seen[0]["variables"] == {
        "owner": "owner",
        "repo": "repo",
        "number": 501,
    }


def test_a_newer_manual_close_supersedes_an_older_merged_pr_close() -> None:
    """#6957 round-2 review F4: reopened-then-manually-closed is a DECLINE.

    An issue closed by a merged PR, reopened, then closed by hand has two
    ClosedEvents. Scanning backward for *any* PR-backed close returned the stale
    merge and recorded a manual decline as shipped, permanently closing the
    source case file. Only the newest close counts.
    """
    client, _ = _closing_pr_client(
        [
            # Older: closed by a merged PR...
            {"closer": {"number": 42, "url": "https://x/pull/42", "merged": True}},
            # ...reopened, then most recently closed by a human.
            {"closer": None},
        ]
    )

    assert client.get_closing_pull_request(501) is None


def test_a_newer_unmerged_pr_close_supersedes_an_older_merged_one() -> None:
    client, _ = _closing_pr_client(
        [
            {"closer": {"number": 42, "url": "https://x/pull/42", "merged": True}},
            {"closer": {"number": 43, "url": "https://x/pull/43", "merged": False}},
        ]
    )

    outcome = client.get_closing_pull_request(501)

    assert outcome is not None
    assert outcome["number"] == 43 and outcome["merged"] is False


def test_never_closed_issue_has_no_closing_pull_request() -> None:
    client, _ = _closing_pr_client([])

    assert client.get_closing_pull_request(501) is None
