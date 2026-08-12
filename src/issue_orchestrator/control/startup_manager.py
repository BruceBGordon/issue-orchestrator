"""StartupManager - handles orchestrator startup sequence.

This module extracts the startup logic from orchestrator.py to keep
the orchestrator as a thin mediator.

Hook verification is handled by the launcher (pre-flight doctor checks)
before the orchestrator process starts. The startup sequence here covers
runtime initialization only:

1. Enforce SQLite pragmas and backups
2. Clean up stale claims (in-progress labels without sessions)
3. Clean up idle terminal sessions
4. Discover and restore running sessions
5. Restore + sync queue cache (warm: 1 delta call, cold: full scan)
6. Check in-progress issues (filters from cache when available)
7. Recover pending code reviews
8. Recover pending tech_lead reviews
9. Recover pending validation retries
10. Recover orphaned cleanups
11. Resume issues with partial work
12. Audit queue (uses cached issues when available)
"""

import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Generator, Optional, Sequence

from ..infra.analysis import analyze_issue, IssueState
from ..infra.config import Config
from ..ports.issue import Issue

if TYPE_CHECKING:
    from ..ports.label_store import LabelStore
    from ..ports.queue_cache_store import QueueCacheStore
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .label_manager import LabelManager
    from .label_store_reconciler import FreshLabelSnapshot
    from .worktree_reconciliation import StartupWorktreeReconciler
from ..domain.models import (
    OrchestratorState,
    PendingRetrospectiveReview,
    PendingReview,
    PendingValidationRetry,
    SessionHistoryEntry,
    Session,
    ORCHESTRATOR_PR_MARKER,
)
from ..domain.pr_attempt_scope import scope_prs_to_active_issue_branch
from .actions import AddLabelAction, RemoveLabelAction
from .health_review_trigger import (
    hydrate_last_health_review_at,
    recover_pending_tech_lead_anchors,
)
from .stuck_sweep import hydrate_stuck_sweep_state
from .action_applier import ActionApplier
from .issue_fetch_resilience import IssueFetchResilience, TransientIssueFetchError
from .queue_cache import QueueCache, QueueMutationStatus, record_issue_refreshes
from .review_validity import evaluate_review_validity
from .review_scope import ReviewScopeChecker, extract_issue_number_from_pr
from .retrospective_review import discover_retrospective_review_issues
from .worker_budget import worker_slot_free
from ..events import EventName
from ..ports import EventSink, SessionRunner, make_trace_event, RepositoryHost
from ..ports.session_runner import DiscoveredSession
from ..infra import gh_audit
from ..infra.validation_state import find_pending_retry_artifacts
from ..infra.repo_identity import get_repo_head_sha
from ..infra.sqlite_maintenance import enforce_pragmas_on_startup, run_backups_if_due
from .worktree_manager import get_worktree_path



logger = logging.getLogger(__name__)


class StartupManager:
    """Handles the orchestrator startup sequence.

    This class extracts startup logic from orchestrator to keep
    the orchestrator focused on runtime coordination.
    """

    def __init__(
        self,
        config: Config,
        events: EventSink,
        runner: SessionRunner,
        repository_host: RepositoryHost,
        action_applier: ActionApplier,
        issue_branches_fn: Callable[[], dict[int, str]],
        session_exists_fn: Callable[[str], bool],
        restore_sessions_fn: Callable[[list[DiscoveredSession]], None],
        launch_session_fn: Callable[[Issue], Optional[Session]],
        update_queue_cache_fn: Callable[[], None],
        issue_fetch_resilience: IssueFetchResilience,
        startup_worktree_reconciler: "StartupWorktreeReconciler",
        queue_cache_store: "QueueCacheStore | None" = None,
        label_manager: "LabelManager | None" = None,
        label_store: "LabelStore | None" = None,
        tech_lead_authority: "TechLeadAuthorityStore | None" = None,
    ):
        """Initialize the startup manager.

        Args:
            config: Application configuration
            events: Event sink for trace events
            runner: Session runner for terminal operations
            repository_host: Repository host for GitHub operations
            action_applier: Action applier for label operations
            session_exists_fn: Callback to check if a session exists
            restore_sessions_fn: Callback to restore running sessions
            launch_session_fn: Callback to launch a new session
            update_queue_cache_fn: Callback to update the queue cache
            issue_fetch_resilience: Shared owner of the issue-list fetch
                resilience policy. Guards only the startup queue-sync fetch so a
                transient repository blip degrades-and-continues while a
                persistent repo-not-found/auth failure fails fast; downstream
                recovery phases keep their own error handling.
            startup_worktree_reconciler: Owner of crash-safe worktree recovery.
            queue_cache_store: Persistent store for queue cache (enables warm restarts)
            label_manager: Label registry for prefix-aware queries.
        """
        self.config = config
        self.events = events
        self.runner = runner
        self.repository_host = repository_host
        self._action_applier = action_applier
        self._issue_branches = issue_branches_fn
        self._session_exists = session_exists_fn
        self._restore_sessions = restore_sessions_fn
        self._launch_session = launch_session_fn
        self._update_queue_cache = update_queue_cache_fn
        self._issue_fetch_resilience = issue_fetch_resilience
        self._startup_worktree_reconciler = startup_worktree_reconciler
        self._queue_cache_store = queue_cache_store
        if label_manager is None:
            from .label_manager import LabelManager
            label_manager = LabelManager(config)
        self._lm = label_manager
        self._label_store = label_store
        # Gated-proposal ledger (#6778); None (tests) = no op-backed exclusions.
        self._tech_lead_authority = tech_lead_authority
        self._review_scope = ReviewScopeChecker(
            config,
            repository_host,
            log_prefix="startup",
            require_open_issue=True,
        )
        # Issue numbers whose orchestrator-owned labels were mutated during the
        # current startup recovery (e.g. pr-pending added / in-progress removed
        # for a crash-recovered open PR). ActionApplier writes those mutations
        # through to label_store, so the pre-mutation warm-cache snapshot is now
        # stale. label_store reconciliation must read GitHub fresh for these
        # issues instead of trusting that snapshot, or it would rewrite the
        # mirror back to the pre-recovery labels. Reset at the start of every
        # run_startup.
        self._startup_label_mutated_issues: set[int] = set()
        # Whether this run's queue cache labels came from a successful
        # GitHub-backed sync/full scan (True) rather than a degraded fallback
        # onto the persisted last-known-good snapshot (False). Only fresh labels
        # may be handed to label_store reconciliation as GitHub truth; a
        # degraded startup must force per-issue fresh reads instead. Reset at
        # the start of every run_startup.
        self._queue_labels_fresh: bool = False

    def _build_labels(self, *labels: str) -> list[str]:
        """Build labels list, including filtering.label if configured."""
        result = list(labels)
        if self.config.filtering.label:
            result.append(self.config.filtering.label)
        return result

    def _apply_label_actions(self, actions: list[AddLabelAction | RemoveLabelAction]) -> None:
        """Apply label actions through the ActionApplier."""
        for action in actions:
            # Record the touched issue before applying. The mutation writes
            # through to label_store, so the issue's pre-mutation warm-cache
            # entry can no longer be trusted as GitHub truth during
            # label_store reconciliation; we record unconditionally so a
            # partially-applied action also forces a fresh read rather than a
            # guess. See _reconcile_label_store.
            self._startup_label_mutated_issues.add(action.issue_number)
            result = self._action_applier.apply(action)
            if not result.success:
                logger.warning("[startup] Label action failed: %s", result.error)

    @contextmanager
    def _phase(self, name: str, timings: dict[str, float]) -> Generator[None, None, None]:
        """Time a startup phase and record into ``timings`` keyed by ``name``.

        Logs the elapsed time at INFO so cold-start cost is visible in the
        orchestrator logs even when the dashboard is still "Initializing...".
        """
        phase_start = time.time()
        try:
            yield
        finally:
            elapsed = time.time() - phase_start
            timings[name] = elapsed
            logger.info("[STARTUP_TIMING] phase=%s elapsed=%.3fs", name, elapsed)

    async def run_startup(self, state: OrchestratorState) -> None:
        """Execute the full startup sequence.

        Args:
            state: The orchestrator state to update
        """
        startup_start = time.time()
        state.startup_status = "running"
        self._startup_label_mutated_issues = set()
        self._queue_labels_fresh = False
        timings: dict[str, float] = {}

        # Emit merged configuration for debugging
        self.events.publish(make_trace_event(EventName.CONFIG_MERGED, self.config.to_event_dict()))

        # Log git commit SHA for version tracking
        commit_sha = get_repo_head_sha(self.config.repo_root)
        if commit_sha:
            logger.info("Orchestrator starting: commit=%s (%s)", commit_sha[:7], commit_sha)
        else:
            logger.warning("Orchestrator starting: commit=unknown (could not read git HEAD)")

        # Step 1: Enforce SQLite pragmas and backups
        state.startup_message = "Checking SQLite state..."
        with self._phase("sqlite_pragmas", timings):
            enforce_pragmas_on_startup(self.config)
        if self.config.sqlite_backup.enforce_on_startup:
            with self._phase("sqlite_backups", timings):
                run_backups_if_due(self.config)

        # Step 2: Clean up stale claims
        state.startup_message = "Cleaning up stale claims..."
        logger.info("Starting up - checking for stale in-progress issues...")

        # Step 3: Clean up idle terminal sessions
        state.startup_message = "Cleaning up idle terminal sessions..."
        with self._phase("cleanup_idle_sessions", timings):
            closed_tabs = self.runner.cleanup_idle_sessions()
        if closed_tabs:
            logger.info("Closed %d idle terminal sessions", closed_tabs)
            print(f"  Closed {closed_tabs} idle terminal sessions")

        # Step 4: Discover and restore running sessions
        state.startup_message = "Discovering running sessions..."
        with self._phase("discover_running_sessions", timings):
            running = self.runner.discover_running_sessions()
            if running:
                logger.info("Found %d running sessions to restore tracking", len(running))
                print(f"  Found {len(running)} running sessions to restore tracking")
            self._restore_sessions(running)  # unconditional: sweeps the ledger too

        # Step 5: Restore + sync queue cache (moved early so Steps 6/8 use cache)
        with self._phase("restore_and_sync_queue", timings):
            self._restore_and_sync_queue(state)

        # Step 6: Check in-progress issues and determine action
        state.startup_message = "Scanning local branches..."
        with self._phase("issue_branches_scan", timings):
            issue_branches = self._issue_branches()

        issues_to_resume: list[tuple[Issue, str]] = []
        with self._phase("check_in_progress_issues", timings):
            await self._check_in_progress_issues(state, issue_branches, issues_to_resume)

        # Step 7: Recover pending code reviews
        if self.config.code_review_agent and self.config.code_review_label:
            with self._phase("recover_pending_reviews", timings):
                await self._recover_pending_reviews(state, issue_branches)

        if self.config.retrospective_review_enabled:
            with self._phase("recover_pending_retrospective_reviews", timings):
                self._recover_pending_retrospective_reviews(state)

        # Step 8: Recover awaiting-merge dashboard history
        with self._phase("recover_pr_pending_history", timings):
            self._recover_pr_pending_history(state, issue_branches)

        # Step 9: Recover pending tech_lead reviews
        if self.config.tech_lead_enabled:
            with self._phase("recover_pending_tech_lead", timings):
                await self._recover_pending_tech_lead(state)

        # Step 10: Recover pending validation retries (crash recovery)
        with self._phase("recover_pending_validation_retries", timings):
            self._recover_pending_validation_retries(state, issue_branches)

        # Step 11: Recover orphaned cleanups
        with self._phase("recover_orphaned_cleanups", timings):
            self._recover_orphaned_cleanups(state)

        # Step 12: Resume issues with partial work
        with self._phase("resume_partial_work", timings):
            await self._resume_partial_work(state, issues_to_resume)

        # Step 12b: Reconcile the local label_store against GitHub so the
        # mirror cannot silently diverge from the source of truth.
        with self._phase("reconcile_label_store", timings):
            self._reconcile_label_store(state)

        # Step 13: Audit and cache the queue
        state.startup_message = "Auditing queue..."
        with self._phase("audit_queue", timings):
            from ..infra.audit import audit_queue, print_audit
            audit_entries = audit_queue(
                self.config, state, self.repository_host,
                issue_branches=issue_branches,
                preloaded_issues=list(state.cached_queue_issues) if state.cached_queue_issues else None,
            )
            print_audit(audit_entries)

        # Mark startup complete
        state.startup_status = "complete"
        state.startup_message = ""
        elapsed = time.time() - startup_start
        logger.info("Startup complete in %.1fs", elapsed)
        # Sorted summary lets cold-start hotspots jump out at a glance.
        ranked = sorted(timings.items(), key=lambda kv: kv[1], reverse=True)
        summary = ", ".join(f"{name}={dt:.2f}s" for name, dt in ranked)
        logger.info("[STARTUP_TIMING] summary total=%.2fs %s", elapsed, summary)

        self.events.publish(make_trace_event(EventName.ORCHESTRATOR_READY, {
            "filtering": {
                "label": self.config.filtering.label,
                "milestone": self.config.filtering.milestone,
                "milestones": self.config.filtering.get_milestones(),
            },
            "agents": list(self.config.agents.keys()),
            "max_concurrent": self.config.max_concurrent_sessions,
            "startup_seconds": round(elapsed, 1),
        }))

    async def _check_in_progress_issues(
        self,
        state: OrchestratorState,
        issue_branches: dict[int, str],
        issues_to_resume: list[tuple[Issue, str]],
    ) -> None:
        """Check all in-progress issues and determine action.

        When the queue cache is populated (warm or cold-after-full-scan),
        filters in-progress issues from cache — zero GitHub calls.
        Falls back to per-agent GitHub fetch only when no cache exists.
        """
        if state.cached_queue_issues:
            # Warm path: filter in-progress from cache (0 GitHub calls)
            stale_in_progress = self._recover_stale_in_progress_from_label_store(state.cached_queue_issues)
            queue_cache = QueueCache(self.config, state, self._queue_cache_store)
            for issue in stale_in_progress:
                outcome = queue_cache.upsert_refreshed_issue(issue)
                if outcome.status != QueueMutationStatus.ACCEPTED:
                    logger.warning(
                        "[startup] Recovered locally in-progress issue is out of dashboard queue scope: issue=%d status=%s",
                        issue.number,
                        outcome.status.value,
                    )
            if stale_in_progress and self._queue_cache_store is not None:
                queue_cache.save_snapshot()
            issues_by_number = {
                issue.number: issue
                for issue in state.cached_queue_issues
                if self._lm.is_in_progress(issue.labels)
            }
            for issue in stale_in_progress:
                issues_by_number.setdefault(issue.number, issue)
            issues = list(issues_by_number.values())
            logger.info("[startup] Found %d in-progress issues from cache", len(issues))
            for issue in issues:
                self._analyze_and_handle_issue(state, issue, issue_branches, issues_to_resume, agent_label="")
        else:
            # Cold fallback: per-agent fetch (only when cache is empty)
            for agent_label in self.config.agents.keys():
                issues = self._fetch_in_progress_issues_for_agent(state, agent_label)
                for issue in issues:
                    self._analyze_and_handle_issue(state, issue, issue_branches, issues_to_resume, agent_label)

    def _recover_stale_in_progress_from_label_store(self, cached_issues: list[Issue]) -> list[Issue]:
        """Recover locally in-progress issues omitted from the warm cache snapshot."""
        if self._label_store is None:
            return []
        cached_issue_numbers = {issue.number for issue in cached_issues}
        local_in_progress = sorted(
            issue_number
            for issue_number, labels in self._label_store.load_all().items()
            if self._lm.is_in_progress(sorted(labels))
        )
        if not local_in_progress:
            return []
        missing = [issue_number for issue_number in local_in_progress if issue_number not in cached_issue_numbers]
        if not missing:
            logger.info(
                "[startup] Local label store and cached queue agree on %d in-progress issue(s)",
                len(local_in_progress),
            )
            return []
        logger.warning(
            "[startup] Cached queue omitted %d locally in-progress issue(s): %s",
            len(missing),
            missing,
        )
        recovered: list[Issue] = []
        for issue_number in missing:
            issue = self.repository_host.get_issue(issue_number)
            if issue is None:
                logger.warning(
                    "[startup] Failed to refetch locally in-progress issue missing from cache: issue=%d",
                    issue_number,
                )
                continue
            # Force startup analysis for locally persisted in-progress issues even
            # when the freshly fetched GitHub labels disagree. The recovery bug
            # here is dropping the issue entirely; downstream analysis owns label
            # reconciliation once the issue is back in the control flow.
            recovered.append(issue)
        return recovered

    def _fetch_in_progress_issues_for_agent(self, state: OrchestratorState, agent_label: str) -> list[Issue]:
        """Fetch in-progress issues for a specific agent."""
        state.startup_message = f"Checking in-progress issues for {agent_label}..."
        api_start = time.time()

        milestones = self.config.get_filter_milestones() or [None]
        issues = []
        for milestone in milestones:
            with gh_audit.context(reason=gh_audit.AuditReason.STARTUP_REFRESH, scope=gh_audit.AuditScope.STARTUP):
                issues.extend(self.repository_host.list_issues(
                    labels=self._build_labels(agent_label, self._lm.in_progress),
                    milestone=milestone, limit=self.config.filtering.fetch_limit,
                ))

        elapsed = time.time() - api_start
        logger.debug("Fetched %d in-progress issues for %s in %.1fs", len(issues), agent_label, elapsed)
        print(f"[startup] Fetched {len(issues)} in-progress issues for {agent_label} in {elapsed:.1f}s")
        return issues

    def _recover_pr_pending_history(
        self,
        state: OrchestratorState,
        issue_branches: dict[int, str],
    ) -> None:
        """Rehydrate awaiting-merge visibility for locally pr-pending issues."""
        if self._label_store is None:
            return

        tracked_history = {entry.issue_number for entry in state.session_history}
        queue_cache = QueueCache(self.config, state, self._queue_cache_store)
        local_pr_pending = sorted(
            issue_number
            for issue_number, labels in self._label_store.load_all().items()
            if self._lm.is_pr_pending(sorted(labels))
        )
        if not local_pr_pending:
            return

        recovered = 0
        for issue_number in local_pr_pending:
            if issue_number in tracked_history:
                continue

            issue = self.repository_host.get_issue(issue_number)
            if issue is None:
                logger.warning(
                    "[startup] Failed to refetch locally pr-pending issue for dashboard recovery: issue=%d",
                    issue_number,
                )
                continue

            queue_status = queue_cache.evaluate_issue(issue)
            if queue_status == QueueMutationStatus.REJECTED_OUT_OF_SCOPE:
                logger.info(
                    "[startup] Skipping pr-pending dashboard recovery for out-of-scope issue=%d",
                    issue_number,
                )
                continue

            analysis = analyze_issue(
                issue=issue,
                repo=self.config.repo,
                issue_branches=issue_branches,
                check_session_fn=lambda n: self._session_exists(f"issue-{n}"),
                pr_tracker=self.repository_host,
            )
            if not analysis.has_open_pr or not analysis.pr_url:
                logger.warning(
                    "[startup] Skipping pr-pending dashboard recovery without open PR: issue=%d",
                    issue_number,
                )
                continue

            state.session_history.append(
                SessionHistoryEntry(
                    issue_number=issue.number,
                    title=issue.title,
                    agent_type=issue.agent_type or "agent:unknown",
                    status="completed",
                    runtime_minutes=0,
                    pr_url=analysis.pr_url,
                    status_reason="Recovered awaiting merge state on startup",
                    completed_at=datetime.now(timezone.utc),
                    issue_labels=tuple(issue.labels),
                )
            )
            record_issue_refreshes(state, {issue.number}, time.time())
            tracked_history.add(issue_number)
            recovered += 1

        if recovered:
            logger.info(
                "[startup] Recovered %d pr-pending issue(s) into dashboard history",
                recovered,
            )

    def _reconcile_label_store(self, state: OrchestratorState) -> None:
        """Reconcile the local label_store mirror against GitHub labels.

        Uses the warm queue/scope cache as the zero-cost label source only when
        it is verified fresh for this run, and lets the reconciler fetch any
        out-of-scope or non-fresh stored issues within its budget.
        """
        if self._label_store is None:
            return
        from .label_store_reconciler import LabelStoreReconciler

        reconciler = LabelStoreReconciler(
            label_store=self._label_store,
            label_manager=self._lm,
            repository_host=self.repository_host,
        )
        with gh_audit.context(
            reason=gh_audit.AuditReason.STARTUP_REFRESH,
            scope=gh_audit.AuditScope.STARTUP,
        ):
            result = reconciler.reconcile(self._fresh_label_snapshot(state))
        if result.issues_changed:
            logger.info(
                "[startup] Reconciled label_store: %d issue(s) changed "
                "(+%d/-%d labels)",
                result.issues_changed,
                result.labels_added,
                result.labels_removed,
            )

    def _fresh_label_snapshot(self, state: OrchestratorState) -> "FreshLabelSnapshot":
        """Build the GitHub-truth label snapshot for label_store reconciliation.

        Centralizes the freshness contract so the reconciler never receives
        unqualified cache data:

        - If this run's queue sync degraded onto the persisted (possibly stale)
          snapshot, return a degraded snapshot so the reconciler reads each
          stored issue fresh within budget — or leaves the store untouched when
          that read fails — rather than rewriting the mirror from cache that may
          predate a prior run's label mutation.
        - Otherwise return the freshly-synced queue/scope labels, still omitting
          issues this run mutated (their pre-mutation cache entry is stale, so
          they are re-read fresh too).
        """
        from .label_store_reconciler import FreshLabelSnapshot

        if not self._queue_labels_fresh:
            return FreshLabelSnapshot.degraded()

        labels_by_issue: dict[int, Sequence[str]] = {}
        for issue in [*state.cached_queue_issues, *state.cached_scope_issues]:
            if issue.number in self._startup_label_mutated_issues:
                continue
            labels_by_issue.setdefault(issue.number, issue.labels)
        return FreshLabelSnapshot.from_github_sync(labels_by_issue)

    def _analyze_and_handle_issue(
        self,
        state: OrchestratorState,
        issue: Issue,
        issue_branches: dict[int, str],
        issues_to_resume: list[tuple[Issue, str]],
        agent_label: str,
    ) -> None:
        """Analyze an in-progress issue and handle appropriately."""
        state.startup_message = f"Analyzing issue #{issue.number}..."

        analysis = analyze_issue(
            issue=issue, repo=self.config.repo, issue_branches=issue_branches,
            check_session_fn=lambda n: self._session_exists(f"issue-{n}"),
            pr_tracker=self.repository_host,
        )

        if issue.is_blocked:
            print(f"  #{issue.number}: Blocked - waiting for intervention")
        elif analysis.has_session:
            print(f"  #{issue.number}: Active session found - resuming monitoring")
        elif analysis.has_open_pr:
            self._handle_issue_with_pr(issue, analysis)
        elif analysis.has_partial_work:
            print(f"  #{issue.number}: Has branch '{analysis.branch}' with commits - queuing for resume")
            issues_to_resume.append((issue, agent_label))
        elif analysis.is_orphaned_label:
            self._clear_orphaned_label(issue)

    def _handle_issue_with_pr(self, issue: Issue, analysis: IssueState) -> None:
        """Handle issue that has an open PR."""
        if not self._lm.is_pr_pending(issue.labels):
            print(f"  #{issue.number}: Has open PR - adding pr-pending label (crash recovery)")
            self._apply_label_actions([
                AddLabelAction(issue_number=issue.number, label=self._lm.pr_pending, reason="startup recovery: missing pr-pending"),
                RemoveLabelAction(issue_number=issue.number, label=self._lm.in_progress, reason="startup recovery: remove stale in-progress"),
            ])
        else:
            print(f"  #{issue.number}: Has open PR ({analysis.pr_url or 'unknown'}) - already has pr-pending")

    def _clear_orphaned_label(self, issue: Issue) -> None:
        """Clear stale in-progress label from issue."""
        print(f"  #{issue.number}: No session or branch - clearing stale label")
        self._apply_label_actions([
            RemoveLabelAction(issue_number=issue.number, label=self._lm.in_progress, reason="startup recovery: clear stale in-progress"),
        ])

    async def _recover_pending_reviews(
        self,
        state: OrchestratorState,
        issue_branches: dict[int, str],
    ) -> None:
        """Recover PRs needing code review after crash/restart."""
        state.startup_message = "Checking PRs needing code review..."
        print("\nChecking for PRs needing code review...")

        # Caller ensures code_review_label is set before calling this method
        assert self.config.code_review_label is not None
        prs = self.repository_host.get_prs_with_label(self.config.code_review_label)
        for pr in prs:
            pr_number = pr.number
            pr_url = pr.url
            pr_body = pr.body

            issue_number = extract_issue_number_from_pr(pr)

            # Check if PR was created by orchestrator
            if ORCHESTRATOR_PR_MARKER not in pr_body:
                logger.debug(f"PR #{pr_number}: Not created by orchestrator (no marker)")
                continue

            scope = self._review_scope.check_issue_number(issue_number, pr_number)
            if not scope.in_scope:
                continue
            issue = scope.issue if scope.issue is not None else self.repository_host.get_issue(issue_number)
            if not isinstance(issue, Issue):
                issue = None

            scoped = scope_prs_to_active_issue_branch(
                issue_number,
                [pr],
                issue_branches=issue_branches,
            )
            if not scoped.matching:
                logger.info(
                    "[startup] Ignoring review PR from prior attempt: pr=%d issue=%d branch=%s expected_branch=%s",
                    pr_number,
                    issue_number,
                    pr.branch,
                    scoped.expected_branch,
                )
                continue

            validity = evaluate_review_validity(
                config=self.config,
                label_manager=self._lm,
                issue=issue,
                pr=pr,
                review_label_confirmed=True,
            )
            if not validity.valid:
                logger.info(
                    "[startup] Dropping stale pending review recovery: pr=%d issue=%d reason=%s issue_labels=%s pr_labels=%s",
                    pr_number,
                    issue_number,
                    validity.reason,
                    ",".join(validity.issue_labels) or "(missing)",
                    ",".join(validity.pr_labels) or "(none)",
                )
                continue

            # Check if review is already in progress
            if not self._session_exists(f"review-{pr_number}"):
                review = PendingReview(
                    issue_key=self.repository_host.create_issue_key(issue_number),
                    pr_number=pr_number,
                    pr_url=pr_url,
                    branch_name=pr.branch,
                    _issue_number=issue_number,
                    issue_labels=validity.issue_labels,
                )
                if review not in state.pending_reviews:
                    state.pending_reviews.append(review)
                    print(f"  PR #{pr_number}: Queued for code review")
            else:
                print(f"  PR #{pr_number}: Review already in progress")

    async def _recover_pending_tech_lead(self, state: OrchestratorState) -> None:
        """Recover pending tech_lead review issues after crash/restart.

        Anchor requeue and gated-proposal exclusion (#6778) live with the
        trigger owner (``health_review_trigger``).
        """
        state.startup_message = "Checking for pending tech_lead review issues..."
        print("\nChecking for pending tech_lead review issues...")
        # The shared scoped/exhaustive anchor-discovery owner (#6763 finding 7)
        # and the gated-proposal reconciliation (#6778/#6779) both run inside
        # the trigger owner, so recovery applies the same eligibility rule as
        # fact gathering and never requeues a proposal issue as an anchor.
        recover_pending_tech_lead_anchors(
            state,
            repository_host=self.repository_host,
            config=self.config,
            session_exists=self._session_exists,
            tech_lead_authority=self._tech_lead_authority,
        )

    def _recover_pending_retrospective_reviews(self, state: OrchestratorState) -> None:
        """Recover trigger-labeled existing-work review requests on startup."""

        discovered = discover_retrospective_review_issues(
            repository_host=self.repository_host,
            config=self.config,
            already_issue_numbers=state.retrospective_review_in_flight_issue_numbers(),
        )
        for review in discovered:
            state.pending_retrospective_reviews.append(
                PendingRetrospectiveReview(
                    issue_key=self.repository_host.create_issue_key(review.issue_number),
                    issue_number=review.issue_number,
                    issue_title=review.issue_title,
                    agent_label=review.agent_label,
                    trigger_label=review.trigger_label,
                    prior_pr_number=review.prior_pr_number,
                    prior_pr_url=review.prior_pr_url,
                    issue_labels=review.issue_labels,
                )
            )
        if discovered:
            logger.info(
                "[startup] Recovered %d retrospective review request(s)",
                len(discovered),
            )

    def _recover_pending_validation_retries(
        self,
        state: OrchestratorState,
        issue_branches: dict[int, str],
    ) -> None:
        """Recover validation retries from before restart.

        Scans worktrees for issues that were mid-validation-retry when
        the orchestrator restarted. Re-queues them for immediate retry.

        Args:
            state: Orchestrator state to update
            issue_branches: Map of issue numbers to branch names
        """
        recovered = 0
        for issue_number, branch_name in issue_branches.items():
            worktree_path = get_worktree_path(self.config, issue_number)
            if not worktree_path.exists():
                continue

            session_name = f"issue-{issue_number}"
            if self._session_exists(session_name):
                logger.info(
                    "[startup] Validation retry already has a running session: issue=%d",
                    issue_number,
                )
                continue

            # Resolve durable retry artifacts in one pass. The artifact owner has
            # already resolved provenance: review-only and unrecognized run
            # directories are refused upstream, so any artifact returned here
            # carries a concrete coding-side ``source_task`` (#6426).
            artifacts = find_pending_retry_artifacts(worktree_path)
            if artifacts is None or not artifacts.state.can_retry:
                continue

            validation_state = artifacts.state
            retry_prompt = None
            if artifacts.retry_prompt_path is not None:
                try:
                    retry_prompt = artifacts.retry_prompt_path.read_text()
                except OSError:
                    pass

            pending_retry = PendingValidationRetry(
                issue_number=issue_number,
                issue_title=f"Issue #{issue_number}",  # We don't have the full title here
                agent_label="",  # Will be determined when launching
                worktree_path=str(worktree_path),
                branch_name=branch_name,
                original_prompt=retry_prompt,
                validation_error=validation_state.last_error or "Unknown validation error",
                validation_error_file=validation_state.last_error_file,
                retry_count=validation_state.retry_count,
                source_task=artifacts.source_task,
                validation_cmd=validation_state.validation_cmd,
            )
            state.pending_validation_retries.append(pending_retry)
            recovered += 1

            logger.info(
                "[startup] Recovered pending validation retry: issue=%d retry_count=%d/%d",
                issue_number,
                validation_state.retry_count,
                validation_state.max_retries,
            )

        if recovered:
            print(f"\n🔄 Recovered {recovered} pending validation retry(ies)")

    def _recover_orphaned_cleanups(self, state: OrchestratorState) -> None:
        """Recover review-gated issue trees and owned disposable orphans."""
        self._startup_worktree_reconciler.recover(state)

    def _restore_and_sync_queue(self, state: OrchestratorState) -> None:
        """Restore queue cache from SQLite and delta-sync from GitHub.

        Warm start: load cached issues + watermark from SQLite, then use
        ``list_issues_delta`` to fetch only what changed since last persist.
        Cold start: fall back to a full scan via ``_update_queue_cache``.
        Either way, persist the result back to SQLite for the next restart.
        """
        store = self._queue_cache_store
        # Reconcile health-review last-fired from anchor truth (#6763 finding 6).
        hydrate_last_health_review_at(self.config, state, store, self.repository_host)
        # Restore the tech-lead stuck-sweep timer + durable recovery counters
        # so a restart honors the interval and keeps each issue's budget (#6823).
        hydrate_stuck_sweep_state(state, store)
        if store is None:
            # No persistent store configured — fall back to full scan. Guard the
            # fetch so a transient repository blip degrades-and-continues rather
            # than aborting the remaining startup recovery phases.
            state.startup_message = "Caching queue..."
            try:
                self._issue_fetch_resilience.guard(self._update_queue_cache)
                self._queue_labels_fresh = True
            except TransientIssueFetchError as exc:
                self._note_degraded_queue_fetch(exc, state)
            return

        state.startup_message = "Restoring queue cache..."
        cached_issues = store.load_issues(self.config.repo or "")
        cached_watermark = store.load_watermark()
        queue_cache = QueueCache(self.config, state, store)

        try:
            # Guard *only* the issue-list fetch. A persistent repo-not-found or
            # auth failure here raises PermanentIssueFetchError, which propagates
            # out of run_startup so the orchestrator fails fast with an
            # actionable message. A transient failure degrades-and-continues.
            self._issue_fetch_resilience.guard(
                lambda: self._sync_queue_from_github(
                    state, queue_cache, cached_issues, cached_watermark,
                )
            )
        except TransientIssueFetchError as exc:
            # Degrade: come up on the last-known-good cached queue (if any) and
            # let the main loop re-sync. Persist nothing so the good snapshot
            # stays intact, and let the remaining startup phases still run.
            if cached_issues:
                queue_cache.replace_from_refresh(list(cached_issues))
                state.queue_delta_watermark = cached_watermark
            self._note_degraded_queue_fetch(exc, state)
            return

        # The queue now reflects a successful GitHub-backed sync, so its labels
        # are safe to treat as source of truth during label_store reconciliation.
        self._queue_labels_fresh = True
        # Persist updated state to SQLite for next restart (success path only).
        state.startup_message = "Persisting queue cache..."
        queue_cache.save_snapshot()

    def _sync_queue_from_github(
        self,
        state: OrchestratorState,
        queue_cache: QueueCache,
        cached_issues: Sequence[Issue],
        cached_watermark: str | None,
    ) -> None:
        """Perform the startup issue-list fetch and apply it to the queue.

        This is the single repository-host fetch the resilience policy guards at
        startup; a ``RepositoryHostError`` raised here is classified by the
        policy (degrade vs. fail-fast). Callers run it under
        ``issue_fetch_resilience.guard``.
        """
        if cached_watermark and not cached_issues:
            # Corrupt/partial persisted state: watermark exists but no issues were
            # loaded. A delta sync from the stale watermark would only pull issues
            # updated since then, stranding any issues whose labels/state never
            # changed afterwards. Force a cold full scan to rebuild from GitHub.
            logger.warning(
                "[STARTUP] Queue cache inconsistency: watermark=%s but 0 cached issues; "
                "forcing cold full scan to avoid stranding unchanged issues",
                cached_watermark,
            )
            state.startup_message = "Rebuilding queue cache..."
            self._update_queue_cache()
        elif cached_watermark:
            # Warm start: load from SQLite, then delta sync from GitHub
            state.startup_message = "Syncing queue changes from GitHub..."
            logger.info(
                "[STARTUP] Warm start: %d cached issues, watermark=%s",
                len(cached_issues), cached_watermark,
            )
            delta_issues, next_watermark = self.repository_host.list_issues_delta(
                since=cached_watermark, limit=200,
            )
            # Merge: start from cached, apply deltas
            issue_map: dict[int, Issue] = {i.number: i for i in cached_issues}
            for issue in delta_issues:
                if issue.state.lower() == "open":
                    issue_map[issue.number] = issue
                else:
                    issue_map.pop(issue.number, None)

            # Apply eligibility policy (scope + exclusion filters)
            queue_cache.replace_from_refresh(list(issue_map.values()))
            state.queue_delta_watermark = next_watermark or cached_watermark
            logger.info(
                "[STARTUP] Delta sync: %d delta issues, %d in queue after filter",
                len(delta_issues), len(state.cached_queue_issues),
            )
        else:
            # Cold start (first run or empty store): full scan via existing path
            state.startup_message = "Caching queue..."
            logger.info("[STARTUP] Cold start: running full queue scan")
            self._update_queue_cache()

    def _note_degraded_queue_fetch(
        self, exc: TransientIssueFetchError, state: OrchestratorState
    ) -> None:
        """Log a transient startup queue-fetch failure and keep going.

        The orchestrator is label-recoverable: coming up on the (possibly stale
        or empty) cached queue and letting the main loop re-sync is far cheaper
        than refusing to start. Only the queue fetch is skipped — the remaining
        startup recovery phases still run.
        """
        logger.warning(
            "[STARTUP] %s — coming up on the cached queue (%d issue(s)); the main "
            "loop will re-sync. %s",
            exc.summary, len(state.cached_queue_issues), exc.suggested_fix,
        )

    async def _resume_partial_work(
        self,
        state: OrchestratorState,
        issues_to_resume: list[tuple[Issue, str]],
    ) -> None:
        """Resume issues that have partial work (branch with commits but no session)."""
        if not issues_to_resume:
            return

        if state.paused:
            state.startup_message = f"Queueing {len(issues_to_resume)} in-progress issue(s)..."
            print(
                f"\nStartup is paused; queueing {len(issues_to_resume)} "
                "in-progress issue(s) with partial work."
            )
            for issue, _agent_label in issues_to_resume:
                self._queue_partial_work_resume(state, issue)
                print(f"  #{issue.number}: Queued for resume")
            return

        state.startup_message = f"Resuming {len(issues_to_resume)} in-progress issue(s)..."
        print(f"\n🔄 Resuming {len(issues_to_resume)} in-progress issue(s) with partial work...")

        for issue, _agent_label in issues_to_resume:
            if not worker_slot_free(self.config, state.active_sessions):
                print(f"  #{issue.number}: At max capacity, will resume when slot available")
                self._queue_partial_work_resume(state, issue)
                continue

            print(f"  #{issue.number}: Starting session to resume work...")
            session = self._launch_session(issue)
            # Note: launch_session already appends to state.active_sessions
            if session:
                print(f"  #{issue.number}: ✅ Session started")
            else:
                print(f"  #{issue.number}: ❌ Failed to start session")

    def _queue_partial_work_resume(self, state: OrchestratorState, issue: Issue) -> None:
        if issue.number not in state.priority_queue:
            state.priority_queue.insert(0, issue.number)
