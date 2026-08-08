"""ADR-0031 owner boundary for tech_lead session identity and completion effects.

Both tech_lead variants (batch PR review and failure investigation) launch as
``issue-{N}`` sessions under the configured tech lead agent, so nothing about a
session's name distinguishes them. This module is the single owner for:

- **identity**: what makes a session a tech_lead session (the config-declared
  tech lead agent), consolidating the checks previously duplicated in
  ``SessionLauncher`` and ``CompletionActionPlanner``;
- **flavor**: reading the launch-time :class:`TechLeadAssignment` that says
  which variant a session was given (manifest selection keys off it);
- **launch preparation**: per-flavor session inputs (PR manifest download,
  the agent-visible assignment copy) plus the orchestrator-owned
  :class:`TechLeadLaunchAuthority` record that completion later trusts
  (#6761 re-review F1);
- **completion effects**: shaping the requested actions a tech_lead completion
  may execute and classifying the benign "clean audit, nothing to publish"
  outcome so it is treated as success rather than a publish failure.
"""

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.models import RequestedAction
from ..domain.tech_lead_manifest import TechLeadManifest
from ..domain.board_snapshot import BOARD_SNAPSHOT_FILENAME, BoardSnapshot
from ..domain.tech_lead_session import (
    HEALTH_REVIEW_MARKER_LABEL,
    TECH_LEAD_ASSIGNMENT_FILENAME,
    TechLeadAssignment,
    TechLeadLaunchAuthority,
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
)
from .completion_pr_collision import NoCommitsBetweenError
from .tech_lead_evidence import build_evidence_map, write_evidence_map
from .tech_lead_manifest_builder import TechLeadCandidatePolicy, TechLeadManifestBuilder

if TYPE_CHECKING:
    from ..ports.board_snapshot_provider import BoardSnapshotProvider
    from ..infra.config import Config
    from ..ports import ManifestDownloader, RepositoryHost
    from ..ports.issue import Issue
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .worktree_context import ScratchWorktreeIdentity, WorktreeContext

logger = logging.getLogger(__name__)


def is_tech_lead_session(
    tech_lead_review_agent: str | None, agent_type: str | None
) -> bool:
    """True when ``agent_type`` is the configured tech_lead review agent."""
    return bool(tech_lead_review_agent and agent_type == tech_lead_review_agent)


def recover_tech_lead_launch_scope(
    config: "Config",
    issue: "Issue",
    tech_lead_authority: "TechLeadAuthorityStore | None",
) -> "TechLeadLaunchScope | None":
    """Rebuild a RESTORED tech-lead session's launch grant from durable truth.

    A session that survives an orchestrator restart has no in-memory producer to
    hand it a :class:`TechLeadLaunchScope`, and before #6994 round 1 F3 it was
    restored without one — which quietly downgraded a running whole-board review
    to "issue-scoped", so the global barrier lifted and targeted work launched
    alongside an exclusive review, and the dashboard reported the anchor as a
    targeted running issue.

    Everything needed to rebuild the grant is already durable, and this reads it
    in the SAME order the launch path resolves flavor
    (:func:`prepare_tech_lead_session_data`), so a restored run and a fresh one
    can never be classified differently:

    1. the ADR-0031 §4 marker label on the anchor -> ``HEALTH_REVIEW``, whose
       owned cohort comes back from the durable authority ledger;
    2. the batch anchor's title signature -> ``BATCH_REVIEW``;
    3. anything else is an ordinary board issue the tech lead was aimed at, so
       it is a ``FAILURE_INVESTIGATION``.

    Returns ``None`` for a session that is not a tech-lead run at all.
    """
    from .health_review_trigger import (
        HEALTH_REVIEW_MARKER_LABEL,
        is_batch_anchor_title,
    )

    if not is_tech_lead_session(config.tech_lead_review_agent, issue.agent_type):
        return None
    labels = [str(name).casefold() for name in issue.labels]
    if HEALTH_REVIEW_MARKER_LABEL.casefold() in labels:
        cohort = (
            tech_lead_authority.load_storm_cohort(anchor_issue_number=issue.number)
            if tech_lead_authority is not None
            else None
        )
        return TechLeadLaunchScope(
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
            problem_issue_numbers=tuple(
                sorted({problem.issue_number for problem in cohort or ()})
            ),
        )
    if is_batch_anchor_title(issue.title):
        return TechLeadLaunchScope(flavor=TechLeadSessionFlavor.BATCH_REVIEW)
    return TechLeadLaunchScope(flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION)


def failure_investigation_scratch_identity(
    config: "Config",
    issue: "Issue",
    tech_lead_scope: "TechLeadLaunchScope | None",
) -> "ScratchWorktreeIdentity | None":
    """The disposable scratch worktree identity for a failure investigation (#6823).

    A failure investigation launches as an ``issue-{focus}`` session under the
    focus issue's number, so without this it would run in the focus issue's OWN
    worktree on its branch — and the agent could commit into that branch,
    mutating the very evidence it was sent to read (a live run showed a focus
    branch advance by a junk agent commit). Gating on the producer-declared
    ``tech_lead_scope.flavor`` (the reliable signal owned here, ADR-0031) it instead
    runs in a throwaway worktree on a fresh branch off the base branch, keyed to
    this run rather than the focus issue: the focus worktree/branch stay pure
    read-only evidence and an agent commit can only ever land on the disposable
    branch. The name and branch carry a random token so investigations of the
    same focus issue never collide, and the branch does NOT start with the focus
    issue number so ``extract_issue_number_from_branch`` never mistakes it for the
    focus branch.

    Returns ``None`` for every other flavor (batch/health reviews run on their
    own anchor worktrees) and for ordinary non-tech-lead issues, leaving their
    worktree derivation unchanged.
    """
    from .worktree_context import ScratchWorktreeIdentity

    if (
        tech_lead_scope is None
        or tech_lead_scope.flavor is not TechLeadSessionFlavor.FAILURE_INVESTIGATION
    ):
        return None
    token = uuid.uuid4().hex[:12]
    return ScratchWorktreeIdentity(
        worktree_name=f"{config.repo_root.name}-tech-lead-{issue.number}-{token}",
        branch_name=f"tech-lead-investigation-{issue.number}-{token}",
    )


def shape_requested_actions_for_tech_lead(
    requested: tuple[RequestedAction, ...],
) -> tuple[RequestedAction, ...]:
    """Drop POST_COMMENT from a tech_lead completion's requested actions.

    Tech Lead prompts promise the orchestrator posts no comments; the generic
    "## Implementation" template would land on the tracking issue otherwise.
    PUSH_BRANCH/CREATE_PR stay: real prompt/doc improvements should publish.
    """
    return tuple(
        action for action in requested if action is not RequestedAction.POST_COMMENT
    )


def is_benign_tech_lead_no_commits(
    action: RequestedAction, error: BaseException
) -> bool:
    """True when a tech_lead CREATE_PR failed only because there is nothing to publish.

    A clean audit has nothing to publish; that is success, not publish-failure.
    """
    return action is RequestedAction.CREATE_PR and isinstance(
        error, NoCommitsBetweenError
    )


def read_tech_lead_assignment(run_dir: Path) -> TechLeadAssignment | None:
    """Read the launch-time tech_lead assignment from a session run directory.

    Returns None when the assignment file is absent (pre-upgrade sessions).
    Malformed content raises ValueError - callers decide the fail-safe.
    """
    path = run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
    if not path.exists():
        return None
    return TechLeadAssignment.read(path)


def prepare_tech_lead_manifest(
    *,
    config: "Config",
    repository_host: "RepositoryHost",
    manifest_downloader: "ManifestDownloader",
    worktree_path: Path,
    run_dir: Path,
) -> TechLeadManifest | None:
    """Build and download the batch PR manifest for a tech_lead session.

    Returns the populated manifest, or None when no PRs need tech_lead.
    Eligibility comes from the shared candidate owner so the audited set
    matches the threshold set.
    """
    builder = TechLeadManifestBuilder(
        repository_host=repository_host,
        watch_label=config.tech_lead_watch_label,
        candidate_policy=TechLeadCandidatePolicy.from_config(config),
    )

    # Data goes in session run directory
    data_dir = f".issue-orchestrator/sessions/{run_dir.name}/tech-lead-data"
    manifest = builder.build(data_dir)

    if not manifest.prs:
        logger.info("[tech_lead] No PRs need tech_lead review")
        return None

    manifest = manifest_downloader.download(manifest, worktree_path)

    manifest_path = worktree_path / data_dir / "manifest.json"
    manifest.write(manifest_path)

    logger.info(
        "[tech_lead] Prepared manifest with %d PRs: %s",
        len(manifest.prs),
        manifest_path,
    )
    return manifest


def _resolve_health_review_cohort(
    tech_lead_scope: "TechLeadLaunchScope | None",
    *,
    tech_lead_authority: "TechLeadAuthorityStore",
    issue: "Issue",
) -> tuple[int, ...]:
    """The act-level cohort a health review owns (#6780).

    Single owner for "what may this review act on", with two ordered sources —
    both DEDICATED cohort surfaces, never the board snapshot:

    1. the producer's grant, when the review was launched from the pending
       queue (the normal path). The queued item knows its own cohort;
    2. otherwise the durable cohort ledger, keyed by the anchor issue. A
       marker-labeled anchor can also be picked up as an ordinary issue, which
       carries no grant — reading the ledger keeps a storm anchor's authority
       exact on that path too, rather than silently dropping it.

    The two cannot disagree: intake persists to the ledger BEFORE stamping the
    queue item, and startup recovery hydrates the queue item FROM the ledger.

    Returns empty for a periodic health review — it owns no cohort, so it may
    propose but not act. Reading authority from ``BoardSnapshot`` instead
    (as this did before) handed it every unrelated failure on the board.
    """
    if tech_lead_scope is not None:
        return tech_lead_scope.problem_issue_numbers
    cohort = tech_lead_authority.load_storm_cohort(anchor_issue_number=issue.number)
    return tuple(sorted({problem.issue_number for problem in cohort or ()}))


def prepare_tech_lead_session_data(
    *,
    config: "Config",
    repository_host: "RepositoryHost",
    manifest_downloader: "ManifestDownloader",
    tech_lead_authority: "TechLeadAuthorityStore",
    board_snapshot_provider: "BoardSnapshotProvider",
    issue: "Issue",
    ctx: "WorktreeContext",
    tech_lead_scope: "TechLeadLaunchScope | None",
) -> tuple[Path, ...]:
    """Prepare per-flavor tech_lead session inputs (ADR-0031).

    BATCH_REVIEW keeps the existing PR-manifest prep; FAILURE_INVESTIGATION
    and HEALTH_REVIEW must NOT receive the global batch manifest (auditing
    unrelated PRs from a focused investigation was the #6768 B4 defect; a
    health review walks the board snapshot, not a PR batch). Every flavor
    gets a tech-lead-assignment.json copy for the AGENT to read, and — the
    trusted half — an orchestrator-owned :class:`TechLeadLaunchAuthority`
    record persisted outside the agent-writable worktree, keyed by this
    run's identity, which completion reads as the only scope authority
    (#6761 re-review F1). Health reviews record no focus/manifest scope plus
    their OWNED problem cohort (#6780); act-level proposals may target only
    that cohort.

    Flavor resolution: an explicit ``tech_lead_scope`` wins (the pending-queue
    launch path forwards the producer-declared grant); otherwise the
    ADR-0031 §4 marker label on the anchor issue selects HEALTH_REVIEW
    (labels are the crash-safe truth a restart recovers from); otherwise
    BATCH_REVIEW.
    """
    if not is_tech_lead_session(config.tech_lead_review_agent, issue.agent_type):
        return ()
    flavor = (tech_lead_scope.flavor if tech_lead_scope is not None else None) or (
        TechLeadSessionFlavor.HEALTH_REVIEW
        if HEALTH_REVIEW_MARKER_LABEL in issue.labels
        else TechLeadSessionFlavor.BATCH_REVIEW
    )
    run_dir = ctx.run.run_dir
    tech_lead_manifest = None
    if flavor is TechLeadSessionFlavor.BATCH_REVIEW:
        tech_lead_manifest = prepare_tech_lead_manifest(
            config=config,
            repository_host=repository_host,
            manifest_downloader=manifest_downloader,
            worktree_path=ctx.worktree_path,
            run_dir=run_dir,
        )
        if tech_lead_manifest:
            # Store manifest path in session for completion handling
            ctx.update_manifest(
                {"tech_lead_manifest": str(run_dir / "tech-lead-data" / "manifest.json")}
            )
    focused = flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
    assignment = TechLeadAssignment(
        flavor=flavor,
        focus_issue_number=issue.number if focused else None,
        focus_reason=issue.title if focused else "",
    )
    assignment_path = run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
    assignment.write(assignment_path)
    ctx.update_manifest({"tech_lead_assignment": str(assignment_path)})
    focus_issue = issue.number if focused else None
    problem_issue_numbers = (
        _resolve_health_review_cohort(
            tech_lead_scope, tech_lead_authority=tech_lead_authority, issue=issue
        )
        if flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        else ()
    )
    board_snapshot = board_snapshot_provider.snapshot(
        focus_issue, problem_issue_numbers
    )
    tech_lead_authority.record(
        run_id=ctx.run.run_id,
        session_name=ctx.run.session_name,
        authority=TechLeadLaunchAuthority(
            flavor=flavor,
            anchor_issue_number=issue.number,
            focus_issue_number=issue.number if focused else None,
            manifest_pr_numbers=tuple(pr.number for pr in tech_lead_manifest.prs)
            if tech_lead_manifest
            else (),
            problem_issue_numbers=problem_issue_numbers,
        ),
    )
    logger.info("[tech_lead] Wrote %s assignment: %s", flavor.value, assignment_path)
    _write_board_snapshot(
        ctx,
        run_dir,
        board_snapshot,
    )
    return _stage_evidence_map(
        config=config,
        repository_host=repository_host,
        ctx=ctx,
        run_dir=run_dir,
        flavor=flavor,
        focus_issue_number=focus_issue,
        board_snapshot=board_snapshot,
    )


def _write_board_snapshot(
    ctx: "WorktreeContext",
    run_dir: Path,
    snapshot: BoardSnapshot,
) -> None:
    """Write the ADR-0031 §3 board snapshot into the tech-lead-data directory.

    The tech_lead prompt treats board-snapshot.json as authoritative required
    input, so build/write failures propagate and fail the launch loudly
    (fail-fast: a DB/log bug must not silently launch a session missing its
    input — the launcher converts the exception into a failed LaunchResult).
    The run-manifest entry is recorded only after a successful write so it
    never points at a missing file.
    """
    snapshot_path = run_dir / "tech-lead-data" / BOARD_SNAPSHOT_FILENAME
    snapshot.write(snapshot_path)
    ctx.update_manifest({"board_snapshot": str(snapshot_path)})


def _focus_failure_artifact_hints(
    board_snapshot: BoardSnapshot, focus_issue_number: int
) -> tuple[str, ...]:
    """Artifact-hint paths on the focus issue's board failure, if present.

    The board snapshot already carries recent failures with their on-disk
    artifact hints; the focus issue's failure (when on the board) supplies the
    run-dir locations the investigation should start from. Returns empty when
    the focus issue is not among the recent failures — the sibling-worktree
    glob in :func:`tech_lead_evidence.build_evidence_map` still finds its run-dirs.
    """
    for failure in board_snapshot.recent_failures:
        if failure.issue_number == focus_issue_number:
            return tuple(failure.artifact_hints)
    return ()


def _stage_evidence_map(
    *,
    config: "Config",
    repository_host: "RepositoryHost",
    ctx: "WorktreeContext",
    run_dir: Path,
    flavor: TechLeadSessionFlavor,
    focus_issue_number: int | None,
    board_snapshot: BoardSnapshot,
) -> tuple[Path, ...]:
    """Best-effort: stage the read-side evidence map for a tech_lead session.

    Returns the map's typed sandbox read-roots (empty on the BATCH_REVIEW no-map
    path or on a best-effort staging failure) so the launcher can grant a
    sandboxed tech lead read access to exactly the god-view it advertises, while
    writes stay confined to the scratch worktree (#6824 R5).

    Unlike :func:`_write_board_snapshot` (fail-fast, because board-snapshot.json
    is a REQUIRED agent input), the evidence map is an ENHANCEMENT — a
    deliberate exception to the fail-fast house style. The whole build+write is
    wrapped so ANY failure only logs a warning and continues: failing to stage
    evidence must never fail the session launch. The manifest entry is recorded
    only after a successful write, so it never points at a missing file.

    Per flavor: BATCH_REVIEW gets no evidence map (it audits a PR batch, not
    orchestrator-state facts); FAILURE_INVESTIGATION gets the full focus map
    (the god-view substrate + the focus issue's own run-dirs + a GitHub
    warm-cache); HEALTH_REVIEW gets the full SYSTEM map — the same substrate
    (all SQLite stores, roots) plus whole-system run-dirs enumerated across
    every worktree, since it assesses the whole floor and has no single focus
    (``build_evidence_map`` keys both off ``focus_issue_number`` being None).
    """
    if flavor is TechLeadSessionFlavor.BATCH_REVIEW:
        return ()
    try:
        artifact_hints = (
            _focus_failure_artifact_hints(board_snapshot, focus_issue_number)
            if focus_issue_number is not None
            else ()
        )
        evidence = build_evidence_map(
            config=config,
            repository_host=repository_host,
            focus_issue_number=focus_issue_number,
            artifact_hints=artifact_hints,
        )
        path = write_evidence_map(run_dir, evidence)
        ctx.update_manifest({"evidence_map": str(path)})
        return evidence.sandbox_read_roots()
    except Exception as exc:  # noqa: BLE001 - evidence map is best-effort, never fatal
        logger.warning("[tech_lead] Evidence map staging failed (non-fatal): %s", exc)
        return ()
