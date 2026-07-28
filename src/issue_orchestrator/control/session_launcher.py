"""SessionLauncher - handles launching agent sessions.

This module extracts session launching logic from the orchestrator.
It coordinates:
1. Agent configuration resolution
2. Worktree creation and setup
3. Label management during launch
4. Session creation via SessionManager
5. State machine transitions
6. Event emission

The orchestrator calls into this for all session launching, keeping
the orchestrator focused on coordination and main loop logic.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Callable, Mapping, Sequence

if TYPE_CHECKING:
    from ..ports.agent_callback_endpoint import AgentCallbackEndpoint
    from ..ports.board_snapshot_provider import BoardSnapshotProvider
    from ..domain.state_machines.issue_machine import IssueStateMachine
    from ..domain.state_machines.session_machine import SessionStateMachine
    from ..domain.state_machines.review_machine import ReviewStateMachine
    from .dependency_evaluator import DependencyEvaluator
    from .action_applier import ActionApplier
    from ..ports.claim_manager import ClaimManager
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .provider_resilience import ProviderResilienceManager
    from .label_manager import LabelManager

from ..infra.config import Config
from ..infra.logging_config import issue_log, log_context
from ..events import EventName
from ..domain.models import (
    AgentConfig,
    Issue,
    PendingReview,
    PendingRetrospectiveReview,
    PendingRework,
    PendingValidationRetry,
    Session,
    SessionKey,
    TaskKind,
    get_completion_path,
)
from ..domain.session_run import SessionRunAssets
from ..domain.dependency_gates import Gate
from .worktree_context import WorktreeContext
from .stack_base import StackBaseDecision
from ..infra.validation_state import DEFAULT_RETRY_TEMPLATE
from ..domain.tech_lead_session import TechLeadLaunchScope
from .tech_lead_session_policy import (
    failure_investigation_scratch_identity,
    is_tech_lead_session,
    prepare_tech_lead_session_data,
)
from ..ports import (
    ManifestDownloader,
    EventSink,
    RepositoryHost,
    Issue as IssueProtocol,
    WorkingCopy,
    CommandRunner,
)
from ..ports.session_output import SessionOutput
from ..ports.event_sink import SessionStartedEventPayload, make_session_started_event
from ..ports.worktree_manager import WorktreeManager, WorktreeReuseOptions
from ..ports.event_sink import make_run_scoped_event, make_trace_event
from .provider_availability import ProviderAvailabilityPolicy
from .action_applier import ActionApplier
from .actions import Action, AddLabelAction, RemoveLabelAction
from .tech_lead_needs_human_reconcile import TechLeadNeedsHumanLifecycle, discover_tech_lead_needs_human_issue_numbers
from .session_manager import SessionManager, SessionRef
from .session_launch_types import ClaimAcquisitionResult, LaunchResult
from .session_rework_launcher import (
    ReworkLaunchDependencies,
    launch_rework_session as launch_rework_flow,
)
from .session_review_support import (
    build_review_existing_work,
    review_launch_validity,
)
from .retrospective_review import (
    build_retrospective_review_existing_work,
    resolve_prior_pr_for_launch,
)
from .session_worktree_diagnostics import (
    build_worktree_error_comment,
    write_worktree_diagnostic,
)
from .transition_log import log_transition
from .isolation import build_runtime_tool_env
from .session_env import build_session_env_exports
from .provider_command_wrapper import ProviderCommandWrapper

logger = logging.getLogger(__name__)
_TRUNCATION_MARKER_BUDGET = 30
_MIN_USEFUL_TRUNCATED_HEAD = 100


@dataclass(frozen=True)
class _DependencyFreshness:
    """Outcome of the just-before-launch dependency recheck (ADR-0029 / #6596).

    ``failure`` is a non-``None`` :class:`LaunchResult` when the work gate is no
    longer open (the launch must abort). ``stack_base_branch`` carries the same
    gate report's selected stack base so the launcher can seed the successor's
    worktree from the predecessor branch without re-evaluating (and re-gathering
    predecessor facts), avoiding a second round of GitHub reads.
    """

    failure: "LaunchResult | None" = None
    stack_base_branch: str | None = None


def detect_existing_work(
    worktree_path: Path,
    working_copy: WorkingCopy,
    *,
    seed_ref: str | None = None,
) -> Optional[str]:
    """Check if worktree has commits ahead of main and return context for agent."""
    try:
        if seed_ref:
            head_sha = working_copy.get_head_sha(worktree_path)
            if head_sha and head_sha == seed_ref:
                return None

        commits = working_copy.get_commits_ahead_of_main(worktree_path)
        if not commits:
            return None

        branch = working_copy.get_current_branch(worktree_path) or "unknown"
        commit_list = "\n".join(
            f"  - {c.short_sha} {c.message}" for c in commits[:10]
        )
        if len(commits) > 10:
            commit_list += f"\n  ... and {len(commits) - 10} more"

        return (
            f"This worktree has {len(commits)} existing commit(s) from a previous session. "
            f"Branch: {branch}. Commits: {commit_list}. "
            f"EVALUATE this existing work BEFORE starting fresh."
        )
    except Exception as e:
        logger.warning("Failed to detect existing work: %s", e)
        return None


def _truncate_with_tail(text: str, max_length: int = 4000, tail_length: int = 2000) -> str:
    """Truncate long validation output while preserving the summary tail."""
    if len(text) <= max_length:
        return text
    head_length = max_length - tail_length - _TRUNCATION_MARKER_BUDGET
    if head_length < _MIN_USEFUL_TRUNCATED_HEAD:
        return f"[...truncated {len(text) - tail_length} chars...]\n\n{text[-tail_length:]}"
    omitted = len(text) - head_length - tail_length
    return f"{text[:head_length]}\n\n[...truncated {omitted} chars...]\n\n{text[-tail_length:]}"


class SessionLauncher:
    """Launches agent sessions for issues, reviews, and reworks.

    Dependencies:
    - config: Configuration with agent definitions
    - events: EventSink for trace events
    - repository_host: For GitHub reads during launch
    - action_applier: For applying label/comment mutations
    - session_manager: For terminal session operations
    - manifest_downloader: For downloading PR data in tech_lead sessions
    - get_issue_machine: Callback to get/create issue state machines
    - get_session_machine: Callback to get/create session state machines
    - get_review_machine: Callback to get/create review state machines
    """

    def __init__(
        self,
        config: Config,
        events: EventSink,
        repository_host: RepositoryHost,
        action_applier: ActionApplier,
        session_manager: SessionManager,
        worktree_manager: WorktreeManager,
        working_copy: WorkingCopy,
        command_runner: CommandRunner,
        session_output: SessionOutput,
        manifest_downloader: ManifestDownloader,
        tech_lead_authority: "TechLeadAuthorityStore",
        session_exists_fn: Callable[[str], bool],
        create_session_fn: Callable[[str, str, Path, str | None], bool],
        get_issue_machine: Callable[["IssueProtocol"], Optional["IssueStateMachine"]],
        get_session_machine: Callable[[str, int, int], Optional["SessionStateMachine"]],
        get_review_machine: Callable[[int, int], Optional["ReviewStateMachine"]],
        refresh_issue_fn: Optional[Callable[[int], Optional["IssueProtocol"]]] = None,
        dependency_evaluator: Optional["DependencyEvaluator"] = None,
        claim_manager: Optional["ClaimManager"] = None,
        provider_resilience: Optional["ProviderResilienceManager"] = None,
        remove_session_machine: Callable[[str], None] | None = None,
        label_manager: Optional["LabelManager"] = None,
        send_to_session_fn: Optional[Callable[[str, str], bool]] = None,
        *,
        # Required (keyword-only): tech_lead prompts treat board-snapshot.json as
        # authoritative required input, so the launcher must always be able to
        # produce one. Tests inject a null-object/fake provider, never None.
        board_snapshot_provider: "BoardSnapshotProvider",
        agent_callback_endpoint: "AgentCallbackEndpoint",
    ):
        self.config = config
        self.events = events
        self.repository_host = repository_host
        self._action_applier = action_applier
        self.session_manager = session_manager
        self._worktree_manager = worktree_manager
        self._working_copy = working_copy
        self._command_runner = command_runner
        self._session_output = session_output
        self._manifest_downloader = manifest_downloader
        self._tech_lead_authority = tech_lead_authority
        self._board_snapshot_provider = board_snapshot_provider
        self._agent_callback_endpoint = agent_callback_endpoint
        self._session_exists = session_exists_fn
        self._create_session = create_session_fn
        self._get_issue_machine = get_issue_machine
        self._get_session_machine = get_session_machine
        self._get_review_machine = get_review_machine
        self._refresh_issue = refresh_issue_fn
        self._dependency_evaluator = dependency_evaluator
        self._claim_manager = claim_manager
        self._provider_resilience = provider_resilience
        self._provider_policy = ProviderAvailabilityPolicy(config, provider_resilience) if provider_resilience else None
        self._provider_command_wrapper: ProviderCommandWrapper | None = None
        self._remove_session_machine = remove_session_machine
        self._send_to_session = send_to_session_fn
        if label_manager is None:
            from .label_manager import LabelManager
            label_manager = LabelManager(config)
        self._lm = label_manager
        self._tech_lead_needs_human = TechLeadNeedsHumanLifecycle(
            labels=label_manager,
            events=events,
            read_labels=repository_host.get_issue_labels_fresh,
            discover_marked_issue_numbers=lambda: discover_tech_lead_needs_human_issue_numbers(repository_host, label_manager.tech_lead_needs_human),
            apply_actions=lambda actions, context: self._apply_actions(
                actions, context=context
            ),
        )

    def _worktree_reuse_options(
        self,
        *,
        allow_remote_branch_delete: bool = True,
        force_fresh: bool = False,
        preserve_branch: bool = False,
    ) -> WorktreeReuseOptions:
        options = WorktreeReuseOptions(
            reuse_push_preflight=self.config.reuse_push_preflight,
            worktree_branch_on_recreate=self.config.worktree_branch_on_recreate,
            allow_no_verify_dry_run_preflight=self.config.allow_no_verify_dry_run_preflight,
            allow_remote_branch_delete=allow_remote_branch_delete,
            preserve_branch=preserve_branch,
        )
        if force_fresh:
            options.disable_reuse = True
            options.worktree_branch_on_recreate = "create_new_branch"
        return options

    @staticmethod
    def _extra_provider_args_from_labels(labels: Sequence[str]) -> dict[str, str] | None:
        """Build per-issue provider arg overrides from issue labels.

        Currently supports:
        - ``verbose`` label → ``{"verbose": "true"}``
        """
        args: dict[str, str] = {}
        if "verbose" in labels:
            args["verbose"] = "true"
        return args or None

    @staticmethod
    def _session_identity_launch_metadata(
        agent_config: "AgentConfig",
        *,
        extra_provider_args: dict[str, str] | None,
    ) -> dict[str, object]:
        return {
            "provider": str(agent_config.provider or ""),
            "model": str(agent_config.model or ""),
            "permission_mode": agent_config.effective_permission_mode,
            "timeout_minutes": int(agent_config.timeout_minutes),
            "extra_provider_args": dict(extra_provider_args or {}),
        }

    def _apply_actions(self, actions: list[Action], *, context: str) -> bool:
        """Apply mutations through the ActionApplier."""
        all_ok = True
        for action in actions:
            result = self._action_applier.apply(action)
            if not result.success:
                all_ok = False
                logger.warning(
                    "[launch] Failed to apply %s (%s): %s",
                    action.action_type.value,
                    context,
                    result.error,
                )
        return all_ok

    def escalate_issue_needs_human(
        self,
        *,
        issue_number: int,
        reason: str,
        comment: str,
        context: str,
        event_data: dict[str, object],
    ) -> bool:
        """Commit the marker-owned needs-human escalation."""
        return self._tech_lead_needs_human.escalate(
            issue_number=issue_number,
            reason=reason,
            comment=comment,
            context=context,
            event_data=event_data,
        )

    def reconcile_stale_tech_lead_needs_human(self, active_sessions: Sequence[Session], *, discover_markers: bool = True) -> None:
        """Recover or clear marker-owned escalation state from GitHub."""
        self._tech_lead_needs_human.reconcile(active_sessions, discover_markers=discover_markers)

    def _interrupted_retry_guard_label(self, mode: str) -> str:
        retry_cfg = self.config.retry.interrupted_sessions
        if mode == "coding":
            return retry_cfg.coding_guard_label
        return retry_cfg.review_guard_label

    def _clear_interrupted_retry_guard_label(self, *, issue_number: int, mode: str, context: str) -> None:
        """Best-effort cleanup of interrupted retry guard at launch boundary."""
        guard_label = self._interrupted_retry_guard_label(mode)
        self._apply_actions([
            RemoveLabelAction(
                issue_number=issue_number,
                label=guard_label,
                reason=f"{mode} session relaunched - clearing interrupted retry guard",
            ),
        ], context=context)

    def _clear_reset_retry_pending_label(self, *, issue_number: int, context: str) -> None:
        """Best-effort cleanup of reset+retry pending guard at launch boundary."""
        pending_label = getattr(self._lm, "reset_retry_pending", None)
        if not isinstance(pending_label, str) or not pending_label:
            resolver = getattr(self._lm, "resolve", None)
            if callable(resolver):
                resolved = resolver("reset-retry-pending")
                pending_label = resolved if isinstance(resolved, str) and resolved else "reset-retry-pending"
            else:
                pending_label = "reset-retry-pending"
        actions: list[Action] = [
            RemoveLabelAction(
                issue_number=issue_number,
                label=pending_label,
                reason="session launched - clearing reset+retry pending guard",
            ),
        ]
        self._apply_actions(actions, context=context)

    def _clear_reset_retry_scratch_pending_label(self, *, issue_number: int, context: str) -> None:
        """Best-effort cleanup of reset+retry-from-scratch pending guard."""
        pending_label = getattr(self._lm, "reset_retry_scratch_pending", None)
        if not isinstance(pending_label, str) or not pending_label:
            resolver = getattr(self._lm, "resolve", None)
            if callable(resolver):
                resolved = resolver("reset-retry-scratch-pending")
                pending_label = (
                    resolved
                    if isinstance(resolved, str) and resolved
                    else "reset-retry-scratch-pending"
                )
            else:
                pending_label = "reset-retry-scratch-pending"
        actions: list[Action] = [
            RemoveLabelAction(
                issue_number=issue_number,
                label=pending_label,
                reason="session launched - clearing reset+retry-from-scratch pending guard",
            ),
        ]
        self._apply_actions(actions, context=context)

    def _clear_launch_retry_guards(
        self, *, issue_number: int, mode: str, suffix: str
    ) -> None:
        """Clear every relaunch retry/reset guard label at a launch boundary.

        Single owner for the guard-clear policy shared by all launch paths
        (coding, validation-retry, review, retrospective-review), which each
        otherwise repeated the same three calls. ``suffix`` distinguishes the
        per-path audit context.
        """
        self._clear_interrupted_retry_guard_label(
            issue_number=issue_number,
            mode=mode,
            context=f"launch_clear_interrupted_guard_{suffix}",
        )
        self._clear_reset_retry_pending_label(
            issue_number=issue_number,
            context=f"launch_clear_reset_retry_pending_{suffix}",
        )
        self._clear_reset_retry_scratch_pending_label(
            issue_number=issue_number,
            context=f"launch_clear_reset_retry_scratch_pending_{suffix}",
        )

    def _build_session_env(
        self,
        *,
        completion_path: str,
        session_id: str,
        agent_label: str,
        issue_number: int,
        run_assets: SessionRunAssets,
        worktree_path: Path,
    ) -> str:
        """Build the common env-export string for all session types.

        Delegates to :mod:`.session_env`, which owns the agent session
        environment contract for every launch path.
        """
        return build_session_env_exports(
            config=self.config,
            completion_path=completion_path,
            session_id=session_id,
            agent_label=agent_label,
            issue_number=issue_number,
            run_dir=run_assets.run_dir,
            worktree_path=worktree_path,
            callback_endpoint=self._agent_callback_endpoint,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Phase helpers for launch_issue_session
    # These represent distinct phases a human would describe when explaining
    # the launch process. See .claude/skills/refactoring/SKILL.md
    # ─────────────────────────────────────────────────────────────────────────

    def _check_launch_preconditions(
        self,
        issue: "IssueProtocol",
        active_sessions: list[Session],
        session_name: str,
    ) -> LaunchResult | None:
        """Validate config and check for conflicts before launching.

        Returns LaunchResult on failure, None if preconditions pass.
        """
        if issue.agent_type is None:
            return LaunchResult(None, False, f"Issue #{issue.number} has no agent type label")

        if not self.config.agents.get(issue.agent_type):
            return LaunchResult(None, False, f"No agent config for {issue.agent_type}")

        if not self.config.repo:
            return LaunchResult(None, False, "No repo configured")

        if any(s.issue.number == issue.number for s in active_sessions):
            log_transition("issue", issue.number, "AVAILABLE", "SKIP", "already in active_sessions")
            return LaunchResult(None, False, "Already in active sessions")

        if self._session_exists(session_name):
            log_transition("issue", issue.number, "AVAILABLE", "SKIP", "terminal session already running")
            return LaunchResult(None, False, "Terminal session already running", keep_queued=True)

        return None

    def _verify_dependencies_fresh(self, issue: "IssueProtocol") -> _DependencyFreshness:
        """CAS check: verify dependencies haven't changed since scheduling.

        Returns a :class:`_DependencyFreshness`: ``failure`` set when the work
        gate is no longer open (abort the launch), otherwise the selected stack
        base branch from the same gate report so the caller can seed a stack
        successor's worktree from the predecessor branch.
        """
        if not self._dependency_evaluator:
            return _DependencyFreshness()

        # Prefer the freshest body for the just-before-launch recheck, but fall
        # back to the already-known issue body when refresh is unavailable/empty
        # so a transient read miss does not collapse a stack successor into an
        # ordinary issue. If no body can be obtained at all, the launch cannot
        # prove the slice is non-stack and must fail closed (retryable) rather
        # than seed the worktree from the default base (#6596 F1).
        fresh_issue = self._refresh_issue(issue.number) if self._refresh_issue else None
        body = (fresh_issue.body if fresh_issue else None) or issue.body
        milestone = fresh_issue.milestone if fresh_issue else issue.milestone
        if body is None:
            reason = f"could not read issue #{issue.number} body to confirm stack base"
            log_transition(
                "issue", issue.number, "AVAILABLE", "SKIP", reason
            )
            self.events.publish(make_trace_event(
                EventName.ISSUE_DEPENDENCY_BLOCKED,
                {
                    "issue_number": issue.number,
                    "issue_title": issue.title,
                    "reason": reason,
                    "gate": Gate.WORK.value,
                    "retryable": True,
                },
            ))
            return _DependencyFreshness(
                failure=LaunchResult(None, False, f"Dependencies not satisfied: {reason}")
            )

        # Re-gather predecessor facts at launch time so a predecessor branch or
        # review-state change between scheduling and launch cannot start stale
        # stack work (ADR-0029 just-before-launch recheck).
        report = self._dependency_evaluator.evaluate_work_gate(
            issue_number=issue.number,
            issue_body=body,
            source_milestone=milestone,
        )
        if not report.can_start_work:
            summary = report.work_summary()
            log_transition(
                "issue", issue.number, "AVAILABLE", "SKIP",
                f"dependencies changed: {summary}"
            )
            self.events.publish(make_trace_event(
                EventName.ISSUE_DEPENDENCY_BLOCKED,
                {
                    "issue_number": issue.number,
                    "issue_title": issue.title,
                    "reason": summary,
                    "gate": Gate.WORK.value,
                    "blocked_reasons": [
                        record.as_dict()
                        for record in report.gate_block_records(Gate.WORK)
                    ],
                },
            ))
            return _DependencyFreshness(
                failure=LaunchResult(None, False, f"Dependencies not satisfied: {summary}")
            )

        return _DependencyFreshness(stack_base_branch=report.stack_base_branch)

    def _stack_base_decision(
        self,
        issue_number: int,
        issue_body: str | None,
        source_milestone: str | None,
    ) -> StackBaseDecision:
        """Typed stack base decision for a launch (ADR-0029 / #6596).

        The single launch-side reader of stack base selection. Returns a
        :class:`StackBaseDecision` so callers can distinguish a non-stack issue
        (proceed on the default base) from an allowed stack successor (seed/reset
        from the predecessor branch) from a blocked stack successor (predecessor
        not ready, ambiguous base, etc. — fail closed and do NOT reset onto the
        default base).

        Absence semantics mirror the publish/work gate: when stack gating is
        wired but ``issue_body`` is unavailable, the launch cannot *prove* the
        slice is non-stack, so it returns a retryable blocked decision rather than
        collapsing an unreadable issue into "ordinary issue" and seeding from the
        default base. When the evaluator is not wired at all, stack gating is off
        and the launch proceeds normally. A present body with no ``Stack-after:``
        edge short-circuits to non-stack with no extra predecessor-fact I/O.
        """
        if not self._dependency_evaluator:
            return StackBaseDecision.not_stack()
        if issue_body is None:
            return StackBaseDecision.blocked(
                f"could not read issue #{issue_number} body to confirm stack base",
                retryable=True,
                is_stack=False,
            )
        if "stack-after" not in issue_body.lower():
            return StackBaseDecision.not_stack()
        report = self._dependency_evaluator.evaluate_work_gate(
            issue_number=issue_number,
            issue_body=issue_body,
            source_milestone=source_milestone,
            emit_event=False,
        )
        return StackBaseDecision.from_stack_report(report, Gate.WORK)

    def _stack_relaunch_blocked_result(
        self,
        *,
        issue_number: int,
        issue_title: str,
        decision: StackBaseDecision,
        context: str,
    ) -> LaunchResult:
        """Emit the dependency-blocked signal and build a blocked launch result.

        Shared by validation retry and rework so a closed stack work gate is
        recorded consistently before any claim/worktree work, instead of silently
        resetting the successor worktree onto the default base.
        """
        reason = decision.reason or "stack work gate blocked"
        log_transition("issue", issue_number, "LAUNCHING", "SKIP", f"{context}: {reason}")
        self.events.publish(make_trace_event(
            EventName.ISSUE_DEPENDENCY_BLOCKED,
            {
                "issue_number": issue_number,
                "issue_title": issue_title,
                "reason": reason,
                "gate": Gate.WORK.value,
                "retryable": decision.retryable,
            },
        ))
        return LaunchResult(None, False, f"Stack dependencies not satisfied: {reason}")

    def _acquire_issue_claim(self, issue: "IssueProtocol") -> ClaimAcquisitionResult:
        """Acquire distributed claim for an issue if claim manager is configured.

        Handles the claim attempt and convergence check. On success, returns
        claim info for passing to Session. On failure, returns error for
        early exit.
        """
        if not self._claim_manager:
            return ClaimAcquisitionResult(success=True)  # No claim needed

        logger.info(issue_log(issue.number, "Acquiring claim..."))
        claim_result = self._claim_manager.attempt_claim(issue.number)

        if not claim_result.success:
            log_transition(
                "issue", issue.number, "LAUNCHING", "CLAIM_FAILED",
                f"claim attempt failed: {claim_result.error}"
            )
            self.events.publish(make_trace_event(
                EventName.CLAIM_CONTESTED,
                {
                    "issue_number": issue.number,
                    "issue_title": issue.title,
                    "error": claim_result.error,
                },
            ))
            return ClaimAcquisitionResult(
                success=False,
                error=f"Failed to claim issue: {claim_result.error}"
            )

        # Run convergence to confirm ownership
        logger.info(issue_log(issue.number, "Running claim convergence..."))
        converged = self._claim_manager.run_convergence(issue.number, claim_result.lease_id or "")

        if not converged:
            log_transition(
                "issue", issue.number, "LAUNCHING", "CLAIM_LOST",
                "convergence failed - another claimant won"
            )
            self._claim_manager.release_claim(issue.number, claim_result.lease_id or "")
            self.events.publish(make_trace_event(
                EventName.CLAIM_LOST,
                {
                    "issue_number": issue.number,
                    "issue_title": issue.title,
                    "lease_id": claim_result.lease_id,
                    "reason": "convergence_failed",
                },
            ))
            return ClaimAcquisitionResult(
                success=False,
                error="Claim convergence failed - another orchestrator won"
            )

        # Claim acquired successfully
        lease_seconds = self.config.claims.lease_seconds
        acquired_at = datetime.now()
        logger.info(issue_log(issue.number, "Claim acquired: lease_id=%s"), claim_result.lease_id)
        self.events.publish(make_trace_event(
            EventName.CLAIM_ACQUIRED,
            {
                "issue_number": issue.number,
                "lease_id": claim_result.lease_id,
            },
        ))
        return ClaimAcquisitionResult(
            success=True,
            lease_id=claim_result.lease_id,
            lease_acquired_at=acquired_at,
            lease_expires_at=acquired_at + timedelta(seconds=lease_seconds),
        )

    def _release_claim_if_held(self, issue_number: int, claim: ClaimAcquisitionResult) -> None:
        """Release claim if one was acquired. Used for cleanup on failure."""
        if self._claim_manager and claim.lease_id:
            self._claim_manager.release_claim(issue_number, claim.lease_id)
            logger.info(issue_log(issue_number, "Released claim: lease_id=%s"), claim.lease_id)

    def _is_tech_lead_session(self, agent_type: str | None) -> bool:
        """Check if this agent type is the tech_lead review agent."""
        return is_tech_lead_session(self.config.tech_lead_review_agent, agent_type)

    def _remove_scratch_worktree_if_disposable(
        self, issue_number: int, worktree_path: Path, is_scratch: bool
    ) -> None:
        """Best-effort removal of a disposable scratch investigation worktree.

        A no-op unless ``is_scratch`` (so callers stay branch-free): used on the
        pre-active launch-failure paths that keep an ordinary coding worktree for
        reuse — a scratch worktree has no reuse path, so it must be removed
        rather than leaked (#6823)."""
        if not is_scratch:
            return
        try:
            # Scratch-only by construction (guarded above): force removal so a
            # leftover untracked artifact can't fail it and leak it (#6824 F8).
            self._worktree_manager.remove(worktree_path, force=True)
            logger.info(issue_log(issue_number, "Removed scratch investigation worktree: %s"), worktree_path)
        except Exception as e:
            logger.warning(
                issue_log(issue_number, "Failed to remove scratch investigation worktree: %s"), e
            )

    def _prepare_tech_lead_session_data(
        self,
        issue: "IssueProtocol",
        ctx: WorktreeContext,
        tech_lead_scope: "TechLeadLaunchScope | None",
    ) -> tuple[Path, ...]:
        """Delegate per-flavor tech_lead launch preparation to the ADR-0031 owner.

        Returns the evidence map's sandbox read-roots (empty for non-focus
        flavors / non-tech-lead / staging failure) for the tech-lead read grant.
        """
        return prepare_tech_lead_session_data(
            config=self.config,
            repository_host=self.repository_host,
            manifest_downloader=self._manifest_downloader,
            tech_lead_authority=self._tech_lead_authority,
            board_snapshot_provider=self._board_snapshot_provider,
            issue=issue,
            ctx=ctx,
            tech_lead_scope=tech_lead_scope,
        )

    def _discard_tech_lead_authority_after_failed_launch(
        self, issue: "IssueProtocol", ctx: WorktreeContext
    ) -> None:
        """Retention (#6769 F3): a launch that dies after recording its
        tech_lead launch authority must not leak the row — the run never starts,
        so no completion seam will ever discard it."""
        if not self._is_tech_lead_session(issue.agent_type):
            return
        self._tech_lead_authority.discard(
            run_id=ctx.run.run_id, session_name=ctx.run.session_name
        )

    def _fail_launch_for_tech_lead_prep(
        self, issue: "IssueProtocol", ctx: WorktreeContext, session_name: str,
        worktree_path: Path, claim: ClaimAcquisitionResult, error: Exception,
    ) -> LaunchResult:
        """Fail the launch when required tech_lead inputs cannot be prepared; the
        result is retry-queued (transient inputs; queue owner bounds retries) and
        prep's authority row is discarded (post-prep guard never runs here)."""
        log_transition("issue", issue.number, "LAUNCHING", "FAILED", "tech_lead session data preparation failed")
        logger.error(issue_log(issue.number, "FAILED: tech_lead session data preparation failed: %s"), error)
        self.events.publish(make_trace_event(
            EventName.SESSION_START_FAILED,
            {
                "issue_number": issue.number,
                "session_name": session_name,
                "reason": "tech_lead_session_data_failed",
                "error": str(error),
            },
        ))
        try:
            self._worktree_manager.remove(worktree_path)
            logger.info(issue_log(issue.number, "Cleaned up worktree after tech_lead data failure: %s"), worktree_path)
        except Exception as cleanup_error:
            logger.warning(
                issue_log(issue.number, "Failed to remove worktree after tech_lead data failure: %s"),
                cleanup_error,
            )
        self._discard_tech_lead_authority_after_failed_launch(issue, ctx)
        self._release_claim_if_held(issue.number, claim)
        return LaunchResult(None, False, f"Tech Lead session data preparation failed: {error}", retry_queued=True)

    def launch_issue_session(  # noqa: C901, PLR0912 - coordinator with claim acquisition, worktree setup, and error handling phases
        self,
        issue: "IssueProtocol",
        active_sessions: list[Session],
        *,
        tech_lead_scope: "TechLeadLaunchScope | None" = None,
    ) -> LaunchResult:
        """Launch a session for an issue.

        This is a coordinator function that orchestrates the multi-step launch process.
        Meaningful phases are extracted as helpers (_check_launch_preconditions,
        _verify_dependencies_fresh, _acquire_issue_claim). Remaining complexity is
        error handling for worktree/label/session failures - these belong inline
        with their operations rather than scattered across separate functions.

        Args:
            issue: The issue to work on
            active_sessions: Current active sessions (for conflict detection)
            tech_lead_scope: For tech-lead-agent sessions, the producer's typed
                grant: which tech_lead variant this launch is, plus the problem
                cohort a health review owns (ADR-0031, #6780). Unset for
                ordinary issues, and for a tech_lead anchor picked up outside the
                pending queue — the flavor then comes from the marker label and
                the cohort from the durable ledger.

        Returns:
            LaunchResult with session if successful
        """
        launch_start = time.time()
        session_name = f"issue-{issue.number}"
        logger.info(issue_log(issue.number, "Session starting: type=code title=%s"), issue.title)

        # Phase 1: Validate preconditions
        if result := self._check_launch_preconditions(issue, active_sessions, session_name):
            return result

        # Safe to access after precondition check - issue.agent_type and agent_config
        # are guaranteed non-None by _check_launch_preconditions
        assert issue.agent_type is not None  # Validated in preconditions
        agent_config = self.config.agents.get(issue.agent_type)
        assert agent_config is not None  # Validated in preconditions
        issue_key = issue.key
        session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)

        _identity_log_extra = log_context(issue_key=issue_key.stable_id(), session_id=session_name)
        logger.info(
            "[launch] Issue session identity: issue=%s issue_key=%s agent=%s task=%s session=%s",
            issue.number, issue_key, issue.agent_type, TaskKind.CODE.value, session_name,
            extra=_identity_log_extra,
        )
        logger.info(
            "[launch] Issue session key: issue=%s session=%s session_key=%s",
            issue.number, session_name, session_key.stable_id(), extra=_identity_log_extra,
        )

        # Phase 2: Verify dependencies haven't changed (CAS check)
        freshness = self._verify_dependencies_fresh(issue)
        if freshness.failure:
            return freshness.failure

        # Provider circuit breaker check
        if result := self._check_provider_circuit(agent_config.provider, issue.number):
            return result

        log_transition("issue", issue.number, "AVAILABLE", "LAUNCHING", "no conflicts")

        # Phase 3: Acquire distributed claim
        claim = self._acquire_issue_claim(issue)
        if not claim.success:
            return claim.as_launch_failure()

        # Phase 4: Prepare worktree
        step_start = time.time()
        logger.info(issue_log(issue.number, "Creating worktree..."))
        from_scratch_pending = self._lm.reset_retry_scratch_pending in issue.labels
        scratch_branch_name: str | None = None
        if from_scratch_pending:
            scratch_branch_name = f"{issue.number}-scratch-{int(time.time())}"
            logger.info(
                issue_log(
                    issue.number,
                    "Reset+retry from scratch requested; forcing fresh branch from base: %s",
                ),
                scratch_branch_name,
            )
        phase_name = "coding-1"  # Initial coding session is always attempt 1
        # A tech_lead FAILURE INVESTIGATION reads its focus issue's worktree/branch
        # as evidence and must never mutate them (#6823): it runs in a fresh,
        # disposable scratch worktree on a throwaway branch off the base, keyed
        # to this run — not the focus issue. Batch/health reviews keep the
        # existing behaviour (their own anchor worktree, preserve_branch=True so
        # a stranded branch's unpushed work is read rather than rebased away).
        investigation_scratch = failure_investigation_scratch_identity(
            self.config, issue, tech_lead_scope
        )
        is_scratch_investigation = investigation_scratch is not None
        ctx = WorktreeContext.create(
            worktree_manager=self._worktree_manager,
            config=self.config,
            events=self.events,
            session_output=self._session_output,
            issue_number=issue.number,
            issue_title=issue.title,
            session_name=session_name,
            agent_label=issue.agent_type,
            branch_name=scratch_branch_name,
            enforce_hooks=self.config.enforce_hooks,
            pre_push_hook=self.config.pre_push_hook,
            reuse_options=self._worktree_reuse_options(
                force_fresh=from_scratch_pending or is_scratch_investigation,
                # The scratch investigation has no subject branch to preserve —
                # it never reuses the focus worktree — so preserve_branch stays
                # for batch/health tech_lead that DOES reuse its anchor worktree.
                preserve_branch=(
                    self._is_tech_lead_session(issue.agent_type)
                    and not is_scratch_investigation
                ),
            ),
            phase_name=phase_name,
            stack_base_branch=freshness.stack_base_branch,
            scratch=investigation_scratch,
        )

        if ctx.error:
            log_transition("issue", issue.number, "LAUNCHING", "BLOCKED", "worktree preparation failed")
            logger.error(issue_log(issue.number, "BLOCKED: worktree preparation failed: %s"), ctx.error)
            write_worktree_diagnostic(ctx.error)
            self.escalate_issue_needs_human(
                issue_number=issue.number,
                reason="worktree preparation failed",
                comment=build_worktree_error_comment(ctx.error),
                context="worktree_prepare_issue",
                event_data={
                    "issue_number": issue.number,
                    "issue_title": issue.title,
                    "reason": str(ctx.error),
                },
            )
            self._release_claim_if_held(issue.number, claim)
            return LaunchResult(None, False, f"Worktree preparation failed: {ctx.error}")

        # Extract values from context for local use
        worktree_path = ctx.worktree_path
        branch_name = ctx.branch_name
        worktree_info = ctx.worktree_info
        run = ctx.run
        claude_project_dir = ctx.claude_project_dir
        extra_args = self._extra_provider_args_from_labels(issue.labels)

        # Write session metadata
        ctx.write_worktree_note()
        ctx.write_session_identity({
            "task": TaskKind.CODE.value,
            "issue_key": issue_key.stable_id(),
            "session_key": session_key.stable_id(),
            "agent": issue.agent_type,
            "reset_from_scratch": from_scratch_pending,
            **self._session_identity_launch_metadata(
                agent_config,
                extra_provider_args=extra_args,
            ),
        })
        if from_scratch_pending:
            ctx.update_manifest({
                "reset_from_scratch": True,
                "review_cache_boundary": "scratch_reset",
                "review_cache_boundary_started_at": run.started_at,
            })

        # Tech Lead inputs (manifest/assignment/board snapshot) are REQUIRED —
        # the prompt calls board-snapshot.json authoritative — so prep
        # failure fails the launch loudly (setup-command seam). The returned
        # evidence read-roots grant a sandboxed tech lead its god-view (#6824 R5).
        evidence_read_roots: tuple[Path, ...] = ()
        try:
            evidence_read_roots = self._prepare_tech_lead_session_data(
                issue, ctx, tech_lead_scope
            )
        except Exception as e:
            return self._fail_launch_for_tech_lead_prep(
                issue, ctx, session_name, worktree_path, claim, e
            )
        # Launch-authority guard (#6769 r4): discard on every pre-start exit; single owner.
        launch_reached_active = False
        try:

            logger.info(
                "[SESSION_RUN_START] run_id=%s session=%s issue=%s",
                run.run_id,
                session_name,
                issue.number,
                extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
            )
            logger.info(
                "[launch] Issue session paths: issue=%s worktree=%s branch=%s",
                issue.number,
                worktree_path,
                branch_name,
            )
            logger.info(
                "[launch] Claude project dir: session=%s path=%s exists=%s",
                session_name,
                claude_project_dir,
                claude_project_dir.exists(),
            )

            worktree_time = time.time() - step_start
            logger.info(
                issue_log(issue.number, "Worktree ready: path=%s branch=%s rebase_status=%s time=%.1fs"),
                worktree_path, branch_name, "CONFLICT" if worktree_info.rebase_failed else "ok", worktree_time
            )

            # Run setup commands
            if self.config.setup_worktree:
                try:
                    self._run_setup_commands(worktree_path)
                except Exception as e:
                    log_transition("issue", issue.number, "LAUNCHING", "FAILED", "setup commands failed")
                    logger.error(issue_log(issue.number, "FAILED: setup commands failed: %s"), e)
                    self.events.publish(make_trace_event(
                        EventName.SESSION_START_FAILED,
                        {
                            "issue_number": issue.number,
                            "session_name": session_name,
                            "reason": "setup_commands_failed",
                            "error": str(e),
                        },
                    ))
                    try:
                        self._worktree_manager.remove(worktree_path)
                        logger.info(issue_log(issue.number, "Cleaned up worktree after setup failure: %s"), worktree_path)
                    except Exception as cleanup_error:
                        logger.warning(
                            issue_log(issue.number, "Failed to remove worktree after setup failure: %s"),
                            cleanup_error,
                        )
                    self._release_claim_if_held(issue.number, claim)
                    return LaunchResult(None, False, f"Setup commands failed: {e}")

            # New coding attempt starts now; clear interrupted retry guard.
            self._clear_launch_retry_guards(
                issue_number=issue.number,
                mode="coding",
                suffix="coding",
            )

            # Add in-progress label
            step_start = time.time()
            in_progress_label = self._lm.in_progress
            label_ok = self._apply_actions([
                AddLabelAction(
                    issue_number=issue.number,
                    label=in_progress_label,
                    reason="session launched",
                    issue_key=issue.key.stable_id(),
                ),
            ], context="launch_in_progress_label")
            if not label_ok:
                log_transition("issue", issue.number, "LAUNCHING", "FAILED", "in-progress label failed")
                logger.error(issue_log(issue.number, "FAILED: could not add in-progress label"))
                self.events.publish(make_trace_event(
                    EventName.SESSION_START_FAILED,
                    {
                        "issue_number": issue.number,
                        "session_name": session_name,
                        "reason": "in_progress_label_failed",
                    },
                ))
                try:
                    self._worktree_manager.remove(worktree_path)
                    logger.info(issue_log(issue.number, "Cleaned up worktree after launch failure: %s"), worktree_path)
                except Exception as e:
                    logger.warning(issue_log(issue.number, "Failed to remove worktree after launch failure: %s"), e)
                self._release_claim_if_held(issue.number, claim)
                return LaunchResult(None, False, "Failed to add in-progress label")
            label_time = time.time() - step_start
            logger.info("[launch] Label added in %.1fs", label_time)

            # Check for existing work and rebase status
            existing_work = detect_existing_work(
                worktree_path,
                self._working_copy,
                seed_ref=self.config.worktree_seed_ref,
            )
            if existing_work:
                logger.info("[launch] Found existing work - agent will evaluate before starting fresh")

            # Add merge conflict warning if rebase failed
            if worktree_info.rebase_failed:
                conflict_warning = (
                    "WARNING: This branch could not be rebased onto main due to merge conflicts. "
                    "The code is out of date. You should resolve the conflicts by running: "
                    "git fetch origin main && git rebase origin/main. "
                    "If conflicts occur, resolve them and continue with: git rebase --continue. "
                    "This is critical to ensure tests pass with the latest code."
                )
                if existing_work:
                    existing_work = f"{existing_work}\n\n{conflict_warning}"
                else:
                    existing_work = conflict_warning
                logger.warning("[launch] Rebase failed - agent will need to resolve merge conflicts")

            # Build command
            rendered_prompt = agent_config.render_initial_prompt(
                issue_number=issue.number,
                issue_title=issue.title,
                worktree=worktree_path,
                existing_work=existing_work,
            )
            prompt_path = self._persist_session_prompt(run.run_dir, rendered_prompt)
            base_command = agent_config.get_command_for_prompt(
                rendered_prompt,
                issue_number=issue.number,
                issue_title=issue.title,
                worktree=worktree_path,
                task_kind=TaskKind.CODE.value,
                evidence_read_roots=evidence_read_roots,
                extra_provider_args=extra_args,
            )
            base_command = self._wrap_provider_command(base_command, agent_config, run.run_dir, extra_provider_args=extra_args)
            completion_path = get_completion_path(issue.agent_type, run_dir=run.run_dir.name)
            self._session_output.update_manifest(
                run.run_dir,
                {
                    "completion_path": completion_path,
                    "session_prompt_path": prompt_path,
                },
            )
            env_exports = self._build_session_env(
                completion_path=completion_path,
                session_id=run.session_name,
                agent_label=issue.agent_type,
                issue_number=issue.number,
                run_assets=run,
                worktree_path=worktree_path,
            )
            if self.config.e2e_pr_labels:
                labels_str = ",".join(self.config.e2e_pr_labels)
                env_exports += f" E2E_PR_LABELS='{labels_str}'"
            command = f"{env_exports} && {base_command}"
            logger.info(
                "[launch] Issue session command: issue=%s session=%s worktree=%s completion=%s command=%s",
                issue.number,
                session_name,
                worktree_path,
                completion_path,
                command,
            )

            # Create terminal session
            step_start = time.time()
            session_created = self._create_session(session_name, command, worktree_path, issue.title)
            logger.info(
                "[launch] Issue session create result: issue=%s session=%s created=%s",
                issue.number,
                session_name,
                session_created,
            )
            _session_time = time.time() - step_start

            if not session_created:
                log_transition("issue", issue.number, "LAUNCHING", "FAILED", "session creation failed")
                logger.error(issue_log(issue.number, "FAILED: session creation failed"))
                self._apply_actions([
                    RemoveLabelAction(
                        issue_number=issue.number,
                        label=self._lm.in_progress,
                        reason="session creation failed",
                        issue_key=issue.key.stable_id(),
                    ),
                ], context="launch_session_creation_failed")
                # A coding worktree is kept for reuse on retry, but a scratch
                # investigation worktree is a throwaway with no reuse path — the
                # session never became active, so remove it here to avoid leaking
                # a scratch workspace (#6823).
                self._remove_scratch_worktree_if_disposable(
                    issue.number, worktree_path, is_scratch_investigation
                )
                self._release_claim_if_held(issue.number, claim)
                return LaunchResult(None, False, "Failed to create terminal session")
            launch_reached_active = True  # terminal RUNNING = irreversible (#6769 r5)

            log_transition("issue", issue.number, "LAUNCHING", "ACTIVE", "session launched", {"agent": issue.agent_type})

            # Create session object with domain identity
            session = Session(
                key=session_key,
                issue=issue,
                agent_config=agent_config,
                terminal_id=session_name,
                worktree_path=worktree_path,
                branch_name=branch_name,
                completion_path=completion_path,
                run_assets=run,
                agent_label=issue.agent_type,
                original_prompt=rendered_prompt,
                lease_id=claim.lease_id,
                lease_acquired_at=claim.lease_acquired_at,
                lease_expires_at=claim.lease_expires_at,
                # A scratch investigation worktree is disposable: completion must
                # always remove it, regardless of the cleanup config (#6823).
                scratch_worktree=is_scratch_investigation,
            )

            total_time = time.time() - launch_start
            logger.info(
                issue_log(issue.number, "Session launched: type=code agent=%s time=%.1fs"),
                issue.agent_type, total_time
            )

            full_completion_path = (worktree_path / completion_path).resolve()
            session_started_payload: SessionStartedEventPayload = {
                "issue_number": issue.number,
                "session_id": session_name,
                "agent": issue.agent_type,
                "task": "code",
                "worktree_path": str(worktree_path),
                "branch_name": branch_name,
                "reset_from_scratch": from_scratch_pending,
                "run_id": run.run_id,
                "run_dir": str(run.run_dir),
                "completion_path": completion_path,
                "completion_path_absolute": str(full_completion_path),
                "session_prompt_path": prompt_path,
            }
            if from_scratch_pending:
                session_started_payload["review_cache_boundary_started_at"] = run.started_at
            self.events.publish(make_session_started_event(session_started_payload))

            # State machine transitions
            self._trigger_issue_session_state_transitions(issue, session_name, agent_config.timeout_minutes)

            return LaunchResult(session, True)
        finally:
            if not launch_reached_active:
                self._discard_tech_lead_authority_after_failed_launch(issue, ctx)

    def launch_validation_retry_session(
        self,
        retry: PendingValidationRetry,
        active_sessions: list[Session],
    ) -> LaunchResult:
        """Launch a coding session that continues after validation failure."""
        resolved = self._resolve_validation_retry_issue(retry)
        if resolved is None:
            return LaunchResult(
                None,
                False,
                f"No agent config available for validation retry #{retry.issue_number}",
            )
        issue, agent_config, agent_label = resolved

        session_name = f"issue-{issue.number}"
        if result := self._check_launch_preconditions(issue, active_sessions, session_name):
            return result
        if result := self._check_provider_circuit(agent_config.provider, issue.number):
            return result

        retry_count = max(1, retry.retry_count)
        issue_key = issue.key
        session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
        logger.info(
            "[launch] Validation retry identity: issue=%s issue_key=%s agent=%s "
            "task=%s session=%s retry_count=%s",
            issue.number,
            issue_key,
            agent_label,
            TaskKind.CODE.value,
            session_name,
            retry_count,
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )
        log_transition(
            "issue",
            issue.number,
            "VALIDATION_RETRY_QUEUED",
            "LAUNCHING",
            f"retry_count={retry_count}",
        )

        # Honor the stack work gate before claim/worktree work: a blocked or
        # ambiguous stack predecessor must not reset this successor's worktree
        # (and a None base must not silently fall back to the default branch).
        stack_decision = self._stack_base_decision(
            issue.number, issue.body, issue.milestone
        )
        if not stack_decision.allowed:
            return self._stack_relaunch_blocked_result(
                issue_number=issue.number,
                issue_title=issue.title,
                decision=stack_decision,
                context="validation retry stack gate",
            )

        claim = self._acquire_issue_claim(issue)
        if not claim.success:
            return claim.as_launch_failure()

        phase_name = f"coding-{retry_count + 1}"
        ctx = WorktreeContext.create(
            worktree_manager=self._worktree_manager,
            config=self.config,
            events=self.events,
            session_output=self._session_output,
            issue_number=issue.number,
            issue_title=issue.title,
            session_name=session_name,
            agent_label=agent_label,
            branch_name=retry.branch_name or None,
            enforce_hooks=self.config.enforce_hooks,
            pre_push_hook=self.config.pre_push_hook,
            reuse_options=self._worktree_reuse_options(allow_remote_branch_delete=False),
            phase_name=phase_name,
            stack_base_branch=stack_decision.base_branch,
        )
        if ctx.error:
            log_transition("issue", issue.number, "LAUNCHING", "BLOCKED", "worktree preparation failed")
            logger.error(issue_log(issue.number, "BLOCKED: worktree preparation failed: %s"), ctx.error)
            write_worktree_diagnostic(ctx.error)
            self._release_claim_if_held(issue.number, claim)
            return LaunchResult(None, False, f"Worktree preparation failed: {ctx.error}")

        worktree_path = ctx.worktree_path
        branch_name = ctx.branch_name
        run = ctx.run
        extra_args = self._extra_provider_args_from_labels(issue.labels)
        retry_prompt = self._render_validation_retry_prompt(
            retry=retry,
            issue=issue,
            agent_config=agent_config,
            retry_count=retry_count,
        )

        ctx.write_worktree_note()
        ctx.write_session_identity({
            "task": TaskKind.CODE.value,
            "issue_key": issue_key.stable_id(),
            "session_key": session_key.stable_id(),
            "agent": agent_label,
            "validation_retry": True,
            "validation_retry_count": retry_count,
            "validation_error_file": retry.validation_error_file,
            **self._session_identity_launch_metadata(
                agent_config,
                extra_provider_args=extra_args,
            ),
        })
        ctx.update_manifest({
            "validation_retry": True,
            "validation_retry_count": retry_count,
            "validation_error": retry.validation_error,
            "validation_error_file": retry.validation_error_file,
        })

        if setup_failure := self._run_validation_retry_setup(issue, worktree_path, claim):
            return setup_failure

        self._clear_launch_retry_guards(
            issue_number=issue.number,
            mode="coding",
            suffix="validation_retry",
        )

        label_ok = self._apply_actions([
            AddLabelAction(
                issue_number=issue.number,
                label=self._lm.in_progress,
                reason="validation retry launched",
                issue_key=issue.key.stable_id(),
            ),
        ], context="launch_validation_retry_in_progress_label")
        if not label_ok:
            log_transition("issue", issue.number, "LAUNCHING", "FAILED", "in-progress label failed")
            self._release_claim_if_held(issue.number, claim)
            return LaunchResult(None, False, "Failed to add in-progress label")

        prompt_path = self._persist_session_prompt(run.run_dir, retry_prompt)
        self._session_output.write_retry_prompt(run.run_dir, retry_prompt)
        base_command = agent_config.get_command_for_prompt(
            retry_prompt,
            issue_number=issue.number,
            issue_title=issue.title,
            worktree=worktree_path,
            task_kind=TaskKind.CODE.value,
            extra_provider_args=extra_args,
        )
        base_command = self._wrap_provider_command(base_command, agent_config, run.run_dir, extra_provider_args=extra_args)
        completion_path = get_completion_path(agent_label, run_dir=run.run_dir.name)
        self._session_output.update_manifest(
            run.run_dir,
            {
                "completion_path": completion_path,
                "session_prompt_path": prompt_path,
            },
        )
        env_exports = self._build_session_env(
            completion_path=completion_path,
            session_id=run.session_name,
            agent_label=agent_label,
            issue_number=issue.number,
            run_assets=run,
            worktree_path=worktree_path,
        )
        command = f"{env_exports} && {base_command}"
        logger.info(
            "[launch] Validation retry command: issue=%s session=%s worktree=%s "
            "completion=%s command=%s",
            issue.number,
            session_name,
            worktree_path,
            completion_path,
            command,
        )

        session_created = self._create_session(session_name, command, worktree_path, issue.title)
        if not session_created:
            log_transition("issue", issue.number, "LAUNCHING", "FAILED", "session creation failed")
            self._apply_actions([
                RemoveLabelAction(
                    issue_number=issue.number,
                    label=self._lm.in_progress,
                    reason="validation retry session creation failed",
                    issue_key=issue.key.stable_id(),
                ),
            ], context="launch_validation_retry_session_creation_failed")
            self._release_claim_if_held(issue.number, claim)
            return LaunchResult(None, False, "Failed to create terminal session")

        session = Session(
            key=session_key,
            issue=issue,
            agent_config=agent_config,
            terminal_id=session_name,
            worktree_path=worktree_path,
            branch_name=branch_name,
            completion_path=completion_path,
            run_assets=run,
            agent_label=agent_label,
            validation_retry_count=retry_count,
            original_prompt=retry.original_prompt,
            lease_id=claim.lease_id,
            lease_acquired_at=claim.lease_acquired_at,
            lease_expires_at=claim.lease_expires_at,
        )
        log_transition(
            "issue",
            issue.number,
            "LAUNCHING",
            "ACTIVE",
            f"validation retry launched retry_count={retry_count}",
        )

        full_completion_path = (worktree_path / completion_path).resolve()
        self.events.publish(make_session_started_event({
            "issue_number": issue.number,
            "session_id": session_name,
            "agent": agent_label,
            "task": "code",
            "worktree_path": str(worktree_path),
            "branch_name": branch_name,
            "run_id": run.run_id,
            "run_dir": str(run.run_dir),
            "completion_path": completion_path,
            "completion_path_absolute": str(full_completion_path),
            "session_prompt_path": prompt_path,
            "retry_count": retry_count,
        }))
        self._trigger_issue_session_state_transitions(issue, session_name, agent_config.timeout_minutes)
        return LaunchResult(session, True)

    def _run_validation_retry_setup(
        self,
        issue: Issue,
        worktree_path: Path,
        claim: ClaimAcquisitionResult,
    ) -> LaunchResult | None:
        """Run setup commands before retrying preserved work.

        Validation retries intentionally keep the existing worktree, so
        configured setup commands must be idempotent and non-destructive.
        """
        if not self.config.setup_worktree:
            return None
        try:
            self._run_setup_commands(worktree_path)
        except Exception as e:
            log_transition("issue", issue.number, "LAUNCHING", "FAILED", "setup commands failed")
            logger.error(issue_log(issue.number, "FAILED: setup commands failed: %s"), e)
            self._release_claim_if_held(issue.number, claim)
            return LaunchResult(None, False, f"Setup commands failed: {e}")
        return None

    def _resolve_validation_retry_issue(
        self, retry: PendingValidationRetry
    ) -> tuple[Issue, AgentConfig, str] | None:
        """Resolve a validation retry into an issue snapshot, agent config, label.

        Returns ``None`` when no agent label can be determined or no agent config
        is registered for it, so the caller has a single readiness branch instead
        of separately re-checking the agent label and the config. The agent label
        is returned as a concrete ``str`` so the caller never has to re-narrow the
        optional ``Issue.agent_type`` property.
        """
        fresh_issue = self._refresh_issue(retry.issue_number) if self._refresh_issue else None
        agent_label = retry.agent_label or (fresh_issue.agent_type if fresh_issue else None)
        if not agent_label:
            return None
        agent_config = self.config.agents.get(agent_label)
        if not agent_config:
            return None
        labels = list(fresh_issue.labels) if fresh_issue else []
        if agent_label not in labels:
            labels.append(agent_label)
        issue = Issue(
            number=retry.issue_number,
            title=(fresh_issue.title if fresh_issue else retry.issue_title),
            labels=labels,
            state=(fresh_issue.state if fresh_issue else "open"),
            repo=self.config.repo or "",
            milestone=(fresh_issue.milestone if fresh_issue else None),
            body=(fresh_issue.body if fresh_issue else None),
            milestone_number=(fresh_issue.milestone_number if fresh_issue else None),
            milestone_due_on=(fresh_issue.milestone_due_on if fresh_issue else None),
        )
        return issue, agent_config, agent_label

    def _render_validation_retry_prompt(
        self,
        *,
        retry: PendingValidationRetry,
        issue: Issue,
        agent_config: AgentConfig,
        retry_count: int,
    ) -> str:
        """Render the prompt used to send a validation failure back to a coder."""
        if retry.original_prompt and retry.original_prompt.lstrip().startswith("# Validation Retry"):
            return retry.original_prompt
        validation_cmd = retry.validation_cmd or self.config.validation.quick.cmd or ""
        original_task = retry.original_prompt or f"Work on issue #{issue.number}: {issue.title}"
        template = DEFAULT_RETRY_TEMPLATE
        template_path = agent_config.retry_prompt_template or self.config.retry.retry_prompt_template
        if template_path:
            full_template_path = self.config.repo_root / template_path
            if full_template_path.exists():
                try:
                    template = full_template_path.read_text()
                except OSError as exc:
                    logger.warning("Failed to load retry template from %s: %s", full_template_path, exc)
            else:
                logger.warning("Retry template not found at %s, using default", full_template_path)
        display_count = retry_count + 1
        display_max = self.config.retry.max_validation_retries + 1
        return template.format(
            original_task=original_task,
            validation_cmd=validation_cmd,
            error_file=retry.validation_error_file or "unknown",
            error_summary=_truncate_with_tail(retry.validation_error or "Unknown validation error"),
            retry_count=display_count,
            max_retries=display_max,
            retries_remaining=max(0, display_max - display_count),
        )

    def launch_review_session(
        self,
        review: PendingReview,
        active_sessions: list[Session],
    ) -> LaunchResult:
        """Launch a code review session for a PR."""
        # Get the reviewer for this agent (per-agent override or default)
        agent_label = self.config.get_reviewer_for_agent(review.agent_label) if review.agent_label else self.config.code_review_agent
        if not agent_label:
            return LaunchResult(None, False, "No code review agent configured")

        agent_config = self.config.agents.get(agent_label)
        if not agent_config:
            return LaunchResult(None, False, f"No agent config for {agent_label}")

        if result := self._check_provider_circuit(agent_config.provider, review.issue_number):
            return result

        # Check for conflicts
        session_name = f"review-{review.pr_number}"
        validity = review_launch_validity(
            review=review,
            config=self.config,
            repository_host=self.repository_host,
            label_manager=self._lm,
        )
        if not validity.valid:
            log_transition(
                "review",
                review.pr_number,
                "QUEUED",
                "SKIP",
                f"stale pending review: {validity.reason}",
            )
            logger.info(
                "[launch] Dropping stale pending review: pr=%s issue=%s reason=%s issue_labels=%s pr_labels=%s",
                review.pr_number,
                review.issue_number,
                validity.reason,
                ",".join(validity.issue_labels) or "(missing)",
                ",".join(validity.pr_labels) or "(none)",
            )
            self.events.publish(
                make_trace_event(
                    EventName.REVIEW_SKIPPED,
                    {
                        "pr_number": review.pr_number,
                        "issue_number": review.issue_number,
                        "reason": f"stale_pending_review:{validity.reason}",
                    },
                )
            )
            return LaunchResult(None, False, f"Stale pending review: {validity.reason}")

        if any(s.terminal_id == session_name for s in active_sessions):
            log_transition("review", review.pr_number, "QUEUED", "SKIP", "already in active_sessions")
            return LaunchResult(None, False, "Already in active sessions")

        if self._session_exists(session_name):
            log_transition("review", review.pr_number, "QUEUED", "SKIP", "terminal session already running")
            return LaunchResult(None, False, "Terminal session already running", keep_queued=True)

        if not self.config.repo:
            return LaunchResult(None, False, "No repo configured")
        issue_key = review.issue_key
        session_key = SessionKey(issue=issue_key, task=TaskKind.REVIEW)
        log_transition("review", review.pr_number, "QUEUED", "LAUNCHING", "no conflicts")
        logger.info(
            "[launch] Review session identity: issue=%s issue_key=%s pr=%s agent=%s task=%s session=%s branch=%s",
            review.issue_number,
            issue_key,
            review.pr_number,
            agent_label,
            TaskKind.REVIEW.value,
            session_name,
            review.branch_name,
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )
        logger.info(
            "[launch] Review session key: issue=%s pr=%s session=%s session_key=%s",
            review.issue_number,
            review.pr_number,
            session_name,
            session_key.stable_id(),
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )

        # Determine review attempt number from rework_count
        # First review is review-1, after first rework it's review-2, etc.
        review_machine = self._get_review_machine(review.pr_number, review.issue_number)
        rework_count = review_machine.rework_count if review_machine else 0
        review_attempt = rework_count + 1
        phase_name = f"review-{review_attempt}"
        extra_args = self._extra_provider_args_from_labels(review.issue_labels)

        # Create and prepare worktree using WorktreeContext
        ctx = WorktreeContext.create(
            worktree_manager=self._worktree_manager,
            config=self.config,
            events=self.events,
            session_output=self._session_output,
            issue_number=review.issue_number,
            issue_title=f"Review PR #{review.pr_number}",
            session_name=session_name,
            agent_label=agent_label,
            branch_name=review.branch_name,
            enforce_hooks=False,
            reuse_options=self._worktree_reuse_options(allow_remote_branch_delete=False),
            phase_name=phase_name,
        )

        # Handle worktree preparation errors
        if ctx.error:
            log_transition("review", review.pr_number, "LAUNCHING", "BLOCKED", "worktree preparation failed")
            logger.error(issue_log(review.issue_number, "BLOCKED: worktree preparation failed for review: %s"), ctx.error)
            write_worktree_diagnostic(ctx.error)
            self.escalate_issue_needs_human(
                issue_number=review.issue_number,
                reason="worktree preparation failed",
                comment=build_worktree_error_comment(ctx.error),
                context="worktree_prepare_review",
                event_data={
                    "issue_number": review.issue_number,
                    "pr_number": review.pr_number,
                    "reason": str(ctx.error),
                },
            )
            return LaunchResult(None, False, f"Worktree preparation failed: {ctx.error}")

        # Extract values from context
        worktree_path = ctx.worktree_path
        worktree_info = ctx.worktree_info
        run = ctx.run
        claude_project_dir = ctx.claude_project_dir

        # Write session metadata
        ctx.write_worktree_note()
        ctx.write_session_identity({
            "task": TaskKind.REVIEW.value,
            "issue_key": issue_key.stable_id(),
            "pr_number": review.pr_number,
            "session_key": session_key.stable_id(),
            "agent": agent_label,
            **self._session_identity_launch_metadata(
                agent_config,
                extra_provider_args=extra_args,
            ),
        })
        # New review attempt starts now; clear interrupted retry guard.
        self._clear_launch_retry_guards(
            issue_number=review.issue_number,
            mode="review",
            suffix="review",
        )

        logger.info(
            "[SESSION_RUN_START] run_id=%s session=%s issue=%s",
            run.run_id,
            session_name,
            review.issue_number,
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )
        logger.info(
            "[launch] Review session paths: issue=%s pr=%s worktree=%s branch=%s",
            review.issue_number,
            review.pr_number,
            worktree_path,
            review.branch_name,
        )
        logger.info(
            "[launch] Claude project dir: session=%s path=%s exists=%s",
            session_name,
            claude_project_dir,
            claude_project_dir.exists(),
        )

        existing_work = build_review_existing_work(
            worktree_info=worktree_info,
            pr_number=review.pr_number,
            repository_host=self.repository_host,
            keep_current_label=self._lm.review_keep_approach,
        )

        # Build command
        rendered_prompt = agent_config.render_initial_prompt(
            issue_number=review.issue_number,
            issue_title=f"Review PR #{review.pr_number}",
            worktree=worktree_path,
            pr_number=review.pr_number,
            existing_work=existing_work,
            task_kind=TaskKind.REVIEW.value,
        )
        prompt_path = self._persist_session_prompt(run.run_dir, rendered_prompt)
        base_command = agent_config.get_command(
            issue_number=review.issue_number,
            issue_title=f"Review PR #{review.pr_number}",
            worktree=worktree_path,
            pr_number=review.pr_number,
            existing_work=existing_work,
            task_kind=TaskKind.REVIEW.value,
            extra_provider_args=extra_args,
        )
        base_command = self._wrap_provider_command(
            base_command,
            agent_config,
            run.run_dir,
            extra_provider_args=extra_args,
        )
        completion_path = get_completion_path(agent_label, run_dir=run.run_dir.name)
        self._session_output.update_manifest(
            run.run_dir,
            {
                "completion_path": completion_path,
                "session_prompt_path": prompt_path,
            },
        )
        env_exports = self._build_session_env(
            completion_path=completion_path,
            session_id=run.session_name,
            agent_label=agent_label,
            issue_number=review.issue_number,
            run_assets=run,
            worktree_path=worktree_path,
        )
        command = f"{env_exports} && {base_command}"
        logger.info(
            "[launch] Review session command: issue=%s pr=%s session=%s worktree=%s completion=%s command=%s",
            review.issue_number,
            review.pr_number,
            session_name,
            worktree_path,
            completion_path,
            command,
        )

        # Create session
        session_created = self._create_session(session_name, command, worktree_path, f"Review PR #{review.pr_number}")
        logger.info(
            "[launch] Review session create result: issue=%s pr=%s session=%s created=%s",
            review.issue_number,
            review.pr_number,
            session_name,
            session_created,
        )

        # Create pseudo-issue for session tracking
        pseudo_issue = Issue(
            number=review.issue_number,
            title=f"Review PR #{review.pr_number}",
            labels=[agent_label],
        )

        # Create session with domain identity (REVIEW task type)
        session = Session(
            key=session_key,
            issue=pseudo_issue,
            agent_config=agent_config,
            terminal_id=session_name,
            worktree_path=worktree_path,
            branch_name=review.branch_name,
            completion_path=completion_path,
            run_assets=run,
            agent_label=agent_label,
            pr_number=review.pr_number,
            rework_cycle=rework_count if rework_count > 0 else None,
        )

        log_transition("review", review.pr_number, "LAUNCHING", "ACTIVE", "session launched")

        # Emit event
        full_completion_path = (worktree_path / completion_path).resolve()
        self.events.publish(make_run_scoped_event(EventName.REVIEW_STARTED, {
            "pr_number": review.pr_number,
            "issue_number": review.issue_number,
            "agent": agent_label,
            "task": "review",
            "session_name": session_name,
            "run_id": run.run_id,
            "run_dir": str(run.run_dir),
            "completion_path": completion_path,
            "completion_path_absolute": str(full_completion_path),
            "session_prompt_path": prompt_path,
        }))

        # State machine transition
        self._trigger_review_state_transition(review.pr_number, review.issue_number)

        return LaunchResult(session, True)

    def launch_retrospective_review_session(
        self,
        review: PendingRetrospectiveReview,
        active_sessions: list[Session],
    ) -> LaunchResult:
        """Launch a reviewer session to audit an existing implementation."""
        agent_label = (
            self.config.get_reviewer_for_agent(review.agent_label)
            if review.agent_label
            else self.config.code_review_agent
        )
        if not agent_label:
            return LaunchResult(None, False, "No code review agent configured")

        agent_config = self.config.agents.get(agent_label)
        if not agent_config:
            return LaunchResult(None, False, f"No agent config for {agent_label}")

        if result := self._check_provider_circuit(agent_config.provider, review.issue_number):
            return result

        session_name = SessionRef.for_retrospective_review(review.issue_number).name
        if any(s.terminal_id == session_name for s in active_sessions):
            log_transition(
                "retrospective-review",
                review.issue_number,
                "QUEUED",
                "SKIP",
                "already in active_sessions",
            )
            return LaunchResult(None, False, "Already in active sessions")

        if self._session_exists(session_name):
            log_transition(
                "retrospective-review",
                review.issue_number,
                "QUEUED",
                "SKIP",
                "terminal session already running",
            )
            return LaunchResult(None, False, "Terminal session already running", keep_queued=True)

        if not self.config.repo:
            return LaunchResult(None, False, "No repo configured")

        # Resolve the prior orchestrator PR now that we know the launch will
        # proceed — lazily, for this one issue, so discovery (startup recovery
        # and the per-tick scan) stays free of per-issue PR searches.
        resolve_prior_pr_for_launch(review, self.repository_host)

        issue_key = review.issue_key
        session_key = SessionKey(issue=issue_key, task=TaskKind.RETROSPECTIVE_REVIEW)
        log_transition(
            "retrospective-review",
            review.issue_number,
            "QUEUED",
            "LAUNCHING",
            "no conflicts",
        )
        logger.info(
            "[launch] Retrospective review identity: issue=%s issue_key=%s prior_pr=%s "
            "agent=%s source_agent=%s session=%s",
            review.issue_number,
            issue_key,
            review.prior_pr_number,
            agent_label,
            review.agent_label,
            session_name,
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )

        ctx = WorktreeContext.create(
            worktree_manager=self._worktree_manager,
            config=self.config,
            events=self.events,
            session_output=self._session_output,
            issue_number=review.issue_number,
            issue_title=f"Review Existing Implementation #{review.issue_number}",
            session_name=session_name,
            agent_label=agent_label,
            branch_name=None,
            enforce_hooks=False,
            reuse_options=self._worktree_reuse_options(allow_remote_branch_delete=False),
            phase_name="retrospective-review-1",
        )

        if ctx.error:
            log_transition(
                "retrospective-review",
                review.issue_number,
                "LAUNCHING",
                "BLOCKED",
                "worktree preparation failed",
            )
            logger.error(
                issue_log(
                    review.issue_number,
                    "BLOCKED: worktree preparation failed for retrospective review: %s",
                ),
                ctx.error,
            )
            write_worktree_diagnostic(ctx.error)
            self.escalate_issue_needs_human(
                issue_number=review.issue_number,
                reason="retrospective review worktree preparation failed",
                comment=build_worktree_error_comment(ctx.error),
                context="worktree_prepare_retrospective_review",
                event_data={
                    "issue_number": review.issue_number,
                    "reason": str(ctx.error),
                    "task": TaskKind.RETROSPECTIVE_REVIEW.value,
                },
            )
            return LaunchResult(None, False, f"Worktree preparation failed: {ctx.error}")

        worktree_path = ctx.worktree_path
        worktree_info = ctx.worktree_info
        run = ctx.run
        extra_args = self._extra_provider_args_from_labels(review.issue_labels)

        ctx.write_worktree_note()
        ctx.write_session_identity({
            "task": TaskKind.RETROSPECTIVE_REVIEW.value,
            "issue_key": issue_key.stable_id(),
            "session_key": session_key.stable_id(),
            "agent": agent_label,
            "source_agent": review.agent_label,
            "trigger_label": review.trigger_label,
            "prior_pr_number": review.prior_pr_number,
            "prior_pr_url": review.prior_pr_url,
            **self._session_identity_launch_metadata(
                agent_config,
                extra_provider_args=extra_args,
            ),
        })

        self._clear_launch_retry_guards(
            issue_number=review.issue_number,
            mode="review",
            suffix="retrospective_review",
        )

        existing_work = build_retrospective_review_existing_work(review)
        if worktree_info.rebase_failed:
            existing_work = (
                f"{existing_work}\n\nWARNING: This review worktree could not be "
                "rebased onto main due to merge conflicts. Include that risk in "
                "your verdict."
            )
        prompt_pr_number = review.prior_pr_number or review.issue_number
        issue_title = (
            f"Review Existing Implementation #{review.issue_number}: "
            f"{review.issue_title}"
        )
        rendered_prompt = agent_config.render_initial_prompt(
            issue_number=review.issue_number,
            issue_title=issue_title,
            worktree=worktree_path,
            pr_number=prompt_pr_number,
            existing_work=existing_work,
            task_kind=TaskKind.RETROSPECTIVE_REVIEW.value,
        )
        prompt_path = self._persist_session_prompt(run.run_dir, rendered_prompt)
        base_command = agent_config.get_command_for_prompt(
            rendered_prompt,
            issue_number=review.issue_number,
            issue_title=issue_title,
            worktree=worktree_path,
            pr_number=prompt_pr_number,
            task_kind=TaskKind.RETROSPECTIVE_REVIEW.value,
            extra_provider_args=extra_args,
        )
        base_command = self._wrap_provider_command(
            base_command,
            agent_config,
            run.run_dir,
            extra_provider_args=extra_args,
        )
        completion_path = get_completion_path(agent_label, run_dir=run.run_dir.name)
        self._session_output.update_manifest(
            run.run_dir,
            {
                "completion_path": completion_path,
                "session_prompt_path": prompt_path,
            },
        )
        env_exports = self._build_session_env(
            completion_path=completion_path,
            session_id=run.session_name,
            agent_label=agent_label,
            issue_number=review.issue_number,
            run_assets=run,
            worktree_path=worktree_path,
        )
        command = f"{env_exports} && {base_command}"
        logger.info(
            "[launch] Retrospective review command: issue=%s session=%s worktree=%s "
            "completion=%s command=%s",
            review.issue_number,
            session_name,
            worktree_path,
            completion_path,
            command,
        )

        session_created = self._create_session(session_name, command, worktree_path, issue_title)
        logger.info(
            "[launch] Retrospective review create result: issue=%s session=%s created=%s",
            review.issue_number,
            session_name,
            session_created,
        )
        if not session_created:
            log_transition(
                "retrospective-review",
                review.issue_number,
                "LAUNCHING",
                "FAILED",
                "session creation failed",
            )
            return LaunchResult(None, False, "Failed to create terminal session")

        pseudo_issue = Issue(
            number=review.issue_number,
            title=issue_title,
            labels=list(dict.fromkeys([*review.issue_labels, review.agent_label, agent_label, review.trigger_label])),
        )
        session = Session(
            key=session_key,
            issue=pseudo_issue,
            agent_config=agent_config,
            terminal_id=session_name,
            worktree_path=worktree_path,
            branch_name=ctx.branch_name,
            completion_path=completion_path,
            run_assets=run,
            agent_label=agent_label,
            pr_number=review.prior_pr_number,
            original_prompt=rendered_prompt,
        )

        log_transition(
            "retrospective-review",
            review.issue_number,
            "LAUNCHING",
            "ACTIVE",
            "session launched",
        )
        full_completion_path = (worktree_path / completion_path).resolve()
        self.events.publish(make_run_scoped_event(EventName.REVIEW_STARTED, {
            "issue_number": review.issue_number,
            "prior_pr_number": review.prior_pr_number,
            "prior_pr_url": review.prior_pr_url,
            "agent": agent_label,
            "source_agent": review.agent_label,
            "task": TaskKind.RETROSPECTIVE_REVIEW.value,
            "session_name": session_name,
            "run_id": run.run_id,
            "run_dir": str(run.run_dir),
            "completion_path": completion_path,
            "completion_path_absolute": str(full_completion_path),
            "session_prompt_path": prompt_path,
            "trigger_label": review.trigger_label,
        }))

        return LaunchResult(session, True)

    def launch_rework_session(
        self,
        rework: PendingRework,
        active_sessions: list[Session],
    ) -> LaunchResult:
        """Launch a rework session to fix issues found in review."""
        deps = ReworkLaunchDependencies(
            config=self.config,
            events=self.events,
            repository_host=self.repository_host,
            worktree_manager=self._worktree_manager,
            session_output=self._session_output,
            label_manager=self._lm,
            session_exists=self._session_exists,
            create_session=self._create_session,
            apply_actions=self._apply_actions,
            worktree_reuse_options=self._worktree_reuse_options,
            session_identity_launch_metadata=self._session_identity_launch_metadata,
            clear_interrupted_retry_guard_label=self._clear_interrupted_retry_guard_label,
            clear_reset_retry_pending_label=self._clear_reset_retry_pending_label,
            clear_reset_retry_scratch_pending_label=self._clear_reset_retry_scratch_pending_label,
            persist_session_prompt=self._persist_session_prompt,
            wrap_provider_command=self._wrap_provider_command,
            build_session_env=self._build_session_env,
            check_provider_circuit=self._check_provider_circuit,
            resolve_stack_decision=self._stack_decision_for_issue_number,
        )
        return launch_rework_flow(rework, active_sessions, deps)

    def _stack_decision_for_issue_number(self, issue_number: int) -> StackBaseDecision:
        """Resolve a rework issue's stack base decision from its freshest body.

        Rework reuses the existing successor branch, so its worktree must be
        reset onto the predecessor branch just like the initial launch — else the
        reuse preflight would rebase the successor onto the default branch and the
        publish ancestry gate would block it. Resolves the issue body via refresh
        and delegates to :meth:`_stack_base_decision`, which fails the rework
        closed (retryable) when stack gating is wired but the body cannot be read
        — never collapsing an unreadable issue into "ordinary issue" (#6596 F1).
        """
        fresh_issue = self._refresh_issue(issue_number) if self._refresh_issue else None
        body = fresh_issue.body if fresh_issue else None
        milestone = fresh_issue.milestone if fresh_issue else None
        return self._stack_base_decision(issue_number, body, milestone)

    def _run_setup_commands(self, worktree_path: Path) -> None:
        """Run setup commands in worktree."""
        step_start = time.time()
        for cmd in self.config.setup_worktree:
            logger.debug("Running setup command: %s", cmd)
            logger.info("[launch] Running setup: %s", cmd)
            result = self._command_runner.run(
                cmd,
                shell=True,
                cwd=worktree_path,
                env=build_runtime_tool_env(worktree_path),
            )
            if result.timed_out:
                logger.error("[launch] Setup command timed out: %s", cmd)
                raise RuntimeError(f"setup command timed out: {cmd}")
            if result.returncode != 0:
                stderr = result.stderr.strip() or "no stderr captured"
                logger.error("Setup command failed: %s\n%s", cmd, stderr)
                raise RuntimeError(
                    f"setup command failed: {cmd} (exit_code={result.returncode}): {stderr}"
                )
        setup_time = time.time() - step_start
        logger.info("[launch] Setup completed in %.1fs", setup_time)

    def _persist_session_prompt(self, run_dir: Path, prompt_text: str) -> str:
        """Persist rendered launch prompt into run-scoped artifacts."""
        prompt_path = self._session_output.write_session_prompt(run_dir, prompt_text)
        return str(prompt_path)

    def _send_initial_prompt(self, session_name: str, prompt_path: Path, agent_config: "AgentConfig") -> None:
        """Send the initial prompt to an interactive session via PTY stdin.

        Instead of typing the full prompt text (which garbles in the TUI),
        we send a short file-reference instruction. The agent reads the file
        to get the full prompt content.
        """
        if not self._send_to_session:
            logger.warning("[launch] No send_to_session_fn configured; cannot deliver prompt to %s", session_name)
            return
        # Give the TUI time to initialize before sending the prompt.
        time.sleep(3)
        msg = f"Read and follow your instructions in {prompt_path}"
        sent = self._send_to_session(session_name, msg)
        logger.info("[launch] Sent initial prompt to interactive session %s: success=%s", session_name, sent)

    def _wrap_provider_command(
        self,
        base_command: str,
        agent_config: "AgentConfig",
        run_dir: Path,
        *,
        extra_provider_args: Mapping[str, object] | None = None,
    ) -> str:
        """Wrap provider command with retry/circuit reporting.

        Interactive providers are returned as-is — they manage their own
        lifecycle and don't use the provider_runner subprocess wrapper.
        """
        return self._get_provider_command_wrapper().wrap(
            base_command,
            agent_config,
            run_dir,
            extra_provider_args=extra_provider_args,
        )

    def _get_provider_command_wrapper(self) -> ProviderCommandWrapper:
        if self._provider_command_wrapper is None:
            self._provider_command_wrapper = ProviderCommandWrapper(
                self.config.provider_resilience.short_retry
            )
        return self._provider_command_wrapper

    def _check_provider_circuit(self, provider: str | None, issue_number: int) -> Optional["LaunchResult"]:
        if not provider or not self._provider_policy:
            return None
        if not self._provider_policy.is_open(provider):
            return None
        blocked_label = self._provider_policy.blocked_label()
        self._apply_actions([
            AddLabelAction(
                issue_number=issue_number,
                label=blocked_label,
                reason=f"provider unavailable: {provider}",
            ),
        ], context="provider_unavailable")
        return LaunchResult(None, False, f"Provider unavailable: {provider}")

    def _trigger_issue_session_state_transitions(
        self,
        issue: "IssueProtocol",
        session_name: str,
        timeout_minutes: int,
    ) -> None:
        """Trigger state machine transitions for issue session launch."""
        from ..domain.state_machines.issue_machine import IssueState
        from ..domain.state_machines.session_machine import SessionState

        logger.debug(f"[STATE_MACHINE] Triggering transitions for issue #{issue.number}")
        issue_machine = self._get_issue_machine(issue)
        if issue_machine.state == IssueState.AVAILABLE.value:
            logger.debug(f"[STATE_MACHINE] Issue #{issue.number}: AVAILABLE -> CLAIMED")
            issue_machine.claim()
            logger.debug(f"[STATE_MACHINE] Issue #{issue.number}: CLAIMED -> IN_PROGRESS")
            issue_machine.start()

        session_machine = self._get_session_machine(session_name, issue.number, timeout_minutes)
        if session_machine.state != SessionState.PENDING.value:
            logger.warning(
                "[STATE_MACHINE] Session %s unexpected state %s during launch; resetting",
                session_name,
                session_machine.state,
            )
            if self._remove_session_machine is not None:
                self._remove_session_machine(session_name)
                session_machine = self._get_session_machine(session_name, issue.number, timeout_minutes)
            else:
                return

        logger.debug(f"[STATE_MACHINE] Session {session_name}: PENDING -> STARTING")
        session_machine.launch()
        logger.debug(f"[STATE_MACHINE] Session {session_name}: STARTING -> RUNNING")
        session_machine.started()

    def _trigger_review_state_transition(self, pr_number: int, issue_number: int) -> None:
        """Trigger state machine transition for review session."""
        from ..domain.state_machines.review_machine import ReviewState

        review_machine = self._get_review_machine(pr_number, issue_number)
        if review_machine.state == ReviewState.PENDING.value:
            logger.debug(f"[STATE_MACHINE] PR #{pr_number}: PENDING -> IN_REVIEW")
            review_machine.start_review()
