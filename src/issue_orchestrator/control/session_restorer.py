"""SessionRestorer - handles restoring session tracking after restart.

This module extracts session restoration logic from the orchestrator.
It handles:
1. Discovering running sessions from the terminal backend
2. Finding corresponding worktrees
3. Fetching issue details
4. Creating Session objects for tracking

Called during startup to restore tracking for sessions that survived a restart.
"""

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional, cast

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.tech_lead_authority import TechLeadAuthorityStore

from ..domain.issue_key import GitHubIssueKey
from ..domain.session_key import SessionKey, TaskKind
from ..domain.models import Issue, RETROSPECTIVE_REVIEW_TERMINAL_PREFIX, Session
from ..domain.session_run import SessionRunAssets
from ..ports import RepositoryHost, WorkingCopy
from ..ports.session_runner import DiscoveredSession
from .tech_lead_session_policy import recover_tech_lead_launch_scope

logger = logging.getLogger(__name__)

_CANONICAL_SESSION_PREFIXES = (
    "issue-",
    "review-",
    RETROSPECTIVE_REVIEW_TERMINAL_PREFIX,
    "rework-",
    "tech-lead-",
)
_REVIEW_SESSION_RE = re.compile(r"^review-(\d+)$")
_REVIEW_TITLE_RE = re.compile(r"\bReview PR #(\d+)\b")


class SessionConfigurationIdentityError(RuntimeError):
    """A surviving session cannot be safely restored under this configuration."""


class SessionConfigurationModeMismatchError(SessionConfigurationIdentityError):
    """A surviving session belongs to another effective launch configuration."""


class SessionConfigurationIdentityVerificationError(SessionConfigurationIdentityError):
    """A surviving session's effective launch configuration cannot be verified."""


class SessionRestorer:
    """Handles restoring session tracking after orchestrator restart.

    Dependencies:
    - config: Configuration with agent settings
    - repository_host: For fetching issue details and cleanup
    """

    def __init__(
        self,
        config: "Config",
        repository_host: RepositoryHost,
        working_copy: WorkingCopy,
        tech_lead_authority: "TechLeadAuthorityStore | None" = None,
    ):
        self.config = config
        self.repository_host = repository_host
        self.working_copy = working_copy
        # Durable cohort ledger, read when rebuilding a restored health
        # review's owned scope (#6994 round 1 F3). Optional so unrelated tests
        # need not wire it; without it a restored storm review still recovers
        # its GLOBAL flavor (the barrier that matters) with an empty cohort.
        self.tech_lead_authority = tech_lead_authority

    def restore_sessions(
        self,
        running: list[DiscoveredSession],
        already_tracked: list[Session],
    ) -> list[Session]:
        """Restore tracking for sessions that are still running after restart.

        Args:
            running: List of dicts from discover_running_sessions() with
                     {issue_number, tab_name, is_review}
            already_tracked: Sessions already being tracked (to avoid duplicates)

        Returns:
            List of newly restored Session objects
        """
        restored = []

        for session_info in running:
            issue_number = self._issue_number(session_info)

            try:
                session = self._restore_single_session(
                    session_info=session_info,
                    already_tracked=already_tracked + restored,
                )
                if session:
                    restored.append(session)
                    logger.info(
                        "Restored tracking for session %s (issue #%d)",
                        session.terminal_id,
                        issue_number,
                    )
                    print(f"  Restored: {session.terminal_id} (#{issue_number})")

            except SessionConfigurationIdentityError:
                raise
            except Exception as e:
                logger.exception(
                    "Failed to restore session for issue #%d: %s", issue_number, e
                )
                print(f"  Warning: Failed to restore session for #{issue_number}: {e}")

        return restored

    def canonical_terminal_id(self, session_info: DiscoveredSession) -> str:
        """Return the canonical terminal id for a discovered or known terminal."""
        session_name = str(session_info.get("session_name") or "")
        if session_name.startswith(_CANONICAL_SESSION_PREFIXES):
            return session_name

        issue_number = self._issue_number(session_info)
        tab_name = str(session_info.get("tab_name") or "")
        if session_info.get("is_review"):
            pr_number = self._review_pr_number(session_info)
            if pr_number is not None:
                return f"review-{pr_number}"
            logger.warning(
                "[ORPHAN] Could not derive review PR number from discovered session; "
                "falling back to issue number: issue=%s tab_name=%r session_name=%r",
                issue_number,
                tab_name,
                session_name,
            )
            return f"review-{issue_number}"

        if tab_name.startswith(_CANONICAL_SESSION_PREFIXES):
            return tab_name
        # Legacy records without session_name predate durable canonical ids.
        # Non-review records were overwhelmingly issue sessions, so issue-N is
        # the best recoverable identity if the tab title is also noncanonical.
        return f"issue-{issue_number}"

    def restore_known_terminal(
        self,
        *,
        issue_number: int,
        session_name: str,
        run_dir: Path,
        is_review: bool,
        already_tracked: list[Session],
        tab_name: str = "",
    ) -> list[Session]:
        """Restore tracking for a terminal whose canonical id is already known."""
        discovered = DiscoveredSession(
            issue_number=issue_number,
            tab_name=tab_name,
            is_review=is_review,
            session_name=session_name,
            run_dir=str(run_dir),
        )
        return self.restore_sessions([discovered], already_tracked)

    def _assert_restored_session_mode(
        self,
        run_assets: SessionRunAssets,
        session_name: str,
    ) -> None:
        """Reject a relaunch that would reinterpret a live session under another mode."""
        identity_path = run_assets.run_dir / "session-identity.json"
        identity: dict[str, object] = {}
        if identity_path.is_file():
            try:
                loaded = json.loads(identity_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise SessionConfigurationIdentityVerificationError(
                    f"Cannot verify configuration identity for live session {session_name}: "
                    f"{identity_path} is unreadable"
                ) from exc
            if not isinstance(loaded, dict):
                raise SessionConfigurationIdentityVerificationError(
                    f"Cannot verify configuration identity for live session {session_name}: "
                    f"{identity_path} must contain a JSON object"
                )
            identity = loaded

        identity_keys = (
            "configuration_mode",
            "config_name",
            "config_fingerprint",
        )
        recorded_values = tuple(identity.get(key) for key in identity_keys)
        current_values = (
            self.config.configuration_mode,
            self.config.config_name,
            self.config.config_fingerprint,
        )
        if not all(isinstance(value, str) for value in recorded_values):
            raise SessionConfigurationIdentityVerificationError(
                f"Cannot verify configuration identity for live session {session_name}: "
                f"{identity_path} lacks mode, config name, or effective fingerprint"
            )
        recorded_identity = cast(tuple[str, str, str], recorded_values)
        if recorded_identity != current_values:
            recorded_mode, recorded_config, recorded_fingerprint = recorded_identity
            raise SessionConfigurationModeMismatchError(
                "Cannot start Repository Engine with configuration "
                f"{self.config.configuration_mode!r}/{self.config.config_name!r} "
                f"({self.config.config_fingerprint[:12]}): live session {session_name!r} "
                f"was launched with {recorded_mode!r}/{recorded_config!r} "
                f"({recorded_fingerprint[:12]}). Drain or terminate live sessions before "
                "switching or editing configuration."
            )

    def _restore_single_session(
        self,
        session_info: DiscoveredSession,
        already_tracked: list[Session],
    ) -> Optional[Session]:
        """Restore a single session.

        Returns:
            Session object if restored, None if skipped
        """
        issue_number = self._issue_number(session_info)
        tab_name = str(session_info.get("tab_name") or "")
        is_review = session_info["is_review"]
        session_name = self.canonical_terminal_id(session_info)

        # Skip if already tracking this session
        if any(s.terminal_id == session_name for s in already_tracked):
            logger.info("Session %s already tracked - skipping restore", session_name)
            return None

        run_assets = self._required_run_assets(session_info, session_name)
        self._assert_restored_session_mode(run_assets, session_name)

        # Determine session type and session_name
        restored_pr_number: int | None = None
        if is_review and not session_name.startswith(
            RETROSPECTIVE_REVIEW_TERMINAL_PREFIX
        ):
            match = _REVIEW_SESSION_RE.match(session_name)
            restored_pr_number = int(match.group(1)) if match else issue_number

        worktree_path = run_assets.worktree_path
        branch_name = self._get_branch_name(worktree_path)

        # Fetch single issue details to get agent type
        issue_obj = self.repository_host.get_issue(issue_number)
        agent_config = None

        if issue_obj and issue_obj.agent_type:
            agent_config = self.config.agents.get(issue_obj.agent_type)

        if not issue_obj:
            # Create minimal issue object for reviews or if issue not found
            issue_obj = Issue(
                number=issue_number,
                title=tab_name.replace("#", "").strip(),
                labels=[],
            )

        if not agent_config:
            # Use first available agent config as fallback
            agent_config = next(iter(self.config.agents.values()), None)

        if not agent_config:
            logger.warning(
                "No agent config available for session %s - skipping", session_name
            )
            return None

        if not self.config.repo:
            logger.warning("No repo configured for session %s - skipping", session_name)
            return None

        # Create session with domain identity
        issue_key = GitHubIssueKey(repo=self.config.repo, external_id=str(issue_number))
        if session_name.startswith(RETROSPECTIVE_REVIEW_TERMINAL_PREFIX):
            task_kind = TaskKind.RETROSPECTIVE_REVIEW
        else:
            task_kind = TaskKind.REVIEW if is_review else TaskKind.CODE
        session_key = SessionKey(issue=issue_key, task=task_kind)
        # Use the agent type from issue labels, or the first available agent as fallback
        agent_label_val = issue_obj.agent_type or next(
            iter(self.config.agents.keys()), "unknown"
        )
        return Session(
            key=session_key,
            issue=issue_obj,
            agent_config=agent_config,
            terminal_id=session_name,
            worktree_path=worktree_path,
            branch_name=branch_name,
            run_assets=run_assets,
            agent_label=agent_label_val,
            pr_number=restored_pr_number,
            # Rebuild the tech-lead launch grant from durable truth. Without it
            # a restored whole-board review stops acting as the exclusive
            # barrier it is, and the dashboard misreports it (#6994 F3).
            tech_lead_scope=recover_tech_lead_launch_scope(
                self.config, issue_obj, self.tech_lead_authority
            ),
        )

    def _required_run_assets(
        self,
        session_info: DiscoveredSession,
        session_name: str,
    ) -> SessionRunAssets:
        raw: object = session_info.get("run_dir")
        if type(raw) is not str or not raw:
            message = (
                f"Discovered active session {session_name} has no recorded run_dir"
            )
            raise SessionConfigurationIdentityVerificationError(
                f"Cannot verify configuration identity: {message}"
            )
        run_dir = Path(raw)
        manifest_path = run_dir / "manifest.json"
        if not run_dir.exists() or not manifest_path.exists():
            message = f"Discovered active session {session_name} run assets are missing: {run_dir}"
            raise SessionConfigurationIdentityVerificationError(
                f"Cannot verify configuration identity: {message}"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest root must be an object")
            return SessionRunAssets.from_manifest_payload(
                run_dir=run_dir,
                manifest=manifest,
            )
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            message = (
                f"Discovered active session {session_name} has invalid run assets at "
                f"{run_dir}: {exc}"
            )
            raise SessionConfigurationIdentityVerificationError(
                f"Cannot verify configuration identity: {message}"
            ) from exc

    def _get_branch_name(self, worktree_path: Path) -> str:
        """Get the current branch name for a worktree.

        Uses WorkingCopy to get branch name.
        """
        branch = self.working_copy.get_current_branch(worktree_path)
        if not branch:
            logger.warning("Failed to get branch name for %s", worktree_path)
            return "unknown"
        return branch

    @staticmethod
    def _issue_number(session_info: DiscoveredSession) -> int:
        return int(session_info.get("issue_number") or 0)

    @staticmethod
    def _review_pr_number(session_info: DiscoveredSession) -> int | None:
        session_name = str(session_info.get("session_name") or "")
        match = _REVIEW_SESSION_RE.match(session_name)
        if match:
            return int(match.group(1))

        tab_name = str(session_info.get("tab_name") or "")
        match = _REVIEW_TITLE_RE.search(tab_name)
        if match:
            return int(match.group(1))
        return None
