"""Rework session launch flow."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..domain.issue_key import IssueKey
from ..domain.coder_prompt import (
    CoderPromptAddendumUnavailable,
    PreparedCoderPromptAddendum,
)
from ..domain.models import (
    AgentConfig,
    Issue,
    PendingRework,
    Session,
    SessionKey,
    TaskKind,
    get_completion_path,
)
from ..domain.session_run import SessionRunAssets
from ..events import EventName
from ..infra.config import Config
from ..infra.logging_config import issue_log, log_context
from ..ports import EventSink, RepositoryHost
from ..ports.event_sink import make_run_scoped_event, make_trace_event
from ..ports.session_output import SessionOutput
from ..ports.coder_prompt import CoderPromptAddendumProvider
from ..ports.worktree_manager import WorktreeManager, WorktreeReuseOptions
from .actions import Action, AddCommentAction, AddLabelAction, RemoveLabelAction
from .launch_transaction import (
    NO_LAUNCH_WORK_CLAIM,
    LaunchWorkClaim,
    abandon_claim_unless_spawned,
)
from .session_launch_types import LaunchDisposition, LaunchResult
from .stack_base import StackBaseDecision
from .session_review_support import copy_review_feedback_to_rework, format_reviewer_feedback
from .session_worktree_diagnostics import (
    build_worktree_error_comment,
    write_worktree_diagnostic,
)
from .transition_log import log_transition
from .worktree_context import WorktreeContext
from .needs_human_block import NeedsHumanCause

if TYPE_CHECKING:
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)


class ActionApplierFn(Protocol):
    def __call__(self, actions: list[Action], *, context: str) -> bool: ...


class SessionExistsFn(Protocol):
    def __call__(self, session_name: str, /) -> bool: ...


class SessionCreatorFn(Protocol):
    def __call__(
        self,
        session_name: str,
        command: str,
        worktree_path: Path,
        title: str | None,
        /,
    ) -> bool: ...


class WorktreeReuseOptionsFactory(Protocol):
    def __call__(
        self,
        *,
        allow_remote_branch_delete: bool = True,
        force_fresh: bool = False,
    ) -> WorktreeReuseOptions: ...


class SessionIdentityMetadataBuilder(Protocol):
    def __call__(
        self,
        agent_config: AgentConfig,
        *,
        extra_provider_args: dict[str, str] | None,
    ) -> dict[str, object]: ...


class GuardLabelClearer(Protocol):
    def __call__(self, *, issue_number: int, context: str) -> None: ...


class InterruptedGuardLabelClearer(Protocol):
    def __call__(self, *, issue_number: int, mode: str, context: str) -> None: ...


class PromptPersister(Protocol):
    def __call__(self, run_dir: Path, prompt_text: str) -> str: ...


class ProviderCommandWrapper(Protocol):
    def __call__(self, base_command: str, agent_config: AgentConfig, run_dir: Path, /) -> str: ...


class SessionEnvBuilder(Protocol):
    def __call__(
        self,
        *,
        completion_path: str,
        session_id: str,
        agent_label: str,
        issue_number: int,
        run_assets: SessionRunAssets,
        worktree_path: Path,
    ) -> str: ...


class ProviderReadinessChecker(Protocol):
    def __call__(self, provider: str | None, issue_number: int) -> LaunchResult | None: ...


class StackDecisionResolverFn(Protocol):
    def __call__(self, issue_number: int) -> StackBaseDecision: ...


@dataclass(frozen=True)
class ReworkLaunchDependencies:
    """Dependencies needed by the rework launch coordinator."""

    config: Config
    events: EventSink
    repository_host: RepositoryHost
    worktree_manager: WorktreeManager
    session_output: SessionOutput
    label_manager: LabelManager
    session_exists: SessionExistsFn
    create_session: SessionCreatorFn
    apply_actions: ActionApplierFn
    worktree_reuse_options: WorktreeReuseOptionsFactory
    session_identity_launch_metadata: SessionIdentityMetadataBuilder
    clear_interrupted_retry_guard_label: InterruptedGuardLabelClearer
    clear_reset_retry_pending_label: GuardLabelClearer
    clear_reset_retry_scratch_pending_label: GuardLabelClearer
    persist_session_prompt: PromptPersister
    wrap_provider_command: ProviderCommandWrapper
    build_session_env: SessionEnvBuilder
    check_provider_ready: ProviderReadinessChecker
    resolve_stack_decision: StackDecisionResolverFn
    coder_prompt_addendum: CoderPromptAddendumProvider


@dataclass(frozen=True)
class _ReworkLaunchAdmission:
    """All non-mutating inputs required before rework worktree preparation."""

    stack_base_branch: str | None
    coder_prompt: PreparedCoderPromptAddendum


def _admit_rework_launch(
    *,
    deps: ReworkLaunchDependencies,
    active_sessions: list[Session],
    session_name: str,
    issue_number: int,
    pr_number: int,
    coder_prompt: PreparedCoderPromptAddendum,
) -> _ReworkLaunchAdmission | LaunchResult:
    """Resolve non-mutating gates before worktree or queue mutations."""
    preflight_failure, stack_base_branch = _rework_preflight(
        deps,
        active_sessions=active_sessions,
        session_name=session_name,
        issue_number=issue_number,
        pr_number=pr_number,
    )
    if preflight_failure is not None:
        return preflight_failure
    return _ReworkLaunchAdmission(
        stack_base_branch=stack_base_branch,
        coder_prompt=coder_prompt,
    )


def _rework_preflight(
    deps: ReworkLaunchDependencies,
    *,
    active_sessions: list[Session],
    session_name: str,
    issue_number: int,
    pr_number: int,
) -> tuple[LaunchResult | None, str | None]:
    """Run rework launch preflight: session conflicts then the stack work gate.

    Returns ``(failure, stack_base_branch)``. ``failure`` is a non-``None``
    :class:`LaunchResult` when an existing session conflicts, or when the stack
    work gate is closed — predecessor not ready or ambiguous base — so rework
    fails closed (the dependency-blocked signal is emitted) instead of resetting
    the reused successor worktree onto the default base (#6596). Otherwise
    ``stack_base_branch`` is the predecessor branch (or ``None`` for a non-stack
    issue / merged predecessor → default base).
    """
    conflict = check_rework_conflicts(
        session_name,
        active_sessions,
        issue_number,
        session_exists=deps.session_exists,
    )
    if conflict is not None:
        return conflict, None

    decision = deps.resolve_stack_decision(issue_number)
    if decision.allowed:
        return None, decision.base_branch
    reason = decision.reason or "stack work gate blocked"
    log_transition("rework", issue_number, "LAUNCHING", "SKIP", f"stack gate: {reason}")
    deps.events.publish(make_trace_event(
        EventName.ISSUE_DEPENDENCY_BLOCKED,
        {
            "issue_number": issue_number,
            "issue_title": f"Rework #{pr_number}",
            "reason": reason,
            "gate": "work",
            "retryable": decision.retryable,
        },
    ))
    return LaunchResult(None, False, f"Stack dependencies not satisfied: {reason}"), None


def _rework_launch_identity(
    rework: PendingRework, deps: ReworkLaunchDependencies
) -> "LaunchResult | tuple[AgentConfig, int, PreparedCoderPromptAddendum]":
    """Everything a rework launch must know before it reads anything remote.

    The agent it will run as, the issue it belongs to, required prompt input,
    and whether that agent's provider is usable. Prompt preparation comes first
    because provider refusal can write a shared blocked label and durable record.
    All of this stays ahead of :func:`resolve_rework_pr`, avoiding a GitHub read
    for any refused launch.
    """
    agent_config = deps.config.agents.get(rework.agent_type)
    if not agent_config:
        return LaunchResult(None, False, f"No agent config for {rework.agent_type}")
    issue_number = rework.resolve_issue_number()
    if issue_number is None:
        return LaunchResult(
            None, False, f"Unresolved issue number for rework {rework.issue_key}"
        )
    prepared_coder_prompt = deps.coder_prompt_addendum.prepare(
        task=TaskKind.REWORK,
        agent_label=rework.agent_type,
    )
    if isinstance(prepared_coder_prompt, CoderPromptAddendumUnavailable):
        return LaunchResult.required_input_unavailable(prepared_coder_prompt.reason)
    if result := deps.check_provider_ready(agent_config.provider, issue_number):
        return result
    return agent_config, issue_number, prepared_coder_prompt


def launch_rework_session(
    rework: PendingRework,
    active_sessions: list[Session],
    deps: ReworkLaunchDependencies,
    *,
    work_claim: LaunchWorkClaim = NO_LAUNCH_WORK_CLAIM,
) -> LaunchResult:
    """Launch a rework session to fix issues found in review."""
    resolved = _rework_launch_identity(rework, deps)
    if isinstance(resolved, LaunchResult):
        return resolved
    agent_config, issue_number, prepared_coder_prompt = resolved

    issue_key = rework.issue_key
    session_key = SessionKey(issue=issue_key, task=TaskKind.REWORK)
    pr_number, branch_name = resolve_rework_pr(deps.repository_host, rework, issue_number)

    session_name = f"rework-{issue_number}"
    # Preflight: session conflicts, then the stack work gate. A blocked/ambiguous
    # stack predecessor fails the rework closed before the reused successor
    # worktree is reset onto the default base (#6596).
    admission = _admit_rework_launch(
        deps=deps,
        active_sessions=active_sessions,
        session_name=session_name,
        issue_number=issue_number,
        pr_number=pr_number,
        coder_prompt=prepared_coder_prompt,
    )
    if isinstance(admission, LaunchResult):
        return admission
    stack_base_branch = admission.stack_base_branch
    prepared_coder_prompt = admission.coder_prompt

    log_transition("rework", issue_number, "QUEUED", "LAUNCHING", f"no conflicts, cycle={rework.rework_cycle}")
    logger.info(
        "[launch] Rework session identity: issue=%s issue_key=%s pr=%s agent=%s task=%s "
        "session=%s branch=%s cycle=%s",
        issue_number,
        issue_key,
        pr_number,
        rework.agent_type,
        TaskKind.REWORK.value,
        session_name,
        branch_name,
        rework.rework_cycle,
        extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
    )
    logger.info(
        "[launch] Rework session key: issue=%s pr=%s session=%s session_key=%s",
        issue_number,
        pr_number,
        session_name,
        session_key.stable_id(),
        extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
    )

    coding_attempt = rework.rework_cycle + 1
    phase_name = f"coding-{coding_attempt}"
    ctx = WorktreeContext.create(
        worktree_manager=deps.worktree_manager,
        config=deps.config,
        events=deps.events,
        session_output=deps.session_output,
        issue_number=issue_number,
        issue_title=f"Rework #{pr_number}",
        session_name=session_name,
        agent_label=rework.agent_type,
        branch_name=branch_name,
        enforce_hooks=deps.config.enforce_hooks,
        pre_push_hook=deps.config.pre_push_hook,
        reuse_options=deps.worktree_reuse_options(allow_remote_branch_delete=False),
        phase_name=phase_name,
        stack_base_branch=stack_base_branch,
    )

    if ctx.error:
        log_transition("rework", issue_number, "LAUNCHING", "BLOCKED", "worktree preparation failed")
        logger.error(issue_log(issue_number, "BLOCKED: worktree preparation failed for rework: %s"), ctx.error)
        write_worktree_diagnostic(ctx.error)
        needs_human_label = deps.label_manager.needs_human
        deps.apply_actions([
            AddLabelAction(
                issue_number=issue_number,
                label=needs_human_label,
                reason="worktree preparation failed",
                needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
            ),
            AddCommentAction(
                number=issue_number,
                comment=build_worktree_error_comment(ctx.error),
                reason="worktree preparation failed",
            ),
        ], context="worktree_prepare_rework")
        deps.events.publish(make_trace_event(
            EventName.ISSUE_NEEDS_HUMAN,
            {
                "issue_number": issue_number,
                "pr_number": pr_number,
                "reason": str(ctx.error),
            },
        ))
        return LaunchResult(None, False, f"Worktree preparation failed: {ctx.error}")

    worktree_path = ctx.worktree_path
    worktree_info = ctx.worktree_info
    run = ctx.run
    claude_project_dir = ctx.claude_project_dir

    if failure := work_claim.hold_before_spawn(run, issue_number=issue_number):
        return failure

    with abandon_claim_unless_spawned(work_claim, run) as spawn:

        ctx.write_worktree_note()
        ctx.write_session_identity({
            "task": TaskKind.REWORK.value,
            "issue_key": issue_key.stable_id(),
            "pr_number": pr_number,
            "session_key": session_key.stable_id(),
            "agent": rework.agent_type,
            "rework_cycle": rework.rework_cycle,
            **deps.session_identity_launch_metadata(
                agent_config,
                extra_provider_args=None,
            ),
        })
        deps.clear_interrupted_retry_guard_label(
            issue_number=issue_number,
            mode="coding",
            context="launch_clear_interrupted_guard_rework",
        )
        deps.clear_reset_retry_pending_label(
            issue_number=issue_number,
            context="launch_clear_reset_retry_pending_rework",
        )
        deps.clear_reset_retry_scratch_pending_label(
            issue_number=issue_number,
            context="launch_clear_reset_retry_scratch_pending_rework",
        )

        logger.info(
            "[SESSION_RUN_START] run_id=%s session=%s issue=%s",
            run.run_id,
            session_name,
            issue_number,
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )
        logger.info(
            "[launch] Rework session paths: issue=%s pr=%s worktree=%s branch=%s",
            issue_number,
            pr_number,
            worktree_path,
            branch_name,
        )
        logger.info(
            "[launch] Claude project dir: session=%s path=%s exists=%s",
            session_name,
            claude_project_dir,
            claude_project_dir.exists(),
        )

        existing_work = build_rework_existing_work(worktree_info.rebase_failed)
        if existing_work:
            logger.warning("[launch] Rebase failed for rework - agent will need to resolve merge conflicts")

        copy_review_feedback_to_rework(
            worktree_path=worktree_path,
            pr_number=pr_number,
            rework_run_assets=run,
        )

        feedback_sections: list[str] = []
        if rework.feedback:
            feedback_sections.append(rework.feedback)

        reviewer_feedback = format_reviewer_feedback(
            pr_number=pr_number,
            repository_host=deps.repository_host,
            cache_minutes=deps.config.reviewer_feedback_cache_minutes,
            run_assets=run,
            sleep_fn=time.sleep,
        )
        if reviewer_feedback:
            feedback_sections.append(reviewer_feedback)

        if feedback_sections:
            combined_feedback = "\n\n".join(feedback_sections)
            existing_work = f"{existing_work}\n\n{combined_feedback}" if existing_work else combined_feedback
            logger.info("[launch] Including rework feedback in session prompt")
            deps.session_output.save_review_feedback(
                worktree_path=worktree_path,
                cycle=rework.rework_cycle,
                feedback=combined_feedback,
                pr_number=pr_number,
            )

        issue_title = f"Rework PR #{pr_number} (cycle {rework.rework_cycle})"
        rendered_prompt = agent_config.render_initial_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            worktree=worktree_path,
            pr_number=pr_number,
            existing_work=existing_work,
        )
        rendered_prompt = prepared_coder_prompt.compose(rendered_prompt)
        prompt_path = deps.persist_session_prompt(run.run_dir, rendered_prompt)
        base_command = agent_config.get_command_for_prompt(
            rendered_prompt,
            issue_number=issue_number,
            issue_title=issue_title,
            worktree=worktree_path,
            pr_number=pr_number,
            task_kind=TaskKind.REWORK.value,
        )
        base_command = deps.wrap_provider_command(base_command, agent_config, run.run_dir)
        completion_path = get_completion_path(rework.agent_type, run_dir=run.run_dir.name)
        deps.session_output.update_manifest(
            run.run_dir,
            {
                "completion_path": completion_path,
                "session_prompt_path": prompt_path,
            },
        )
        env_exports = deps.build_session_env(
            completion_path=completion_path,
            session_id=run.session_name,
            agent_label=rework.agent_type,
            issue_number=issue_number,
            run_assets=run,
            worktree_path=worktree_path,
        )
        command = f"{env_exports} && {base_command}"
        logger.info(
            "[launch] Rework session command: issue=%s pr=%s session=%s worktree=%s completion=%s command=%s",
            issue_number,
            pr_number,
            session_name,
            worktree_path,
            completion_path,
            command,
        )

        session_created = deps.create_session(session_name, command, worktree_path, f"Rework #{issue_number}")
        logger.info(
            "[launch] Rework session create result: issue=%s pr=%s session=%s created=%s",
            issue_number,
            pr_number,
            session_name,
            session_created,
        )
        if not session_created:
            # Reported success regardless of this until #6999 F5. The damage was
            # worse here than for a review: the launch went on to publish
            # REWORK_STARTED and to strip the ``needs-rework`` trigger from the
            # PR, so the one label that could have re-queued the rework was
            # removed on behalf of a terminal that never existed. Failing before
            # any of that leaves the trigger in place and hands the request back
            # through the same compensation every other queue uses.
            log_transition(
                "rework", issue_number, "LAUNCHING", "FAILED", "session creation failed"
            )
            logger.error(
                issue_log(issue_number, "FAILED: rework session creation failed")
            )
            return LaunchResult.terminal_spawn_failed()
        spawn.mark_spawned()  # terminal RUNNING = irreversible

        rework_issue = Issue(
            number=issue_number,
            title=f"Rework #{pr_number}",
            labels=[rework.agent_type],
        )
        session = Session(
            key=session_key,
            issue=rework_issue,
            agent_config=agent_config,
            terminal_id=session_name,
            worktree_path=worktree_path,
            branch_name=branch_name,
            completion_path=completion_path,
            run_assets=run,
            agent_label=rework.agent_type,
            pr_number=pr_number,
            rework_cycle=rework.rework_cycle,
            original_prompt=rendered_prompt,
        )

        log_transition("rework", issue_number, "LAUNCHING", "ACTIVE", f"session launched, cycle={rework.rework_cycle}")
        logger.info("Launched rework session for issue #%d (cycle %d)", issue_number, rework.rework_cycle)

        full_completion_path = (worktree_path / completion_path).resolve()
        deps.events.publish(make_run_scoped_event(EventName.REWORK_STARTED, {
            "issue_number": issue_number,
            "pr_number": pr_number,
            "agent": rework.agent_type,
            "task": "rework",
            "session_name": session_name,
            "rework_cycle": rework.rework_cycle,
            "run_id": run.run_id,
            "run_dir": str(run.run_dir),
            "completion_path": completion_path,
            "completion_path_absolute": str(full_completion_path),
            "session_prompt_path": prompt_path,
        }))

        update_rework_cycle_label(
            pr_number,
            issue_number,
            issue_key,
            rework.rework_cycle,
            label_manager=deps.label_manager,
            apply_actions=deps.apply_actions,
            events=deps.events,
        )

        deps.apply_actions([
            RemoveLabelAction(
                issue_number=pr_number,
                label=deps.label_manager.needs_rework,
                reason="rework started",
            ),
        ], context="rework_remove_needs_rework")
        deps.events.publish(make_trace_event(EventName.PR_VIEW_CHANGED, {
            "pr_number": pr_number,
            "issue_number": issue_number,
            "issue_key": issue_key.stable_id(),
            "removed": [deps.label_manager.needs_rework],
        }))

        return LaunchResult(session, True)


def check_rework_conflicts(
    session_name: str,
    active_sessions: list[Session],
    issue_number: int,
    *,
    session_exists: SessionExistsFn,
) -> LaunchResult | None:
    """Return a launch failure when a rework terminal is already active."""
    if any(s.terminal_id == session_name for s in active_sessions):
        log_transition("rework", issue_number, "QUEUED", "SKIP", "already in active_sessions")
        return LaunchResult(None, False, "Already in active sessions")
    if session_exists(session_name):
        log_transition("rework", issue_number, "QUEUED", "SKIP", "terminal session already running")
        return LaunchResult(None, False, "Terminal session already running", disposition=LaunchDisposition.EXISTING_TERMINAL)
    return None


def resolve_rework_pr(
    repository_host: RepositoryHost,
    rework: PendingRework,
    issue_number: int,
) -> tuple[int, str]:
    """Resolve PR number and branch for a rework session."""
    if rework.pr_number:
        pr_info = repository_host.get_pr(rework.pr_number)
        if pr_info:
            return pr_info.number, pr_info.branch or f"{issue_number}-rework"
    return resolve_rework_pr_details(repository_host, issue_number)


def resolve_rework_pr_details(repository_host: RepositoryHost, issue_number: int) -> tuple[int, str]:
    """Resolve the first open PR for an issue, or fall back to a rework branch."""
    prs = repository_host.get_prs_for_issue(issue_number)
    if not prs:
        return issue_number, f"{issue_number}-rework"
    pr = prs[0]
    return pr.number, pr.branch


def build_rework_existing_work(rebase_failed: bool) -> str | None:
    if not rebase_failed:
        return None
    return (
        "WARNING: This branch could not be rebased onto main due to merge conflicts. "
        "The code is out of date. You should resolve the conflicts by running: "
        "git fetch origin main && git rebase origin/main. "
        "If conflicts occur, resolve them and continue with: git rebase --continue. "
        "This is critical to ensure tests pass with the latest code."
    )


def update_rework_cycle_label(
    pr_number: int,
    issue_number: int,
    issue_key: IssueKey,
    cycle: int,
    *,
    label_manager: LabelManager,
    apply_actions: ActionApplierFn,
    events: EventSink,
) -> None:
    """Update the rework cycle label on a PR."""
    actions: list[Action] = []
    removed: list[str] = []
    for i in range(1, cycle):
        label = label_manager.rework_cycle(i)
        removed.append(label)
        actions.append(RemoveLabelAction(
            issue_number=pr_number,
            label=label,
            reason="rework cycle update",
        ))
    added_label = label_manager.rework_cycle(cycle)
    actions.append(AddLabelAction(
        issue_number=pr_number,
        label=added_label,
        reason="rework cycle update",
    ))
    apply_actions(actions, context="rework_cycle_label")
    events.publish(make_trace_event(EventName.PR_VIEW_CHANGED, {
        "pr_number": pr_number,
        "issue_number": issue_number,
        "issue_key": issue_key.stable_id(),
        "added": [added_label],
        "removed": removed,
    }))
