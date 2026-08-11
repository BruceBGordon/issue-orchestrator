"""Composition of the completion pipeline.

Extracted from ``bootstrap`` so the composition root stays navigable —
same split as ``bootstrap_tech_lead``. Owns construction of the
completion processor and the session controller, including the
collaborators they share.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..execution.git_working_copy import GitWorkingCopy
from ..execution.command_runner import LocalCommandRunner
from ..execution.review_artifact_reader import ManifestReviewArtifactReader
from ..execution.session_output_adapter import FileSystemSessionOutput
from ..infra import runtime_identity
from ..control.completion_ports import LabelAdapter, PRAdapter
from ..infra.config import Config
from ..ports import EventSink
from ..ports.coder_prompt import (
    CoderPromptAddendumProvider,
    NO_CODER_PROMPT_ADDENDUM,
)

if TYPE_CHECKING:
    from ..control.needs_human_block import SharedNeedsHumanBlock
    from ..control.open_issue_corpus import OpenIssueCorpusManager
    from ..ports.completion_handler_factory import CompletionHandlerFactory
    from ..ports.repository_host import RepositoryHost
    from ..ports.session_output import SessionOutput
    from ..domain.attempt import AttemptKey
    from ..domain.issue_key import IssueKey
    from ..ports.validation_attempt_key_factory import ValidationAttemptKeyFactory
    from ..control.completion_processor import CompletionProcessor
    from ..control.label_manager import LabelManager
    from ..control.session_controller import SessionController
    from ..control.provider_resilience import ProviderResilienceManager
    from ..ports.turn_mailbox import TurnMailbox
    from ..ports.agent_callback_endpoint import AgentCallbackEndpoint
    from ..ports.attempt_store import AttemptStore
    from ..control.background_job_supervisor import BackgroundJobSupervisor
    from ..execution.persistent_exchange_pair_registry_inmemory import (
        InMemoryPersistentExchangePairRegistry,
    )
    from ..ports.tech_lead_authority import TechLeadAuthorityStore


class CompletionRepositoryPorts(LabelAdapter, PRAdapter, Protocol):
    """What the completion pipeline needs from the repository host.

    The composition root passes a ``GitHubAdapter``, but this module
    only ever uses it as these two ports — so it depends on them rather
    than the concrete adapter, and the entrypoints-use-protocols
    guardrail holds without an exemption.
    """


def _validation_junit_xml_paths(config: Config) -> tuple[str, ...]:
    from ..infra.validation_junit_paths import configured_validation_junit_xml_paths

    return configured_validation_junit_xml_paths(config)


class _IssueKeyValidationAttemptKeyFactory:
    """Derives validation attempt identity from a stable issue key."""

    def for_validation_attempt(
        self,
        *,
        issue_key: "IssueKey",
        head_sha: str,
    ) -> "AttemptKey":
        from ..domain.attempt import AttemptKey

        return AttemptKey(issue_key, head_sha)


def _validation_attempt_key_factory(
    config: Config,
) -> "ValidationAttemptKeyFactory":
    _ = config
    return _IssueKeyValidationAttemptKeyFactory()


def create_completion_components(
    config: Config,
    github: "CompletionRepositoryPorts | None",
    events: EventSink,
    working_copy: GitWorkingCopy,
    session_output: FileSystemSessionOutput,
    command_runner: LocalCommandRunner,
    provider_resilience: ProviderResilienceManager | None = None,
    label_manager: "LabelManager | None" = None,
    background_job_supervisor: "BackgroundJobSupervisor | None" = None,
    pair_registry: "InMemoryPersistentExchangePairRegistry | None" = None,
    attempt_store: "AttemptStore | None" = None,
    turn_mailbox: "TurnMailbox | None" = None,
    tech_lead_authority: "TechLeadAuthorityStore | None" = None,
    open_issue_corpus: "OpenIssueCorpusManager | None" = None,
    # The completion handler needs a full repository host; ``github`` above is
    # only guaranteed to satisfy the narrower label/PR completion port, so the
    # handler factory is built only when the caller supplies the real thing.
    repository_host: "RepositoryHost | None" = None,
    *,
    # Required: the composition root owns the single shared endpoint.
    agent_callback_endpoint: "AgentCallbackEndpoint",
    # The one owner of the shared needs-human block. The agent-requested
    # NEEDS_HUMAN completion outcome routes through it, and the label adapter
    # below refuses that label by value, so the two halves cannot disagree.
    needs_human_block: "SharedNeedsHumanBlock",
    coder_prompt_addendum: CoderPromptAddendumProvider = NO_CODER_PROMPT_ADDENDUM,
) -> tuple[
    "CompletionProcessor | None",
    "SessionController | None",
    "CompletionHandlerFactory | None",
]:
    """Create the completion processor, controller and handler factory.

    One call because they are one subsystem: all three consume the same
    repository host, session output and label registry, and the facade should
    receive them assembled rather than assemble them itself (#6999 A4).
    """
    from ..control.completion_processor import CompletionProcessor
    from ..control.pre_publish_gate import PrePublishGate
    from ..control.session_controller import SessionController
    from ..control.label_manager import LabelManager as _LM
    from ..execution.run_evidence import RunEvidenceRecorder
    from ..execution.persistent_exchange_pair_registry_inmemory import (
        InMemoryPersistentExchangePairRegistry,
    )
    from ..execution.persistent_review_exchange_runner import (
        PersistentReviewExchangeRunner,
    )
    from ..control.governed_label_set import GovernedLabelSet
    from ..control.review_exchange_lifecycle import (
        ReviewExchangeCancellation,
        cancel_issue_review_exchange,
    )

    if github is None:
        # No repository host: there is no completion pipeline to build.
        return None, None, None
    if label_manager is None:
        label_manager = _LM(config)
    if pair_registry is None:
        pair_registry = InMemoryPersistentExchangePairRegistry()

    def _cancel_review_exchange(
        issue_number: int,
        reason: str,
    ) -> ReviewExchangeCancellation:
        return cancel_issue_review_exchange(
            issue_number=issue_number,
            reason=reason,
            pair_registry=pair_registry,
            job_supervisor=background_job_supervisor,
        )

    completion_processor = CompletionProcessor(
        # The governed shared block is refused here BY VALUE, so an
        # agent-supplied ``pr_labels`` entry cannot mint a cause-free block
        # (#6999 F2 round 4). The typed NEEDS_HUMAN completion outcome routes
        # through the owner instead, which is where a cause gets recorded.
        label_adapter=GovernedLabelSet(
            labels=github, governed_label=label_manager.needs_human
        ),
        pr_adapter=github,
        git_adapter=working_copy,
        session_output=session_output,
        # The review exchange delivers verdicts through the orchestrator-owned
        # mailbox: agents run `exchange-respond`, the Control API delivers into
        # the open turn slot, and send_round polls the mailbox (#6549).
        review_exchange_runner=PersistentReviewExchangeRunner(
            session_output,
            pair_registry,
            turn_mailbox=turn_mailbox,
            coder_prompt_addendum=coder_prompt_addendum,
        ),
        event_bus=None,
        label_config=label_manager.to_label_config_dict(),
        pre_publish_gate=PrePublishGate(command_runner) if config.enforce_hooks else None,
        config=config,
        background_job_supervisor=background_job_supervisor,
        agent_callback_endpoint=agent_callback_endpoint,
        review_exchange_canceller=_cancel_review_exchange,
        review_artifact_reader=ManifestReviewArtifactReader(),
        runtime_identity=runtime_identity.resolve_runtime_identity(),
        tech_lead_authority=tech_lead_authority,
        needs_human_block=needs_human_block,
    )

    session_controller_instance = SessionController(
        completion_processor=completion_processor,
        events=events,
        session_output=session_output,
        working_copy=working_copy,
        command_runner=command_runner if config.validation.quick.cmd else None,
        validation_cmd=config.validation.quick.cmd,
        validation_timeout_seconds=config.validation.quick.timeout_seconds,
        validation_junit_xml_paths=_validation_junit_xml_paths(config),
        validation_evidence_recorder=RunEvidenceRecorder(session_output),
        attempt_store=attempt_store,
        validation_attempt_key_factory=_validation_attempt_key_factory(config),
        max_validation_retries=config.retry.max_validation_retries,
        review_exchange_canceller=_cancel_review_exchange,
    )

    completion_handler_factory = (
        build_completion_handler_factory(
            config,
            events=events,
            repository_host=repository_host,
            session_output=session_output,
            tech_lead_authority=tech_lead_authority,
            open_issue_corpus=open_issue_corpus,
            label_manager=label_manager,
            provider_resilience=provider_resilience,
        )
        if repository_host is not None
        and tech_lead_authority is not None
        and open_issue_corpus is not None
        and provider_resilience is not None
        else None
    )
    return (
        completion_processor,
        session_controller_instance,
        completion_handler_factory,
    )


def build_completion_handler_factory(
    config: Config,
    *,
    events: EventSink,
    repository_host: "RepositoryHost",
    session_output: "SessionOutput",
    tech_lead_authority: "TechLeadAuthorityStore",
    open_issue_corpus: "OpenIssueCorpusManager",
    label_manager: "LabelManager",
    provider_resilience: ProviderResilienceManager,
) -> "CompletionHandlerFactory":
    """Implement ``ports.completion_handler_factory.CompletionHandlerFactory``.

    Closes over the application dependencies; the facade passes only its own
    runtime state (#6999 A4).
    """
    from ..control.completion_handler import CompletionHandler
    from ..control.provider_availability import ProviderAvailabilityPolicy

    # Completion never applies the provider-blocked label itself; it asks this
    # owner for the transition that carries the durable issue-scoped record
    # with it (#6999 F5/A2).
    provider_availability = ProviderAvailabilityPolicy(
        config, provider_resilience, label_manager
    )

    def factory(*, state_machines, active_sessions):
        from ..control.active_sessions import active_session_run_id

        return CompletionHandler(
            config,
            events,
            repository_host,
            lambda issue: state_machines.issue_machines.get(issue.number),
            lambda name: state_machines.session_machines.get(name),
            lambda pr_number: state_machines.review_machines.get(pr_number),
            session_output,
            tech_lead_authority,
            open_issue_corpus,
            lambda n: active_session_run_id(active_sessions(), n),
            provider_availability,
            remove_session_machine_fn=state_machines.remove_session_machine,
            label_manager=label_manager,
        )

    return factory
