"""HTTP adapter for orchestrator control/query endpoints."""

from __future__ import annotations

import os
from typing import Any, Callable
import threading

import httpx

from ..domain.pause_state import PauseActor
from ..ports.orchestrator_api import OrchestratorApi


def _default_token_provider() -> str | None:
    """Resolve the admin Control API bearer token for outgoing calls.

    Resolution order (security #5987 F3 + #6017 review P3):

    1. ``ISSUE_ORCHESTRATOR_API_TOKEN`` env var — fastest path and
       used by MCP / CLI clients launched by the orchestrator.
    2. The on-disk admin token file if it already exists — lets a
       standalone Control Center or an operator CLI reach an
       orchestrator that is already running in another process
       without the operator manually exporting the secret.

    Neither path creates the token file; only server-side startup
    (``ControlAPIServer.start`` / ``control_center.main``) does.
    """
    from_env = os.environ.get("ISSUE_ORCHESTRATOR_API_TOKEN")
    if from_env:
        return from_env
    from ..infra.api_token import read_existing_admin_token

    return read_existing_admin_token()


def _auth_headers(token_provider: Callable[[], str | None]) -> dict[str, str]:
    token = token_provider()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def probe_orchestrator_json(
    url: str,
    *,
    timeout_seconds: float,
    token_provider: Callable[[], str | None] = _default_token_provider,
) -> dict[str, Any] | None:
    """Return a JSON object from an orchestrator endpoint, or ``None`` on probe failure.

    After #5987 F3 landed, every Control API route requires a bearer
    token. Probes that forgot to send one silently received 401 and the
    caller treated the orchestrator as absent — the exact symptom
    flagged in the #6017 re-review P4. Route the probe through the
    same token resolver as the HTTP adapter so lifecycle probes
    continue to see authenticated engines.
    """
    headers = _auth_headers(token_provider)
    try:
        response = httpx.get(url, timeout=timeout_seconds, headers=headers)
        response.raise_for_status()
    except Exception:
        return None

    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class OrchestratorHttpApi(OrchestratorApi):
    def __init__(
        self,
        base_url_provider: Callable[[], str],
        refresh_base_url: Callable[[], str] | None = None,
        client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
        token_provider: Callable[[], str | None] = _default_token_provider,
    ) -> None:
        self._base_url_provider = base_url_provider
        self._refresh_base_url = refresh_base_url
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._client_lock = threading.Lock()
        self._token_provider = token_provider

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._base_url_provider()}{path}"
        headers = _auth_headers(self._token_provider)
        try:
            with self._client_lock:
                response = self._client.request(
                    method, url, json=json_body, headers=headers
                )
                response.raise_for_status()
                return response.json()
        except httpx.RequestError:
            if not self._refresh_base_url:
                raise
            refreshed_url = f"{self._refresh_base_url()}{path}"
            with self._client_lock:
                response = self._client.request(
                    method, refreshed_url, json=json_body, headers=headers
                )
                response.raise_for_status()
                return response.json()

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/api/status")

    def info(self) -> dict[str, Any]:
        return self._request("GET", "/api/info")

    def excluded_issues(self) -> dict[str, Any]:
        return self._request("GET", "/api/excluded-issues")

    def blocked_issues(self) -> dict[str, Any]:
        return self._request("GET", "/api/blocked-issues")

    def stale_issues(self) -> dict[str, Any]:
        return self._request("GET", "/api/stale-issues")

    def dependency_problems(self) -> dict[str, Any]:
        return self._request("GET", "/api/dependency-problems")

    def history(self) -> dict[str, Any]:
        return self._request("GET", "/api/history")

    def pause(self) -> dict[str, Any]:
        return self._request("POST", "/api/pause")

    def resume(self) -> dict[str, Any]:
        return self._request("POST", "/api/resume")

    def refresh(self, inflight_stable_ids: list[str]) -> dict[str, Any]:
        return self._request("POST", "/api/refresh", json_body={"inflight_stable_ids": inflight_stable_ids})

    def shutdown(
        self,
        *,
        reason: str,
        actor: str = "orchestrator_http_api.shutdown",
        force: bool = False,
    ) -> dict[str, Any]:
        """Request orchestrator shutdown via HTTP.

        ``reason`` is required: the target's ``/api/shutdown``
        endpoint rejects empty reasons (the contract is "tell us
        why" so the target log records the calling intent).
        """
        if not reason or not reason.strip():
            raise ValueError(
                "shutdown() requires a non-empty reason; "
                "the /api/shutdown contract rejects unreasoned shutdowns",
            )
        return self._request(
            "POST",
            f"/api/shutdown?force={str(force).lower()}",
            json_body={"reason": reason, "actor": actor},
        )

    def session_worktree(self, issue_number: int) -> dict[str, Any]:
        return self._request("GET", f"/api/session/worktree/{issue_number}")

    def session_manifest(self, issue_number: int) -> dict[str, Any]:
        return self._request("GET", f"/api/session/manifest/{issue_number}")

    def _session_run_dir(self, issue_number: int) -> str:
        manifest = self.session_manifest(issue_number)
        run_dir = manifest.get("run_dir")
        if not isinstance(run_dir, str) or not run_dir:
            raise RuntimeError(f"session manifest missing run_dir for issue #{issue_number}")
        return run_dir

    def session_phases(self, issue_number: int) -> dict[str, Any]:
        return self._request("GET", f"/api/session/phases/{issue_number}")

    def session_claude_log(self, issue_number: int, limit: int) -> dict[str, Any]:
        run_dir = self._session_run_dir(issue_number)
        return self._request("GET", f"/api/session/claude-log/{issue_number}?limit={limit}&run_dir={run_dir}")

    def session_orchestrator_log(self, issue_number: int) -> dict[str, Any]:
        return self._request("GET", f"/api/session/orchestrator-log/{issue_number}")

    def send(self, issue_number: int, text: str) -> dict[str, Any]:
        return self._request("POST", f"/api/send/{issue_number}", json_body={"text": text})

    def kill(self, issue_number: int) -> dict[str, Any]:
        return self._request("POST", f"/api/kill/{issue_number}")

    def focus(self, issue_number: int) -> dict[str, Any]:
        return self._request("POST", f"/api/focus/{issue_number}")

    def doctor(self) -> dict[str, Any]:
        return self._request("GET", "/api/doctor")


class OrchestratorAsyncHttpApi:
    def __init__(
        self,
        base_url_provider: Callable[[], str],
        refresh_base_url: Callable[[], str] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
        token_provider: Callable[[], str | None] = _default_token_provider,
        *,
        pause_actor: PauseActor,
    ) -> None:
        self._base_url_provider = base_url_provider
        self._refresh_base_url = refresh_base_url
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._token_provider = token_provider
        # REQUIRED, with no default: this one client class is instantiated by
        # both MCP and the Control Center, so a default would silently
        # mis-attribute every pause from whichever surface did not set it.
        self._pause_actor = pause_actor

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url_provider()}{path}"
        headers = _auth_headers(self._token_provider)
        try:
            response = await self._client.request(
                method, url, json=json_body, headers=headers
            )
            response.raise_for_status()
            return response.json()
        except httpx.RequestError:
            if not self._refresh_base_url:
                raise
            refreshed_url = f"{self._refresh_base_url()}{path}"
            response = await self._client.request(
                method, refreshed_url, json=json_body, headers=headers
            )
            response.raise_for_status()
            return response.json()

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/status")

    async def info(self) -> dict[str, Any]:
        return await self._request("GET", "/api/info")

    async def excluded_issues(self) -> dict[str, Any]:
        return await self._request("GET", "/api/excluded-issues")

    async def blocked_issues(self) -> dict[str, Any]:
        return await self._request("GET", "/api/blocked-issues")

    async def stale_issues(self) -> dict[str, Any]:
        return await self._request("GET", "/api/stale-issues")

    async def dependency_problems(self) -> dict[str, Any]:
        return await self._request("GET", "/api/dependency-problems")

    async def history(self) -> dict[str, Any]:
        return await self._request("GET", "/api/history")

    async def pause(self) -> dict[str, Any]:
        # Declare who is asking so the pause journal can tell the operator
        # surfaces apart.
        return await self._request(
            "POST", "/api/pause", json_body={"actor": str(self._pause_actor)}
        )

    async def resume(self) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/resume", json_body={"actor": str(self._pause_actor)}
        )

    async def refresh(self, inflight_stable_ids: list[str]) -> dict[str, Any]:
        return await self._request("POST", "/api/refresh", json_body={"inflight_stable_ids": inflight_stable_ids})

    async def shutdown(
        self,
        *,
        reason: str,
        actor: str = "orchestrator_http_api.shutdown",
        force: bool = False,
    ) -> dict[str, Any]:
        """Request orchestrator shutdown via HTTP (async).

        ``reason`` is required: the target's ``/api/shutdown``
        endpoint rejects empty reasons (the contract is "tell us
        why" so the target log records the calling intent).
        """
        if not reason or not reason.strip():
            raise ValueError(
                "shutdown() requires a non-empty reason; "
                "the /api/shutdown contract rejects unreasoned shutdowns",
            )
        return await self._request(
            "POST",
            f"/api/shutdown?force={str(force).lower()}",
            json_body={"reason": reason, "actor": actor},
        )

    async def session_worktree(self, issue_number: int) -> dict[str, Any]:
        return await self._request("GET", f"/api/session/worktree/{issue_number}")

    async def session_manifest(self, issue_number: int) -> dict[str, Any]:
        return await self._request("GET", f"/api/session/manifest/{issue_number}")

    async def _session_run_dir(self, issue_number: int) -> str:
        manifest = await self.session_manifest(issue_number)
        run_dir = manifest.get("run_dir")
        if not isinstance(run_dir, str) or not run_dir:
            raise RuntimeError(f"session manifest missing run_dir for issue #{issue_number}")
        return run_dir

    async def session_phases(self, issue_number: int) -> dict[str, Any]:
        return await self._request("GET", f"/api/session/phases/{issue_number}")

    async def session_claude_log(self, issue_number: int, limit: int) -> dict[str, Any]:
        run_dir = await self._session_run_dir(issue_number)
        return await self._request("GET", f"/api/session/claude-log/{issue_number}?limit={limit}&run_dir={run_dir}")

    async def session_orchestrator_log(self, issue_number: int) -> dict[str, Any]:
        return await self._request("GET", f"/api/session/orchestrator-log/{issue_number}")

    async def send(self, issue_number: int, text: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/send/{issue_number}", json_body={"text": text})

    async def kill(self, issue_number: int) -> dict[str, Any]:
        return await self._request("POST", f"/api/kill/{issue_number}")

    async def focus(self, issue_number: int) -> dict[str, Any]:
        return await self._request("POST", f"/api/focus/{issue_number}")

    async def doctor(self) -> dict[str, Any]:
        return await self._request("GET", "/api/doctor")
