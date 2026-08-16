"""CleanupManager - handles worktree and session cleanup.

This module extracts cleanup logic from the orchestrator:
1. process_deferred_cleanups - Clean up after reviews complete
2. recover_orphaned_cleanups - Clean up orphaned worktrees on startup

Cleanup is deferred when:
- Tech Lead workflow: wait for tech-lead-reviewed label
- Code review workflow: wait for code-reviewed label
"""

import logging
import time
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from ..ports.repository_host import RepositoryHostError

if TYPE_CHECKING:
    from ..infra.config import Config, AgentConfig
    from ..domain.models import PendingCleanup
    from ..ports import RepositoryHost
    from ..ports.worktree_manager import WorktreeManager
    from ..ports.pull_request_tracker import PRInfo

logger = logging.getLogger(__name__)


class CleanupManager:
    """Manages worktree and session cleanup.

    Dependencies:
    - config: Configuration with cleanup settings
    - repository_host: For fetching PRs with labels
    - kill_session_fn: Function to kill terminal sessions
    - session_exists_fn: Function to check if session exists
    - get_worktree_path_fn: Function to get worktree path for issue
    """

    def __init__(
        self,
        config: "Config",
        repository_host: "RepositoryHost",
        worktree_manager: "WorktreeManager",
        kill_session_fn: Callable[[str], None],
        session_exists_fn: Callable[[str], bool],
        get_worktree_path_fn: Callable[[int, "AgentConfig"], Path],
        get_session_name_fn: Callable[[int, str], str],
    ):
        self.config = config
        self.repository_host = repository_host
        self._worktree_manager = worktree_manager
        self._kill_session = kill_session_fn
        self._session_exists = session_exists_fn
        self._get_worktree_path = get_worktree_path_fn
        self._get_session_name = get_session_name_fn
        self._tech_lead_issue_last_failure: float | None = None

    def should_retry_tech_lead_issue(self, cooldown_seconds: int = 60) -> bool:
        """Throttle tech_lead issue creation failures to avoid tight retry loops."""
        now = time.time()
        if self._tech_lead_issue_last_failure is None:
            return True
        return (now - self._tech_lead_issue_last_failure) >= cooldown_seconds

    def mark_tech_lead_issue_failure(self) -> None:
        """Record a tech_lead issue creation failure for throttling."""
        self._tech_lead_issue_last_failure = time.time()

    def process_deferred_cleanups(
        self,
        pending_cleanups: list["PendingCleanup"],
    ) -> list["PendingCleanup"]:
        """Process deferred cleanups for sessions awaiting review completion.

        Checks pending cleanups and performs cleanup when:
        - Tech Lead workflow: PR has tech-lead-reviewed label
        - Code review workflow: PR has code-reviewed label

        Args:
            pending_cleanups: List of pending cleanups to process

        Returns:
            Updated list with processed cleanups removed
        """
        if not pending_cleanups:
            return pending_cleanups

        cleanup_label = self._get_cleanup_label()
        if not cleanup_label:
            return pending_cleanups

        reviewed_pr_numbers = self._get_reviewed_pr_numbers(cleanup_label)
        if reviewed_pr_numbers is None:
            return pending_cleanups

        cleanups_to_remove = self._process_pending_cleanups(pending_cleanups, reviewed_pr_numbers, cleanup_label)

        remaining = [c for c in pending_cleanups if c not in cleanups_to_remove]
        if cleanups_to_remove:
            logger.info(f"[CLEANUP] Processed {len(cleanups_to_remove)} deferred cleanups")

        return remaining

    def _get_cleanup_label(self) -> str | None:
        """Get the label that indicates review is complete."""
        if self.config.tech_lead_enabled:
            label = self.config.tech_lead_reviewed_label
        elif self.config.code_review_agent:
            label = self.config.code_reviewed_label
        else:
            logger.warning("[CLEANUP] Found deferred cleanups but no review workflow configured")
            return None

        if not label:
            logger.warning("[CLEANUP] No cleanup label configured")
            return None

        return label

    def _get_reviewed_pr_numbers(self, cleanup_label: str) -> set[int] | None:
        """Get PR numbers that have the cleanup label."""
        try:
            reviewed_prs = self.repository_host.get_prs_with_label(cleanup_label)
            return {pr.number for pr in reviewed_prs}
        except RepositoryHostError:
            raise
        except Exception as e:
            logger.warning(f"[CLEANUP] Failed to fetch PRs with label {cleanup_label}: {e}")
            return None

    def _get_cleanup_settings(self) -> tuple[bool, bool]:
        """Get cleanup settings (close_tabs, remove_worktrees) based on workflow."""
        if self.config.tech_lead_enabled:
            return (
                self.config.cleanup.with_tech_lead.close_ai_session_tabs,
                self.config.cleanup.with_tech_lead.remove_worktrees,
            )
        return (
            self.config.cleanup.without_tech_lead.close_ai_session_tabs,
            self.config.cleanup.without_tech_lead.remove_worktrees,
        )

    def _process_pending_cleanups(
        self,
        pending_cleanups: list["PendingCleanup"],
        reviewed_pr_numbers: set[int],
        cleanup_label: str,
    ) -> list["PendingCleanup"]:
        """Process each pending cleanup and return list of successful cleanups."""
        close_tabs, remove_wt = self._get_cleanup_settings()
        cleanups_to_remove = []

        for pending in pending_cleanups:
            if pending.pr_number not in reviewed_pr_numbers:
                continue

            logger.info(f"[CLEANUP] PR #{pending.pr_number} has '{cleanup_label}' label - cleaning up")
            cleanup_succeeded = self._execute_cleanup(pending, close_tabs, remove_wt)

            if cleanup_succeeded:
                cleanups_to_remove.append(pending)
            else:
                logger.warning("[CLEANUP] Cleanup incomplete for #%d - leaving pending for retry", pending.issue_number)

        return cleanups_to_remove

    def _execute_cleanup(self, pending: "PendingCleanup", close_tabs: bool, remove_wt: bool) -> bool:
        """Execute cleanup for a single pending cleanup. Returns True if successful."""
        success = True

        if close_tabs:
            try:
                self._kill_session(pending.terminal_id)
                logger.info(f"[CLEANUP] Closed terminal session for #{pending.issue_number}")
            except Exception as e:
                logger.warning(f"[CLEANUP] Failed to close session for #{pending.issue_number}: {e}")
                success = False

        if remove_wt:
            if self._remove_worktree_for_cleanup(
                pending.worktree_path,
                issue_number=pending.issue_number,
            ):
                logger.info(f"[CLEANUP] Removed worktree for #{pending.issue_number}")
            else:
                success = False

        return success

    def _remove_worktree_for_cleanup(self, worktree_path: Path, *, issue_number: int) -> bool:
        """Remove a worktree, escalating only when no user changes would be lost."""
        try:
            self._worktree_manager.remove_checkout(worktree_path)
            return True
        except Exception as first_error:
            try:
                can_force = self._worktree_manager.can_remove_without_user_changes(worktree_path)
            except Exception as safety_error:
                logger.warning(
                    "[CLEANUP] Failed to remove worktree for #%d: %s; "
                    "could not verify forced removal safety: %s",
                    issue_number,
                    first_error,
                    safety_error,
                )
                return False

            if not can_force:
                logger.warning(
                    "[CLEANUP] Failed to remove worktree for #%d: %s; "
                    "tracked or non-runtime changes are present",
                    issue_number,
                    first_error,
                )
                return False

            logger.info(
                "[CLEANUP] Retrying forced worktree removal for #%d after "
                "runtime-only dirty state blocked clean removal",
                issue_number,
            )
            try:
                self._worktree_manager.remove_checkout(worktree_path, force=True)
                return True
            except Exception as force_error:
                logger.warning(
                    "[CLEANUP] Failed forced worktree removal for #%d: %s",
                    issue_number,
                    force_error,
                )
                return False

    def recover_orphaned_cleanups(
        self,
        set_startup_message: Callable[[str], None] | None = None,
    ) -> int:
        """Recover and process orphaned cleanups from before restart.

        Called during startup to clean up worktrees for PRs that were reviewed
        (have tech-lead-reviewed or code-reviewed label) but weren't cleaned up before
        the orchestrator stopped.

        Args:
            set_startup_message: Optional callback to set startup status message

        Returns:
            Number of orphaned worktrees cleaned up
        """
        cleanup_label = self._get_cleanup_label()
        if not cleanup_label:
            return 0

        close_tabs, remove_wt = self._get_cleanup_settings()

        if set_startup_message:
            set_startup_message("Checking for orphaned cleanups...")
        print(f"\nChecking for orphaned cleanups (PRs with '{cleanup_label}' label)...")

        reviewed_prs = self._fetch_reviewed_prs(cleanup_label)
        if not reviewed_prs:
            print("  No reviewed PRs found")
            return 0

        # Consistency check: PRs with code-reviewed label should not be draft
        draft_fixed = self._fix_draft_reviewed_prs(reviewed_prs)
        if draft_fixed > 0:
            print(f"  Fixed {draft_fixed} draft PR(s) with '{cleanup_label}' label")

        cleaned_count = self._cleanup_orphaned_worktrees(reviewed_prs, close_tabs, remove_wt)

        if cleaned_count > 0:
            print(f"  Cleaned up {cleaned_count} orphaned worktree(s)")
        else:
            print("  No orphaned worktrees found")

        return cleaned_count

    def _fetch_reviewed_prs(self, cleanup_label: str) -> list["PRInfo"]:
        """Fetch PRs with the cleanup label."""
        try:
            return self.repository_host.get_prs_with_label(cleanup_label)
        except RepositoryHostError:
            raise
        except Exception as e:
            logger.warning(f"[CLEANUP] Failed to fetch reviewed PRs: {e}")
            return []

    def _cleanup_orphaned_worktrees(
        self,
        reviewed_prs: list["PRInfo"],
        close_tabs: bool,
        remove_wt: bool,
    ) -> int:
        """Clean up orphaned worktrees for reviewed PRs."""
        cleaned_count = 0

        for pr in reviewed_prs:
            issue_number = self._worktree_manager.extract_issue_number(pr.branch)
            if issue_number is None:
                continue

            session_name = self._get_session_name(issue_number, "issue")
            if self._session_exists(session_name):
                logger.debug(f"[CLEANUP] Session {session_name} still running - skipping")
                continue

            if self._cleanup_worktree_for_issue(issue_number, session_name, close_tabs, remove_wt):
                cleaned_count += 1

        return cleaned_count

    def _cleanup_worktree_for_issue(
        self,
        issue_number: int,
        session_name: str,
        close_tabs: bool,
        remove_wt: bool,
    ) -> bool:
        """Clean up worktree for a specific issue. Returns True if cleaned."""
        for _agent_label, agent_config in self.config.agents.items():
            worktree_path = self._get_worktree_path(issue_number, agent_config)

            if not worktree_path.exists():
                continue

            logger.info(f"[CLEANUP] Found orphaned worktree for #{issue_number} at {worktree_path}")
            print(f"  #{issue_number}: Cleaning up orphaned worktree")

            if close_tabs:
                try:
                    self._kill_session(session_name)
                except Exception:
                    pass  # Session probably already gone

            if remove_wt:
                if self._remove_worktree_for_cleanup(
                    worktree_path,
                    issue_number=issue_number,
                ):
                    logger.info(f"[CLEANUP] Removed orphaned worktree for #{issue_number}")
                else:
                    return False

            return True  # Found the worktree, no need to check other agents

        return False

    def _fix_draft_reviewed_prs(self, prs: list["PRInfo"]) -> int:
        """Fix PRs that have code-reviewed label but are still draft.

        This is a consistency check that runs on startup. If a PR has
        the code-reviewed label, it should not be draft - mark it ready.

        This handles edge cases where:
        - Review session completed but crashed before marking PR ready
        - Race condition between label addition and draft status update
        - Manual label addition to a draft PR

        Args:
            prs: List of PRInfo objects to check

        Returns:
            Number of PRs fixed
        """
        fixed_count = 0
        for pr in prs:
            # Check if PR is draft (PRInfo has draft attribute)
            if getattr(pr, "draft", None) is True:
                try:
                    logger.info(
                        "[STARTUP] PR #%d has code-reviewed label but is draft - marking ready",
                        pr.number
                    )
                    self.repository_host.set_pr_draft(pr.number, False)
                    fixed_count += 1
                except Exception as e:
                    logger.warning(
                        "[STARTUP] Failed to mark PR #%d as ready: %s",
                        pr.number, e
                    )
        return fixed_count
