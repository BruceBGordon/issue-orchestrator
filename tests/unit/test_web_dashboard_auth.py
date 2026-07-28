"""Auth middleware tests for the Web Dashboard (port 8080).

Parallels ``test_control_api_auth.py`` — the dashboard uses the same
three-path gate (bearer / session cookie + CSRF / SSE query token)
that the Control Center shipped in #6011. This module pins the gate
against the actual dashboard surface.

See security #5987 F3. The gap this closes: before PR 8 every
dashboard route (``/api/shutdown``, ``/api/open-file``, ``/api/kill``,
the ``/api/events`` SSE stream, and the ``/api/test/*`` fixtures) was
reachable with no credentials.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints.web import (
    app,
    configure_dashboard_admin_token,
    get_configured_dashboard_admin_token,
)
from issue_orchestrator.infra import browser_session


@pytest.fixture
def authed_client():
    """TestClient with dashboard auth turned on."""
    prev = get_configured_dashboard_admin_token()
    configure_dashboard_admin_token("test-admin-token")
    try:
        yield TestClient(app)
    finally:
        configure_dashboard_admin_token(prev)


@pytest.fixture
def open_client():
    """TestClient with dashboard auth disabled (test default)."""
    prev = get_configured_dashboard_admin_token()
    configure_dashboard_admin_token(None)
    try:
        yield TestClient(app)
    finally:
        configure_dashboard_admin_token(prev)


# ---------------------------------------------------------------------------
# Bearer token path
# ---------------------------------------------------------------------------


def test_missing_header_on_mutating_route_returns_401(
    authed_client: TestClient,
) -> None:
    # /api/test/cleanup was an unauthenticated destructive endpoint
    # before PR 8 — pin it.
    resp = authed_client.post("/api/test/cleanup")
    assert resp.status_code == 401
    assert "credentials" in resp.json()["error"]


def test_wrong_bearer_token_returns_401(authed_client: TestClient) -> None:
    resp = authed_client.post(
        "/api/test/cleanup",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"error": "invalid bearer token"}


def test_correct_bearer_token_passes_middleware(
    authed_client: TestClient,
) -> None:
    """Valid bearer passes auth — the 500 downstream is immaterial
    because the orchestrator isn't wired in this test; what matters
    is that the middleware did not itself return 401.
    """
    resp = authed_client.post(
        "/api/test/cleanup",
        headers={"Authorization": "Bearer test-admin-token"},
    )
    assert resp.status_code != 401


def test_get_view_model_requires_auth(authed_client: TestClient) -> None:
    resp = authed_client.get("/api/view-model")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Public paths
# ---------------------------------------------------------------------------


def test_static_asset_is_public(authed_client: TestClient) -> None:
    # Any request under /static/ should bypass auth. Even a 404 (the
    # asset may not exist in this layout) proves the gate didn't
    # short-circuit.
    resp = authed_client.get("/static/brand/logo.svg")
    assert resp.status_code != 401


def test_favicon_is_public(authed_client: TestClient) -> None:
    resp = authed_client.get("/favicon.ico")
    assert resp.status_code == 200


def test_root_renders_login_form_when_unauthenticated(
    authed_client: TestClient,
) -> None:
    """Anonymous visitor to ``/`` gets the login form, not the raw
    dashboard HTML or a 401 JSON blob. Parallels the Control Center.
    """
    resp = authed_client.get("/")
    assert resp.status_code == 200
    assert "Sign in" in resp.text
    assert 'action="/login"' in resp.text


def test_authenticated_root_renders_csrf_meta_and_browser_auth_script(
    logged_in_dashboard_client: TestClient,
) -> None:
    """The repository dashboard must bootstrap the same browser auth
    adapter as Control Center. Otherwise embedded Resume/Pause POSTs
    fail CSRF and EventSource reconnects fall back to unauthenticated
    ``/api/events``.
    """
    session_id = logged_in_dashboard_client.cookies.get(browser_session.SESSION_COOKIE)
    assert session_id
    csrf = browser_session.get_csrf_token(session_id)
    assert csrf

    resp = logged_in_dashboard_client.get("/?embedded=1&theme=light")

    assert resp.status_code == 200
    assert '<meta name="io-browser-auth-required" content="1">' in resp.text
    assert f'<meta name="io-csrf-token" content="{csrf}">' in resp.text
    assert '<script src="/static/js/browser_auth.js"></script>' in resp.text
    assert resp.text.index('/static/js/browser_auth.js') < resp.text.index(
        '/static/js/dashboard/controls_refresh.js'
    )


def test_fake_auth_dashboard_mode_catches_missing_csrf(
    logged_in_dashboard_client: TestClient,
    fake_browser_auth,
) -> None:
    missing_csrf = logged_in_dashboard_client.post("/api/resume")
    assert missing_csrf.status_code == 403
    assert "csrf" in missing_csrf.json()["error"].lower()

    with_csrf = logged_in_dashboard_client.post(
        "/api/resume",
        headers=fake_browser_auth.csrf_headers(logged_in_dashboard_client),
    )
    assert with_csrf.status_code not in (401, 403), with_csrf.text


def test_authenticated_settings_page_renders_csrf_meta_and_browser_auth_script(
    logged_in_dashboard_client: TestClient,
) -> None:
    """The settings page must bootstrap the same browser auth adapter as
    the dashboard. Without the CSRF meta tag and ``browser_auth.js`` the
    ``Save Changes`` POST to ``/api/settings`` carries no ``X-CSRF-Token``
    header and the auth gate rejects it with
    ``missing or invalid csrf token``.
    """
    session_id = logged_in_dashboard_client.cookies.get(browser_session.SESSION_COOKIE)
    assert session_id
    csrf = browser_session.get_csrf_token(session_id)
    assert csrf

    resp = logged_in_dashboard_client.get("/settings")

    assert resp.status_code == 200
    assert '<meta name="io-browser-auth-required" content="1">' in resp.text
    assert f'<meta name="io-csrf-token" content="{csrf}">' in resp.text
    assert '<script src="/static/js/browser_auth.js"></script>' in resp.text
    # The auth wrapper must install before the page's other scripts so it
    # is in place before any Save / Create-issue POST fires.
    assert resp.text.index('/static/js/browser_auth.js') < resp.text.index(
        '/static/js/theme_resolution.js'
    )


def test_unauthenticated_settings_page_renders_login(
    auth_enabled_dashboard_client: TestClient,
) -> None:
    """An anonymous visitor to ``/settings`` gets the login form (like
    ``/``), not a raw 401 JSON — the page is public so its handler can
    own the unauthenticated fallback.
    """
    resp = auth_enabled_dashboard_client.get("/settings")

    assert resp.status_code == 200
    assert "Sign in" in resp.text
    assert 'action="/login"' in resp.text


def test_settings_post_without_csrf_is_rejected_with_csrf_accepted(
    logged_in_dashboard_client: TestClient,
    fake_browser_auth,
) -> None:
    """``POST /api/settings`` is gated by the CSRF check. This is the
    exact request the settings ``Save`` button issues; the regression
    being fixed is that it had no way to send the token.
    """
    missing_csrf = logged_in_dashboard_client.post("/api/settings", json={})
    assert missing_csrf.status_code == 403
    assert "csrf" in missing_csrf.json()["error"].lower()

    with_csrf = logged_in_dashboard_client.post(
        "/api/settings",
        json={},
        headers=fake_browser_auth.csrf_headers(logged_in_dashboard_client),
    )
    # Passes the CSRF gate; the test app has no orchestrator wired, so the
    # handler returns 503 rather than 401/403 — what matters is that auth
    # no longer blocks the save.
    assert with_csrf.status_code not in (401, 403), with_csrf.text


# ---------------------------------------------------------------------------
# Login flow + session cookie
# ---------------------------------------------------------------------------


def test_login_with_wrong_token_returns_form_with_error(
    authed_client: TestClient,
) -> None:
    resp = authed_client.post(
        "/login",
        data={"token": "nope"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    assert "Invalid token" in resp.text


def test_login_with_wrong_token_json_returns_401(
    authed_client: TestClient,
) -> None:
    resp = authed_client.post(
        "/login", json={"token": "nope"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid token"


def test_login_mints_session_cookie(authed_client: TestClient) -> None:
    browser_session.initialize()
    resp = authed_client.post(
        "/login", json={"token": "test-admin-token"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["session_id"]
    assert browser_session.SESSION_COOKIE in resp.cookies


def test_mutating_request_with_session_requires_csrf(
    authed_client: TestClient,
) -> None:
    """Logged-in browser POSTs must carry ``X-CSRF-Token`` — defending
    against cross-site request forgery from another tab.
    """
    browser_session.initialize()
    login = authed_client.post("/login", json={"token": "test-admin-token"})
    assert login.status_code == 200

    # Same client keeps the cookie; omit X-CSRF-Token.
    resp = authed_client.post("/api/test/cleanup")
    assert resp.status_code == 403
    assert "csrf" in resp.json()["error"].lower()


def test_safe_method_with_session_does_not_need_csrf(
    authed_client: TestClient,
) -> None:
    browser_session.initialize()
    login = authed_client.post("/login", json={"token": "test-admin-token"})
    assert login.status_code == 200
    # GET should succeed purely on the session cookie.
    resp = authed_client.get("/api/view-model")
    assert resp.status_code != 401
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# SSE token
# ---------------------------------------------------------------------------


def test_sse_without_session_returns_401(authed_client: TestClient) -> None:
    resp = authed_client.get("/api/events")
    assert resp.status_code == 401


def test_sse_token_is_accepted_by_middleware(
    authed_client: TestClient,
) -> None:
    """Logged-in browser flow: POST /login → GET /api/sse-token.

    We can't exercise ``/api/events`` itself in a TestClient unit test
    (the SSE handler streams forever), but the middleware is the
    gate: if ``evaluate_request`` accepts the query-string token the
    request is through. Assert the gate decision directly here and
    leave the end-to-end reconnect path to the e2e suite.
    """
    from issue_orchestrator.entrypoints._auth_middleware import (
        check_browser_session_auth,
    )
    from issue_orchestrator.entrypoints.web import _DASHBOARD_SURFACE

    browser_session.initialize()
    login = authed_client.post("/login", json={"token": "test-admin-token"})
    assert login.status_code == 200

    token_resp = authed_client.get("/api/sse-token")
    assert token_resp.status_code == 200
    token = token_resp.json()["sse_token"]
    assert token

    # Build a bare Request that mirrors what the SSE client would send.
    class _Req:
        def __init__(self, session_id: str, token: str) -> None:
            self.cookies = {browser_session.SESSION_COOKIE: session_id}
            self.query_params = {browser_session.SSE_TOKEN_QUERY: token}
            self.headers: dict[str, str] = {}
            self.method = "GET"

            class _URL:
                path = _DASHBOARD_SURFACE.sse_path

            self.url = _URL()

    # Grab the session id from the TestClient's cookie jar.
    session_id = authed_client.cookies.get(browser_session.SESSION_COOKIE)
    assert session_id

    ok, message, status = check_browser_session_auth(
        _Req(session_id, token),  # type: ignore[arg-type]
        _DASHBOARD_SURFACE,
    )
    assert ok, (message, status)


def test_sse_with_wrong_token_is_rejected(
    authed_client: TestClient,
) -> None:
    browser_session.initialize()
    login = authed_client.post("/login", json={"token": "test-admin-token"})
    assert login.status_code == 200
    resp = authed_client.get("/api/events?sse_token=obviously-wrong")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Off-by-default behaviour
# ---------------------------------------------------------------------------


def test_unconfigured_admin_token_means_no_gate(
    open_client: TestClient,
) -> None:
    """TestClient default (no admin token) leaves every route open,
    matching the existing behaviour unit tests rely on.
    """
    # /api/view-model is a read route; if auth were somehow enforced
    # we'd see 401. We only care that the middleware doesn't block
    # it.
    resp = open_client.get("/api/view-model")
    assert resp.status_code != 401


# ---------------------------------------------------------------------------
# Mounted Control API routes: the dashboard ``app`` mounts ``control_app``
# so requests to ``/control/*`` on port 8080 flow through the dashboard
# middleware first. Regression for re-review P2 on #6041 which noted
# the gap was only manually verified.
# ---------------------------------------------------------------------------


def test_mounted_control_route_requires_auth_on_dashboard(
    authed_client: TestClient,
) -> None:
    resp = authed_client.get("/control/orchestrator/status")
    assert resp.status_code == 401


def test_mounted_control_route_accepts_dashboard_bearer(
    authed_client: TestClient,
) -> None:
    """A caller with a valid dashboard bearer must also pass the
    mounted Control API middleware. Both surfaces use the same
    shared-secret so the same token works on both.
    """
    from issue_orchestrator.entrypoints.control_api import (
        configure_api_token,
        get_configured_agent_callback_token,
        get_configured_api_token,
    )

    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    configure_api_token("test-admin-token", agent_callback=None)
    try:
        resp = authed_client.get(
            "/control/orchestrator/status",
            headers={"Authorization": "Bearer test-admin-token"},
        )
        # 200 or 5xx both prove we passed both middlewares; the only
        # failure we care about is 401/403 from auth.
        assert resp.status_code not in (401, 403), resp.text
    finally:
        configure_api_token(prev_admin, agent_callback=prev_agent)


# ---------------------------------------------------------------------------
# Agent-callback token on the dashboard surface (#6913).
#
# ``control_app`` is mounted at ``""``, so the agent-callback routes are
# served on the dashboard port and the dashboard gate runs first. It
# used to pass ``agent=None`` with no route matcher, so a *valid* agent
# token was rejected on the only surface that served the route. The
# round runner then read the undelivered verdict as an unresponsive
# agent, SIGKILLed a healthy coder, and stranded validated work.
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_callback_tokens():
    """Configure both surfaces with a real agent-callback token."""
    from issue_orchestrator.entrypoints.control_api import (
        configure_api_token,
        get_configured_agent_callback_token,
        get_configured_api_token,
    )

    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    configure_api_token("test-admin-token", agent_callback="test-agent-token")
    try:
        yield
    finally:
        configure_api_token(prev_admin, agent_callback=prev_agent)


@pytest.mark.parametrize(
    "path",
    ["/api/review-exchange/respond", "/api/preflight-push", "/api/issues/6410/resume"],
)
def test_agent_callback_token_reaches_allowlisted_route_on_dashboard(
    authed_client: TestClient,
    agent_callback_tokens: None,
    path: str,
) -> None:
    """The scoped agent token must pass the dashboard gate on the
    allowlisted routes. Any 401/403 here is the #6913 regression: the
    agent holds a valid credential and still cannot deliver.
    """
    resp = authed_client.post(
        path,
        headers={"Authorization": "Bearer test-agent-token"},
        json={},
    )
    assert resp.status_code not in (401, 403), (
        f"agent-callback token rejected on {path}: "
        f"{resp.status_code} {resp.text}"
    )


@pytest.mark.parametrize("path", ["/api/shutdown", "/api/resume", "/api/kill"])
def test_agent_callback_token_still_rejected_off_allowlist(
    authed_client: TestClient,
    agent_callback_tokens: None,
    path: str,
) -> None:
    """Honouring the token must not widen it into an admin credential.

    ``/api/resume`` is the dashboard's own pause/resume action and is a
    deliberate near-miss: the allowlist matches
    ``/api/issues/{n}/resume`` by prefix+suffix, and a looser
    ``endswith("/resume")`` would hand agents this route too.
    """
    resp = authed_client.post(
        path,
        headers={"Authorization": "Bearer test-agent-token"},
    )
    assert resp.status_code in (401, 403), resp.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/resume",
        "/resume",
        "/api/issues/6410/kill",
        # Non-numeric and extra-segment near misses. These 404 today, but
        # a prefix+suffix match authorized them at the *auth* layer, so a
        # future admin route of this shape would silently inherit
        # agent-token access (#6924 F3).
        "/api/issues/not-an-int/resume",
        "/api/issues/6410/extra/resume",
        "/api/issues//resume",
        "/api/issues/6410/resume/extra",
    ],
)
def test_near_miss_paths_are_not_agent_callback_routes(path: str) -> None:
    """Pin the allowlist to the declared route template exactly."""
    from issue_orchestrator.entrypoints._auth_middleware import (
        is_agent_callback_route,
    )

    assert is_agent_callback_route(path) is False, f"{path} must require admin"


def test_declared_resume_template_is_an_agent_callback_route() -> None:
    from issue_orchestrator.entrypoints._auth_middleware import (
        is_agent_callback_route,
    )

    assert is_agent_callback_route("/api/issues/6410/resume") is True
    assert is_agent_callback_route("/api/issues/1/resume") is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/issues/not-an-int/resume",
        "/api/issues/6410/extra/resume",
        "/api/issues/6410/resume/extra",
    ],
)
def test_near_miss_paths_rejected_over_http(
    authed_client: TestClient,
    agent_callback_tokens: None,
    path: str,
) -> None:
    """The same near misses, asserted through the real auth middleware."""
    resp = authed_client.post(
        path,
        headers={"Authorization": "Bearer test-agent-token"},
    )
    assert resp.status_code in (401, 403), (
        f"{path} passed auth with an agent-callback token: {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Browser-session policy ownership — the dashboard must honor the
# operator-set ``ui.browser_session.*`` values, not silently fall
# back to the module defaults (#6041 re-review P2).
# ---------------------------------------------------------------------------


def test_dashboard_startup_honors_browser_session_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running ``_configure_dashboard_auth`` with a config object
    must thread ``browser_session_ttl_seconds`` / ``sse_token_ttl_seconds``
    / ``browser_session_max`` into ``browser_session.initialize``.
    Before the P2 fix this ran with no arguments and the operator's
    values were ignored.
    """
    from dataclasses import dataclass

    from issue_orchestrator.entrypoints.run_orchestrator import (
        _configure_dashboard_auth,
    )
    from issue_orchestrator.entrypoints.web import (
        configure_dashboard_admin_token,
        get_configured_dashboard_admin_token,
    )
    from issue_orchestrator.infra import browser_session as bs_module

    @dataclass
    class _FakeConfig:
        browser_session_ttl_seconds: int = 900
        sse_token_ttl_seconds: int = 10
        browser_session_max: int = 7

    monkeypatch.setenv(
        "ISSUE_ORCHESTRATOR_API_TOKEN",
        "bootstrap-admin-token-that-is-long-enough",
    )

    previous_token = get_configured_dashboard_admin_token()
    previous_ttl = bs_module.SESSION_TTL_SECONDS
    previous_sse = bs_module.SSE_TOKEN_TTL_SECONDS
    previous_max = bs_module.MAX_SESSIONS
    try:
        _configure_dashboard_auth(dev_no_auth=False, config=_FakeConfig())
        assert bs_module.SESSION_TTL_SECONDS == 900
        assert bs_module.SSE_TOKEN_TTL_SECONDS == 10
        assert bs_module.MAX_SESSIONS == 7
    finally:
        configure_dashboard_admin_token(previous_token)
        bs_module.initialize(
            session_ttl_seconds=previous_ttl,
            sse_token_ttl_seconds=previous_sse,
            max_sessions=previous_max,
        )


def test_engine_startup_configures_control_api_agent_callback_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_orchestrator`` serves ``control_app`` mounted under the
    dashboard app, so it must configure the Control API tokens too.

    It previously configured only the dashboard admin token, leaving
    ``_agent_callback_token = None`` in the engine process — so even a
    request that got past the outer gate had nothing to match against.
    Agents could never deliver a verdict to the running engine (#6913).
    """
    from dataclasses import dataclass

    from issue_orchestrator.entrypoints.control_api import (
        configure_api_token,
        get_configured_agent_callback_token,
        get_configured_api_token,
    )
    from issue_orchestrator.entrypoints.run_orchestrator import (
        _configure_dashboard_auth,
    )
    from issue_orchestrator.entrypoints.web import (
        configure_dashboard_admin_token,
        get_configured_dashboard_admin_token,
    )

    @dataclass
    class _FakeConfig:
        browser_session_ttl_seconds: int = 900
        sse_token_ttl_seconds: int = 10
        browser_session_max: int = 7

    monkeypatch.setenv(
        "ISSUE_ORCHESTRATOR_API_TOKEN",
        "bootstrap-admin-token-that-is-long-enough",
    )
    # Deliberately ABSENT. Pre-seeding this made the earlier version of
    # this test vacuous: it could not tell "the engine published the
    # token" from "the token happened to be inherited" (#6924 F2).
    monkeypatch.delenv("ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN", raising=False)

    prev_dashboard = get_configured_dashboard_admin_token()
    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    try:
        _configure_dashboard_auth(dev_no_auth=False, config=_FakeConfig())
        configured = get_configured_agent_callback_token()
        assert configured, "engine must configure an agent-callback token"
        assert get_configured_api_token() == (
            "bootstrap-admin-token-that-is-long-enough"
        )
        # The same value must reach agent subprocesses, which inherit it
        # from the process environment via ``agent_runner_env``.
        assert os.environ.get("ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN") == configured, (
            "the API demands a secret its agents never receive"
        )
    finally:
        configure_dashboard_admin_token(prev_dashboard)
        configure_api_token(prev_admin, agent_callback=prev_agent)


def test_dev_no_auth_clears_the_agent_callback_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents must not carry a secret the now-open API does not enforce."""
    from dataclasses import dataclass

    from issue_orchestrator.entrypoints.control_api import (
        configure_api_token,
        get_configured_agent_callback_token,
        get_configured_api_token,
    )
    from issue_orchestrator.entrypoints.run_orchestrator import (
        _configure_dashboard_auth,
    )
    from issue_orchestrator.entrypoints.web import (
        configure_dashboard_admin_token,
        get_configured_dashboard_admin_token,
    )

    @dataclass
    class _FakeConfig:
        browser_session_ttl_seconds: int = 900
        sse_token_ttl_seconds: int = 10
        browser_session_max: int = 7

    monkeypatch.setenv("ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN", "stale-agent-token")

    prev_dashboard = get_configured_dashboard_admin_token()
    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    try:
        _configure_dashboard_auth(dev_no_auth=True, config=_FakeConfig())
        assert get_configured_agent_callback_token() is None
        assert "ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN" not in os.environ
    finally:
        configure_dashboard_admin_token(prev_dashboard)
        configure_api_token(prev_admin, agent_callback=prev_agent)


def test_cookie_minted_via_cc_login_works_on_dashboard() -> None:
    """Stateless cross-process sessions: a cookie minted by the
    Control API ``/login`` endpoint validates against the dashboard
    ``app`` middleware as long as both have been initialized with
    the same admin token.

    Simulates the two-process operator flow without spawning a real
    second process: configure CC + dashboard with the shared admin
    token, log in once via CC's ``/login``, then attach the resulting
    cookie to a dashboard request and assert it passes auth. Before
    the stateless-cookie change this would 401 because each process
    held its own random secret and its own ``_SESSIONS`` dict.
    """
    from issue_orchestrator.entrypoints.control_api import (
        configure_api_token,
        control_app,
        get_configured_agent_callback_token,
        get_configured_api_token,
    )
    from issue_orchestrator.infra import browser_session as bs_module

    shared_admin = "shared-admin-cross-process-token"

    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    prev_dashboard = get_configured_dashboard_admin_token()

    bs_module.shutdown()
    bs_module.initialize(admin_token=shared_admin)
    configure_api_token(shared_admin, agent_callback=None)
    configure_dashboard_admin_token(shared_admin)
    try:
        cc_client = TestClient(control_app)
        login = cc_client.post("/login", json={"token": shared_admin})
        assert login.status_code == 200
        cookie_value = login.json()["session_id"]
        assert cookie_value

        # New TestClient against the dashboard ``app`` — no shared
        # cookie jar with the CC client. Carry only the cookie value
        # the CC's ``/login`` returned, mimicking what the browser
        # would send to port 8080 after authenticating on 19080.
        dashboard_client = TestClient(app)
        dashboard_client.cookies.set(bs_module.SESSION_COOKIE, cookie_value)
        resp = dashboard_client.get("/api/view-model")
        assert resp.status_code != 401, resp.text
        assert resp.status_code != 403, resp.text
    finally:
        bs_module.shutdown()
        configure_api_token(prev_admin, agent_callback=prev_agent)
        configure_dashboard_admin_token(prev_dashboard)


def test_dev_no_auth_still_honors_browser_session_config() -> None:
    """Even in --dev-no-auth mode the browser_session tunables should
    match what the operator configured — the session module still
    initializes and may be used if the operator later re-enables
    auth via a restart.
    """
    from dataclasses import dataclass

    from issue_orchestrator.entrypoints.run_orchestrator import (
        _configure_dashboard_auth,
    )
    from issue_orchestrator.infra import browser_session as bs_module

    @dataclass
    class _FakeConfig:
        browser_session_ttl_seconds: int = 120
        sse_token_ttl_seconds: int = 5
        browser_session_max: int = 3

    previous_ttl = bs_module.SESSION_TTL_SECONDS
    previous_sse = bs_module.SSE_TOKEN_TTL_SECONDS
    previous_max = bs_module.MAX_SESSIONS
    try:
        _configure_dashboard_auth(dev_no_auth=True, config=_FakeConfig())
        assert bs_module.SESSION_TTL_SECONDS == 120
        assert bs_module.SSE_TOKEN_TTL_SECONDS == 5
        assert bs_module.MAX_SESSIONS == 3
    finally:
        bs_module.initialize(
            session_ttl_seconds=previous_ttl,
            sse_token_ttl_seconds=previous_sse,
            max_sessions=previous_max,
        )
