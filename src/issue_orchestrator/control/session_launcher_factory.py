"""Assembly of a :class:`SessionLauncher` from the dependency bundle.

Its own module because ``session_launcher`` is already an oversized
hotspot and the orchestrator facade should not be assembling twenty
collaborators inline either. Construction is a separate concern from
the launcher's behaviour, so it gets a separate seam.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ..ports import Issue as IssueProtocol
from .session_launcher import SessionLauncher

if TYPE_CHECKING:
    from ..domain.state_machines.issue_machine import IssueStateMachine
    from ..domain.state_machines.review_machine import ReviewStateMachine
    from ..domain.state_machines.session_machine import SessionStateMachine
    from ..infra.config import Config
    from ..ports.board_snapshot_provider import BoardSnapshotProvider
    from .dependency_evaluator import DependencyEvaluator
    from .orchestrator_deps import OrchestratorDeps


def create_session_launcher(
    *,
    config: Config,
    deps: "OrchestratorDeps",
    board_snapshot_provider: "BoardSnapshotProvider",
    session_exists_fn: Callable[[str], bool],
    create_session_fn: Callable[[str, str, Path, str | None], bool],
    get_issue_machine: Callable[["IssueProtocol"], Optional["IssueStateMachine"]],
    get_session_machine: Callable[[str, int, int], Optional["SessionStateMachine"]],
    get_review_machine: Callable[[int, int], Optional["ReviewStateMachine"]],
    refresh_issue_fn: Optional[Callable[[int], Optional["IssueProtocol"]]],
    dependency_evaluator: Optional["DependencyEvaluator"],
) -> SessionLauncher:
    """Build a launcher from the orchestrator's dependency bundle.

    Construction lives next to the class rather than inside the
    orchestrator facade: the launcher takes twenty collaborators, and
    assembling them was the single densest block in an already oversized
    module. Callers supply only what the facade genuinely owns — its
    state-machine accessors and session callbacks.
    """
    return SessionLauncher(
        config, deps.events, deps.repository_host, deps.action_applier, deps.session_manager,
        deps.worktree_manager, deps.working_copy, deps.command_runner, deps.session_output,
        deps.manifest_downloader, deps.tech_lead_authority,
        session_exists_fn,
        create_session_fn, get_issue_machine, get_session_machine,
        get_review_machine, refresh_issue_fn, dependency_evaluator,
        claim_manager=deps.claim_manager,
        provider_resilience=deps.provider_resilience,
        remove_session_machine=deps.state_machine_manager.remove_session_machine,
        label_manager=deps.label_manager,
        send_to_session_fn=lambda name, text: deps.session_manager.runner.send_to_session_by_name(name, text),
        board_snapshot_provider=board_snapshot_provider,
        agent_callback_endpoint=deps.agent_callback_endpoint,
    )
