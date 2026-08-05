"""Bearer-token middleware tests for the Control API.

See security issue #5987 (F3) + #6017 review. When
``configure_api_token`` has been called, every HTTP request to the
Control API must carry an ``Authorization: Bearer <token>`` header
matching either the admin secret or — for the narrow allowlist in
``_AGENT_CALLBACK_ROUTES`` — the scoped agent-callback secret. When
unconfigured (the default in unit tests), the middleware is a no-op.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints.control_api import (
    configure_api_token,
    control_app,
    get_configured_agent_callback_token,
    get_configured_api_token,
)


@pytest.fixture
def authed_client():
    """TestClient with the admin token enabled; no agent-callback token."""
    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    configure_api_token("test-admin-token", agent_callback=None)
    try:
        yield TestClient(control_app)
    finally:
        configure_api_token(prev_admin, agent_callback=prev_agent)


@pytest.fixture
def dual_token_client():
    """TestClient with both admin and agent-callback tokens configured."""
    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    configure_api_token("test-admin-token", agent_callback="test-agent-token")
    try:
        yield TestClient(control_app)
    finally:
        configure_api_token(prev_admin, agent_callback=prev_agent)


def test_missing_header_returns_401(
    authed_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(
        logging.INFO, logger="issue_orchestrator.entrypoints._auth_middleware"
    )

    resp = authed_client.get("/api/status")

    assert resp.status_code == 401
    # Message is "missing credentials" since neither the bearer path
    # nor the browser-session path matched.
    assert "credentials" in resp.json()["error"]
    assert "Auth rejected GET /api/status on control_api" in caplog.text
    assert "missing credentials" in caplog.text
    assert any(record.levelno == logging.INFO for record in caplog.records)


def test_wrong_scheme_returns_401(authed_client: TestClient) -> None:
    resp = authed_client.get(
        "/api/status", headers={"Authorization": "Basic dXNlcjpwYXNz"}
    )

    assert resp.status_code == 401
    # "Basic" doesn't match the Bearer branch; falls through to the
    # browser-session path, which also fails (no cookie) → 401.
    assert "credentials" in resp.json()["error"]


def test_wrong_token_returns_401(
    authed_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(
        logging.WARNING, logger="issue_orchestrator.entrypoints._auth_middleware"
    )

    resp = authed_client.get(
        "/api/status", headers={"Authorization": "Bearer wrong-token"}
    )

    assert resp.status_code == 401
    assert resp.json() == {"error": "invalid bearer token"}
    assert "invalid bearer token" in caplog.text
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_correct_token_passes_middleware(authed_client: TestClient) -> None:
    # /api/status requires an orchestrator to be set to return a 200; the
    # point here is that middleware does NOT reject it. Anything other
    # than 401 proves the token check passed.
    resp = authed_client.get(
        "/api/status", headers={"Authorization": "Bearer test-admin-token"}
    )

    assert resp.status_code != 401


def test_no_token_configured_means_no_enforcement() -> None:
    """Default unit-test setup: auth is off and requests flow through."""
    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    configure_api_token(None, agent_callback=None)
    try:
        client = TestClient(control_app)
        resp = client.get("/api/status")
        assert resp.status_code != 401
    finally:
        configure_api_token(prev_admin, agent_callback=prev_agent)


def test_mutating_route_also_requires_token(authed_client: TestClient) -> None:
    """POST routes must be gated, not just GETs."""
    resp = authed_client.post("/api/pause")

    assert resp.status_code == 401


def test_fake_auth_control_api_mode_catches_missing_csrf(
    auth_enabled_control_client: TestClient,
    fake_browser_auth,
) -> None:
    fake_browser_auth.login(auth_enabled_control_client)

    missing_csrf = auth_enabled_control_client.post("/api/resume")
    assert missing_csrf.status_code == 403
    assert "csrf" in missing_csrf.json()["error"].lower()

    with_csrf = auth_enabled_control_client.post(
        "/api/resume",
        headers=fake_browser_auth.csrf_headers(auth_enabled_control_client),
    )
    assert with_csrf.status_code not in (401, 403), with_csrf.text


def test_sse_route_also_requires_token(authed_client: TestClient) -> None:
    """The SSE event stream leaks internal state; it must require auth too."""
    resp = authed_client.get("/api/events")

    assert resp.status_code == 401


def test_agent_callback_token_allows_preflight_push(
    dual_token_client: TestClient,
) -> None:
    """Agent-callback token works on the allowlisted route."""
    resp = dual_token_client.post(
        "/api/preflight-push",
        headers={"Authorization": "Bearer test-agent-token"},
        json={"worktree": "/tmp"},
    )

    # Anything other than 401 proves the middleware accepted the token.
    assert resp.status_code != 401


def test_agent_callback_token_allows_issue_resume(
    dual_token_client: TestClient,
) -> None:
    """Resume routes are in the agent-callback allowlist."""
    resp = dual_token_client.post(
        "/api/issues/123/resume",
        headers={"Authorization": "Bearer test-agent-token"},
    )

    assert resp.status_code != 401


def test_agent_callback_token_rejected_on_admin_only_route(
    dual_token_client: TestClient,
) -> None:
    """Agent-callback token must NOT grant access to e.g. pause / shutdown.

    Regression for #6017 review P2: sharing the admin token with
    untrusted agents was exactly the scoping failure called out by the
    reviewer. The scoped token must be scoped.
    """
    resp = dual_token_client.post(
        "/api/pause", headers={"Authorization": "Bearer test-agent-token"}
    )

    assert resp.status_code == 401
    assert resp.json() == {"error": "invalid bearer token"}


def test_agent_callback_token_rejected_on_sse(
    dual_token_client: TestClient,
) -> None:
    resp = dual_token_client.get(
        "/api/events", headers={"Authorization": "Bearer test-agent-token"}
    )

    assert resp.status_code == 401


def test_admin_token_still_works_on_allowlisted_route(
    dual_token_client: TestClient,
) -> None:
    resp = dual_token_client.post(
        "/api/preflight-push",
        headers={"Authorization": "Bearer test-admin-token"},
        json={"worktree": "/tmp"},
    )

    assert resp.status_code != 401


def test_sse_token_endpoint_sets_no_store_cache_headers(
    browser_auth_client: TestClient,
) -> None:
    """The SSE token must never be cached by browsers or proxies (#6017
    re-review-3 P2 mitigation).
    """
    _login_and_get_session(browser_auth_client)

    resp = browser_auth_client.get("/api/sse-token")

    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.headers.get("pragma") == "no-cache"
    assert resp.headers.get("referrer-policy") == "no-referrer"


def test_sse_token_endpoint_tokens_are_single_use(
    browser_auth_client: TestClient,
) -> None:
    """First-use succeeds; replay within TTL is rejected."""
    _login_and_get_session(browser_auth_client)

    issued = browser_auth_client.get("/api/sse-token")
    assert issued.status_code == 200
    token = issued.json()["sse_token"]

    with browser_auth_client.stream(
        "GET", f"/api/events?sse_token={token}"
    ) as first:
        assert first.status_code not in (401, 403)

    replay = browser_auth_client.get(f"/api/events?sse_token={token}")
    assert replay.status_code == 401


def test_access_log_redaction_scrubs_sse_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The uvicorn.access logger must never emit a raw sse_token.

    Pins the filter installation + substitution pattern together so a
    refactor that drops either half trips this test.
    """
    import logging as _logging

    from issue_orchestrator.entrypoints._auth_middleware import (
        install_access_log_redaction,
    )

    install_access_log_redaction()
    logger = _logging.getLogger("uvicorn.access")
    with caplog.at_level(_logging.INFO, logger="uvicorn.access"):
        logger.info(
            '127.0.0.1:0 - "GET /api/events?sse_token=abc123def456 HTTP/1.1" 200'
        )

    combined = " ".join(r.getMessage() for r in caplog.records)
    assert "abc123def456" not in combined
    assert "sse_token=REDACTED" in combined


def test_bearer_token_comparison_is_constant_time() -> None:
    """verify_token must accept the exact token and reject anything else.

    Actual timing-attack resistance is covered in test_api_token.py; this
    test pins the behavioural contract that the middleware relies on.
    """
    from issue_orchestrator.infra.api_token import verify_token

    assert verify_token("abc", "abc")
    assert not verify_token("abc", "abd")
    assert not verify_token("abc", "abcd")


# ---------------------------------------------------------------------------
# Browser session path (cookie + CSRF + SSE token) — security #6017 P3.
# ---------------------------------------------------------------------------


@pytest.fixture
def browser_auth_client():
    """TestClient with admin token enabled AND browser session initialized."""
    from issue_orchestrator.infra import browser_session

    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    configure_api_token("test-admin-token", agent_callback="test-agent-token")
    browser_session.initialize(secret=b"test-secret")
    try:
        yield TestClient(control_app)
    finally:
        browser_session.shutdown()
        configure_api_token(prev_admin, agent_callback=prev_agent)


def _login_and_get_session(client: TestClient) -> tuple[str, str]:
    """Log in via ``POST /login`` and return ``(session_id, csrf_token)``."""
    from issue_orchestrator.infra import browser_session

    resp = client.post(
        "/login",
        headers={"Content-Type": "application/json"},
        json={"token": "test-admin-token"},
    )
    assert resp.status_code == 200, resp.text
    session_id = resp.cookies.get(browser_session.SESSION_COOKIE)
    assert session_id
    csrf = browser_session.get_csrf_token(session_id)
    assert csrf
    return session_id, csrf


def test_root_without_session_serves_login_page(
    browser_auth_client: TestClient,
) -> None:
    """GET / with no session must NOT mint credentials — show login.

    Regression for #6017 re-review-2 P1: earlier versions set an
    ``io_session`` cookie and CSRF token for any anonymous visitor,
    letting a local process scrape them and call mutating routes
    without the admin token.
    """
    resp = browser_auth_client.get("/")

    assert resp.status_code == 200
    assert "Sign in" in resp.text
    assert "Paste the local admin token" in resp.text
    assert "This is not your GitHub token" in resp.text
    assert "io-csrf-token" not in resp.text
    from issue_orchestrator.infra import browser_session

    assert resp.cookies.get(browser_session.SESSION_COOKIE) is None


def test_login_with_wrong_token_does_not_issue_session(
    browser_auth_client: TestClient,
) -> None:
    resp = browser_auth_client.post(
        "/login",
        headers={"Content-Type": "application/json"},
        json={"token": "wrong-token"},
    )

    assert resp.status_code == 401
    from issue_orchestrator.infra import browser_session

    assert resp.cookies.get(browser_session.SESSION_COOKIE) is None


def test_login_with_correct_token_issues_session(
    browser_auth_client: TestClient,
) -> None:
    sid, csrf = _login_and_get_session(browser_auth_client)

    assert sid and csrf


def test_root_with_session_serves_dashboard(
    browser_auth_client: TestClient,
) -> None:
    sid, _csrf = _login_and_get_session(browser_auth_client)

    resp = browser_auth_client.get("/")

    assert resp.status_code == 200
    assert '<meta name="io-browser-auth-required" content="1">' in resp.text
    assert "io-csrf-token" in resp.text
    assert "Sign in" not in resp.text


def test_mutating_request_without_csrf_is_rejected(
    browser_auth_client: TestClient,
) -> None:
    _login_and_get_session(browser_auth_client)

    resp = browser_auth_client.post("/api/pause")

    assert resp.status_code in (401, 403)


def test_mutating_request_with_valid_csrf_is_accepted(
    browser_auth_client: TestClient,
) -> None:
    _sid, csrf = _login_and_get_session(browser_auth_client)

    resp = browser_auth_client.post(
        "/api/pause", headers={"X-CSRF-Token": csrf}
    )

    assert resp.status_code not in (401, 403)


def test_sse_requires_sse_token_even_with_session(
    browser_auth_client: TestClient,
) -> None:
    _login_and_get_session(browser_auth_client)

    resp = browser_auth_client.get("/api/events")

    assert resp.status_code == 401


def test_sse_endpoint_accepts_issued_sse_token(
    browser_auth_client: TestClient,
) -> None:
    sid, _csrf = _login_and_get_session(browser_auth_client)
    from issue_orchestrator.infra import browser_session

    token = browser_session.issue_sse_token(sid)
    assert token is not None

    with browser_auth_client.stream(
        "GET", f"/api/events?sse_token={token}"
    ) as resp:
        assert resp.status_code not in (401, 403)


def test_sse_token_endpoint_requires_session(
    browser_auth_client: TestClient,
) -> None:
    resp = browser_auth_client.get("/api/sse-token")

    assert resp.status_code == 401


def test_sse_token_endpoint_returns_token_when_session_present(
    browser_auth_client: TestClient,
) -> None:
    _login_and_get_session(browser_auth_client)

    resp = browser_auth_client.get("/api/sse-token")

    assert resp.status_code == 200
    body = resp.json()
    assert "sse_token" in body
    assert body["ttl_seconds"] > 0


def test_bearer_token_path_still_works_alongside_browser_session(
    browser_auth_client: TestClient,
) -> None:
    """MCP / CLI with bearer token should not need cookies at all."""
    resp = browser_auth_client.post(
        "/api/pause", headers={"Authorization": "Bearer test-admin-token"}
    )

    assert resp.status_code not in (401, 403)


def test_scraped_csrf_without_login_cannot_mutate(
    browser_auth_client: TestClient,
) -> None:
    """Even if an attacker somehow obtains a CSRF-looking string,
    without a valid session cookie the mutation is rejected.

    Regression for #6017 re-review-2 P1 second-order attack.
    """
    # Manufacture a CSRF value directly via the session store — this is
    # the best case for the attacker. Without the matching cookie the
    # middleware must still reject.
    from issue_orchestrator.infra import browser_session

    session_id, csrf = browser_session.create_session()
    del session_id  # we intentionally do NOT send the cookie

    resp = browser_auth_client.post(
        "/api/pause", headers={"X-CSRF-Token": csrf}
    )

    assert resp.status_code in (401, 403)


def test_form_login_redirects_to_dashboard(
    browser_auth_client: TestClient,
) -> None:
    resp = browser_auth_client.post(
        "/login",
        data={"token": "test-admin-token"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers.get("location") == "/"
    from issue_orchestrator.infra import browser_session

    assert resp.cookies.get(browser_session.SESSION_COOKIE)


def test_form_login_with_wrong_token_returns_login_page(
    browser_auth_client: TestClient,
) -> None:
    resp = browser_auth_client.post("/login", data={"token": "bad"})

    assert resp.status_code == 200
    assert "Invalid token" in resp.text
