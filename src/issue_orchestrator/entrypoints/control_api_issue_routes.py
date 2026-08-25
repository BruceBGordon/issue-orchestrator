"""Agent-facing issue action routes for the Control API."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..control.actions import ActionResultType, CloseIssueAction
from ..control.claim_gate import ClaimLostError
from ..control.queue_cache import QueueCache
from ..control.reconciliation import ReconciliationRequired, build_expected_for_mutation
from ..control.worktree_manager import get_worktree_path
from ..domain.models import get_completion_path
from ..domain.review_exchange_verdict import ExchangeVerdict
from ..domain.session_run import SessionRunAssets
from ..domain.terminal_launch import TerminalLaunch, TerminalShell
from ..ports.operator_issue_commands import (
    OperatorCommandIntent,
    OperatorCommandOutcome,
    OperatorCommandStatus,
)
from ..infra.env import ENV_PREFIX
from .control_api_issue_support import ControlApiIssueDependency, StateLockFn

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator
    from ..ports import Issue as IssueProtocol

logger = logging.getLogger(__name__)

control_issue_router = APIRouter()


#: What each command is called once it has happened, for the operator reading
#: the toast. Wording is transport policy: the command reports the transition,
#: this decides how to say it (#6999 F6 round 7).
_OPERATOR_COMMAND_WORDING = {
    OperatorCommandIntent.RETRY: ("queued for retry", "retried"),
    OperatorCommandIntent.DISMISS: ("dismissed", "dismissed"),
}


def _operator_command(
    deps: "ControlApiIssueDependency", issue_number: int, intent: OperatorCommandIntent
) -> JSONResponse:
    """Run one operator command and map its typed outcome to HTTP (#6999 F5).

    ALL that is left here is transport. The command owns which labels it
    clears, that the shared block goes first, that a refusal stops everything
    after it, and that local retry/queue state is settled only once the GitHub
    side committed. Both buttons route through this one function, so neither
    can grow its own version of that ordering - which is precisely how dismiss
    came to prune its state and report success over a refusal retry honoured.
    """
    orchestrator = deps.get_orchestrator()
    if orchestrator is None:
        return JSONResponse(
            {"success": False, "error": "Orchestrator not initialized"},
            status_code=503,
        )
    commands = orchestrator.operator_issue_commands
    invoke = {
        OperatorCommandIntent.RETRY: commands.retry,
        OperatorCommandIntent.DISMISS: commands.dismiss,
    }[intent]
    try:
        outcome = invoke(issue_number)
    except Exception as exc:
        logger.exception(
            "Error running %s on issue #%d: %s", intent.value, issue_number, exc
        )
        return JSONResponse({"success": False, "error": str(exc)}, status_code=500)
    return _operator_command_response(outcome)


def _settled_body(outcome: OperatorCommandOutcome, done: str, attempted: str) -> dict:
    del attempted
    return {
        "success": True,
        "message": f"Issue #{outcome.issue_number} {done}",
        "removed_labels": list(outcome.removed),
    }


def _still_blocked_body(
    outcome: OperatorCommandOutcome, done: str, attempted: str
) -> dict:
    del done
    cause = (
        f"{', '.join(outcome.held_by)} still requires it"
        if outcome.held_by
        else "it could not be cleared"
    )
    return {
        "success": False,
        "error": (
            f"Issue #{outcome.issue_number} was not {attempted}: "
            f"{outcome.blocked} is still on the issue because {cause}."
        ),
        "removed_labels": list(outcome.removed),
        "failed_labels": [outcome.blocked],
        "held_by": list(outcome.held_by),
    }


def _incomplete_body(
    outcome: OperatorCommandOutcome, done: str, attempted: str
) -> dict:
    del done
    return {
        "success": False,
        "error": (
            f"Issue #{outcome.issue_number} was not {attempted}: failed to "
            f"remove {list(outcome.failed)} from GitHub. Removed "
            f"{list(outcome.removed)} successfully; retry the action."
        ),
        "removed_labels": list(outcome.removed),
        "failed_labels": list(outcome.failed),
    }


#: One row per outcome the command can produce: how to describe it, and what
#: that means over HTTP. A table rather than a branch chain because the set is
#: closed and exhaustive - a new outcome should fail loudly with a KeyError
#: here, not fall through some `else` into a plausible-looking response.
#:
#: Neither unsettled row is a 500 (nothing went wrong; the command did exactly
#: what it should) and neither is a 200 (the issue really is still blocked).
#: 409 with the labels still on the issue lets the dashboard say so instead of
#: showing an optimistic toast.
_OPERATOR_RESPONSE_BY_STATUS = {
    OperatorCommandStatus.COMMITTED: (_settled_body, 200),
    OperatorCommandStatus.STILL_BLOCKED: (_still_blocked_body, 409),
    OperatorCommandStatus.INCOMPLETE: (_incomplete_body, 409),
}


def _operator_command_response(outcome: OperatorCommandOutcome) -> JSONResponse:
    """Turn one typed outcome into this API's response contract."""
    done, attempted = _OPERATOR_COMMAND_WORDING[outcome.intent]
    build, code = _OPERATOR_RESPONSE_BY_STATUS[outcome.status]
    return JSONResponse(build(outcome, done, attempted), status_code=code)


@control_issue_router.post("/api/preflight-push")
async def preflight_push(request: Request) -> JSONResponse:
    """Check if a git push would succeed (dry-run).

    This endpoint allows coding-done/reviewer-done to verify a push would work
    before completing, while the agent is still active and can fix any issues.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    worktree_path = body.get("worktree")
    if not worktree_path:
        return JSONResponse({"error": "worktree is required"}, status_code=400)

    worktree = Path(worktree_path)
    if not worktree.exists():
        return JSONResponse({"error": f"Worktree does not exist: {worktree}"}, status_code=400)

    from ..execution import GitWorkingCopy

    result = GitWorkingCopy().push_preflight(worktree)
    return JSONResponse({
        "would_succeed": result.would_succeed,
        "error": result.error,
        "fix_hint": result.fix_hint,
    })


@control_issue_router.post("/api/review-exchange/respond")
async def review_exchange_respond(
    request: Request,
    deps: ControlApiIssueDependency,
) -> JSONResponse:
    """Deliver a review-exchange turn verdict into the orchestrator-owned slot.

    Called by the ``exchange-respond`` agent CLI. The orchestrator binds the
    delivery to the turn it currently has open for ``key`` (the per-role
    routing identifier) — the agent supplies only its verdict, never the
    turn identity. Returns the delivery status so the CLI can report it; the
    HTTP status is 200 for any well-formed delivery (accepted or rejected),
    reserving 4xx/5xx for malformed requests or a missing orchestrator.
    """
    try:
        body = _ReviewExchangeRespondBody.from_wire(await request.json())
    except json.JSONDecodeError:
        return JSONResponse(
            {"status": "error", "detail": "Invalid JSON body"}, status_code=400
        )
    except _ReviewExchangeRespondRequestError as exc:
        return exc.as_response()
    orchestrator = deps.get_orchestrator()
    if orchestrator is None:
        return JSONResponse(
            {"status": "error", "detail": "Orchestrator not initialized"},
            status_code=503,
        )
    mailbox = orchestrator.deps.services.turn_mailbox
    if mailbox is None:
        return JSONResponse(
            {"status": "error", "detail": "Turn mailbox not configured"},
            status_code=503,
        )
    result = mailbox.deliver(body.key, dict(body.verdict.to_wire()))
    logger.info(
        "[REVIEW_EXCHANGE] verdict delivery key=%s status=%s turn_id=%s",
        body.key,
        result.status.value,
        result.turn_id,
    )
    return JSONResponse({"status": result.status.value, "turn_id": result.turn_id})


@dataclass(frozen=True, slots=True)
class _ReviewExchangeRespondBody:
    """Validated request envelope for one review-exchange verdict callback."""

    key: str
    verdict: ExchangeVerdict

    @classmethod
    def from_wire(cls, raw: object) -> "_ReviewExchangeRespondBody":
        if not isinstance(raw, Mapping):
            raise _ReviewExchangeRespondRequestError(
                "request body must be a JSON object"
            )
        key = raw.get("key")
        if not isinstance(key, str) or not key.strip():
            raise _ReviewExchangeRespondRequestError("key is required")
        try:
            verdict = ExchangeVerdict.from_wire(raw.get("payload"))
        except ValueError as exc:
            raise _ReviewExchangeRespondRequestError(str(exc)) from exc
        return cls(key=key.strip(), verdict=verdict)


@dataclass(frozen=True, slots=True)
class _ReviewExchangeRespondRequestError(Exception):
    """HTTP 400 error for a malformed review-exchange callback body."""

    detail: str

    def as_response(self) -> JSONResponse:
        return JSONResponse(
            {"status": "error", "detail": self.detail},
            status_code=400,
        )


@control_issue_router.post("/api/issues/{issue_number}/resume")
async def resume_issue(
    issue_number: int,
    request: Request,
    deps: ControlApiIssueDependency,
) -> JSONResponse:
    """Resume orchestrator processing for a blocked/debug issue."""
    orchestrator = deps.get_orchestrator()
    if orchestrator is None:
        return JSONResponse(
            {"success": False, "error": "Orchestrator not initialized"},
            status_code=503,
        )

    worktree = get_worktree_path(orchestrator.config, issue_number)
    if not worktree.exists():
        return JSONResponse({
            "success": False,
            "error": f"Worktree not found: {worktree}",
            "hint": "The worktree may have been cleaned up. Check if the issue is still blocked.",
        }, status_code=404)

    try:
        resume_contract = _resolve_resume_run_contract(
            request_body=await _read_resume_request_body(request),
            worktree=worktree,
            orchestrator=orchestrator,
        )
    except _ResumeRunContractError as exc:
        return exc.as_response()
    run_assets = resume_contract.run_assets
    completion_path = resume_contract.completion_path
    completion_record = worktree / completion_path
    if not completion_record.exists():
        return JSONResponse({
            "success": False,
            "error": "No completion record found",
            "hint": "Run 'coding-done completed --implementation ... --problems ...' first.",
        }, status_code=404)

    issue_title = _get_issue_title(orchestrator, issue_number, deps.with_state_lock)
    try:
        result = orchestrator.deps.completion_processor.process(
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            completion_path=completion_path,
            run_assets=run_assets,
        )
        return JSONResponse({
            "success": result.success,
            "message": result.message,
            "pr_url": result.pr_url,
            "actions_taken": result.actions_taken,
            "errors": result.errors,
        })
    except Exception as exc:
        logger.exception("Error processing completion for issue #%d: %s", issue_number, exc)
        return JSONResponse({
            "success": False,
            "error": f"Processing failed: {exc}",
        }, status_code=500)


@dataclass(frozen=True, slots=True)
class _ResumeRunContract:
    """Typed request contract for manual resume of one active run."""

    run_assets: SessionRunAssets
    completion_path: str


@dataclass(frozen=True, slots=True)
class _ResumeRequestBody:
    """Validated manual resume request body."""

    run_dir: Path


@dataclass(frozen=True, slots=True)
class _ResumeRunContractError(Exception):
    """HTTP error for an invalid manual resume run contract."""

    status_code: int
    error: str
    hint: str

    def as_response(self) -> JSONResponse:
        return JSONResponse(
            {"success": False, "error": self.error, "hint": self.hint},
            status_code=self.status_code,
        )


async def _read_resume_request_body(request: Request) -> _ResumeRequestBody:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise _ResumeRunContractError(
            status_code=400,
            error="Resume request body must be a JSON object",
            hint="Send {'run_dir': '<absolute session run directory>'}.",
        )
    raw_run_dir = body.get("run_dir")
    if not isinstance(raw_run_dir, str) or not raw_run_dir.strip():
        raise _ResumeRunContractError(
            status_code=400,
            error="run_dir is required",
            hint="coding-done --resume must pass ISSUE_ORCHESTRATOR_RUN_DIR to the Control API.",
        )
    return _ResumeRequestBody(run_dir=Path(raw_run_dir))


def _resolve_resume_run_contract(
    *,
    request_body: _ResumeRequestBody,
    worktree: Path,
    orchestrator: "Orchestrator",
) -> _ResumeRunContract:
    requested_run_dir = request_body.run_dir
    if not requested_run_dir.is_absolute():
        raise _ResumeRunContractError(
            status_code=400,
            error="run_dir must be absolute",
            hint="The session run owner must inject the absolute session run directory.",
        )
    run_dir = requested_run_dir.resolve()
    manifest = orchestrator.deps.session_output.read_manifest(run_dir)
    if not manifest:
        raise _ResumeRunContractError(
            status_code=409,
            error="Recorded run manifest not found",
            hint="Resume processing requires a complete session run manifest.",
        )
    try:
        run_assets = SessionRunAssets.from_manifest_payload(
            run_dir=run_dir,
            manifest=manifest,
        )
    except (TypeError, ValueError) as exc:
        raise _ResumeRunContractError(
            status_code=409,
            error=f"Recorded run assets are invalid: {exc}",
            hint="Resume processing requires a complete typed run contract.",
        ) from exc
    if run_assets.worktree_path.resolve() != worktree.resolve():
        raise _ResumeRunContractError(
            status_code=409,
            error="Recorded run worktree does not match issue worktree",
            hint="The resume request must use the run directory created for this issue worktree.",
        )
    completion_path = _completion_path_from_resume_manifest(manifest)
    completion_abs = (worktree / completion_path).resolve()
    try:
        completion_abs.relative_to(run_assets.run_dir.resolve())
    except ValueError as exc:
        raise _ResumeRunContractError(
            status_code=409,
            error="Recorded run completion_path is outside run_dir",
            hint="The run owner must record a completion path contained by the injected run directory.",
        ) from exc
    return _ResumeRunContract(run_assets=run_assets, completion_path=completion_path)


def _completion_path_from_resume_manifest(manifest: Mapping[str, object]) -> str:
    raw_completion_path = manifest.get("completion_path")
    if not isinstance(raw_completion_path, str) or not raw_completion_path.strip():
        raise _ResumeRunContractError(
            status_code=409,
            error="Recorded run manifest has no completion_path",
            hint="Resume processing requires the run owner to record completion_path.",
        )
    completion_path = raw_completion_path.strip()
    rel = Path(completion_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise _ResumeRunContractError(
            status_code=409,
            error="Recorded run manifest has invalid completion_path",
            hint="completion_path must be a relative path inside the issue worktree.",
        )
    return completion_path


@control_issue_router.post("/api/issues/{issue_number}/debug-session")
async def launch_debug_session(  # noqa: C901 - debug session with validation and setup phases
    issue_number: int,
    deps: ControlApiIssueDependency,
) -> JSONResponse:
    """Launch an interactive debug session for a blocked issue."""
    orchestrator = deps.get_orchestrator()
    if orchestrator is None:
        return JSONResponse(
            {"success": False, "error": "Orchestrator not initialized"},
            status_code=503,
        )

    config = orchestrator.config
    state = orchestrator.state
    worktree = get_worktree_path(config, issue_number)
    if not worktree.exists():
        return JSONResponse({
            "success": False,
            "error": f"Worktree not found: {worktree}",
            "hint": "The worktree may have been cleaned up. The issue needs to be re-run first.",
        }, status_code=404)

    def _cached_issue() -> "IssueProtocol | None":
        for cached_issue in state.cached_queue_issues:
            if cached_issue.number == issue_number:
                return cached_issue
        return None

    issue: IssueProtocol | None = deps.with_state_lock(_cached_issue)
    if not issue:
        try:
            issue = orchestrator.deps.repository_host.get_issue(issue_number)
        except Exception as exc:
            logger.warning("Could not fetch issue #%d: %s", issue_number, exc)

    if not issue:
        return JSONResponse({
            "success": False,
            "error": f"Issue #{issue_number} not found",
            "hint": "The issue may have been closed or doesn't exist.",
        }, status_code=404)

    agent_type = issue.agent_type
    if not agent_type:
        return JSONResponse({
            "success": False,
            "error": "Issue has no agent type label",
            "hint": "Add an agent label (e.g., 'agent:claude') to the issue.",
        }, status_code=400)

    agent_config = config.agents.get(agent_type)
    if not agent_config:
        return JSONResponse({
            "success": False,
            "error": f"No agent config for {agent_type}",
            "hint": "Check your orchestrator configuration.",
        }, status_code=400)

    session_name = f"debug-{issue_number}"
    if orchestrator.deps.runner.session_exists(issue_number, session_name):
        return JSONResponse({
            "success": False,
            "error": f"Debug session already exists: {session_name}",
            "hint": "A debug session is already running. Focus on it or kill it first.",
        }, status_code=409)

    debug_context = (
        "This is an INTERACTIVE DEBUG SESSION. A previous automated run failed or was blocked. "
        "Work with the user to investigate and fix the issue. When done, the user will run "
        "'coding-done --resume' to continue the orchestrator flow."
    )
    base_command = agent_config.get_command(
        issue_number=issue_number,
        issue_title=issue.title,
        worktree=worktree,
        existing_work=debug_context,
        task_kind="code",
    )

    run_assets = orchestrator.deps.session_output.start_run(
        worktree,
        session_name,
        issue_number=issue_number,
        agent_label=agent_type,
        backend=config.terminal_adapter or "subprocess",
    )
    completion_path = get_completion_path(agent_type, run_dir=run_assets.run_dir.name)
    orchestrator.deps.session_output.update_manifest(
        run_assets.run_dir,
        {
            "completion_path": completion_path,
            "issue_number": issue_number,
            "agent_label": agent_type,
        },
    )

    env_exports = f"export ORCHESTRATOR_ISSUE_NUMBER='{issue_number}'"
    env_exports += f" ORCHESTRATOR_API_PORT='{config.control_api_port}'"
    env_exports += f" ORCHESTRATOR_AGENT_LABEL='{agent_type}'"
    env_exports += f" ORCHESTRATOR_SESSION_ID='{session_name}'"
    env_exports += f" {ENV_PREFIX}COMPLETION_PATH='{completion_path}'"
    env_exports += f" {ENV_PREFIX}VALIDATION_OUTPUT_DIR='{run_assets.run_dir}'"
    env_exports += f" {ENV_PREFIX}RUN_DIR='{run_assets.run_dir}'"
    orch_bin = Path(sys.executable).parent
    env_exports += f' PATH="{orch_bin}:$PATH"'
    command = f"{env_exports} && {base_command}"

    logger.info(
        "[debug-session] Launching for issue #%d: session=%s worktree=%s agent=%s",
        issue_number,
        session_name,
        worktree,
        agent_type,
    )
    session_created = orchestrator.deps.runner.create_session(
        session_id=issue_number,
        launch=TerminalLaunch.classified(command, TerminalShell.BASH),
        working_dir=str(worktree),
        title=f"Debug #{issue_number}",
        session_name=session_name,
    )

    if not session_created:
        return JSONResponse({
            "success": False,
            "error": "Failed to create terminal session",
            "hint": "Check if tmux is running and accessible.",
        }, status_code=500)

    return JSONResponse({
        "success": True,
        "session_name": session_name,
        "worktree_path": str(worktree),
        "agent": agent_type.replace("agent:", ""),
        "hint": "Debug session launched. When done, run 'coding-done --resume' to process completion.",
    })


@control_issue_router.post("/api/issues/{issue_number}/retry")
async def retry_issue(
    issue_number: int,
    deps: ControlApiIssueDependency,
) -> JSONResponse:
    """Retry a blocked issue by removing the blocked label and re-queueing."""
    return _operator_command(deps, issue_number, OperatorCommandIntent.RETRY)


@control_issue_router.post("/api/issues/{issue_number}/dismiss")
async def dismiss_issue(
    issue_number: int,
    deps: ControlApiIssueDependency,
) -> JSONResponse:
    """Dismiss a blocked issue without retrying."""
    return _operator_command(deps, issue_number, OperatorCommandIntent.DISMISS)


@control_issue_router.post("/api/issues/{issue_number}/close")
async def close_issue(
    issue_number: int,
    deps: ControlApiIssueDependency,
) -> JSONResponse:
    """Close an issue that is blocked because its awaiting-merge PR is gone."""
    orchestrator = deps.get_orchestrator()
    if orchestrator is None:
        return JSONResponse(
            {"success": False, "error": "Orchestrator not initialized"},
            status_code=503,
        )

    try:
        lm = orchestrator.deps.label_manager
        action = CloseIssueAction(
            issue_number=issue_number,
            reason="user closed issue from pr-closed blocked state",
            expected=build_expected_for_mutation(
                required={lm.blocked_pr_closed},
            ),
        )
        result = orchestrator.deps.action_applier.apply(action)
        if result.result_type != ActionResultType.SUCCESS:
            return JSONResponse(
                {
                    "success": False,
                    "error": result.error or "Issue close action failed",
                },
                status_code=500,
            )

        def _prune_state() -> None:
            QueueCache(
                orchestrator.config,
                orchestrator.state,
                orchestrator.deps.queue_cache_store,
            ).remove_issue_and_save(issue_number)

        deps.with_state_lock(_prune_state)
        logger.info("[close] Issue #%d closed from pr-closed blocked state", issue_number)
        return JSONResponse({
            "success": True,
            "message": f"Issue #{issue_number} closed",
            "issue_number": issue_number,
        })
    except ReconciliationRequired as exc:
        logger.info("[close] Issue #%d close requires reconciliation: %s", issue_number, exc)
        return JSONResponse(
            {
                "success": False,
                "error": "Issue state changed; refresh before closing",
            },
            status_code=409,
        )
    except ClaimLostError as exc:
        logger.info("[close] Issue #%d claim lost before close: %s", issue_number, exc)
        return JSONResponse(
            {
                "success": False,
                "error": "Issue claim changed; refresh before closing",
            },
            status_code=409,
        )
    except Exception as exc:
        logger.exception("Error closing issue #%d: %s", issue_number, exc)
        return JSONResponse({
            "success": False,
            "error": str(exc),
        }, status_code=500)


def _get_issue_title(
    orchestrator: "Orchestrator",
    issue_number: int,
    with_state_lock: StateLockFn,
) -> str:
    """Resolve issue title from cache, falling back to GitHub."""
    issue_title = f"Issue #{issue_number}"
    try:
        def _cached_title() -> str | None:
            for issue in orchestrator.state.cached_queue_issues:
                if issue.number == issue_number:
                    return issue.title
            return None

        cached_title = with_state_lock(_cached_title)
        if cached_title:
            return cached_title

        issue_data = orchestrator.deps.repository_host.get_issue(issue_number)
        if issue_data:
            return issue_data.title
    except Exception as exc:
        logger.warning("Could not fetch issue title for #%d: %s", issue_number, exc)

    return issue_title
