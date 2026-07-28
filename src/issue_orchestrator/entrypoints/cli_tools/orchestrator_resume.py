"""Control API resume helper for agent completion commands."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

from ...infra.env import ENV_PREFIX, get_env


@dataclass(frozen=True, slots=True)
class ApiHeader:
    """One HTTP header for an agent-scoped Control API callback."""

    name: str
    value: str


@dataclass(frozen=True, slots=True)
class ApiRequestHeaders:
    """Typed HTTP headers for an agent-scoped Control API callback."""

    headers: tuple[ApiHeader, ...]

    @classmethod
    def from_agent_environment(cls) -> "ApiRequestHeaders":
        values = [ApiHeader("Content-Type", "application/json")]
        token = os.environ.get("ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN")
        if token:
            values.append(ApiHeader("Authorization", f"Bearer {token}"))
        return cls(headers=tuple(values))

    def to_mutable_mapping(self) -> MutableMapping[str, str]:
        """Project to the mutable mapping required by ``urllib``."""
        return {header.name: header.value for header in self.headers}


def api_request_headers() -> ApiRequestHeaders:
    """Build Control API request headers for agent-scoped callbacks."""
    return ApiRequestHeaders.from_agent_environment()


def resolve_control_api_port() -> str | None:
    """Resolve the port serving the Control API, or ``None`` if unset.

    Single owner for the rule every agent callback needs, so the three
    callback paths (``exchange-respond``, preflight-push, resume) cannot
    answer it differently.

    ``control_api_port: 0`` means "bind any free port", so a literal
    ``"0"`` is a request, never a reachable destination. It is also a
    *truthy* string, so a naive ``a or b`` chain returns it and shadows
    the live port the review exchange injects as
    ``ORCHESTRATOR_API_PORT`` — which is how every verdict became
    undeliverable (#6913). Treat it as unset so callers fail honestly
    instead of dialling ``localhost:0``.
    """
    for name in ("ISSUE_ORCHESTRATOR_API_PORT", "ORCHESTRATOR_API_PORT"):
        raw = os.environ.get(name)
        if raw is None:
            continue
        port = raw.strip()
        if port and port != "0":
            return port
    return None


@dataclass(frozen=True, slots=True)
class ResumeTarget:
    """Typed Control API endpoint identity for an agent resume callback."""

    port: str
    issue_number: str

    @classmethod
    def from_agent_environment(cls) -> "ResumeTarget":
        raw_issue_number = get_env("ISSUE_NUMBER") or os.environ.get(
            "ORCHESTRATOR_ISSUE_NUMBER"
        )
        port = resolve_control_api_port() or ""
        issue_number = raw_issue_number.strip() if raw_issue_number else ""
        missing: list[str] = []
        if port == "":
            missing.append("ISSUE_ORCHESTRATOR_API_PORT")
        if issue_number == "":
            missing.append("ISSUE_ORCHESTRATOR_ISSUE_NUMBER")
        if missing:
            raise ValueError(
                f"missing environment variables: {', '.join(missing)}"
            )
        return cls(port=port, issue_number=issue_number)

    def url(self) -> str:
        return f"http://localhost:{self.port}/api/issues/{self.issue_number}/resume"


@dataclass(frozen=True, slots=True)
class ResumeRequestBody:
    """Typed request body for an agent-triggered resume callback."""

    run_dir: Path

    def __post_init__(self) -> None:
        if not self.run_dir.is_absolute():
            raise ValueError(f"{ENV_PREFIX}RUN_DIR must be absolute")

    @classmethod
    def from_agent_environment(cls) -> "ResumeRequestBody":
        run_dir = get_env("RUN_DIR")
        if not run_dir or not run_dir.strip():
            raise ValueError(f"{ENV_PREFIX}RUN_DIR is required")
        return cls(run_dir=Path(run_dir))

    def to_json_bytes(self) -> bytes:
        return json.dumps({"run_dir": str(self.run_dir)}).encode("utf-8")


def trigger_orchestrator_resume(verbose: bool = False) -> tuple[bool, str | None]:
    """Trigger the orchestrator to resume processing for this issue."""
    try:
        target = ResumeTarget.from_agent_environment()
        request_body = ResumeRequestBody.from_agent_environment()
    except ValueError as exc:
        return False, (
            f"Cannot resume: {exc}. Completion record written. "
            "Resume processing from the web UI."
        )

    if verbose:
        print(f"Triggering orchestrator resume for issue #{target.issue_number}...")

    try:
        req = urllib.request.Request(
            target.url(),
            data=request_body.to_json_bytes(),
            headers=api_request_headers().to_mutable_mapping(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
            if result.get("success"):
                return True, None
            return False, result.get("error", "Unknown error from orchestrator")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
            error_data = json.loads(body)
            return False, error_data.get("error", f"HTTP {exc.code}: {body}")
        except Exception:
            return False, f"HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return False, f"Could not reach orchestrator API: {exc}"
    except Exception as exc:
        return False, f"Resume request failed: {exc}"
