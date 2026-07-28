"""Shared browser-friendly auth helpers for the Control API and Web Dashboard.

Both surfaces use the same three-path auth model established in #6011:

1. **Bearer token** on ``Authorization`` header — programmatic clients.
2. **Browser session cookie + CSRF** — after ``POST /login``.
3. **Short-lived single-use SSE token** on ``EventSource`` URLs
   (``EventSource`` cannot send headers).

The Control Center shipped this first (#6011, #6017 re-reviews); PR 8
extends the same stack to the Web Dashboard on port 8080. Keeping the
helpers in one module ensures the two surfaces cannot drift.

This module owns:

- ``check_bearer_auth`` / ``check_browser_session_auth`` — used by
  each app's middleware.
- ``install_access_log_redaction`` — scrubs the short-lived SSE token
  from uvicorn's access log (#6017 re-review-3 P2).
- ``render_login_page`` — the minimal self-contained login form that
  both ``/`` handlers fall back to when the visitor has no session.

It also owns ``is_agent_callback_route`` — the single allowlist of
routes the scoped agent-callback token may reach. Both surfaces must
answer that question identically: the Web Dashboard mounts
``control_app`` at ``""``, so the agent-callback routes are served on
the dashboard port and the dashboard's gate runs *first*. When the two
surfaces disagreed, a valid agent token was rejected on the only port
that actually served the route (#6913) — a delivery failure the round
runner then misread as an unresponsive agent, destroying validated
work. One owner, one answer.

It does NOT own the admin / agent-callback token state. Each app
configures its own tokens via ``configure_api_token`` (Control API)
or ``configure_dashboard_admin_token`` (Web Dashboard); they typically
point at the same shared secret loaded from
``~/.issue-orchestrator/api-token``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ..infra import browser_session
from ..infra.api_token import verify_token

logger = logging.getLogger(__name__)

# Routes the agent-callback token is allowed to reach. Anything NOT in
# this set requires the admin token.
#
# Honest scope: this allowlist limits what the agent-callback token
# can do IF an agent holds only that token. It does NOT stop an
# agent that reads ``~/.issue-orchestrator/api-token`` off the same
# filesystem (agents run with the real HOME under the same user;
# see issue #6024) from mutating any route. The callback token is
# defense in depth — it narrows the default blast radius and is
# the right shape for a future isolated-agent model — not a
# privilege boundary against same-user agents today.
_AGENT_CALLBACK_ROUTES: frozenset[str] = frozenset(
    {"/api/preflight-push", "/api/review-exchange/respond"}
)

# ``/api/issues/{issue_number}/resume`` has a variable segment, so it is
# matched against the declared route template exactly. A prefix+suffix
# test also authorized shapes like ``/api/issues/x/extra/resume``; those
# 404 today, but a future admin route of that shape would silently
# inherit agent-token access (#6924 F3).
_AGENT_CALLBACK_ROUTE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/issues/\d+/resume$"),
)


def is_agent_callback_route(path: str) -> bool:
    """Whether ``path`` is reachable with the scoped agent-callback token.

    Shared by every surface that serves these routes. The Web Dashboard
    mounts ``control_app`` at ``""``, so it serves them too and must
    apply the same answer — see the module docstring.
    """
    if path in _AGENT_CALLBACK_ROUTES:
        return True
    return any(
        pattern.match(path) is not None
        for pattern in _AGENT_CALLBACK_ROUTE_PATTERNS
    )


BROWSER_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Matches the SSE single-use token in query strings so it can be
# stripped from HTTP access-log messages before they're emitted.
# Uvicorn's access log format includes the full request line with
# query params, which would otherwise persist a still-valid-for-a-few
# seconds credential (#6017 re-review-3 P2).
_SSE_TOKEN_QUERY_PATTERN = re.compile(
    r"(?P<sep>[?&])sse_token=[^&\s\"']+"
)
_SSE_TOKEN_REDACTION = r"\g<sep>sse_token=REDACTED"


class _SseTokenAccessLogFilter(logging.Filter):
    """Scrub ``sse_token=...`` from every uvicorn.access log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging crash on us
            return True
        if "sse_token=" not in msg:
            return True
        scrubbed = _SSE_TOKEN_QUERY_PATTERN.sub(_SSE_TOKEN_REDACTION, msg)
        record.msg = scrubbed
        record.args = None
        return True


def install_access_log_redaction() -> None:
    """Attach the SSE-token redaction filter to uvicorn's access logger.

    Idempotent: re-running (e.g. across test fixtures) does not stack
    duplicate filters.
    """
    access_logger = logging.getLogger("uvicorn.access")
    for existing in access_logger.filters:
        if isinstance(existing, _SseTokenAccessLogFilter):
            return
    access_logger.addFilter(_SseTokenAccessLogFilter())


@dataclass(frozen=True)
class AuthSurfaceConfig:
    """Per-surface knobs that differ between Control API and Dashboard.

    ``sse_path`` — the fully-qualified SSE endpoint URL, e.g. ``/api/events``.
    ``public_paths`` — exact paths that bypass auth (login form, etc.).
    ``public_prefixes`` — path prefixes that bypass auth (static assets).
    ``agent_callback_matcher`` — returns True for paths that the agent-
      callback scoped token is allowed to reach. Both real surfaces pass
      :func:`is_agent_callback_route`: the dashboard mounts
      ``control_app`` at ``""``, so it serves those routes and its gate
      runs first. The always-False default is for surfaces that serve no
      agent-callback route at all; relying on it for the dashboard is
      what made every agent callback 401 (#6913, #6924).
    """

    sse_path: str
    public_paths: frozenset[str]
    name: str = "unknown"
    public_prefixes: tuple[str, ...] = ()
    agent_callback_matcher: Callable[[str], bool] = field(
        default=lambda _path: False
    )

    def is_public(self, path: str) -> bool:
        if path in self.public_paths:
            return True
        return any(path.startswith(prefix) for prefix in self.public_prefixes)


def check_bearer_auth(
    request: Request,
    admin: str | None,
    agent: str | None,
    surface: AuthSurfaceConfig,
) -> str | None:
    """Evaluate the ``Authorization: Bearer`` path.

    Returns ``"ok"`` when the header is valid for the route,
    ``"invalid"`` when a Bearer header was present but did not match,
    or ``None`` when no Bearer header was supplied (caller should
    fall through to browser-session checks).
    """
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return None
    provided = header[len("Bearer "):].strip()
    if admin is not None and verify_token(admin, provided):
        return "ok"
    if (
        agent is not None
        and surface.agent_callback_matcher(request.url.path)
        and verify_token(agent, provided)
    ):
        return "ok"
    return "invalid"


def check_browser_session_auth(
    request: Request, surface: AuthSurfaceConfig
) -> tuple[bool, str, int]:
    """Evaluate the browser session + CSRF + SSE-token path.

    Returns ``(ok, message, status)``. When ``ok`` is True the request
    is authenticated; ``message`` and ``status`` are only meaningful
    when ``ok`` is False.
    """
    session_id = request.cookies.get(browser_session.SESSION_COOKIE)
    if not session_id:
        return (False, "missing credentials — please sign in", 401)
    if not browser_session.session_is_valid(session_id):
        return (False, "session expired — please sign in again", 401)
    if request.url.path == surface.sse_path:
        sse_token = request.query_params.get(browser_session.SSE_TOKEN_QUERY)
        if browser_session.verify_sse_token(sse_token, session_id):
            return (True, "", 200)
        return (False, "invalid sse token", 401)
    if request.method in BROWSER_SAFE_METHODS:
        return (True, "", 200)
    csrf = request.headers.get(browser_session.CSRF_HEADER)
    if browser_session.verify_csrf(session_id, csrf):
        return (True, "", 200)
    return (False, "missing or invalid csrf token", 403)


def evaluate_request(
    request: Request,
    admin: str | None,
    agent: str | None,
    surface: AuthSurfaceConfig,
) -> Response | None:
    """Run the full auth gate on a request.

    Returns ``None`` if the request should proceed to the route
    handler, or a ``Response`` to short-circuit (401 / 403) otherwise.

    When both ``admin`` and ``agent`` are ``None`` the gate is
    disabled entirely — used by tests (``TestClient``) and by the
    explicit ``--dev-no-auth`` operator flag.
    """
    if surface.is_public(request.url.path):
        return None
    if admin is None and agent is None:
        return None
    bearer_result = check_bearer_auth(request, admin, agent, surface)
    if bearer_result == "ok":
        return None
    if bearer_result == "invalid":
        logger.warning(
            "Auth rejected %s %s on %s: invalid bearer token",
            request.method,
            request.url.path,
            surface.name,
        )
        return JSONResponse({"error": "invalid bearer token"}, status_code=401)
    ok, message, status = check_browser_session_auth(request, surface)
    if ok:
        return None
    log_level = (
        logging.INFO
        if status == 401 and message.startswith("missing credentials")
        else logging.WARNING
    )
    logger.log(
        log_level,
        "Auth rejected %s %s on %s: status=%d reason=%s",
        request.method,
        request.url.path,
        surface.name,
        status,
        message,
    )
    return JSONResponse({"error": message}, status_code=status)


# ---------------------------------------------------------------------------
# Shared login page
# ---------------------------------------------------------------------------


def render_login_page(
    *, action_url: str = "/login", error: str | None = None
) -> HTMLResponse:
    """Render a minimal self-contained login form.

    Both the Control Center and the Web Dashboard serve this form when
    an unauthenticated browser visits ``/``. It posts the admin bearer
    token (from ``~/.issue-orchestrator/api-token``) as a password
    field back to the same surface's ``/login`` endpoint, which
    verifies it and mints a session cookie.

    The form is intentionally CSS-inline and script-free so it cannot
    depend on any authenticated fetch to boot.
    """
    error_html = f'<p class="err">{error}</p>' if error else ""
    content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Issue Orchestrator — Sign in</title>
<link rel="icon" type="image/svg+xml" href="/static/brand/logo.svg">
<style>
body {{ font-family: sans-serif; display: flex; align-items: center;
       justify-content: center; height: 100vh; margin: 0;
       background: #0f1419; color: #e6e6e6; }}
form {{ background: #1c2330; padding: 32px; border-radius: 8px;
        min-width: 320px; box-shadow: 0 4px 16px rgba(0,0,0,0.4); }}
h1 {{ margin: 0 0 12px; font-size: 20px; }}
p  {{ margin: 0 0 16px; color: #9aa5b1; font-size: 13px; line-height: 1.4; }}
.err {{ color: #ff6b6b; }}
input[type=password] {{ width: 100%; padding: 10px; border-radius: 4px;
                        border: 1px solid #334155; background: #0f1419;
                        color: #e6e6e6; box-sizing: border-box;
                        font-family: monospace; }}
button {{ margin-top: 12px; width: 100%; padding: 10px; border: 0;
         border-radius: 4px; background: #3b82f6; color: white;
         font-weight: 600; cursor: pointer; }}
code {{ background: #0f1419; padding: 2px 6px; border-radius: 3px;
        font-size: 12px; }}
</style>
</head>
<body>
<form method="POST" action="{action_url}">
<h1>Issue Orchestrator</h1>
<p>Paste the local admin token from <code>~/.issue-orchestrator/api-token</code>.</p>
<p>This is not your GitHub token. It protects the Control Center on this
machine, including repository engines, worktrees, agent sessions, logs,
and configuration.</p>
{error_html}
<input type="password" name="token" autofocus autocomplete="off"
       placeholder="Admin token" required>
<button type="submit">Sign in</button>
</form>
</body>
</html>"""
    return HTMLResponse(content)


@dataclass(frozen=True)
class BrowserPageAuth:
    """Browser-auth template context for a top-level HTML page.

    Renders straight into the ``io-csrf-token`` / ``io-browser-auth-required``
    meta tags that ``browser_auth.js`` reads to attach ``X-CSRF-Token`` to
    mutating fetches. ``auth_required`` is the typed source of truth;
    ``browser_auth_required`` is its meta-tag string form so callers render
    directly without re-deriving ``"1"``/``"0"`` at each site.
    """

    csrf_token: str
    auth_required: bool

    @property
    def browser_auth_required(self) -> str:
        """``content`` value for the ``io-browser-auth-required`` meta tag."""
        return "1" if self.auth_required else "0"


def resolve_browser_page_auth(
    request: Request, *, auth_enabled: bool
) -> BrowserPageAuth | HTMLResponse:
    """Resolve the browser-auth context for a public top-level HTML page.

    Returns a ``BrowserPageAuth`` to render into the page, or a login
    ``HTMLResponse`` to short-circuit when auth is enabled but the caller
    has no valid session.

    Public HTML pages (the dashboard ``/``, ``/settings``, and the Control
    Center ``/``) bypass the middleware gate so an anonymous visitor sees
    the login form instead of a raw 401 JSON; that makes each page handler
    responsible for the same CSRF-bootstrap decision. Routing every such
    page through this one helper keeps the rule single-owned so the
    surfaces cannot drift apart.

    ``auth_enabled`` is whether the surface's auth gate is active. Each
    surface computes it from its own token set (the dashboard has only an
    admin token; the Control API also has an agent-callback token), so it
    is passed in rather than derived here.
    """
    if not auth_enabled:
        return BrowserPageAuth(csrf_token="", auth_required=False)
    session_id = request.cookies.get(browser_session.SESSION_COOKIE)
    if not session_id or not browser_session.session_is_valid(session_id):
        return render_login_page(action_url="/login")
    return BrowserPageAuth(
        csrf_token=browser_session.get_csrf_token(session_id) or "",
        auth_required=True,
    )


async def handle_login_post(
    request: Request, admin_token: str | None
) -> Response:
    """Shared implementation for ``POST /login``.

    Accepts both ``application/x-www-form-urlencoded`` (from the HTML
    form) and ``application/json`` (for programmatic flows). Verifies
    the admin token in constant time, mints a session cookie on
    success. Returns ``Response`` objects so the caller (the app's
    route handler) can bolt on any app-specific response shaping.
    """
    if admin_token is None:
        return JSONResponse({"status": "ok"})
    content_type = request.headers.get("content-type", "")
    is_json = content_type.startswith("application/json")
    token: str | None = None
    if is_json:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - tolerate malformed body
            body = {}
        if isinstance(body, dict):
            raw = body.get("token")
            token = raw if isinstance(raw, str) else None
    else:
        form = await request.form()
        raw = form.get("token")
        token = raw if isinstance(raw, str) else None
    if not token or not verify_token(admin_token, token):
        if is_json:
            return JSONResponse({"error": "invalid token"}, status_code=401)
        return render_login_page(error="Invalid token. Try again.")

    session_id, _csrf = browser_session.create_session()
    if is_json:
        response: Response = JSONResponse(
            {"status": "ok", "session_id": session_id}
        )
    else:
        response = Response(status_code=303)
        response.headers["Location"] = "/"
    response.set_cookie(
        browser_session.SESSION_COOKIE,
        session_id,
        max_age=browser_session.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


def issue_sse_token_response(request: Request) -> JSONResponse:
    """Return a short-lived single-use SSE token for the caller's session."""
    session_id = request.cookies.get(browser_session.SESSION_COOKIE)
    if not session_id:
        return JSONResponse({"error": "no session"}, status_code=401)
    token = browser_session.issue_sse_token(session_id)
    if token is None:
        return JSONResponse({"error": "session invalid"}, status_code=401)
    response = JSONResponse({
        "sse_token": token,
        "ttl_seconds": browser_session.SSE_TOKEN_TTL_SECONDS,
    })
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
