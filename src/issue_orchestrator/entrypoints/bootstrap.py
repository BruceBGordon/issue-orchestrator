"""Composition root for the issue orchestrator.

This module is the ONLY place where dependencies are wired together.
It creates concrete adapters and injects them into the orchestrator.

This is the "app layer" that knows about all concrete implementations
but keeps that knowledge out of the core (orchestrator).

Principle: The orchestrator core imports only Protocols (ports).
           This module imports concrete implementations (adapters).

Principle: "No Nulls in Orchestrator"
           - Bootstrap is the single source of truth for choosing implementations
           - Orchestrator has no Optional deps, no Null defaults
           - Tests explicitly pass fakes/nulls
"""

import logging
import os
import sys
import time
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from ..control.background_job_supervisor import BackgroundJobSupervisor
from ..infra.agent_callback_endpoint import RuntimeAgentCallbackEndpoint
from .bootstrap_provider import (
    build_provider_circuit_store,
    build_provider_launch_sampler,
    build_provider_readiness_probe,
    build_provider_resilience,
)
from .bootstrap_claims import ClaimComponents, assemble_claim_components, lease_config_from
from .bootstrap_pair_registry import build_pair_registry_with_worktree_hook
from .bootstrap_pending_work import (
    build_pending_work_wiring,
    require_repository_host,
)
from .bootstrap_session_launcher import build_session_launcher_factory
from .bootstrap_operator_commands import build_operator_issue_command_factory
from .bootstrap_completion import (
    _validation_attempt_key_factory,
    build_completion_handler_factory,
    create_completion_components,
)
from ..infra.config import Config
from ..infra.env import ENV_PREFIX
from ..adapters.github.repo import get_repo_from_git, GitRepoError
from ..ports.event_sink import EventSink, NullEventSink
from ..ports.issue_tracker import IssueTracker
from ..ports.session_runner import SessionRunner, NullSessionRunner
from ..ports.timeline_reader import NullTimelineReader
from ..ports.timeline_store import NullTimelineStore, TimelineStore
from ..ports.timeline_writer import NullTimelineWriter
from ..control.orchestrator_deps import OrchestratorDeps
from ..control.provider_resilience import ProviderResilienceManager
from ..execution import (
    create_plugin_manager,
    PluggyEventSink,
    PluggySessionRunner,
    LifecycleSSEPlugin,
    GitHubAdapter,
    CompositeEventSink,
    SqliteGoalPilotStore,
    QueueCacheStore,
    TimelineEventSink,
    DefaultTimelineReader,
    SqliteTimelineStore,
    TimelineStoreConfig,
    DefaultTimelineWriter,
)
from ..execution.gh_guard import install_gh_guard
from ..events import EventHub, SequencedEventSink
from ..control import (
    Planner,
    Scheduler,
    SessionManager,
    LabelSync,
)
from ..control.action_applier import ActionApplier
from ..control.governed_label_set import GovernedLabelSet
from ..control.fact_gatherer import FactGatherer
from ..control.health_gate import HealthGate
from ..adapters.github import GitHubAuth, GitHubIssueResolver, GitHubCache, build_github_auth
from ..adapters.github.ref_claim_adapter import (
    GitHubRefClaimAdapter,
    GitHubRefRunLedgerAdapter,
)
from ..execution.verification_service import DefaultVerificationService
from ..ports.verification import VerificationBudget
from ..execution.worktree_adapter import GitWorktreeManager
from ..execution.git_working_copy import GitWorkingCopy
from ..execution.command_runner import LocalCommandRunner
from ..ports.provider_readiness import (
    NO_PROVIDER_READINESS_PROBE,
    ProviderReadinessProbe,
)
from ..execution.session_output_adapter import FileSystemSessionOutput
from ..execution.review_artifact_reader import ManifestReviewArtifactReader
from ..execution.thread_background_job_runner import ThreadBackgroundJobRunner
from ..control.completion_dispatcher import (
    BackgroundCompletionDispatcher,
    SynchronousCompletionDispatcher,
)
from ..control.dependency_evaluator import DependencyEvaluator
from ..control.workflows import ReviewWorkflow, RetrospectiveReviewWorkflow, ReworkWorkflow, TechLeadWorkflow
from ..control.worktree_manager import extract_issue_branches
from ..infra import gh_audit, runtime_identity
from .bootstrap_tech_lead import (
    create_board_snapshot_builder,
    create_tech_lead_composition,
    wire_tech_lead_act_executors,
)
from ..infra.repo_identity import state_dir
from ..infra.secret_env import (
    configure_extra_forbidden_env_vars,
)
from ..control.tech_lead_run_ownership import TechLeadRunOwnership
from ..ports.claim_manager import ClaimManager, NullClaimManager
from ..ports.run_ledger_store import SingleInstanceRunLedgerStore
from ..domain.lease_config import LeaseConfig

if TYPE_CHECKING:
    from ..ports.label_set import LabelSet
    from ..control.label_manager import LabelManager
    from ..infra.orchestrator import Orchestrator
    from ..ports.attempt_store import AttemptStore
    from ..control.pr_scanner import PRScanner
    from ..control.session_restorer import SessionRestorer
    from ..control.completion_processor import CompletionProcessor
    from ..control.publish_recovery import PublishRecoveryService
    from ..control.session_controller import SessionController
    from ..adapters.github.fresh_issue_reader import GitHubFreshIssueReader
    from ..ports.fresh_issue_reader import FreshIssueReader
    from ..ports.e2e_issue_tracker import E2EIssueTracker
    from ..ports.attempt_store import AttemptStore
    from ..ports.tech_lead_authority import TechLeadAuthorityStore

logger = logging.getLogger(__name__)


ISSUE_ORCHESTRATOR_PYTHON_ENV = "ISSUE_ORCHESTRATOR_PYTHON"


def export_orchestrator_python() -> None:
    """Publish the orchestrator's interpreter path for child subprocesses.

    The pre-push hook installed in every target-repo worktree calls a helper
    from the ``issue_orchestrator`` package, but a Kotlin/Node/etc. target
    has no venv where the package is importable. This exports
    ``ISSUE_ORCHESTRATOR_PYTHON=sys.executable`` so any subprocess the
    orchestrator spawns (``git push`` and its hooks, most importantly) can
    resolve a Python that *can* import us.

    Uses ``setdefault`` so operator overrides (tests, dev envs that point
    at a different interpreter) are never clobbered.
    """
    os.environ.setdefault(ISSUE_ORCHESTRATOR_PYTHON_ENV, sys.executable)


def _resolve_repo(config: Config) -> str | None:
    """Resolve repo name from config or auto-detect from git remote."""
    repo = config.repo
    if not repo:
        try:
            repo = get_repo_from_git()
            logger.info("Auto-detected repository from git remote: %s", repo)
            config.repo = repo
        except GitRepoError as e:
            logger.warning("Could not auto-detect repository: %s", e)
            repo = None
    return repo


def _create_github_auth(repo: str, config: Config) -> GitHubAuth:
    """Create the shared GitHub auth owner for API and git transport."""
    return build_github_auth(
        **config.github_auth_kwargs(),
        repo=repo,
        api_url=config.github_api_url,
        timeout_seconds=float(config.github_http_timeout_seconds),
    )


def _create_github_adapter(repo: str, config: Config, auth: GitHubAuth) -> GitHubAdapter:
    """Create GitHub adapter with cache and verification service."""
    cache_ttl = float(max(0, getattr(config, "fetch_layer_network_sync_seconds", 0)))
    github_cache = GitHubCache(default_ttl=cache_ttl)

    default_budget = VerificationBudget(
        timeout_seconds=config.gh_write_verify_timeout_seconds,
        max_attempts=20,
        initial_delay_ms=config.gh_write_verify_initial_delay_ms,
        max_delay_ms=config.gh_write_verify_max_delay_ms,
        backoff_factor=config.gh_write_verify_backoff,
        jitter_ms=config.gh_write_verify_jitter_ms,
    )
    verification_service = DefaultVerificationService(default_budget=default_budget)

    return GitHubAdapter(
        repo,
        config=config,
        cache=github_cache,
        verification_service=verification_service,
        auth=auth,
    )


def _setup_event_sinks(
    base_events: PluggyEventSink,
    github: GitHubAdapter | None,
    *extra_sinks: EventSink,
) -> tuple[EventSink, EventHub | None]:
    """Set up event sinks and event hub."""
    event_hub = EventHub() if github else None
    sinks: list[EventSink] = [base_events]
    if event_hub:
        sinks.append(event_hub)
    sinks.extend(extra_sinks)
    if len(sinks) > 1:
        events = CompositeEventSink(*sinks)
    else:
        events = base_events
    events = SequencedEventSink(events)
    return events, event_hub


def _configure_gh_audit(
    config: Config,
    events: EventSink,
    github: GitHubAdapter | None,
) -> None:
    """Configure GitHub audit logging."""
    gh_audit.set_event_sink(events)
    if github:
        gh_audit.set_rate_limit_fetcher(github.get_rate_limit_snapshot)
    gh_audit.configure(
        enabled=config.gh_audit_enabled,
        include_events=config.gh_audit_events,
        audit_path=config.gh_audit_file,
    )
    gh_audit.configure_rate_limit(
        every_calls=config.gh_rate_limit_every_calls,
        warn_fraction=config.gh_rate_limit_warn_fraction,
        warn_remaining=config.gh_rate_limit_warn_remaining,
    )
    if config.gh_rate_limit_startup:
        rl_start = time.time()
        gh_audit.check_rate_limit("startup")
        logger.info(
            "[STARTUP_TIMING] phase=gh_rate_limit_probe elapsed=%.3fs",
            time.time() - rl_start,
        )


def _create_claim_components(
    config: Config,
    github: GitHubAdapter | None,
    events: EventSink,
    io_claimed_label: str = "io:claimed",
) -> ClaimComponents:
    """Choose both coordination stores from one deployment setting."""
    lease = lease_config_from(config) if github and config.claims.enabled else LeaseConfig()
    if github and config.claims.enabled:
        claimant_id = config.claims.claimant_id or f"orchestrator-{os.getpid()}"
        manager = GitHubRefClaimAdapter(
            client=github.http_client, claimant_id=claimant_id, config=lease,
            events=events, label_adapter=github, io_claimed_label=io_claimed_label,
        )
        ledger = GitHubRefRunLedgerAdapter(
            client=github.http_client, claimant_id=claimant_id, config=lease,
        )
        logger.info("Claims enabled: claimant_id=%s, lease=%ds", claimant_id, lease.lease_seconds)
    else:
        manager = NullClaimManager()
        ledger = SingleInstanceRunLedgerStore(lease_seconds=lease.lease_seconds)
        logger.info(
            "Claims disabled: running in single-orchestrator mode. "
            "Multi-machine coordination is OFF. To enable, set claims.enabled=true in config."
        )
    return assemble_claim_components(manager, ledger, lease, events)


def _create_planner(
    config: Config,
    github: GitHubAdapter | None,
    events: EventSink,
    provider_resilience: ProviderResilienceManager | None = None,
    label_manager: "LabelManager | None" = None,
) -> tuple[Planner, Scheduler, DependencyEvaluator | None, LabelSync | None]:
    """Create planner and supporting control plane components."""
    issue_resolver = None
    if github and config.repo:
        issue_resolver = GitHubIssueResolver(
            repo=config.repo,
            issue_tracker=github,
            events=events,
        )

    predecessor_facts_provider = None
    if github:
        from ..control.label_manager import LabelManager
        from ..execution.stack_predecessor_facts import GitStackPredecessorFactsProvider

        predecessor_facts_provider = GitStackPredecessorFactsProvider(
            github, label_manager or LabelManager(config), repo=config.repo,
        )

    dependency_evaluator = DependencyEvaluator(
        issue_checker=github,
        events=events,
        issue_resolver=issue_resolver,
        repo=config.repo,
        foundation_milestone=config.foundation_milestone,
        predecessor_facts_provider=predecessor_facts_provider,
    ) if github else None

    scheduler = Scheduler(config=config, dependency_evaluator=dependency_evaluator)

    # The governed block is refused BY VALUE here, so a computed sync
    # collection cannot smuggle it past its owner (#6999 F2 round 4).
    label_sync = LabelSync(
        labels=GovernedLabelSet(labels=github, governed_label=label_manager.needs_human),
        events=events, pr_tracker=github, label_manager=label_manager,
    ) if github and label_manager else None

    review_workflow = ReviewWorkflow(config=config, events=events)
    retrospective_review_workflow = RetrospectiveReviewWorkflow(config=config, events=events)
    rework_workflow = ReworkWorkflow(config=config, events=events, label_manager=label_manager)
    tech_lead_workflow = TechLeadWorkflow(config=config, events=events)

    planner = Planner(
        config=config,
        scheduler=scheduler,
        dependency_evaluator=dependency_evaluator,
        review_workflow=review_workflow,
        retrospective_review_workflow=retrospective_review_workflow,
        rework_workflow=rework_workflow,
        tech_lead_workflow=tech_lead_workflow,
        provider_resilience=provider_resilience,
        label_manager=label_manager,
    )
    return planner, scheduler, dependency_evaluator, label_sync


def _create_io_adapters(github_auth: GitHubAuth | None = None) -> tuple[
    GitWorktreeManager,
    GitWorkingCopy,
    LocalCommandRunner,
    FileSystemSessionOutput,
]:
    """Create IO adapter instances."""
    return (
        GitWorktreeManager(),
        GitWorkingCopy(git_auth=github_auth),
        LocalCommandRunner(),
        FileSystemSessionOutput(),
    )


def create_attempt_store(config: Config) -> "AttemptStore":
    """Create the attempt store for this repository."""
    from ..adapters.sidecar_attempt_store import SidecarAttemptStore

    return SidecarAttemptStore(config.repo_root)


def _wire_stack_publish_gate(
    completion_processor: "CompletionProcessor | None",
    dependency_evaluator: DependencyEvaluator | None,
    github: GitHubAdapter | None,
    command_runner: "LocalCommandRunner",
    config: Config,
) -> None:
    """Wire the stack publish-gate + branch ancestry (ADR-0029 / #6596).

    Attaches the git ancestry checker to the single dependency-gate evaluator
    and gives the completion processor a :class:`StackPublishGate` so a
    Stack-after: successor's PR is based on its predecessor branch and a blocked
    publish gate fails fast. A no-op when any collaborator is absent, so
    non-stack deployments and tests keep prior behavior.
    """
    if completion_processor is None or dependency_evaluator is None or github is None:
        return
    from ..control.stack_publish_gate import StackBaseGate
    from ..execution.stack_branch_ancestry import GitStackBranchAncestry

    dependency_evaluator.attach_branch_ancestry(GitStackBranchAncestry(command_runner))
    completion_processor.attach_stack_publish_gate(
        StackBaseGate(
            evaluator=dependency_evaluator,
            issue_reader=github,
            configured_base_branch=config.worktree_base_branch_override,
        )
    )


def _build_publish_recovery(
    *,
    repository_host: "GitHubAdapter",
    completion_processor: "CompletionProcessor",
    label_manager: "LabelManager",
    fresh_issue_reader: "FreshIssueReader",
    action_applier: "ActionApplier",
    config: Config,
    tech_lead_authority: "TechLeadAuthorityStore",
) -> "PublishRecoveryService":
    """Wire the "Retry publish" owner: durable locator store + dedicated runner.

    The republish runs on its own :class:`ThreadBackgroundJobRunner` (drained by
    ``PublishRecoveryService.drain_completed_retries`` each tick), NOT the shared
    completion/review-exchange runners — those are drained by other owners and
    would steal or drop republish results.
    """
    from ..control.publish_recovery import PublishRecoveryService
    from ..execution.json_publish_retry_locator_store import (
        JsonPublishRetryLocatorStore,
    )

    locator_store = JsonPublishRetryLocatorStore(
        state_dir(config.repo_root) / "publish_retry_locators.json"
    )
    return PublishRecoveryService(
        repository_host=repository_host,
        completion_processor=completion_processor,
        locator_store=locator_store,
        runner=ThreadBackgroundJobRunner(),
        label_manager=label_manager,
        fresh_issue_reader=fresh_issue_reader,
        action_applier=action_applier,
        code_review_agent_configured=bool(config.code_review_agent),
        tech_lead_authority=tech_lead_authority,
    )


def _validate_required_deps(
    github: GitHubAdapter | None,
    event_hub: EventHub | None,
    planner: Planner | None,
    session_manager: SessionManager | None,
    label_sync: LabelSync | None,
    action_applier: ActionApplier | None,
    fact_gatherer: FactGatherer | None,
    pr_scanner: "PRScanner | None",
    session_restorer: "SessionRestorer | None",
    completion_processor: "CompletionProcessor | None",
    session_controller_instance: "SessionController | None",
    fresh_issue_reader: "GitHubFreshIssueReader | None",
    e2e_issue_tracker: "E2EIssueTracker | None",
) -> None:
    """Validate all required dependencies are present."""
    # GitHub requires special error message
    require_repository_host(github)
    # Check all other required deps with a data-driven approach
    deps_to_check = [
        (event_hub, "EventHub"),
        (planner, "Planner"),
        (session_manager, "SessionManager"),
        (label_sync, "LabelSync"),
        (action_applier, "ActionApplier"),
        (fact_gatherer, "FactGatherer"),
        (pr_scanner, "PRScanner"),
        (session_restorer, "SessionRestorer"),
        (completion_processor, "CompletionProcessor"),
        (session_controller_instance, "SessionController"),
        (fresh_issue_reader, "FreshIssueReader"),
        (e2e_issue_tracker, "E2EIssueTracker"),
    ]
    for dep, name in deps_to_check:
        if dep is None:
            raise ValueError(f"{name} is required")


class Dependencies:
    """Container for all injected dependencies.

    This keeps the orchestrator constructor signature clean by bundling
    all dependencies into a single object.
    """

    def __init__(
        self,
        events: EventSink,
        runner: SessionRunner,
        github: GitHubAdapter | None = None,
    ):
        self.events = events
        self.runner = runner
        self.github = github


def build_orchestrator(
    config: Config,
    enable_ipc: bool = True,
    enable_sse: bool = True,
) -> "Orchestrator":
    """Build a fully-wired orchestrator with all dependencies.

    This is the composition root - the only place that knows about
    concrete implementations.

    Args:
        config: Application configuration
        enable_ipc: Whether to enable IPC event broadcasting
        enable_sse: Whether to enable SSE event broadcasting

    Returns:
        Fully configured Orchestrator instance
    """
    from ..infra.orchestrator import Orchestrator
    from ..control.pr_scanner import PRScanner
    from ..control.session_restorer import SessionRestorer
    from ..control.state_machine_manager import StateMachineManager
    from ..adapters.github.fresh_issue_reader import GitHubFreshIssueReader
    from ..execution.tech_lead_downloader import TechLeadDownloader
    from ..execution.e2e_issue_tracker_adapter import GitHubE2EIssueTracker

    install_gh_guard()

    # Make repo root visible to terminal plugins.
    os.environ[f"{ENV_PREFIX}REPO_ROOT"] = str(config.repo_root)
    configure_extra_forbidden_env_vars([config.github_app_private_key_env])

    # TODO(env-scope): if a future use-case needs a *different* Python for
    # a specific subprocess (e.g. an adapter that must invoke a target
    # repo's own interpreter), consider narrowing this global export to a
    # per-subprocess env injection at the git/push call site. For now one
    # global export covers every consumer cleanly.
    export_orchestrator_python()

    # Create the pluggy plugin manager and register SSE plugin
    pm = create_plugin_manager(
        terminal_plugin=config.terminal_adapter,
        ui_mode=config.ui_mode,
        session_interactions_enabled=config.session_interactions.enabled,
        worktree_base=config.worktree_base,
    )
    if enable_sse:
        try:
            pm.register(LifecycleSSEPlugin(), name="lifecycle_sse")
            logger.info("SSE lifecycle plugin registered")
        except Exception as e:
            logger.warning("Failed to register SSE plugin: %s", e)

    # Create port adapters
    base_events = PluggyEventSink(pm)
    runner = PluggySessionRunner(pm)

    # Timeline store + reader/writer + event sink
    # instance_id uniquely identifies this orchestrator process lifetime.
    # Used to scope timeline queries (e.g. E2E run detail views).
    instance_id = str(uuid4())
    logger.info("Orchestrator instance_id=%s", instance_id)
    timeline_store = SqliteTimelineStore(
        state_dir(config.repo_root) / "timeline.sqlite",
        TimelineStoreConfig(max_records=config.timeline.max_records),
        instance_id=instance_id,
    )
    timeline_reader = DefaultTimelineReader(timeline_store)
    timeline_writer = DefaultTimelineWriter(timeline_store)
    timeline_sink = TimelineEventSink(timeline_writer)

    # Resolve repo and create GitHub adapter
    repo = _resolve_repo(config)
    github_auth = _create_github_auth(repo, config) if repo else None
    github = _create_github_adapter(repo, config, github_auth) if repo and github_auth else None

    # Set up event sinks
    events, event_hub = _setup_event_sinks(base_events, github, timeline_sink)

    # Configure GitHub audit logging
    _configure_gh_audit(config, events, github)
    if github:
        _check_github_token_scopes(config, github)

    # Create label manager (shared instance for all control-layer components)
    from ..control.label_manager import LabelManager as _LabelManager
    label_manager = _LabelManager(config)

    # Create claim management components
    claim_gate, lease_renewer, _lease_config, claim_manager, run_ownership = _create_claim_components(
        config, github, events, io_claimed_label=label_manager.io_claimed,
    )

    queue_cache_store = QueueCacheStore(
        state_dir(config.repo_root) / "queue_cache.sqlite"
    )
    provider_resilience = build_provider_resilience(
        config, events, build_provider_circuit_store(state_dir(config.repo_root))
    )

    # Create IO adapters
    worktree_manager, working_copy, command_runner, session_output = _create_io_adapters(github_auth)

    provider_readiness_probe = build_provider_readiness_probe(command_runner)
    provider_launch_sampler = build_provider_launch_sampler(
        config, provider_resilience, provider_readiness_probe, label_manager
    )

    # Create planner and control plane components
    planner, _scheduler, _dependency_evaluator, label_sync = _create_planner(config, github, events, provider_resilience, label_manager=label_manager)
    session_manager = SessionManager(runner=runner, events=events, config=config)

    goal_pilot_store = SqliteGoalPilotStore(repo_root=config.repo_root)
    attempt_store = create_attempt_store(config)

    # Create manifest downloader for tech_lead sessions
    manifest_downloader = TechLeadDownloader(
        repository_host=github,
        command_runner=command_runner,
    ) if github else None

    # Create cache-bypassing reader
    fresh_issue_reader = GitHubFreshIssueReader(repo=config.repo, config=config) if github else None

    e2e_issue_tracker = GitHubE2EIssueTracker(github.http_client) if github else None

    # Every label writer EXCEPT the shared-block owner gets a capability that
    # refuses the governed label by value (#6999 F2 round 4): a check the caller
    # never received cannot be routed around.
    governed_labels = GovernedLabelSet(
        labels=github, governed_label=label_manager.needs_human
    ) if github else None

    # Create action applier (IO boundary)
    action_applier = ActionApplier(
        # ``github`` is None only on the no-repository path, which fails with a
        # ValueError further down; the applier is never used before then.
        labels=cast("LabelSet", governed_labels),
        sessions=session_manager,
        events=events,
        repository_host=github,
        worktree_manager=worktree_manager,
        fresh_issue_reader=fresh_issue_reader,
        label_manager=label_manager,
        reconcile=True,
        # A whole-repository anchor must be RESERVED before it is created, so
        # the applier that owns the create owns the reservation too (#6994).
        run_ownership=run_ownership,
    ) if github else None

    tech_lead = create_tech_lead_composition(
        config, github, events, queue_cache_store=queue_cache_store,
        provider_resilience=provider_resilience,
    )
    tech_lead_authority = tech_lead.authority
    tech_lead_board_publisher = tech_lead.board_publisher
    fact_gatherer = tech_lead.fact_gatherer

    # Create PR scanner and session restorer
    pr_scanner = (
        PRScanner(
            config=config,
            repository=github,
            events=events,
            issue_branches_fn=lambda: extract_issue_branches(working_copy, config.repo_root),
        )
        if github
        else None
    )
    session_restorer = SessionRestorer(
        config=config,
        repository_host=github,
        working_copy=working_copy,
        tech_lead_authority=tech_lead_authority,
    ) if github else None

    # Create state machine manager
    state_machine_manager = StateMachineManager(config=config)

    # Background job runner keeps long review-exchange work off the main tick.
    # One runner + one supervisor for the whole process: the supervisor drains
    # completions on each tick so failures cannot silently resubmit forever.
    # TODO(concurrency): no cap on in-flight jobs — N deferring issues spawn
    # N concurrent subprocesses. Add a bounded executor if this becomes a real
    # load issue.
    background_job_runner = ThreadBackgroundJobRunner()
    background_job_supervisor = BackgroundJobSupervisor(background_job_runner)

    # The persistent exchange pair registry is process-scoped: one
    # registry for the orchestrator's lifetime, shared across every
    # review exchange. Built here (above completion components and
    # InfraServices) so the shutdown / reset / escalation paths can
    # reach it through ``deps.pair_registry``.
    pair_registry = build_pair_registry_with_worktree_hook()

    # Process-scoped rendezvous between the exchange worker thread and the
    # agent-facing ``exchange-respond`` Control API handler. One instance,
    # shared by the runner (which opens/awaits slots) and InfraServices
    # (which the Control API reads to deliver verdicts).
    from ..execution.review_exchange_turn_mailbox import InMemoryTurnMailbox
    turn_mailbox = InMemoryTurnMailbox()

    # One instance per orchestrator, shared by everything that needs to
    # know where agents can call back: the review exchange, the session
    # launcher, and the server-started hook that publishes the bound
    # port into it (#6924).
    agent_callback_endpoint = RuntimeAgentCallbackEndpoint()


    # Built here, before the completion pipeline, because that pipeline needs
    # the shared-block owner: the agent's typed needs_human outcome routes
    # through it (#6999 F2 round 4).
    repository_host = require_repository_host(github)
    pending_work = build_pending_work_wiring(
        repo_root=config.repo_root,
        repository_host=repository_host,
        action_applier=cast("ActionApplier", action_applier),
        label_writer=repository_host,
        label_manager=label_manager, events=events)

    completion_processor, session_controller_instance, completion_handler_factory = create_completion_components(
        config, github, events, working_copy, session_output, command_runner, provider_resilience,
        label_manager=label_manager,
        background_job_supervisor=background_job_supervisor,
        agent_callback_endpoint=agent_callback_endpoint,
        pair_registry=pair_registry,
        attempt_store=attempt_store,
        turn_mailbox=turn_mailbox,
        tech_lead_authority=tech_lead_authority,
        tech_lead_run_activity=tech_lead.run_activity,
        open_issue_corpus=tech_lead.open_issue_corpus,
        repository_host=github,
        needs_human_block=pending_work.needs_human_block,
    )
    _wire_stack_publish_gate(
        completion_processor, _dependency_evaluator, github, command_runner, config,
    )

    # Create health gate
    health_gate = HealthGate(
        rate_limit_threshold=getattr(config, "rate_limit_warn_remaining", 100),
    )

    # Validate all dependencies are present
    _validate_required_deps(
        github, event_hub, planner, session_manager, label_sync,
        action_applier, fact_gatherer, pr_scanner, session_restorer,
        completion_processor, session_controller_instance, fresh_issue_reader,
        e2e_issue_tracker,
    )

    # Type assertions after validation (validation raises if any are None)
    assert github is not None
    assert event_hub is not None
    assert planner is not None
    assert session_manager is not None
    assert label_sync is not None
    assert action_applier is not None
    assert fact_gatherer is not None
    assert pr_scanner is not None
    assert session_restorer is not None
    assert completion_processor is not None
    assert session_controller_instance is not None
    assert completion_handler_factory is not None
    assert fresh_issue_reader is not None
    assert manifest_downloader is not None
    assert e2e_issue_tracker is not None

    publish_recovery = _build_publish_recovery(
        repository_host=github,
        completion_processor=completion_processor,
        label_manager=label_manager,
        fresh_issue_reader=fresh_issue_reader,
        action_applier=action_applier,
        config=config,
        tech_lead_authority=tech_lead_authority,
    )

    # Build infrastructure services bundle
    from ..control.infra_services import InfraServices
    from ..execution.label_store import LabelStore
    label_store = LabelStore(state_dir(config.repo_root) / "label_store.sqlite")

    # Wire post-construction collaborators into action_applier: the pair
    # registry + shared supervisor so escalation / history-reconcile /
    # STOP_SESSION boundaries terminate hidden review-exchange runtime,
    # label_store for write-through persistence, and publish_recovery so
    # issue terminal boundaries abandon publish retries (post-construction
    # because PublishRecoveryService depends on this applier).
    if action_applier is not None:
        action_applier.pair_registry = pair_registry
        action_applier.background_job_supervisor = background_job_supervisor
        action_applier.label_store = label_store
        action_applier.publish_recovery = publish_recovery

    infra_services = InfraServices(
        label_manager=label_manager,
        label_store=label_store,
        queue_cache_store=queue_cache_store,
        provider_resilience=provider_resilience,
        provider_readiness_probe=provider_readiness_probe,
        provider_launch_sampler=provider_launch_sampler,
        timeline_reader=timeline_reader,
        timeline_store=timeline_store,
        timeline_writer=timeline_writer,
        goal_pilot_store=goal_pilot_store,
        attempt_store=attempt_store,
        tech_lead_authority=tech_lead_authority,
        tech_lead_run_activity=tech_lead.run_activity,
        promotion_target=tech_lead.promotion_target,
        open_issue_corpus=tech_lead.open_issue_corpus,
        pair_registry=pair_registry,
        turn_mailbox=turn_mailbox,
        background_job_supervisor=background_job_supervisor,
        instance_id=instance_id,
        state_health_check=timeline_store.check_health,
    )

    # Bundle all dependencies into OrchestratorDeps (no nulls, no optionals)
    # Assembly of the session launcher lives here, at the composition
    # root, rather than in the facade or the control layer (#6924 A3-R2).
    session_launcher_factory = build_session_launcher_factory(
        config=config,
        events=events,
        repository_host=github,
        action_applier=action_applier,
        session_manager=session_manager,
        worktree_manager=worktree_manager,
        working_copy=working_copy,
        command_runner=command_runner,
        session_output=session_output,
        manifest_downloader=manifest_downloader,
        tech_lead_authority=tech_lead_authority,
        claim_manager=claim_manager,
        provider_resilience=provider_resilience,
        state_machine_manager=state_machine_manager,
        label_manager=label_manager,
        agent_callback_endpoint=agent_callback_endpoint,
        provider_readiness_probe=provider_readiness_probe,
        needs_human_block=pending_work.needs_human_block,
    )
    deps = OrchestratorDeps(
        events=events,
        runner=runner,
        repository_host=github,
        e2e_issue_tracker=e2e_issue_tracker,
        fresh_issue_reader=fresh_issue_reader,
        event_hub=event_hub,
        planner=planner,
        session_manager=session_manager,
        label_sync=label_sync,
        action_applier=action_applier,
        fact_gatherer=fact_gatherer,
        pr_scanner=pr_scanner,
        session_restorer=session_restorer,
        worktree_manager=worktree_manager,
        working_copy=working_copy,
        command_runner=command_runner,
        session_output=session_output,
        manifest_downloader=manifest_downloader,
        pending_work_claims=pending_work.claims,
        claim_quarantine=pending_work.quarantine,
        needs_human_block=pending_work.needs_human_block,
        state_machine_manager=state_machine_manager,
        completion_processor=completion_processor,
        session_controller=session_controller_instance,
        # Run completion decisions (publish gate + push + PR) off the tick thread
        # on a dedicated runner so a slow publish never blocks the heartbeat.
        completion_dispatcher=BackgroundCompletionDispatcher(ThreadBackgroundJobRunner()),
        health_gate=health_gate,
        agent_callback_endpoint=agent_callback_endpoint,
        session_launcher_factory=session_launcher_factory,
        completion_handler_factory=completion_handler_factory,
        operator_issue_command_factory=build_operator_issue_command_factory(
            config,
            repository_host=github,
            label_manager=label_manager,
            needs_human_block=pending_work.needs_human_block,
            fresh_issue_reader=fresh_issue_reader,
            queue_cache_store=queue_cache_store,
        ),
        board_snapshot_builder=create_board_snapshot_builder(
            config, timeline_store, tech_lead_board_publisher, working_copy
        ),
        claim_manager=claim_manager,
        claim_gate=claim_gate,
        lease_renewer=lease_renewer,
        run_ownership=run_ownership,
        publish_recovery=publish_recovery,
        services=infra_services,
    )

    orchestrator = Orchestrator(config=config, deps=deps)
    # Act-level executor wiring closes over live orchestrator state (#6764/#6778).
    wire_tech_lead_act_executors(orchestrator)
    return orchestrator


def _check_github_token_scopes(config: Config, github: GitHubAdapter) -> None:
    if getattr(github, "auth_kind", None) == "github_app":
        logger.info("Skipping OAuth scope check for GitHub App installation auth")
        return
    required = {scope.strip() for scope in (config.github_required_scopes or []) if scope.strip()}
    allowed = {scope.strip() for scope in (config.github_allowed_scopes or []) if scope.strip()}
    try:
        scopes = set(github.get_token_scopes())
    except Exception as exc:
        logger.warning("Failed to fetch GitHub token scopes: %s", exc)
        return

    if required and not required.issubset(scopes):
        missing = sorted(required - scopes)
        raise ValueError(f"GitHub token missing required scopes: {missing}")

    if allowed and not scopes.issubset(allowed):
        extra = sorted(scopes - allowed)
        raise ValueError(f"GitHub token has disallowed scopes: {extra}")

    if scopes:
        logger.info("GitHub token scopes: %s", ", ".join(sorted(scopes)))
    else:
        logger.info("GitHub token scopes unavailable (fine-grained token or missing header)")


def build_orchestrator_for_testing(
    config: Config,
    github: GitHubAdapter,  # Required - no more hiding None
    events: EventSink | None = None,
    runner: SessionRunner | None = None,
    planner: Planner | None = None,
    session_manager: SessionManager | None = None,
    action_applier: ActionApplier | None = None,
    fact_gatherer: FactGatherer | None = None,
    claim_manager: ClaimManager | None = None,
    provider_readiness_probe: "ProviderReadinessProbe | None" = None,
    run_ownership: TechLeadRunOwnership | None = None,
) -> "Orchestrator":
    """Build an orchestrator for testing with mock dependencies.

    IMPORTANT: github (RepositoryHost) is now REQUIRED. Tests must provide
    a mock/fake GitHub adapter. This follows the "no nulls" principle -
    tests explicitly provide their fakes rather than relying on defaults.

    Args:
        config: Application configuration
        github: Mock GitHubAdapter (required - tests must provide)
        events: Mock EventSink (defaults to NullEventSink - explicit null)
        runner: Mock SessionRunner (defaults to NullSessionRunner - explicit null)
        planner: Mock Planner (defaults to creating one with no dependencies)
        session_manager: Mock SessionManager (defaults to creating one)
        action_applier: Mock ActionApplier (defaults to creating one from github)
        fact_gatherer: Mock FactGatherer (defaults to creating one from github)
        provider_readiness_probe: Fake provider-readiness port. Defaults to the
            explicit "no probe wired" reader — a test composition must never
            shell out to a real provider CLI (#6999 F6).
        run_ownership: Optional cross-engine tech-lead run owner for tests.

    Returns:
        Orchestrator configured with test dependencies
    """
    from ..infra.orchestrator import Orchestrator
    from ..ports.background_job import NullBackgroundJobRunner

    install_gh_guard()

    # Tests must explicitly pass NullEventSink/NullSessionRunner if they don't care
    # We provide sensible defaults but tests should be explicit
    events = events or NullEventSink()
    runner = runner or NullSessionRunner()
    events = SequencedEventSink(events)
    background_job_supervisor = BackgroundJobSupervisor(NullBackgroundJobRunner())

    provider_resilience = build_provider_resilience(config, events)

    # Create label manager (shared instance for all control-layer components)
    from ..control.label_manager import LabelManager as _LabelManager
    label_manager = _LabelManager(config)

    default_label_sync = None
    # Only bound when this root builds the planner; a caller-injected planner
    # carries its own evaluator, so the stack publish-gate stays unwired there.
    _dependency_evaluator: DependencyEvaluator | None = None

    # Create adapters for IO operations
    worktree_manager = GitWorktreeManager()
    working_copy = GitWorkingCopy()
    command_runner = LocalCommandRunner()
    session_output = FileSystemSessionOutput()

    # A test composition must never shell out to a real provider CLI: readiness
    # defaults to the explicit "no probe wired" reader (UNKNOWN => launchable,
    # no circuit writes) and tests inject a fake when they mean to exercise it.
    provider_readiness_probe = provider_readiness_probe or NO_PROVIDER_READINESS_PROBE
    provider_launch_sampler = build_provider_launch_sampler(
        config, provider_resilience, provider_readiness_probe, label_manager
    )

    # Create default planner if not provided
    if planner is None:
        planner, _scheduler, _dependency_evaluator, default_label_sync = _create_planner(
            config=config,
            github=github,
            events=events,
            provider_resilience=provider_resilience,
            label_manager=label_manager,
        )

    # Create default session manager if not provided
    if session_manager is None:
        session_manager = SessionManager(runner=runner, events=events, config=config)
    goal_pilot_store = SqliteGoalPilotStore(repo_root=config.repo_root)
    attempt_store = create_attempt_store(config)

    from ..execution.tech_lead_downloader import TechLeadDownloader
    from unittest.mock import MagicMock
    manifest_downloader = TechLeadDownloader(
        repository_host=github,
        command_runner=command_runner,
    )

    class _TestFreshIssueReader:
        """Fallback FreshIssueReader for tests without network dependencies."""

        def __init__(self, issue_tracker: IssueTracker) -> None:
            self._issue_tracker = issue_tracker

        def read_issue_labels(self, issue_number: int) -> list[str]:
            return self._issue_tracker.get_issue_labels(issue_number)

    fresh_issue_reader = _TestFreshIssueReader(github)
    e2e_issue_tracker = MagicMock()

    # Create default action applier
    if action_applier is None:
        action_applier = ActionApplier(
            labels=GovernedLabelSet(
                labels=github, governed_label=label_manager.needs_human
            ),
            sessions=session_manager,
            events=events,
            repository_host=github,
            worktree_manager=worktree_manager,
            fresh_issue_reader=fresh_issue_reader,
            label_manager=label_manager,
            reconcile=False,  # Disable for testing by default
        )

    tech_lead = create_tech_lead_composition(config, github, events, fact_gatherer)
    tech_lead_authority_for_testing = tech_lead.authority
    tech_lead_board_publisher_for_testing = tech_lead.board_publisher
    fact_gatherer = tech_lead.fact_gatherer
    assert fact_gatherer is not None

    # Create HealthGate for testing
    health_gate = HealthGate(
        rate_limit_threshold=100,
    )

    # Create PRScanner for testing
    from ..control.pr_scanner import PRScanner
    pr_scanner = PRScanner(
        config=config,
        repository=github,
        events=events,
        issue_branches_fn=lambda: extract_issue_branches(working_copy, config.repo_root),
    )

    # Create SessionRestorer for testing
    from ..control.session_restorer import SessionRestorer
    session_restorer = SessionRestorer(
        config=config,
        repository_host=github,
        working_copy=working_copy,
        tech_lead_authority=tech_lead_authority_for_testing,
    )

    # Create StateMachineManager for testing
    from ..control.state_machine_manager import StateMachineManager
    state_machine_manager = StateMachineManager(config=config)

    # Create CompletionProcessor for testing
    from ..control.completion_processor import CompletionProcessor
    from ..control.pre_publish_gate import PrePublishGate
    from ..execution.persistent_review_exchange_runner import (
        PersistentReviewExchangeRunner,
    )
    from ..control.review_exchange_lifecycle import (
        ReviewExchangeCancellation,
        cancel_issue_review_exchange,
    )
    pair_registry_for_testing = build_pair_registry_with_worktree_hook()
    from ..execution.review_exchange_turn_mailbox import InMemoryTurnMailbox
    turn_mailbox = InMemoryTurnMailbox()

    # One instance per orchestrator, shared by everything that needs to
    # know where agents can call back: the review exchange, the session
    # launcher, and the server-started hook that publishes the bound
    # port into it (#6924).
    agent_callback_endpoint = RuntimeAgentCallbackEndpoint()
    # This composition never binds a Control API, so it answers the
    # endpoint question here. Production answers it from the CLI run
    # mode (declare) or the server-started hook (publish); without an
    # answer the launcher correctly refuses to start sessions (#6924 F7).
    agent_callback_endpoint.declare_unavailable()

    if action_applier is not None:
        action_applier.pair_registry = pair_registry_for_testing
        action_applier.background_job_supervisor = background_job_supervisor
        action_applier.tech_lead_ops = tech_lead_authority_for_testing
        action_applier.promotion_target = tech_lead.promotion_target

    def _cancel_review_exchange_for_testing(
        issue_number: int,
        reason: str,
    ) -> ReviewExchangeCancellation:
        return cancel_issue_review_exchange(
            issue_number=issue_number,
            reason=reason,
            pair_registry=pair_registry_for_testing,
            job_supervisor=background_job_supervisor,
        )

    pending_work = build_pending_work_wiring(
        repo_root=config.repo_root, repository_host=github,
        action_applier=action_applier, label_writer=github,
        label_manager=label_manager, events=events)

    completion_processor = CompletionProcessor(
        label_adapter=GovernedLabelSet(
            labels=github, governed_label=label_manager.needs_human
        ),
        pr_adapter=github,
        git_adapter=working_copy,
        session_output=session_output,
        review_exchange_runner=PersistentReviewExchangeRunner(
            session_output, pair_registry_for_testing, turn_mailbox=turn_mailbox,
        ),
        event_bus=None,
        label_config=label_manager.to_label_config_dict(),
        pre_publish_gate=PrePublishGate(command_runner) if config.enforce_hooks else None,
        config=config,
        background_job_supervisor=background_job_supervisor,
        agent_callback_endpoint=agent_callback_endpoint,
        review_exchange_canceller=_cancel_review_exchange_for_testing,
        review_artifact_reader=ManifestReviewArtifactReader(),
        runtime_identity=runtime_identity.resolve_runtime_identity(),
        tech_lead_authority=tech_lead_authority_for_testing,
        needs_human_block=pending_work.needs_human_block,
    )
    _wire_stack_publish_gate(
        completion_processor, _dependency_evaluator, github, command_runner, config,
    )

    # Create SessionController for testing (with optional validation gate)
    from ..control.session_controller import SessionController
    session_controller = SessionController(
        completion_processor=completion_processor,
        events=events,
        session_output=session_output,
        working_copy=working_copy,
        command_runner=command_runner if config.validation.quick.cmd else None,
        validation_cmd=config.validation.quick.cmd,
        validation_timeout_seconds=config.validation.quick.timeout_seconds,
        attempt_store=attempt_store,
        validation_attempt_key_factory=_validation_attempt_key_factory(config),
        review_exchange_canceller=_cancel_review_exchange_for_testing,
    )

    # Create LabelSync for testing
    label_sync = default_label_sync or LabelSync(labels=GovernedLabelSet(labels=github, governed_label=label_manager.needs_human), events=events, pr_tracker=github, label_manager=label_manager)

    # Create EventHub for testing
    event_hub = EventHub()
    timeline_reader = NullTimelineReader()
    timeline_writer = NullTimelineWriter()
    timeline_store: TimelineStore = NullTimelineStore()

    # Create claim components for testing (NullClaimManager by default).
    lease_config = LeaseConfig()
    claim_manager = claim_manager or NullClaimManager()
    claims = assemble_claim_components(
        claim_manager,
        SingleInstanceRunLedgerStore(lease_seconds=lease_config.lease_seconds),
        lease_config,
        events,
    )
    claim_gate, lease_renewer = claims.claim_gate, claims.lease_renewer
    run_ownership = run_ownership or claims.run_ownership

    publish_recovery = _build_publish_recovery(
        repository_host=github,
        completion_processor=completion_processor,
        label_manager=label_manager,
        fresh_issue_reader=fresh_issue_reader,
        action_applier=action_applier,
        config=config,
        tech_lead_authority=tech_lead_authority_for_testing,
    )

    # Queue cache store for testing (uses repo_root state dir)
    queue_cache_store = QueueCacheStore(
        state_dir(config.repo_root) / "queue_cache.sqlite"
    )

    # Build infrastructure services bundle
    from ..control.infra_services import InfraServices
    from ..execution.label_store import LabelStore
    label_store = LabelStore(state_dir(config.repo_root) / "label_store.sqlite")

    # Wire post-construction collaborators into action_applier (same as the
    # primary path): label_store for write-through persistence, publish_recovery
    # so issue terminal boundaries abandon publish retries.
    if action_applier is not None:
        action_applier.label_store = label_store
        action_applier.publish_recovery = publish_recovery
        action_applier.run_ownership = run_ownership

    infra_services = InfraServices(
        label_manager=label_manager,
        label_store=label_store,
        queue_cache_store=queue_cache_store,
        provider_resilience=provider_resilience,
        provider_readiness_probe=provider_readiness_probe,
        provider_launch_sampler=provider_launch_sampler,
        timeline_reader=timeline_reader,
        timeline_store=timeline_store,
        timeline_writer=timeline_writer,
        goal_pilot_store=goal_pilot_store,
        attempt_store=attempt_store,
        tech_lead_authority=tech_lead_authority_for_testing,
        tech_lead_run_activity=tech_lead.run_activity,
        promotion_target=tech_lead.promotion_target,
        open_issue_corpus=tech_lead.open_issue_corpus,
        pair_registry=pair_registry_for_testing,
        turn_mailbox=turn_mailbox,
        background_job_supervisor=background_job_supervisor,
    )

    # Bundle all dependencies into OrchestratorDeps (no nulls, no optionals)
    # Assembly of the session launcher lives here, at the composition
    # root, rather than in the facade or the control layer (#6924 A3-R2).
    session_launcher_factory = build_session_launcher_factory(
        config=config,
        events=events,
        repository_host=github,
        action_applier=action_applier,
        session_manager=session_manager,
        worktree_manager=worktree_manager,
        working_copy=working_copy,
        command_runner=command_runner,
        session_output=session_output,
        manifest_downloader=manifest_downloader,
        tech_lead_authority=tech_lead_authority_for_testing,
        claim_manager=claim_manager,
        provider_resilience=provider_resilience,
        state_machine_manager=state_machine_manager,
        label_manager=label_manager,
        agent_callback_endpoint=agent_callback_endpoint,
        provider_readiness_probe=provider_readiness_probe,
        needs_human_block=pending_work.needs_human_block,
    )
    completion_handler_factory = build_completion_handler_factory(
        config,
        events=events,
        repository_host=github,
        session_output=session_output,
        tech_lead_authority=tech_lead_authority_for_testing,
        tech_lead_run_activity=tech_lead.run_activity,
        open_issue_corpus=tech_lead.open_issue_corpus,
        label_manager=label_manager,
        provider_resilience=provider_resilience,
    )
    deps = OrchestratorDeps(
        events=events,
        runner=runner,
        repository_host=github,
        e2e_issue_tracker=e2e_issue_tracker,
        fresh_issue_reader=fresh_issue_reader,
        event_hub=event_hub,
        planner=planner,
        session_manager=session_manager,
        label_sync=label_sync,
        action_applier=action_applier,
        fact_gatherer=fact_gatherer,
        pr_scanner=pr_scanner,
        session_restorer=session_restorer,
        worktree_manager=worktree_manager,
        working_copy=working_copy,
        command_runner=command_runner,
        session_output=session_output,
        manifest_downloader=manifest_downloader,
        pending_work_claims=pending_work.claims,
        claim_quarantine=pending_work.quarantine,
        needs_human_block=pending_work.needs_human_block,
        state_machine_manager=state_machine_manager,
        completion_processor=completion_processor,
        session_controller=session_controller,
        # Tests default to synchronous; async dispatch is exercised explicitly.
        completion_dispatcher=SynchronousCompletionDispatcher(),
        health_gate=health_gate,
        agent_callback_endpoint=agent_callback_endpoint,
        session_launcher_factory=session_launcher_factory,
        completion_handler_factory=completion_handler_factory,
        operator_issue_command_factory=build_operator_issue_command_factory(
            config,
            repository_host=github,
            label_manager=label_manager,
            needs_human_block=pending_work.needs_human_block,
            fresh_issue_reader=fresh_issue_reader,
            queue_cache_store=queue_cache_store,
        ),
        board_snapshot_builder=create_board_snapshot_builder(
            config, timeline_store, tech_lead_board_publisher_for_testing, working_copy
        ),
        claim_manager=claim_manager,
        claim_gate=claim_gate,
        lease_renewer=lease_renewer,
        run_ownership=run_ownership,
        publish_recovery=publish_recovery,
        services=infra_services,
    )

    return Orchestrator(config=config, deps=deps)
