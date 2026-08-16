"""Composition-root assembly of the session launcher.

Implements ``ports.session_launcher_factory.SessionLauncherFactory`` by
closing over the application dependencies the composition root already
built. The orchestrator facade then supplies only its own callbacks
rather than handing the whole bundle across a layer boundary
(#6924 A3-R2).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ..control.needs_human_block import SharedNeedsHumanBlock
from ..control.session_launcher import SessionLauncher
from ..ports.coder_prompt import (
    CoderPromptAddendumProvider,
    NO_CODER_PROMPT_ADDENDUM,
)
from ..ports.provider_readiness import ProviderReadinessProbe

if TYPE_CHECKING:
    from ..control.dependency_evaluator import DependencyEvaluator
    from ..domain.state_machines.issue_machine import IssueStateMachine
    from ..domain.state_machines.review_machine import ReviewStateMachine
    from ..domain.state_machines.session_machine import SessionStateMachine
    from ..infra.config import Config
    from ..ports.agent_callback_endpoint import AgentCallbackEndpoint
    from ..ports.board_snapshot_provider import BoardSnapshotProvider
    from ..ports.issue import Issue as IssueProtocol
    from ..ports.session_launcher_factory import SessionLauncherFactory


def build_session_launcher_factory(
    *,
    config: "Config",
    events,
    repository_host,
    action_applier,
    session_manager,
    worktree_manager,
    working_copy,
    command_runner,
    session_output,
    manifest_downloader,
    tech_lead_authority,
    claim_manager,
    provider_resilience,
    state_machine_manager,
    label_manager,
    agent_callback_endpoint: "AgentCallbackEndpoint",
    provider_readiness_probe: ProviderReadinessProbe,
    needs_human_block: SharedNeedsHumanBlock,
    coder_prompt_addendum: CoderPromptAddendumProvider = NO_CODER_PROMPT_ADDENDUM,
) -> "SessionLauncherFactory":
    """Bind the application dependencies; return the facade-facing factory."""

    def _factory(
        *,
        board_snapshot_provider: "BoardSnapshotProvider",
        session_exists_fn: Callable[[str], bool],
        create_session_fn: Callable[[str, str, Path, str | None], bool],
        get_issue_machine: Callable[
            ["IssueProtocol"], Optional["IssueStateMachine"]
        ],
        get_session_machine: Callable[
            [str, int, int], Optional["SessionStateMachine"]
        ],
        get_review_machine: Callable[[int, int], Optional["ReviewStateMachine"]],
        refresh_issue_fn: Optional[Callable[[int], Optional["IssueProtocol"]]],
        dependency_evaluator: Optional["DependencyEvaluator"],
    ) -> SessionLauncher:
        return SessionLauncher(
            config, events, repository_host, action_applier, session_manager,
            worktree_manager, working_copy, command_runner, session_output,
            manifest_downloader, tech_lead_authority,
            session_exists_fn,
            create_session_fn, get_issue_machine, get_session_machine,
            get_review_machine, refresh_issue_fn, dependency_evaluator,
            claim_manager=claim_manager,
            provider_resilience=provider_resilience,
            remove_session_machine=state_machine_manager.remove_session_machine,
            label_manager=label_manager,
            send_to_session_fn=lambda name, text: (
                session_manager.runner.send_to_session_by_name(name, text)
            ),
            board_snapshot_provider=board_snapshot_provider,
            agent_callback_endpoint=agent_callback_endpoint,
            provider_readiness_probe=provider_readiness_probe,
            needs_human_block=needs_human_block,
            coder_prompt_addendum=coder_prompt_addendum,
        )

    return _factory
