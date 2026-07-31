"""Persistent-session review-exchange runner.

Drives a coder↔reviewer review exchange where each role is one persistent
agent process — opened at exchange start, prompted once per round via
``send_round``, and terminated explicitly at exchange end. The PTY for
each role captures one continuous ``terminal-recording.jsonl`` spanning
every round of the exchange, plus a ``chapters.json`` sidecar that marks
each prompt/feedback boundary so the session viewer can scrub straight
to "where the reviewer's round-2 comments start."

The reviewer runs in a separate worktree from the coder; the caller is
responsible for creating that worktree before invoking this runner and
removing it after. Between rounds the caller may inject a
``before_reviewer_round`` callback to e.g. fast-forward the reviewer
worktree to the coder's branch tip.

This module owns the round-loop semantics — validation gating,
no-progress termination, and event emission.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..domain.artifact_contracts import (
    AgentProvider,
    AgentRole,
    AgentTurnArtifactScope,
    ArtifactContractViolation,
    ArtifactRef,
    ChapterSidecarArtifact,
    CoderTurnCompleted,
    CoderTurnStarted,
    ExchangeRunId,
    ExistingFile,
    ExistingNonEmptyFile,
    IssueNumber,
    PositiveAttemptIndex,
    PositiveRoundIndex,
    PromptArtifact,
    ReviewResponseArtifact,
    ReviewerTurnCompleted,
    ReviewerTurnStarted,
    TerminalRecordingArtifact,
)
from ..domain.review_exchange_manifest import (
    ReviewExchangeManifestHeader,
    ReviewExchangeRecordingPaths,
)
from ..domain.exchange_chapter import (
    CHAPTER_SECTION_FEEDBACK,
    CHAPTER_SECTION_PROMPT,
    CHAPTER_SECTION_TIMEOUT,
)
from ..domain.models import AgentConfig
from ..domain.review_exchange import (
    ReviewExchangeOutcome,
    ReviewExchangeResponse,
    build_coder_prompt,
    build_reviewer_prompt,
)
from ..domain.review_exchange_failures import (
    is_process_unusable_failure,
    round_failure_chapter_label,
)
from ..domain.review_artifacts import (
    NitPolicy,
    REVIEW_DECISION_FILENAME,
    REVIEW_REPORT_FILENAME,
    ReviewArtifactPair,
    ReviewDecision,
    persist_review_artifact_pair,
    review_requires_nit_rework,
)
from ..domain.review_exchange_resume import is_no_completion_reason
from ..domain.review_exchange_turn import (
    ReviewExchangePromptFiles,
    ReviewExchangeTurnPacket,
    ReviewExchangeTurnResult,
    Role,
    TurnResultKind,
)
from ..ports.turn_mailbox import TurnMailbox
from ..domain.review_exchange_run import ReviewExchangeRun, ReviewExchangeRunAssets
from ..domain.review_exchange_summary import ReviewExchangeReason, ReviewExchangeStatus, ReviewExchangeSummaryArtifactRef, ReviewExchangeSummaryV1, ReviewExchangeTerminalState
from ..domain.runtime_config import RuntimeConfigReference
from ..domain import review_exchange_turn_artifacts as turn_artifacts
from ..events import EventContext, EventName
from ..infra.env import ENV_PREFIX
from ..infra.logging_config import log_context
from ..infra.repo_identity import get_repo_head_sha
from ..infra.terminal_recording import TERMINAL_RECORDING_FILENAME
from ..ports import (
    EventSink,
    TraceEvent,
    make_review_exchange_completed_event,
    make_review_exchange_round_completed_event,
    make_trace_event,
)
from ..ports.session_output import SessionOutput
from ..ports.review_exchange_approval_gate import ReviewExchangeApprovalGate
from .persistent_exchange_pair_registry_inmemory import (
    InMemoryPersistentExchangePairRegistry,
    PersistentExchangePair,
)
from . import persistent_pair_contract as _pair_contract
from .persistent_round_runner import (
    PersistentRoundError,
    PersistentRoundTimeoutError,
    PersistentSession,
    close_persistent_session,
    open_persistent_session,
    persistent_round_failure_reason,
    send_round,
)
from .persistent_role_prompt_policy import (
    FRESH_CODEX_PROMPT_PROCESS_TRIGGER,
    RoleAttemptWorkspace as _RoleAttemptWorkspace,
    role_prompt_inbox_path as _role_prompt_inbox_path,
    role_session_needs_fresh_prompt_process,
)
from .review_exchange_response_channel import (
    ResponseChannel,
    ReviewExchangeResponseChannels,
    build_bootstrap_prompt,
    build_prompt_inbox_notice,
    prompt_for_response_channel,
    reviewer_prompt_with_artifact_contract,
)
from .recording_contract import recording_event_count

logger = logging.getLogger(__name__)
_acquire_pair_or_emit_failure = _pair_contract.acquire_pair_or_emit_failure
_acquire_pair_with_recording_contract = (
    _pair_contract.acquire_pair_with_recording_contract
)
_emit_review_exchange_failed = _pair_contract.emit_review_exchange_failed
_PairExchangeRunBinding = _pair_contract.PairExchangeRunBinding

_CODER_PROTOCOL_RETRY_LIMIT = 2

# How many times a single role turn may respawn-and-retry after the role
# process is found dead/unusable (e.g. a one-shot reviewer that exited cleanly
# between rounds). One retry is enough for the one-shot-exit case; bounding it
# prevents an infinite respawn loop if the prompt itself deterministically
# crashes the agent before it can respond.
_MAX_ROLE_RESPAWN_RETRIES = 1


def review_exchange_supervisor_timeout_seconds(
    *,
    coder_timeout_seconds: float,
    reviewer_timeout_seconds: float,
    max_rounds: int,
    grace_seconds: float = 300.0,
) -> float:
    """Return the outer wall-clock deadline for one persistent exchange.

    The persistent runner owns coder protocol retries. The background
    supervisor deadline must include that retry budget so it only catches a
    wedged runner, not a legitimate exchange still inside its per-role
    deadlines.
    """
    if coder_timeout_seconds <= 0:
        raise ValueError("coder_timeout_seconds must be positive")
    if reviewer_timeout_seconds <= 0:
        raise ValueError("reviewer_timeout_seconds must be positive")
    if grace_seconds < 0:
        raise ValueError("grace_seconds must be non-negative")
    rounds = max(1, int(max_rounds))
    coder_attempts_per_round = 1 + _CODER_PROTOCOL_RETRY_LIMIT
    per_round = reviewer_timeout_seconds + (
        coder_timeout_seconds * coder_attempts_per_round
    )
    return float(per_round * rounds + grace_seconds)


@dataclass
class _RoleSliceMirror:
    """Translate pair-recording event indices into per-session slice indices.

    The slice file at ``<run_dir>/<role>/terminal-recording.jsonl`` is
    written **continuously** by the role's
    ``MirroredTerminalRecordingWriter`` — registered at exchange start
    via ``add_mirror_recording`` and removed at exchange end. The
    timeline viewer therefore sees agent output update in near real time
    rather than waiting for a chapter boundary to flush.

    What this dataclass owns is the **offset translation** between the
    pair recording (long-lived, accumulates across every exchange the
    pair handles) and the slice (per-exchange, freshly attached). Its
    ``slice_base`` is the pair recording's event count *at exchange
    start* — the first event the slice will mirror. Chapter sidecars
    store ``pair_event_idx - slice_base`` so the viewer can scrub the
    manifest-pointed slice directly. Without that translation, a cached
    pair on exchange 2 would record chapter offsets in the hundreds
    while the slice file holds dozens of events and the web replay
    route's ``all_events[offset:]`` would return an empty window.
    """

    pair_recording: Path
    session_slice: Path
    slice_base: int

    def pair_to_slice_offset(self, pair_event_idx: int) -> int:
        """Translate a pair-recording event index into a slice-local index.

        The slice file is written as a strict subset of the pair
        recording starting at ``slice_base``; the slice's event N
        corresponds to pair event ``slice_base + N``. Chapter sidecars
        store these slice-local offsets so the viewer can scrub the
        manifest-pointed slice directly.

        Raises ``ValueError`` when ``pair_event_idx < slice_base``.
        Chapter recording happens during the exchange, after slice
        attach; an index from before exchange start is a wrong-source
        bug (caller fed an index from a different recording) and
        masking it with a clamp would silently return wrong content.
        """
        if pair_event_idx < self.slice_base:
            raise ValueError(
                f"pair_event_idx={pair_event_idx} is below "
                f"slice_base={self.slice_base}; chapter offsets must "
                "be sampled after the slice mirror is attached at "
                "exchange start. A negative slice index would index "
                "past the start of the slice and silently return "
                "content from prior exchanges.",
            )
        return pair_event_idx - self.slice_base


def _attach_slice_mirror(
    session: PersistentSession,
    slice_path: Path,
) -> None:
    """Register a per-session slice with the role's PTY writer.

    Fails loudly. The slice mirror is load-bearing for the per-session
    timeline contract — without it the viewer reads an empty slice
    file from the manifest while the agent's output continues to flow
    only into the pair recording, recreating the exact "I can't see
    what the reviewer is doing" symptom this PR is supposed to fix.
    Failures here propagate up to ``run_persistent_session_exchange``'s
    top-level handler, which emits ``REVIEW_EXCHANGE_FAILED`` and
    re-raises so the orchestrator's loop bound (PR #6267) can govern
    retries / escalation rather than the silent empty-timeline mode.

    ``log_writer is None`` is a production invariant violation: every
    role session opened by ``open_persistent_session`` carries a real
    ``MirroredTerminalRecordingWriter``. Test fixtures that construct
    sessions directly must wire a writer too — not doing so would mean
    the test was getting a free pass on the live-mirror invariant.
    """
    writer = session.log_writer
    if writer is None:
        raise RuntimeError(
            f"PersistentSession has no log_writer; cannot attach "
            f"per-session slice mirror at {slice_path}. Production "
            "sessions always carry a writer; this indicates either a "
            "regression in open_persistent_session or a test fixture "
            "that bypassed the writer wiring.",
        )
    # ``seed_resize=False`` keeps the slice indexing aligned with the
    # offset translator: the first slice event corresponds to the
    # first pair event written *after* exchange start, with no
    # synthetic leading event to throw off ``pair_to_slice_offset``.
    writer.add_mirror_recording(slice_path, seed_resize=False)


def _detach_slice_mirror(
    session: PersistentSession,
    slice_path: Path,
) -> None:
    """Stop mirroring writes to the per-session slice path.

    Called from a ``finally`` block, so any exception here would
    obscure the exception that put us in the finally — log and
    continue rather than mask the real failure. The flip side of
    ``_attach_slice_mirror``'s fail-fast: if attach succeeded, detach
    almost never fails (the writer's path map is in-process state),
    and if detach somehow fails the worst case is the next exchange
    seeing tail bytes from this exchange in its slice — caught by
    ``test_slice_detaches_at_exchange_end_no_leak_to_next_exchange``.
    """
    writer = session.log_writer
    if writer is None:
        # The attach helper would have raised before we got here, so
        # reaching this branch means someone called detach without
        # ever calling attach. Tolerate so a partial-construction
        # cleanup path stays simple.
        return
    try:
        writer.remove_mirror_recording(slice_path)
    except (OSError, ValueError):
        logger.exception(
            "Failed to detach per-session slice mirror at %s during "
            "exchange teardown; subsequent writes from this writer "
            "may continue to target the slice file. Logging and "
            "continuing — raising here would mask the original "
            "exception that triggered the finally block.",
            slice_path,
        )


def _prepare_session_slice(slice_path: Path) -> None:
    """Create the per-session slice directory and seed an empty file.

    Pre-creating an empty file keeps the timeline viewer's recording
    lookup (``ManifestAccessor.get_review_exchange_recording``) from
    404'ing while a hung exchange is mid-round and no slice events
    have been mirrored yet. ``allow_empty=False`` callers still see
    "empty" as a recoverable condition rather than "missing".
    """
    slice_path.parent.mkdir(parents=True, exist_ok=True)
    slice_path.touch(exist_ok=True)


def _release_pair_after_no_completion(
    *,
    pair_registry: InMemoryPersistentExchangePairRegistry,
    pair: PersistentExchangePair,
    issue_number: int,
    session_name: str,
    outcome: ReviewExchangeOutcome,
) -> bool:
    """Release a wedged persistent pair after a no-completion exchange.

    ``*_no_completion`` means one role timed out before writing the required
    response artifact. The resume policy retries those summaries by launching a
    fresh exchange; with persistent pairs, "fresh" must include the underlying
    PTYs. Otherwise an alive-but-not-reading agent keeps getting reused and the
    retry loop recreates the same timeout.
    """
    if outcome.status != "error" or not is_no_completion_reason(outcome.reason):
        return False
    logger.warning(
        "[REVIEW_EXCHANGE] no-completion result; releasing persistent pair "
        "before retry issue=%s session_name=%s reason=%s coder_pid=%d "
        "reviewer_pid=%d coder_response=%s reviewer_response=%s "
        "reviewer_worktree=%s",
        issue_number,
        session_name,
        outcome.reason,
        pair.coder_session.proc.pid,
        pair.reviewer_session.proc.pid,
        pair.coder_response_path,
        pair.reviewer_response_path,
        pair.reviewer_worktree_path,
    )
    pair_registry.release(
        issue_number,
        reason=f"review-exchange-{outcome.reason}",
    )
    return True


def _release_pair_after_exchange_exception(
    *,
    pair_registry: InMemoryPersistentExchangePairRegistry,
    pair: PersistentExchangePair,
    issue_number: int,
    session_name: str,
    exc: Exception,
) -> None:
    logger.warning(
        "[REVIEW_EXCHANGE] exchange raised; releasing persistent pair "
        "issue=%s session_name=%s exception_type=%s coder_pid=%d "
        "reviewer_pid=%d coder_response=%s reviewer_response=%s "
        "reviewer_worktree=%s",
        issue_number,
        session_name,
        type(exc).__name__,
        pair.coder_session.proc.pid,
        pair.reviewer_session.proc.pid,
        pair.coder_response_path,
        pair.reviewer_response_path,
        pair.reviewer_worktree_path,
    )
    pair_registry.release(issue_number, reason="review-exchange-exception")


def run_persistent_session_exchange(  # noqa: PLR0913
    *,
    exchange_run: ReviewExchangeRun,
    session_output: SessionOutput,
    pair_registry: InMemoryPersistentExchangePairRegistry,
    persistent_pair_root: Path,
    coder_worktree_path: Path,
    reviewer_worktree_factory: Callable[[], Path],
    coder_branch: str | None = None,
    issue_number: int,
    issue_title: str,
    coder_label: str,
    reviewer_label: str,
    coder_agent: AgentConfig,
    reviewer_agent: AgentConfig,
    runtime_config: RuntimeConfigReference,
    max_rounds: int,
    max_no_progress: int,
    require_validation: bool,
    nit_policy: str = "surface",
    initial_validation_record_path: Path | None = None,
    approval_gate: ReviewExchangeApprovalGate | None = None,
    web_port: int | None = None,
    events: EventSink | None = None,
    event_context: EventContext | None = None,
    before_reviewer_round: Callable[[int], None] | None = None,
    turn_mailbox: "TurnMailbox | None" = None,
    response_channels: ReviewExchangeResponseChannels | None = None,
) -> ReviewExchangeOutcome:
    """Run the coder↔reviewer exchange against a registry-owned persistent pair.

    Acquires a pair from ``pair_registry``. The pair contract owner
    requires the live role processes to have been spawned for this exact
    exchange run binding, because their env contains run-scoped values
    (``RUN_DIR``, ``SESSION_ID``, validation output dir). A cached pair from
    any other exchange run is released and respawned before round code sees it.

    The release at broader issue-lifetime boundaries (PR merge, reset-retry,
    escalation, orchestrator shutdown) is still the caller's responsibility.
    This function also lets the pair contract release on run-binding changes,
    process/recording contract failure, and exchange exceptions.
    """
    session_name = exchange_run.session_name
    run_dir = exchange_run.assets.run_dir
    run_id = exchange_run.run_id
    exchange_run_id = run_id
    run_assets = exchange_run.assets
    exchange_dir = exchange_run.assets.exchange_dir
    exchange_dir.mkdir(parents=True, exist_ok=True)
    effective_response_channels = (
        response_channels or ReviewExchangeResponseChannels()
    )
    if turn_mailbox is None:
        effective_response_channels = ReviewExchangeResponseChannels.file_only()
    _write_review_exchange_manifest_header(
        session_output,
        run_dir,
        ReviewExchangeManifestHeader(
            exchange_dir=exchange_dir,
            parent_session_name=exchange_run.parent_session_name,
        ),
    )

    def _emit(event_name: EventName, payload: dict[str, Any]) -> None:
        if events is None or event_context is None:
            return
        enriched = dict(payload)
        enriched["run_dir"] = str(run_dir)
        enriched["session_run_id"] = run_id
        events.publish(make_trace_event(event_name, event_context.enrich(enriched)))

    _emit(EventName.REVIEW_EXCHANGE_STARTED, {
        "issue_number": issue_number,
        "issue_title": issue_title,
        "session_name": session_name,
        "coder_label": coder_label,
        "reviewer_label": reviewer_label,
        "max_rounds": max_rounds,
        "max_no_progress": max_no_progress,
        "require_validation": require_validation,
        "nit_policy": nit_policy,
        "exchange_dir": str(exchange_dir),
    })

    # Pair-scoped paths: the same physical files survive for the role-process
    # pair spawned for this exchange run. The process env is fixed at spawn, so
    # a new exchange run gets a freshly spawned pair rather than rebinding this
    # process to a different run_dir/session.
    #
    # The per-round response file is the one exception: the agent
    # writes it itself, and codex's per-shell-tool seatbelt sandbox
    # constrains writes to the role's worktree (its cwd). A response
    # file under ``pair_dir`` is unreachable from inside that sandbox
    # — every write attempt fails with ``operation not permitted``,
    # the round runner's polling never sees the file, the round times
    # out, and the orchestrator relaunches the exchange forever.
    # Putting the response file inside the role's worktree keeps it
    # within the writable root. The orchestrator process itself is
    # unsandboxed and reads from any path, so polling continues to
    # work unchanged. The file is per-round transient (unlinked at
    # the start of each round in ``send_round``), so losing it on
    # worktree teardown is correct, not a regression.
    pair_dir = persistent_pair_root / f"issue-{issue_number}"
    coder_pair_dir = pair_dir / "coder"
    reviewer_pair_dir = pair_dir / "reviewer"
    coder_response = (
        coder_worktree_path / ".issue-orchestrator" / "review-response.json"
    )
    coder_recording = coder_pair_dir / TERMINAL_RECORDING_FILENAME
    reviewer_recording = reviewer_pair_dir / TERMINAL_RECORDING_FILENAME
    coder_completion = coder_pair_dir / "completion-coder.json"
    pair_validation_record = pair_dir / "validation-record.json"

    # Per-session slice paths: each role's run_dir gets its own
    # ``terminal-recording.jsonl`` populated incrementally at chapter
    # boundaries (see ``_RoleSliceMirror``). The manifest's primary
    # ``<role>_recording`` key points here so the timeline viewer's
    # ``ManifestAccessor.get_review_exchange_recording`` returns the
    # per-session projection by default. The canonical pair recording
    # is preserved under ``<role>_recording_pair`` for power users
    # and cross-exchange forensics.
    coder_session_slice = run_dir / "coder" / TERMINAL_RECORDING_FILENAME
    reviewer_session_slice = run_dir / "reviewer" / TERMINAL_RECORDING_FILENAME
    _prepare_session_slice(coder_session_slice)
    _prepare_session_slice(reviewer_session_slice)
    session_output.update_manifest(
        run_dir,
        ReviewExchangeRecordingPaths(
            persistent_pair_dir=pair_dir,
            coder_recording=coder_session_slice,
            reviewer_recording=reviewer_session_slice,
            coder_recording_pair=coder_recording,
            reviewer_recording_pair=reviewer_recording,
        ).to_manifest_fields(),
    )

    run_validation_record_path = exchange_run.assets.validation_record_path
    pair_validation = _PairValidationMirror(
        pair_dir=pair_dir,
        record_path=pair_validation_record,
        coder_worktree_path=coder_worktree_path,
        run_record_path=run_validation_record_path,
    )
    pair_validation.replace_from_initial(initial_validation_record_path)

    def _spawn_pair() -> PersistentExchangePair:
        # Cache miss: this is the first exchange for the issue (or the
        # previous pair died). Create the reviewer worktree and open
        # both sessions with pair-scoped env paths.
        reviewer_wt_path = reviewer_worktree_factory()
        # Reviewer response file lives inside the reviewer worktree
        # (writable root for the reviewer's seatbelt sandbox); see
        # the ``coder_response`` comment above for the full rationale.
        reviewer_response = (
            reviewer_wt_path / ".issue-orchestrator" / "review-response.json"
        )
        reviewer_report = (
            reviewer_wt_path / ".issue-orchestrator" / REVIEW_REPORT_FILENAME
        )
        coder_spec = _RoleSessionSpec(
            role=Role.CODER,
            agent=coder_agent,
            worktree=coder_worktree_path,
            run_dir=run_dir,
            recording_path=coder_recording,
            response_file=coder_response,
            completion_path=coder_completion,
            validation_output_dir=run_dir,
            review_report_file=None,
            runtime_config=runtime_config,
            agent_label=coder_label,
            web_port=web_port,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            response_channel=effective_response_channels.for_role(Role.CODER),
        )
        coder = _open_role_session_from_spec(coder_spec)
        # Reviewer-spawn-after-coder-success is the canonical
        # partial-construction case: if the reviewer's PTY/process
        # bring-up raises, the coder is already running and would
        # leak unless we close it explicitly. Pre-registry code
        # paired the two opens inside one ``try`` and closed any
        # already-opened session in ``finally``; the registry
        # version preserves that guarantee here so a partial spawn
        # never returns a half-built pair to the registry's cache.
        # (Reviewer doesn't write a coding-done completion, but we
        # still pass pair-scoped completion / validation paths so the
        # env layout is consistent across roles.)
        try:
            reviewer_spec = _RoleSessionSpec(
                role=Role.REVIEWER,
                agent=reviewer_agent,
                worktree=reviewer_wt_path,
                run_dir=run_dir,
                recording_path=reviewer_recording,
                response_file=reviewer_response,
                completion_path=reviewer_pair_dir / "completion-reviewer.json",
                validation_output_dir=run_dir,
                review_report_file=reviewer_report,
                runtime_config=runtime_config,
                agent_label=reviewer_label,
                web_port=web_port,
                issue_number=issue_number,
                issue_title=issue_title,
                session_name=session_name,
                response_channel=effective_response_channels.for_role(Role.REVIEWER),
            )
            reviewer = _open_role_session_from_spec(reviewer_spec)
        except BaseException:
            close_persistent_session(coder)
            raise
        from time import time as _wall_clock

        return PersistentExchangePair(
            coder_session=coder,
            reviewer_session=reviewer,
            reviewer_worktree_path=reviewer_wt_path,
            issue_key=issue_number,
            exchange_run_id=exchange_run_id,
            run_dir=run_dir,
            created_at=_wall_clock(),
            coder_response_path=coder_response,
            reviewer_response_path=reviewer_response,
            reviewer_report_path=reviewer_report,
            coder_recording_path=coder_recording,
            reviewer_recording_path=reviewer_recording,
            coder_completion_path=coder_completion,
            validation_record_path=pair_validation_record,
        )

    pair = _acquire_pair_or_emit_failure(
        pair_registry=pair_registry,
        issue_number=issue_number,
        session_name=session_name,
        exchange_run=_PairExchangeRunBinding.from_exchange_run(exchange_run),
        spawn=_spawn_pair,
        emit=_emit,
    )

    # Always fast-forward the reviewer worktree at the start of every
    # reviewer round, including round 1. Round 1 of a *fresh* pair is a
    # no-op (the worktree was just created at the coder tip); round 1
    # of a *cached* pair from a previous exchange is the load-bearing
    # case — the coder may have advanced its branch between exchanges
    # and the reviewer needs the new tip. The caller-supplied
    # ``before_reviewer_round`` (if any) runs after the FF.
    def _ff_then_caller_hook(round_index: int) -> None:
        if coder_branch is not None:
            from .reviewer_worktree import (
                ReviewerWorktree,
                fast_forward_reviewer_worktree,
            )

            fast_forward_reviewer_worktree(
                ReviewerWorktree(
                    path=pair.reviewer_worktree_path,
                    coder_branch=coder_branch,
                ),
            )
        if before_reviewer_round is not None:
            before_reviewer_round(round_index)

    # Build per-role mirrors after the pair is acquired so the slice
    # bases are sampled from the *current* pair recording size — i.e.
    # everything the agent has emitted up to this exchange's first
    # round. Cached pairs have prior exchanges' content already in the
    # pair recording; the per-session slice must skip past it. The
    # mirror only owns offset translation now (chapter sidecars +
    # SSE payloads) — actual per-event mirroring is wired into the
    # role's ``MirroredTerminalRecordingWriter`` below so the slice
    # file fills in near real time, not just at chapter boundaries.
    coder_slice_base = recording_event_count(
        pair.coder_recording_path,
        require_recording=False,
    )
    reviewer_slice_base = recording_event_count(
        pair.reviewer_recording_path,
        require_recording=False,
    )
    coder_mirror = _RoleSliceMirror(
        pair_recording=pair.coder_recording_path,
        session_slice=coder_session_slice,
        slice_base=coder_slice_base,
    )
    reviewer_mirror = _RoleSliceMirror(
        pair_recording=pair.reviewer_recording_path,
        session_slice=reviewer_session_slice,
        slice_base=reviewer_slice_base,
    )
    coder_session_owner = _RoleSessionOwner(
        pair=pair,
        spec=_RoleSessionSpec(
            role=Role.CODER,
            agent=coder_agent,
            worktree=coder_worktree_path,
            run_dir=run_dir,
            recording_path=pair.coder_recording_path,
            response_file=pair.coder_response_path,
            completion_path=pair.coder_completion_path,
            validation_output_dir=run_dir,
            review_report_file=None,
            runtime_config=runtime_config,
            agent_label=coder_label,
            web_port=web_port,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            response_channel=effective_response_channels.for_role(Role.CODER),
        ),
        slice_path=coder_session_slice,
    )
    reviewer_session_owner = _RoleSessionOwner(
        pair=pair,
        spec=_RoleSessionSpec(
            role=Role.REVIEWER,
            agent=reviewer_agent,
            worktree=pair.reviewer_worktree_path,
            run_dir=run_dir,
            recording_path=pair.reviewer_recording_path,
            response_file=pair.reviewer_response_path,
            completion_path=reviewer_pair_dir / "completion-reviewer.json",
            validation_output_dir=run_dir,
            review_report_file=pair.reviewer_report_path,
            runtime_config=runtime_config,
            agent_label=reviewer_label,
            web_port=web_port,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            response_channel=effective_response_channels.for_role(Role.REVIEWER),
        ),
        slice_path=reviewer_session_slice,
    )

    # Live mirror registration. From this point on, every event the
    # agent's PTY drains into the canonical pair recording is *also*
    # appended to the per-session slice file. A user inspecting the
    # timeline mid-round (during a long reviewer think, or while a
    # round is hung waiting for response-file delivery) sees agent
    # output in near real time instead of waiting for a chapter
    # boundary to fire.
    #
    # Attach is INSIDE the try block so a failure here lands in the
    # REVIEW_EXCHANGE_FAILED handler — the orchestrator's loop bound
    # (PR #6267) governs retries / escalation rather than letting an
    # attach failure silently leave the timeline empty. The detach in
    # ``finally`` is load-bearing for the success path: leaving the
    # slice path attached past exchange end means the writer keeps
    # writing to a path under a possibly-torn-down run_dir, polluting
    # the next exchange's slice with the previous exchange's tail
    # bytes. ``_detach_slice_mirror`` is a no-op when the path was
    # never attached, so a partial attach (coder succeeded, reviewer
    # raised) cleans up safely.
    try:
        coder_session_owner.attach_slice_mirror()
        reviewer_session_owner.attach_slice_mirror()
        outcome = _drive_rounds(
            command=_DriveRoundsCommand(
                session_output=session_output,
                run_assets=run_assets,
                run_dir=run_dir,
                exchange_dir=exchange_dir,
                issue_number=issue_number,
                issue_title=issue_title,
                session_name=session_name,
                exchange_run_id=exchange_run_id,
                coder_session_owner=coder_session_owner,
                reviewer_session_owner=reviewer_session_owner,
                # Round-loop reads/writes pair-scoped files (stable across
                # exchanges) — never the run_dir-derived defaults that B1
                # used. On cache hit, ``pair.coder_response_path`` points
                # to the same file the agent's env was set to at spawn.
                coder_response=pair.coder_response_path,
                reviewer_response=pair.reviewer_response_path,
                reviewer_report_path=pair.reviewer_report_path,
                coder_recording=pair.coder_recording_path,
                reviewer_recording=pair.reviewer_recording_path,
                coder_completion_path=pair.coder_completion_path,
                validation_record_path=pair.validation_record_path,
                prompt_files=ReviewExchangePromptFiles(
                    validation_record=run_validation_record_path,
                ),
                pair_validation=pair_validation,
                coder_timeout_seconds=coder_agent.timeout_minutes * 60,
                reviewer_timeout_seconds=reviewer_agent.timeout_minutes * 60,
                max_rounds=max_rounds,
                max_no_progress=max_no_progress,
                require_validation=require_validation,
                nit_policy=_coerce_runtime_nit_policy(nit_policy),
                approval_gate=approval_gate,
                coder_provider=_agent_provider(coder_agent),
                reviewer_provider=_agent_provider(reviewer_agent),
                before_reviewer_round=_ff_then_caller_hook,
                emit=_emit,
                coder_mirror=coder_mirror,
                reviewer_mirror=reviewer_mirror,
                turn_mailbox=turn_mailbox,
                response_channels=effective_response_channels,
            ),
        )
    except Exception as exc:
        _release_pair_after_exchange_exception(
            pair_registry=pair_registry,
            pair=pair,
            issue_number=issue_number,
            session_name=session_name,
            exc=exc,
        )
        _emit_review_exchange_failed(
            emit=_emit,
            issue_number=issue_number,
            session_name=session_name,
            exc=exc,
        )
        raise
    finally:
        coder_session_owner.detach_slice_mirror()
        reviewer_session_owner.detach_slice_mirror()
        _clear_role_prompt_inbox(pair.coder_response_path)
        _clear_role_prompt_inbox(pair.reviewer_response_path)

    _release_pair_after_no_completion(
        pair_registry=pair_registry,
        pair=pair,
        issue_number=issue_number,
        session_name=session_name,
        outcome=outcome,
    )
    return outcome


@dataclass(frozen=True)
class _PairValidationMirror:
    """Own the pair-scoped validation record's freshness contract.

    The persistent pair owns pair-scoped validation evidence, but validation is
    only valid for the coder worktree's current HEAD. This mirror is the
    single owner for invalidating stale pair records, copying the
    current validation owner's record into pair scope, and asserting
    that a required validation record both passed and matches HEAD.
    """

    pair_dir: Path
    record_path: Path
    coder_worktree_path: Path
    run_record_path: Path | None = None

    def replace_from_initial(self, source: Path | None) -> None:
        """Mirror the caller's current validation source at exchange start.

        A missing source clears any prior pair record. That is
        intentional: an exchange without current validation evidence
        must not inherit the last exchange's passing record.
        """
        self._replace_from(source)

    def refresh_from_completion(
        self,
        payload: dict[str, Any],
        *,
        run_validation_record_path: Path,
    ) -> str | None:
        """Mirror validation evidence produced by this coder turn."""
        source, error = self._completion_validation_source(
            payload,
            run_validation_record_path=run_validation_record_path,
        )
        if error is not None:
            self._clear()
            return error
        self._replace_from(source)
        return None

    def current_validation_error(self) -> str | None:
        return _validation_record_error(
            self.record_path,
            current_head_sha=get_repo_head_sha(self.coder_worktree_path),
        )

    def _completion_validation_source(
        self,
        payload: dict[str, Any],
        *,
        run_validation_record_path: Path,
    ) -> tuple[Path | None, str | None]:
        raw_path = payload.get("validation_record_path")
        if raw_path is not None:
            if not isinstance(raw_path, str) or not raw_path.strip():
                return (
                    None,
                    "completion validation_record_path must be a non-empty string",
                )
            return self._validated_worktree_path(raw_path)
        if run_validation_record_path.exists():
            return run_validation_record_path, None
        return None, None

    def _validated_worktree_path(self, raw_path: str) -> tuple[Path | None, str | None]:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.coder_worktree_path / candidate
        try:
            resolved = candidate.resolve()
            worktree = self.coder_worktree_path.resolve()
            if resolved != self.record_path.resolve():
                resolved.relative_to(worktree)
        except (OSError, ValueError):
            return None, (
                "completion validation_record_path must stay under the coder worktree"
            )
        if not resolved.exists():
            return None, f"completion validation_record_path does not exist: {resolved}"
        if not resolved.is_file():
            return None, f"completion validation_record_path is not a file: {resolved}"
        return resolved, None

    def _replace_from(self, source: Path | None) -> None:
        if source is None or not source.exists():
            self._clear()
            return
        self.pair_dir.mkdir(parents=True, exist_ok=True)
        payload = source.read_bytes()
        _atomic_write_bytes(self.record_path, payload)
        if self.run_record_path is not None:
            _atomic_write_bytes(self.run_record_path, payload)

    def _clear(self) -> None:
        self.record_path.unlink(missing_ok=True)
        if self.run_record_path is not None:
            self.run_record_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class _RoleSessionSpec:
    """Typed launch command for one review-exchange role process."""

    role: Role
    agent: AgentConfig
    worktree: Path
    run_dir: Path
    recording_path: Path
    response_file: Path
    completion_path: Path
    validation_output_dir: Path
    review_report_file: Path | None
    runtime_config: RuntimeConfigReference
    agent_label: str
    web_port: int | None
    issue_number: int
    issue_title: str
    session_name: str
    response_channel: ResponseChannel


@dataclass
class _RoleSessionOwner:
    """Owns one mutable role session inside a cached persistent pair.

    A role turn is complete when its required artifacts parse and validate.
    Some providers still exit after that successful turn even when the
    bootstrap asks them to stay alive. That process exit is not a failure of
    the completed turn; it only matters when the exchange later needs the
    same role again. This owner centralizes that observation and replacement
    policy so round-loop callers do not reach through pair/session internals.
    """

    pair: PersistentExchangePair
    spec: _RoleSessionSpec
    slice_path: Path
    completed_turns_on_current_process: int = 0

    def attach_slice_mirror(self) -> None:
        _attach_slice_mirror(self._current_session(), self.slice_path)

    def detach_slice_mirror(self) -> None:
        _detach_slice_mirror(self._current_session(), self.slice_path)

    def ensure_live(self) -> PersistentSession:
        session = self._current_session()
        if session.is_live:
            if (
                self.completed_turns_on_current_process > 0
                and role_session_needs_fresh_prompt_process(self.spec.agent)
            ):
                return self._respawn(trigger=FRESH_CODEX_PROMPT_PROCESS_TRIGGER)
            return session
        return self._respawn(trigger="session is not live before next prompt")

    def note_turn_completed(self) -> None:
        """Record that the current process has completed a role turn."""
        self.completed_turns_on_current_process += 1

    def respawn(self) -> PersistentSession:
        """Force-replace the role process with a fresh one in the same worktree.

        Used after a round failed because the process is dead/unusable (see
        ``is_process_unusable_failure``). Unlike ``ensure_live``, this does not
        consult ``is_live`` — the round-runner already observed the failure,
        which can race ahead of ``proc.poll()`` (e.g. a prompt-write failure
        where the handle still looks open).
        """
        return self._respawn(trigger="process unusable after round failure")

    def _respawn(self, *, trigger: str) -> PersistentSession:
        session = self._current_session()
        exit_code = session.proc.poll()
        logger.warning(
            "[REVIEW_EXCHANGE] %s %s; respawning role process issue=%s "
            "session_name=%s previous_pid=%d exit_code=%s closed=%s "
            "response_file=%s recording_path=%s worktree=%s",
            self.spec.role.value,
            trigger,
            self.spec.issue_number,
            self.spec.session_name,
            session.proc.pid,
            exit_code,
            session.closed,
            self.spec.response_file,
            self.spec.recording_path,
            self.spec.worktree,
            extra=log_context(
                issue_key=f"issue-{self.spec.issue_number}",
                session_id=self.spec.session_name,
            ),
        )
        _detach_slice_mirror(session, self.slice_path)
        try:
            close_persistent_session(session)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[REVIEW_EXCHANGE] failed to close exited %s session during "
                "respawn; continuing with a fresh process issue=%s "
                "session_name=%s previous_pid=%d",
                self.spec.role.value,
                self.spec.issue_number,
                self.spec.session_name,
                session.proc.pid,
                extra=log_context(
                    issue_key=f"issue-{self.spec.issue_number}",
                    session_id=self.spec.session_name,
                ),
            )

        new_session = _open_role_session_from_spec(self.spec)
        try:
            _attach_slice_mirror(new_session, self.slice_path)
        except BaseException:
            close_persistent_session(new_session)
            raise
        self._replace_session(new_session)
        self.completed_turns_on_current_process = 0
        logger.info(
            "[REVIEW_EXCHANGE] respawned %s session issue=%s session_name=%s "
            "previous_pid=%d new_pid=%d response_file=%s recording_path=%s",
            self.spec.role.value,
            self.spec.issue_number,
            self.spec.session_name,
            session.proc.pid,
            new_session.proc.pid,
            self.spec.response_file,
            self.spec.recording_path,
            extra=log_context(
                issue_key=f"issue-{self.spec.issue_number}",
                session_id=self.spec.session_name,
            ),
        )
        return new_session

    def _current_session(self) -> PersistentSession:
        if self.spec.role is Role.CODER:
            return self.pair.coder_session
        if self.spec.role is Role.REVIEWER:
            return self.pair.reviewer_session
        raise RuntimeError(f"unsupported review-exchange role: {self.spec.role}")

    def _replace_session(self, session: PersistentSession) -> None:
        if self.spec.role is Role.CODER:
            self.pair.coder_session = session
            return
        if self.spec.role is Role.REVIEWER:
            self.pair.reviewer_session = session
            return
        raise RuntimeError(f"unsupported review-exchange role: {self.spec.role}")


# ---------------------------------------------------------------------------
# Session bring-up
# ---------------------------------------------------------------------------


def _derive_bootstrap_agent(agent: AgentConfig, bootstrap_prompt: str) -> AgentConfig:
    """Derive the launch config for one exchange role from the configured agent.

    Uses :func:`dataclasses.replace` so EVERY policy-bearing field — notably the
    ADR-0034 ``sandbox`` opt-in — is preserved by construction. The prior
    field-by-field rebuild silently dropped any field it forgot to copy, which
    is how an opted-in agent launched UNSANDBOXED on the exchange path.

    Only two fields are transformed: ``provider`` resolves through
    ``resolve_launch_provider`` (so an ``ai_system``-only stub launches
    correctly), and ``initial_prompt`` becomes the role bootstrap. Sharing the
    ``provider_args`` reference is safe — the command builder copies it before
    mutating (``AgentConfig._build_provider_command``).
    """
    return replace(
        agent,
        provider=agent.resolve_launch_provider(),
        initial_prompt=bootstrap_prompt,
    )


def _open_role_session_from_spec(spec: _RoleSessionSpec) -> PersistentSession:
    return _open_role_session(spec)


def _open_role_session(spec: _RoleSessionSpec) -> PersistentSession:
    """Build the launch command + env for one role and open the persistent session.

    Per-role files are stable for the pair lifetime within one exchange run:
    response/report files live inside the role worktrees so sandboxed agents can
    write them. Coder validation uses the exchange run dir because
    ``coding-done`` must receive owner-injected run assets.
    """
    role = spec.role.value
    agent = spec.agent
    worktree = spec.worktree
    recording_path = spec.recording_path
    response_file = spec.response_file
    completion_path = spec.completion_path
    validation_output_dir = spec.validation_output_dir
    review_report_file = spec.review_report_file
    agent_label = spec.agent_label
    web_port = spec.web_port
    issue_number = spec.issue_number
    issue_title = spec.issue_title
    session_name = spec.session_name

    bootstrap = build_bootstrap_prompt(
        role=spec.role,
        issue_number=issue_number,
        issue_title=issue_title,
        response_channel=spec.response_channel,
        response_file=response_file,
    )
    bootstrap_agent = _derive_bootstrap_agent(agent, bootstrap)
    command_str = bootstrap_agent.get_command(
        issue_number=issue_number,
        issue_title=issue_title,
        worktree=worktree,
        task_kind=f"review_exchange_{role}",
    )
    import shlex

    command = shlex.split(command_str)

    response_file.parent.mkdir(parents=True, exist_ok=True)
    completion_path.parent.mkdir(parents=True, exist_ok=True)
    validation_output_dir.mkdir(parents=True, exist_ok=True)
    env = _build_role_env(
        role=role,
        response_file=response_file,
        review_report_file=review_report_file,
        completion_path=completion_path,
        validation_output_dir=validation_output_dir,
        worktree=worktree,
        runtime_config=spec.runtime_config,
        agent_label=agent_label,
        web_port=web_port,
        issue_number=issue_number,
        session_name=session_name,
    )
    return open_persistent_session(
        command=command,
        working_dir=worktree,
        env=env,
        recording_path=recording_path,
        # No ``additional_recording_paths`` here — the original B2
        # design tried to mirror the pair-scoped recording into
        # ``run_dir/<role>/terminal-recording.jsonl`` for backward
        # compat, but the writer's mirror paths are fixed at spawn
        # time. If a process were reused for another run, the writer
        # would keep writing to the old run_dir mirror. The pair contract
        # now releases on run-binding changes; this comment documents why
        # static spawn-time mirror paths remain unsafe.
        # ManifestAccessor now reads ``coder_recording`` /
        # ``reviewer_recording`` from the per-exchange manifest
        # instead (they point at the current run's per-session slice),
        # while ``<role>_recording_pair`` preserves the pair-scoped
        # canonical file for diagnostics.
    )


def _build_role_env(
    *,
    role: str,
    response_file: Path,
    review_report_file: Path | None,
    completion_path: Path,
    validation_output_dir: Path,
    worktree: Path,
    runtime_config: RuntimeConfigReference,
    agent_label: str,
    web_port: int | None,
    issue_number: int,
    session_name: str,
) -> dict[str, str]:
    """Compose the agent environment via the shared filtered-env owner.

    Routing through ``build_filtered_env`` is load-bearing: the
    orchestrator process holds GH_TOKEN / GITHUB_TOKEN /
    ISSUE_ORCHESTRATOR_API_TOKEN / CLAUDECODE / SSH_AUTH_SOCK and
    similar credentials that long-lived agent processes must NOT
    inherit. The active runner (``control/review_exchange_loop._run_agent_round``)
    goes through this same helper for the same reason; bypassing it here
    would let coder/reviewer agents run with admin GitHub tokens, the
    Control API admin bearer, etc.

    The agent's env is set once at spawn, so these paths must be scoped to the
    exchange run that owns the spawned process. Agent-written response/report
    files must be inside the role worktree; coder validation paths use the
    owner-provided run directory.
    """
    from ..control.isolation import build_agent_tool_env
    from .agent_runner_env import build_filtered_env

    overrides: dict[str, str] = {
        f"{ENV_PREFIX}COMPLETION_PATH": str(completion_path),
        f"{ENV_PREFIX}SESSION_ID": session_name,
        f"{ENV_PREFIX}AGENT_LABEL": agent_label,
        f"{ENV_PREFIX}ISSUE_NUMBER": str(issue_number),
        f"{ENV_PREFIX}REVIEW_RESPONSE_FILE": str(response_file),
        "ORCHESTRATOR_ISSUE_NUMBER": str(issue_number),
        "ORCHESTRATOR_SESSION_ID": session_name,
    }
    overrides.update(runtime_config.to_env())
    if role == Role.CODER.value:
        overrides[f"{ENV_PREFIX}VALIDATION_OUTPUT_DIR"] = str(validation_output_dir)
        overrides[f"{ENV_PREFIX}RUN_DIR"] = str(validation_output_dir)
    if review_report_file is not None:
        review_report_file.parent.mkdir(parents=True, exist_ok=True)
        overrides[f"{ENV_PREFIX}REVIEW_REPORT_FILE"] = str(review_report_file)
    overrides.update(build_agent_tool_env(worktree, base_env={}))
    if web_port is not None:
        overrides["ORCHESTRATOR_API_PORT"] = str(web_port)
    return build_filtered_env(overrides=overrides)


def _agent_provider(agent: AgentConfig) -> AgentProvider:
    raw = agent.provider if agent.provider else agent.ai_system
    if not raw:
        raise ArtifactContractViolation(
            "AgentProvider",
            "value",
            "agent provider or ai_system must be configured",
        )
    return AgentProvider(raw)


def _coerce_runtime_nit_policy(value: str) -> NitPolicy:
    if value in {"ignore", "surface", "address"}:
        return value  # type: ignore[return-value]
    return "surface"


# ---------------------------------------------------------------------------
# Round loop
# ---------------------------------------------------------------------------


def _agent_turn_scope(
    *,
    issue_number: int,
    exchange_run_id: str,
    round_index: int,
    attempt_index: int,
    role: Role,
    provider: AgentProvider,
) -> AgentTurnArtifactScope:
    return AgentTurnArtifactScope(
        issue_number=IssueNumber(issue_number),
        exchange_run_id=ExchangeRunId(exchange_run_id),
        round_index=PositiveRoundIndex(round_index),
        attempt_index=PositiveAttemptIndex(attempt_index),
        role=_artifact_role(role),
        provider=provider,
    )


def _artifact_role(role: Role) -> AgentRole:
    if role is Role.REVIEWER:
        return AgentRole.REVIEWER
    if role is Role.CODER:
        return AgentRole.CODER
    raise ArtifactContractViolation("AgentRole", "value", f"unsupported role: {role}")


def _turn_prompt_path(
    exchange_dir: Path,
    *,
    round_index: int,
    role: Role,
    attempt_index: int,
) -> Path:
    return turn_artifacts.turn_artifact_path(
        exchange_dir,
        round_index=round_index,
        role=role,
        attempt_index=attempt_index,
        suffix="prompt.md",
        create_dir=True,
    )


def _write_role_prompt_inbox(response_file: Path, prompt_text: str) -> Path:
    path = _role_prompt_inbox_path(response_file)
    _atomic_write_bytes(path, prompt_text.encode("utf-8"))
    return path


def _clear_role_prompt_inbox(response_file: Path) -> None:
    _role_prompt_inbox_path(response_file).unlink(missing_ok=True)


def _persist_turn_prompt(
    exchange_dir: Path,
    *,
    round_index: int,
    role: Role,
    attempt_index: int,
    prompt_text: str,
) -> Path:
    path = _turn_prompt_path(
        exchange_dir,
        round_index=round_index,
        role=role,
        attempt_index=attempt_index,
    )
    path.write_text(prompt_text, encoding="utf-8")
    return path


def _chapters_path(run_dir: Path, role: Role) -> Path:
    return run_dir / role.value / "chapters.json"


def _build_reviewer_turn_started(
    *,
    scope: AgentTurnArtifactScope,
    prompt_path: Path,
    recording_path: Path,
    chapters_path: Path,
) -> ReviewerTurnStarted:
    return ReviewerTurnStarted(
        scope=scope,
        prompt=PromptArtifact(scope=scope, file=ExistingNonEmptyFile(prompt_path)),
        terminal_recording=TerminalRecordingArtifact(
            scope=scope,
            file=ExistingFile(recording_path),
        ),
        chapters=ChapterSidecarArtifact(
            scope=scope,
            file=ExistingFile(chapters_path),
        ),
    )


def _build_coder_turn_started(
    *,
    scope: AgentTurnArtifactScope,
    prompt_path: Path,
    recording_path: Path,
    chapters_path: Path,
) -> CoderTurnStarted:
    return CoderTurnStarted(
        scope=scope,
        prompt=PromptArtifact(scope=scope, file=ExistingNonEmptyFile(prompt_path)),
        terminal_recording=TerminalRecordingArtifact(
            scope=scope,
            file=ExistingFile(recording_path),
        ),
        chapters=ChapterSidecarArtifact(
            scope=scope,
            file=ExistingFile(chapters_path),
        ),
    )


def _event_artifact_refs(refs: tuple[ArtifactRef, ...]) -> list[dict[str, str]]:
    return [ref.to_event_artifact() for ref in refs]


def _turn_completed(
    started: ReviewerTurnStarted | CoderTurnStarted,
    result_path: Path,
    response: ReviewExchangeResponse,
) -> ReviewerTurnCompleted | CoderTurnCompleted:
    response_artifact = ReviewResponseArtifact(
        scope=started.scope,
        file=ExistingNonEmptyFile(result_path),
    )
    if isinstance(started, ReviewerTurnStarted):
        return ReviewerTurnCompleted(
            started=started,
            response=response_artifact,
            response_type=response.response_type,
            response_text=response.response_text,
        )
    return CoderTurnCompleted(
        started=started,
        response=response_artifact,
        response_type=response.response_type,
        response_text=response.response_text,
    )


def _persist_turn_contract(
    exchange_dir: Path,
    *,
    round_index: int,
    role: Role,
    attempt_index: int,
    suffix: str,
    fields: dict[str, object],
) -> Path:
    path = turn_artifacts.turn_artifact_path(
        exchange_dir,
        round_index=round_index,
        role=role,
        attempt_index=attempt_index,
        suffix=f"{suffix}.json",
        create_dir=True,
    )
    path.write_text(json.dumps(fields, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _persist_reviewer_artifact_pair(
    *,
    exchange_dir: Path,
    round_index: int,
    reviewer: ReviewExchangeResponse,
    authored_report_path: Path,
    nit_policy: NitPolicy,
) -> Any:
    decision = ReviewDecision.from_agent_payload(
        reviewer.raw_json,
        response_type=reviewer.response_type,
        response_text=reviewer.response_text,
        nit_policy=nit_policy,
    )
    return persist_review_artifact_pair(
        report_path=turn_artifacts.turn_artifact_path(
            exchange_dir,
            round_index=round_index,
            role=Role.REVIEWER,
            attempt_index=1,
            suffix=REVIEW_REPORT_FILENAME,
            create_dir=True,
        ),
        decision_path=turn_artifacts.turn_artifact_path(
            exchange_dir,
            round_index=round_index,
            role=Role.REVIEWER,
            attempt_index=1,
            suffix=REVIEW_DECISION_FILENAME,
            create_dir=True,
        ),
        decision=decision,
        authored_report_path=authored_report_path,
    )


def _reviewer_response_for_addressable_nits(
    reviewer: ReviewExchangeResponse,
    decision: ReviewDecision,
) -> ReviewExchangeResponse:
    nit_lines = "\n".join(
        f"- {item.id}: {item.title}" if item.title else f"- {item.id}"
        for item in decision.nits
    )
    feedback = (
        f"{reviewer.response_text}\n\n"
        "Reviewer approved the implementation, but this coder is configured "
        "to address nits before PR creation. Address these nits in the normal "
        "rework loop:\n"
        f"{nit_lines}"
    )
    return ReviewExchangeResponse(
        response_type="changes_requested",
        response_text=feedback,
        getting_closer=True,
        raw_json=reviewer.raw_json,
        raw_output=reviewer.raw_output,
    )


@dataclass(frozen=True)
class _ReviewerDecisionResult:
    reviewer: ReviewExchangeResponse
    artifact_pair: ReviewArtifactPair
    addressable_nit_rework: bool
    policy_rework_feedback: str | None = None


def _emit_built_event(
    emit: Callable[[EventName, dict[str, Any]], None],
    event: TraceEvent,
) -> None:
    emit(event.event_type, event.data)


def _finalize_reviewer_decision(
    *,
    exchange_dir: Path,
    round_index: int,
    reviewer: ReviewExchangeResponse,
    reviewer_report_path: Path,
    nit_policy: NitPolicy,
    require_validation: bool,
    pair_validation: _PairValidationMirror,
    approval_gate: ReviewExchangeApprovalGate | None,
) -> _ReviewerDecisionResult:
    validation_error = (
        pair_validation.current_validation_error() if require_validation else None
    )
    approval_error = None
    if (
        reviewer.response_type == "ok"
        and validation_error is None
        and approval_gate is not None
    ):
        approval_error = approval_gate.rejection_reason()
    policy_rework_feedback = validation_error or approval_error
    if reviewer.response_type == "ok" and policy_rework_feedback is not None:
        reviewer = ReviewExchangeResponse(
            response_type="changes_requested",
            response_text=(
                f"{policy_rework_feedback} Address it and continue."
            ),
            getting_closer=False,
            raw_json=None,
            raw_output=reviewer.raw_output,
        )

    artifact_pair = _persist_reviewer_artifact_pair(
        exchange_dir=exchange_dir,
        round_index=round_index,
        reviewer=reviewer,
        authored_report_path=reviewer_report_path,
        nit_policy=nit_policy,
    )
    addressable_nit_rework = review_requires_nit_rework(artifact_pair.decision)
    if addressable_nit_rework:
        reviewer = _reviewer_response_for_addressable_nits(
            reviewer,
            artifact_pair.decision,
        )
    return _ReviewerDecisionResult(
        reviewer=reviewer,
        artifact_pair=artifact_pair,
        addressable_nit_rework=addressable_nit_rework,
        policy_rework_feedback=policy_rework_feedback,
    )


def _log_addressable_nit_rework(
    *,
    decision_result: _ReviewerDecisionResult,
    issue_number: int,
    session_name: str,
    round_index: int,
) -> None:
    if not decision_result.addressable_nit_rework:
        return
    decision = decision_result.artifact_pair.decision
    logger.info(
        "[REVIEW_EXCHANGE] reviewer approved with addressable nits; "
        "routing coder through rework issue=%s session_name=%s "
        "round_index=%d nit_policy=%s nit_ids=%s",
        issue_number,
        session_name,
        round_index,
        decision.nit_policy,
        [item.id for item in decision.nits],
    )


def _coder_role_prompted_event(
    *,
    issue_number: int,
    session_name: str,
    round_index: int,
    prompt_chars: int,
    artifact_refs: list[dict[str, str]],
    reviewer: ReviewExchangeResponse,
    decision_result: _ReviewerDecisionResult,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": round_index,
        "attempt_index": 1,
        "role": "coder",
        "prompt_chars": prompt_chars,
        "artifact_refs": artifact_refs,
    }
    rework_reason = _coder_rework_reason(
        reviewer=reviewer,
        decision_result=decision_result,
    )
    if rework_reason is not None:
        event["rework_reason"] = rework_reason
    return event


def _coder_rework_reason(
    *,
    reviewer: ReviewExchangeResponse,
    decision_result: _ReviewerDecisionResult,
) -> str | None:
    if decision_result.policy_rework_feedback is not None:
        return "acceptance_gate"
    if decision_result.addressable_nit_rework:
        return "nits"
    if reviewer.response_type == "changes_requested":
        return "changes_requested"
    return None


def _coder_reviewer_feedback(
    decision_result: _ReviewerDecisionResult,
) -> str:
    """Render reviewer-authored feedback plus any orchestrator gate rejection."""
    feedback = decision_result.artifact_pair.report_path.read_text(encoding="utf-8")
    if decision_result.policy_rework_feedback is None:
        return feedback
    return (
        feedback.rstrip()
        + "\n\n## Orchestrator acceptance gate\n\n"
        + decision_result.policy_rework_feedback
        + "\n"
    )


@dataclass(frozen=True)
class _DriveRoundsCommand:
    session_output: SessionOutput
    run_assets: ReviewExchangeRunAssets
    run_dir: Path
    exchange_dir: Path
    issue_number: int
    issue_title: str
    session_name: str
    exchange_run_id: str
    coder_session_owner: _RoleSessionOwner
    reviewer_session_owner: _RoleSessionOwner
    coder_response: Path
    reviewer_response: Path
    reviewer_report_path: Path
    coder_recording: Path
    reviewer_recording: Path
    coder_completion_path: Path
    validation_record_path: Path
    prompt_files: ReviewExchangePromptFiles
    pair_validation: _PairValidationMirror
    coder_timeout_seconds: float
    reviewer_timeout_seconds: float
    max_rounds: int
    max_no_progress: int
    require_validation: bool
    nit_policy: NitPolicy
    approval_gate: ReviewExchangeApprovalGate | None
    coder_provider: AgentProvider
    reviewer_provider: AgentProvider
    before_reviewer_round: Callable[[int], None] | None
    emit: Callable[[EventName, dict[str, Any]], None]
    coder_mirror: _RoleSliceMirror
    reviewer_mirror: _RoleSliceMirror
    turn_mailbox: "TurnMailbox | None" = None
    response_channels: ReviewExchangeResponseChannels = field(
        default_factory=ReviewExchangeResponseChannels
    )


def _drive_rounds(command: _DriveRoundsCommand) -> ReviewExchangeOutcome:
    session_output = command.session_output
    run_assets = command.run_assets
    run_dir = command.run_dir
    exchange_dir = command.exchange_dir
    issue_number = command.issue_number
    issue_title = command.issue_title
    session_name = command.session_name
    exchange_run_id = command.exchange_run_id
    coder_session_owner = command.coder_session_owner
    reviewer_session_owner = command.reviewer_session_owner
    coder_response = command.coder_response
    reviewer_response = command.reviewer_response
    reviewer_report_path = command.reviewer_report_path
    coder_recording = command.coder_recording
    reviewer_recording = command.reviewer_recording
    coder_completion_path = command.coder_completion_path
    validation_record_path = command.validation_record_path
    prompt_files = command.prompt_files
    pair_validation = command.pair_validation
    coder_timeout_seconds = command.coder_timeout_seconds
    reviewer_timeout_seconds = command.reviewer_timeout_seconds
    max_rounds = command.max_rounds
    max_no_progress = command.max_no_progress
    require_validation = command.require_validation
    nit_policy = command.nit_policy
    approval_gate = command.approval_gate
    coder_provider = command.coder_provider
    reviewer_provider = command.reviewer_provider
    before_reviewer_round = command.before_reviewer_round
    emit = command.emit
    coder_mirror = command.coder_mirror
    reviewer_mirror = command.reviewer_mirror
    turn_mailbox = command.turn_mailbox
    response_channels = command.response_channels

    no_progress_count = 0
    last_reviewer_text: str | None = None
    last_coder_text: str | None = None

    # One artifact-freshness owner per role, constructed here where the
    # pair-scoped paths are known. Every send attempt (initial, coder
    # protocol retry, in-place respawn) resets through these so a dead
    # process's side artifact can never be paired with a later attempt's
    # response. Reviewer side artifact: the authored review report. Coder
    # side artifact: the completion record (which also gates validation
    # freshness — see _RoleAttemptWorkspace).
    reviewer_workspace = _RoleAttemptWorkspace(
        response_file=reviewer_response,
        side_artifact_paths=(reviewer_report_path,),
    )
    coder_workspace = _RoleAttemptWorkspace(
        response_file=coder_response,
        side_artifact_paths=(coder_completion_path,),
    )

    for round_index in range(1, max_rounds + 1):
        if before_reviewer_round is not None:
            before_reviewer_round(round_index)

        # ----- Reviewer turn -----
        reviewer_packet = ReviewExchangeTurnPacket(
            issue_number=issue_number,
            issue_title=issue_title,
            round_index=round_index,
            role=Role.REVIEWER,
            require_validation=require_validation,
            run_dir=run_dir,
            prompt_files=prompt_files,
            last_coder_text=last_coder_text,
            last_reviewer_text=last_reviewer_text,
        )
        _persist_turn_packet(exchange_dir, reviewer_packet)
        reviewer_prompt_text = reviewer_prompt_with_artifact_contract(
            build_reviewer_prompt(reviewer_packet),
            nit_policy=nit_policy,
        )
        reviewer_response_channel = response_channels.for_role(Role.REVIEWER)
        reviewer_prompt_text = prompt_for_response_channel(
            reviewer_prompt_text,
            role=Role.REVIEWER,
            response_channel=reviewer_response_channel,
            response_file=reviewer_response,
        )
        reviewer_prompt_path = _persist_turn_prompt(
            exchange_dir,
            round_index=round_index,
            role=Role.REVIEWER,
            attempt_index=1,
            prompt_text=reviewer_prompt_text,
        )
        reviewer_scope = _agent_turn_scope(
            issue_number=issue_number,
            exchange_run_id=exchange_run_id,
            round_index=round_index,
            attempt_index=1,
            role=Role.REVIEWER,
            provider=reviewer_provider,
        )
        emit(
            EventName.REVIEW_EXCHANGE_ROUND_STARTED,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "round_index": round_index,
            },
        )
        _record_chapter(
            _ChapterRecordCommand(
                session_output=session_output,
                run_dir=run_dir,
                role="reviewer",
                recording_path=reviewer_recording,
                exchange_run_id=exchange_run_id,
                issue_number=issue_number,
                cycle_index=round_index,
                section=CHAPTER_SECTION_PROMPT,
                label=f"Round {round_index} reviewer prompt",
                session_name=session_name,
                emit=emit,
                mirror=reviewer_mirror,
            )
        )
        reviewer_started = _build_reviewer_turn_started(
            scope=reviewer_scope,
            prompt_path=reviewer_prompt_path,
            recording_path=reviewer_mirror.session_slice,
            chapters_path=_chapters_path(run_dir, Role.REVIEWER),
        )
        _persist_turn_contract(
            exchange_dir,
            round_index=round_index,
            role=Role.REVIEWER,
            attempt_index=1,
            suffix="started",
            fields=reviewer_started.to_manifest_fields(),
        )
        emit(
            EventName.REVIEW_EXCHANGE_ROLE_PROMPTED,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "round_index": round_index,
                "attempt_index": 1,
                "role": "reviewer",
                "prompt_chars": len(reviewer_prompt_text),
                "artifact_refs": _event_artifact_refs(reviewer_started.artifact_refs()),
            },
        )
        reviewer = _send_role_round(
            _RoleRoundCommand(
                owner=reviewer_session_owner,
                workspace=reviewer_workspace,
                role=Role.REVIEWER,
                turn_started=reviewer_started,
                recording_path=reviewer_recording,
                prompt=reviewer_prompt_text,
                timeout_seconds=reviewer_timeout_seconds,
                session_output=session_output,
                run_dir=run_dir,
                exchange_dir=exchange_dir,
                exchange_run_id=exchange_run_id,
                issue_number=issue_number,
                cycle_index=round_index,
                session_name=session_name,
                emit=emit,
                mirror=reviewer_mirror,
                turn_mailbox=turn_mailbox,
                response_channel=reviewer_response_channel,
            )
        )
        if reviewer is None:
            return _build_outcome_for_role_timeout(
                run_assets=run_assets,
                exchange_dir=exchange_dir,
                round_index=round_index,
                role=Role.REVIEWER,
                last_reviewer=None,
                emit=emit,
                issue_number=issue_number,
                session_name=session_name,
                validation_record_path=validation_record_path,
            )

        try:
            decision_result = _finalize_reviewer_decision(
                exchange_dir=exchange_dir,
                round_index=round_index,
                reviewer=reviewer,
                nit_policy=nit_policy,
                reviewer_report_path=reviewer_report_path,
                require_validation=require_validation,
                pair_validation=pair_validation,
                approval_gate=approval_gate,
            )
        except ValueError as exc:
            return _build_outcome_for_reviewer_decision_error(
                run_assets=run_assets,
                exchange_dir=exchange_dir,
                round_index=round_index,
                reviewer=reviewer,
                error=exc,
                emit=emit,
                issue_number=issue_number,
                session_name=session_name,
                validation_record_path=validation_record_path,
            )
        reviewer = decision_result.reviewer
        artifact_pair = decision_result.artifact_pair
        _log_addressable_nit_rework(
            decision_result=decision_result,
            issue_number=issue_number,
            session_name=session_name,
            round_index=round_index,
        )

        if reviewer.response_type == "ok":
            return _complete_with_reviewer_ok(
                run_assets=run_assets,
                exchange_dir=exchange_dir,
                round_index=round_index,
                reviewer=reviewer,
                decision=artifact_pair.decision,
                review_artifacts=artifact_pair.to_event_artifacts(),
                emit=emit,
                issue_number=issue_number,
                session_name=session_name,
                validation_record_path=validation_record_path,
            )
        if reviewer.getting_closer is False:
            no_progress_count += 1
        else:
            no_progress_count = 0
        if max_no_progress > 0 and no_progress_count >= max_no_progress:
            return _stop_for_no_progress(
                run_assets=run_assets,
                exchange_dir=exchange_dir,
                round_index=round_index,
                reviewer=reviewer,
                decision=artifact_pair.decision,
                review_artifacts=artifact_pair.to_event_artifacts(),
                emit=emit,
                issue_number=issue_number,
                session_name=session_name,
                validation_record_path=validation_record_path,
            )

        last_reviewer_text = reviewer.response_text

        # ----- Coder turn -----
        coder_packet = ReviewExchangeTurnPacket(
            issue_number=issue_number,
            issue_title=issue_title,
            round_index=round_index,
            role=Role.CODER,
            require_validation=require_validation,
            run_dir=run_dir,
            reviewer_feedback=_coder_reviewer_feedback(decision_result),
        )
        _persist_turn_packet(exchange_dir, coder_packet)
        coder_response_channel = response_channels.for_role(Role.CODER)
        coder_prompt_text = prompt_for_response_channel(
            build_coder_prompt(coder_packet),
            role=Role.CODER,
            response_channel=coder_response_channel,
            response_file=coder_response,
        )
        coder_prompt_path = _persist_turn_prompt(
            exchange_dir,
            round_index=round_index,
            role=Role.CODER,
            attempt_index=1,
            prompt_text=coder_prompt_text,
        )
        coder_scope = _agent_turn_scope(
            issue_number=issue_number,
            exchange_run_id=exchange_run_id,
            round_index=round_index,
            attempt_index=1,
            role=Role.CODER,
            provider=coder_provider,
        )
        _record_chapter(
            _ChapterRecordCommand(
                session_output=session_output,
                run_dir=run_dir,
                role="coder",
                recording_path=coder_recording,
                exchange_run_id=exchange_run_id,
                issue_number=issue_number,
                cycle_index=round_index,
                section=CHAPTER_SECTION_PROMPT,
                label=f"Round {round_index} coder prompt",
                session_name=session_name,
                emit=emit,
                mirror=coder_mirror,
            )
        )
        coder_started = _build_coder_turn_started(
            scope=coder_scope,
            prompt_path=coder_prompt_path,
            recording_path=coder_mirror.session_slice,
            chapters_path=_chapters_path(run_dir, Role.CODER),
        )
        _persist_turn_contract(
            exchange_dir,
            round_index=round_index,
            role=Role.CODER,
            attempt_index=1,
            suffix="started",
            fields=coder_started.to_manifest_fields(),
        )
        emit(
            EventName.REVIEW_EXCHANGE_ROLE_PROMPTED,
            _coder_role_prompted_event(
                issue_number=issue_number,
                session_name=session_name,
                round_index=round_index,
                prompt_chars=len(coder_prompt_text),
                artifact_refs=_event_artifact_refs(coder_started.artifact_refs()),
                reviewer=reviewer,
                decision_result=decision_result,
            ),
        )
        coder = _send_role_round(
            _RoleRoundCommand(
                owner=coder_session_owner,
                workspace=coder_workspace,
                role=Role.CODER,
                turn_started=coder_started,
                recording_path=coder_recording,
                prompt=coder_prompt_text,
                timeout_seconds=coder_timeout_seconds,
                session_output=session_output,
                run_dir=run_dir,
                exchange_dir=exchange_dir,
                exchange_run_id=exchange_run_id,
                issue_number=issue_number,
                cycle_index=round_index,
                session_name=session_name,
                emit=emit,
                mirror=coder_mirror,
                turn_mailbox=turn_mailbox,
                response_channel=coder_response_channel,
            )
        )
        if coder is None:
            return _build_outcome_for_role_timeout(
                run_assets=run_assets,
                exchange_dir=exchange_dir,
                round_index=round_index,
                role=Role.CODER,
                last_reviewer=reviewer,
                emit=emit,
                issue_number=issue_number,
                session_name=session_name,
                validation_record_path=validation_record_path,
            )

        coder, protocol_outcome = _enforce_coder_protocol(
            _CoderProtocolCommand(
                session_output=session_output,
                coder_session_owner=coder_session_owner,
                coder_workspace=coder_workspace,
                coder=coder,
                reviewer=reviewer,
                coder_provider=coder_provider,
                run_dir=run_dir,
                exchange_dir=exchange_dir,
                coder_recording=coder_recording,
                coder_completion_path=coder_completion_path,
                validation_record_path=validation_record_path,
                pair_validation=pair_validation,
                run_assets=run_assets,
                coder_timeout_seconds=coder_timeout_seconds,
                require_validation=require_validation,
                exchange_run_id=exchange_run_id,
                issue_number=issue_number,
                session_name=session_name,
                cycle_index=round_index,
                emit=emit,
                coder_mirror=coder_mirror,
                turn_mailbox=turn_mailbox,
                response_channel=coder_response_channel,
            )
        )
        if protocol_outcome is not None:
            return protocol_outcome

        decision = artifact_pair.decision
        _emit_built_event(
            emit,
            make_review_exchange_round_completed_event(
                {
                    "issue_number": issue_number,
                    "session_name": session_name,
                    "round_index": round_index,
                    "reviewer_response_type": reviewer.response_type,
                    "reviewer_response_text": reviewer.response_text,
                    "review_decision_verdict": decision.verdict,
                    "review_nit_policy": decision.nit_policy,
                    "review_abstraction_status": decision.abstraction_review.status,
                    "artifacts": artifact_pair.to_event_artifacts(),
                    "coder_response_type": coder.response_type,
                    "coder_response_text": coder.response_text,
                }
            ),
        )
        last_coder_text = coder.response_text

    summary = _write_summary(
        exchange_dir, max_rounds,
        status=ReviewExchangeStatus.STOPPED,
        reason=ReviewExchangeReason.MAX_ROUNDS_EXCEEDED,
        reviewer_response=None,
        validation_record_path=validation_record_path,
    )
    _emit_built_event(emit, make_review_exchange_completed_event({
        "issue_number": issue_number,
        "session_name": session_name,
        "rounds": max_rounds,
        "status": ReviewExchangeStatus.STOPPED.value,
        "reason": ReviewExchangeReason.MAX_ROUNDS_EXCEEDED.value,
    }))
    return ReviewExchangeOutcome(
        status=ReviewExchangeStatus.STOPPED,
        rounds=max_rounds,
        reason=ReviewExchangeReason.MAX_ROUNDS_EXCEEDED,
        run_assets=run_assets,
        reviewer_response=None,
        summary=summary,
    )


@dataclass(frozen=True)
class _CoderProtocolCommand:
    session_output: SessionOutput
    coder_session_owner: _RoleSessionOwner
    coder_workspace: _RoleAttemptWorkspace
    coder: ReviewExchangeResponse
    reviewer: ReviewExchangeResponse
    coder_provider: AgentProvider
    run_dir: Path
    exchange_dir: Path
    coder_recording: Path
    coder_completion_path: Path
    validation_record_path: Path
    pair_validation: _PairValidationMirror
    run_assets: ReviewExchangeRunAssets
    coder_timeout_seconds: float
    require_validation: bool
    exchange_run_id: str
    issue_number: int
    session_name: str
    cycle_index: int
    emit: Callable[[EventName, dict[str, Any]], None]
    coder_mirror: _RoleSliceMirror
    turn_mailbox: "TurnMailbox | None" = None
    response_channel: ResponseChannel = "mailbox"


def _enforce_coder_protocol(
    command: _CoderProtocolCommand,
) -> tuple[ReviewExchangeResponse, ReviewExchangeOutcome | None]:
    """Validate the coder produced its completion-coder.json artifact, retry
    with a remediation prompt up to ``_CODER_PROTOCOL_RETRY_LIMIT`` times,
    and return either the validated response or a terminal outcome.

    Mirrors the active runner's _run_coder_round_with_protocol_retries.
    Without this guardrail a coder could advance the exchange by writing
    only the review-response file while skipping coding-done.
    """
    session_output = command.session_output
    coder_session_owner = command.coder_session_owner
    coder_workspace = command.coder_workspace
    coder = command.coder
    reviewer = command.reviewer
    coder_provider = command.coder_provider
    run_dir = command.run_dir
    exchange_dir = command.exchange_dir
    coder_recording = command.coder_recording
    coder_completion_path = command.coder_completion_path
    validation_record_path = command.validation_record_path
    pair_validation = command.pair_validation
    run_assets = command.run_assets
    coder_timeout_seconds = command.coder_timeout_seconds
    require_validation = command.require_validation
    exchange_run_id = command.exchange_run_id
    issue_number = command.issue_number
    session_name = command.session_name
    cycle_index = command.cycle_index
    emit = command.emit
    coder_mirror = command.coder_mirror
    turn_mailbox = command.turn_mailbox

    protocol_error = _validate_coder_completion(
        completion_path=coder_completion_path,
        pair_validation=pair_validation,
        run_validation_record_path=run_dir / "validation-record.json",
        require_validation=require_validation,
    )
    next_attempt_index = 2
    while (
        protocol_error is not None
        and next_attempt_index <= _CODER_PROTOCOL_RETRY_LIMIT + 1
    ):
        attempt_index = next_attempt_index
        next_attempt_index += 1
        retry_prompt = (
            f"{protocol_error}\n"
            "Run `coding-done completed --implementation '...' --problems '...'` "
            "(or `coding-done blocked --reason '...' --attempted '...'` if you "
            "cannot continue), then submit your verdict again by running "
            "`exchange-respond <ok|disagree> --text '...'`."
        )
        retry_prompt = prompt_for_response_channel(
            retry_prompt,
            role=Role.CODER,
            response_channel=command.response_channel,
            response_file=coder_workspace.response_file,
        )
        retry_prompt_path = _persist_turn_prompt(
            exchange_dir,
            round_index=cycle_index,
            role=Role.CODER,
            attempt_index=attempt_index,
            prompt_text=retry_prompt,
        )
        retry_scope = _agent_turn_scope(
            issue_number=issue_number,
            exchange_run_id=exchange_run_id,
            round_index=cycle_index,
            attempt_index=attempt_index,
            role=Role.CODER,
            provider=coder_provider,
        )
        _record_chapter(
            _ChapterRecordCommand(
                session_output=session_output,
                run_dir=run_dir,
                role="coder",
                recording_path=coder_recording,
                exchange_run_id=exchange_run_id,
                issue_number=issue_number,
                cycle_index=cycle_index,
                section=CHAPTER_SECTION_PROMPT,
                label=f"Round {cycle_index} coder protocol-retry",
                session_name=session_name,
                emit=emit,
                mirror=coder_mirror,
            )
        )
        retry_started = _build_coder_turn_started(
            scope=retry_scope,
            prompt_path=retry_prompt_path,
            recording_path=coder_mirror.session_slice,
            chapters_path=_chapters_path(run_dir, Role.CODER),
        )
        _persist_turn_contract(
            exchange_dir,
            round_index=cycle_index,
            role=Role.CODER,
            attempt_index=attempt_index,
            suffix="started",
            fields=retry_started.to_manifest_fields(),
        )
        emit(
            EventName.REVIEW_EXCHANGE_ROLE_PROMPTED,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "round_index": cycle_index,
                "attempt_index": attempt_index,
                "role": "coder",
                "prompt_chars": len(retry_prompt),
                "protocol_retry": True,
                "artifact_refs": _event_artifact_refs(retry_started.artifact_refs()),
            },
        )
        retry_response = _send_role_round(
            _RoleRoundCommand(
                owner=coder_session_owner,
                workspace=coder_workspace,
                role=Role.CODER,
                turn_started=retry_started,
                recording_path=coder_recording,
                prompt=retry_prompt,
                timeout_seconds=coder_timeout_seconds,
                session_output=session_output,
                run_dir=run_dir,
                exchange_dir=exchange_dir,
                exchange_run_id=exchange_run_id,
                issue_number=issue_number,
                cycle_index=cycle_index,
                session_name=session_name,
                emit=emit,
                mirror=coder_mirror,
                turn_mailbox=turn_mailbox,
                response_channel=command.response_channel,
            )
        )
        if retry_response is None:
            return coder, _build_outcome_for_role_timeout(
                run_assets=run_assets,
                exchange_dir=exchange_dir,
                round_index=cycle_index,
                role=Role.CODER,
                last_reviewer=reviewer,
                emit=emit,
                issue_number=issue_number,
                session_name=session_name,
                validation_record_path=validation_record_path,
            )
        coder = retry_response
        protocol_error = _validate_coder_completion(
            completion_path=coder_completion_path,
            pair_validation=pair_validation,
            run_validation_record_path=run_dir / "validation-record.json",
            require_validation=require_validation,
        )
    if protocol_error is not None:
        return coder, _build_outcome_for_protocol_error(
            run_assets=run_assets,
            exchange_dir=exchange_dir,
            round_index=cycle_index,
            last_reviewer=reviewer,
            last_coder=coder,
            protocol_error=protocol_error,
            emit=emit,
            validation_record_path=validation_record_path,
            issue_number=issue_number,
            session_name=session_name,
        )
    return coder, None


@dataclass(frozen=True)
class _RoleRoundCommand:
    owner: _RoleSessionOwner
    workspace: _RoleAttemptWorkspace
    role: Role
    turn_started: ReviewerTurnStarted | CoderTurnStarted
    recording_path: Path
    prompt: str
    timeout_seconds: float
    session_output: SessionOutput
    run_dir: Path
    exchange_dir: Path
    exchange_run_id: str
    issue_number: int
    cycle_index: int
    session_name: str
    emit: Callable[[EventName, dict[str, Any]], None]
    mirror: _RoleSliceMirror
    turn_mailbox: "TurnMailbox | None" = None
    response_channel: ResponseChannel = "mailbox"
    respawn_retries: int = 0


def _send_role_round(command: _RoleRoundCommand) -> ReviewExchangeResponse | None:
    """Send one role's round prompt and convert the response to a domain object.

    Returns ``None`` if the role timed out or died — the caller emits
    REVIEW_EXCHANGE_ROLE_TIMEOUT and bails out of the exchange.

    If the round fails because the role *process* is dead/unusable (e.g. a
    one-shot reviewer that exited cleanly between rounds), the role is
    respawned in place via ``owner`` and the same turn is retried — up to
    ``_MAX_ROLE_RESPAWN_RETRIES`` times — before giving up. This keeps the
    exchange on the same worktree and advances to the next required turn
    instead of tearing the pair down and restarting at round 1.

    Every attempt — initial, coder protocol retry, and respawn retry — first
    resets ``workspace`` so a dead process's side artifact (reviewer report or
    coder completion) can never be paired with a later attempt's response.

    Persists the per-attempt parsed result as a session artifact under
    ``<exchange_dir>/turns/round-<n>-<role>-attempt-<m>.result.json``
    for replay and diagnostics.
    """
    owner = command.owner
    workspace = command.workspace
    role = command.role
    turn_started = command.turn_started
    recording_path = command.recording_path
    prompt = command.prompt
    timeout_seconds = command.timeout_seconds
    session_output = command.session_output
    run_dir = command.run_dir
    exchange_dir = command.exchange_dir
    exchange_run_id = command.exchange_run_id
    issue_number = command.issue_number
    cycle_index = command.cycle_index
    session_name = command.session_name
    emit = command.emit
    mirror = command.mirror
    turn_mailbox = command.turn_mailbox
    response_channel = command.response_channel
    respawn_retries = command.respawn_retries

    session = owner.ensure_live()
    workspace.prepare_for_attempt()
    role_value = role.value
    attempt_index = turn_started.scope.attempt_index.value
    prompt_inbox_path = _write_role_prompt_inbox(workspace.response_file, prompt)
    pty_notice = build_prompt_inbox_notice(
        role=role,
        round_index=cycle_index,
        attempt_index=attempt_index,
        prompt_path=prompt_inbox_path,
        response_channel=response_channel,
    )
    # Open the orchestrator-owned rendezvous for this turn before prompting,
    # so the agent's exchange-respond delivery has a slot to land in. The
    # routing key is the per-role response-file path (stable, already in the
    # agent's env) — used purely as an opaque identifier; nothing is written
    # to or read from that path. ``response_reader`` makes send_round poll
    # the mailbox instead of the filesystem. When no mailbox is wired (older
    # call sites / unit stubs), send_round falls back to file polling.
    mailbox_key = str(workspace.response_file)
    response_reader: Callable[[], dict[str, Any] | None] | None = None
    use_mailbox = response_channel == "mailbox" and turn_mailbox is not None
    if use_mailbox:
        assert turn_mailbox is not None
        mailbox = turn_mailbox
        mailbox.open(
            mailbox_key,
            turn_id=f"{role_value}-r{cycle_index}-a{attempt_index}",
        )

        def _read_from_mailbox() -> dict[str, Any] | None:
            return mailbox.try_take(mailbox_key)

        response_reader = _read_from_mailbox
    try:
        parsed = send_round(
            session,
            prompt=pty_notice,
            response_file=workspace.response_file,
            timeout_seconds=timeout_seconds,
            # Tag heartbeat/diagnostic logs with role + cycle so an
            # interleaved coder + reviewer log is decodable without
            # cross-referencing PIDs (#6160 e2e regression: 17 minutes
            # of unattributed silence).
            role_label=f"{role_value}@round-{cycle_index}",
            response_reader=response_reader,
        )
    except (PersistentRoundTimeoutError, PersistentRoundError) as exc:
        # Release the slot before respawn/return so a straggling delivery
        # from this failed attempt is rejected (NO_OPEN_SLOT) rather than
        # landing in a later turn's slot. A respawn re-opens a fresh slot.
        if use_mailbox:
            assert turn_mailbox is not None
            turn_mailbox.close(mailbox_key)
        failure_reason = persistent_round_failure_reason(exc)
        logger.warning(
            "[REVIEW_EXCHANGE] %s round failed issue=%s session_name=%s "
            "round_index=%d attempt_index=%d failure_reason=%s pid=%d response_file=%s "
            "recording_path=%s error=%s",
            role_value,
            issue_number,
            session_name,
            cycle_index,
            attempt_index,
            failure_reason,
            session.proc.pid,
            workspace.response_file,
            recording_path,
            exc,
        )
        # The role process is dead/unusable (e.g. a one-shot reviewer that
        # exited cleanly between rounds). Respawn it in place — same worktree,
        # recording, response/completion/validation paths — and retry the same
        # turn rather than tearing the whole pair down and restarting round 1.
        if (
            is_process_unusable_failure(failure_reason)
            and respawn_retries < _MAX_ROLE_RESPAWN_RETRIES
        ):
            logger.warning(
                "[REVIEW_EXCHANGE] %s round %d had a dead/unusable process "
                "(reason=%s pid=%d); respawning in place and retrying the same "
                "turn (retry %d/%d) issue=%s session_name=%s",
                role_value,
                cycle_index,
                failure_reason,
                session.proc.pid,
                respawn_retries + 1,
                _MAX_ROLE_RESPAWN_RETRIES,
                issue_number,
                session_name,
                extra=log_context(
                    issue_key=f"issue-{issue_number}",
                    session_id=session_name,
                ),
            )
            owner.respawn()
            return _send_role_round(
                replace(command, respawn_retries=respawn_retries + 1),
            )
        # The typed result artifact must exist on the failure path
        # too — this is the case operators most need to inspect, and
        # an asymmetric "result.json only on the happy path" contract
        # would leave the on-disk trail incomplete for exactly the
        # rounds that need replay/forensics.
        typed_result = ReviewExchangeTurnResult.for_no_completion(
            str(exc),
            protocol_error_reason=failure_reason,
        )
        result_path = _persist_turn_result(
            exchange_dir,
            round_index=cycle_index,
            role=role,
            attempt_index=attempt_index,
            result=typed_result,
        )
        response = _legacy_response_from_typed_result(typed_result)
        _record_chapter(
            _ChapterRecordCommand(
                session_output=session_output,
                run_dir=run_dir,
                role=role_value,
                recording_path=recording_path,
                exchange_run_id=exchange_run_id,
                issue_number=issue_number,
                cycle_index=cycle_index,
                section=CHAPTER_SECTION_TIMEOUT,
                label=(
                    f"Round {cycle_index} {role_value} "
                    f"{round_failure_chapter_label(failure_reason)}"
                ),
                session_name=session_name,
                emit=emit,
                mirror=mirror,
            )
        )
        completed = _turn_completed(turn_started, result_path, response)
        _persist_turn_contract(
            exchange_dir,
            round_index=cycle_index,
            role=role,
            attempt_index=attempt_index,
            suffix="completed",
            fields=completed.to_manifest_fields(),
        )
        emit(
            EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "round_index": cycle_index,
                "attempt_index": attempt_index,
                "role": role_value,
                "failure_reason": failure_reason,
                "reason": "no_completion",
                "detail": str(exc),
                "artifact_refs": _event_artifact_refs(completed.artifact_refs()),
            },
        )
        return None

    # Verdict received: release the slot so any duplicate/straggling delivery
    # for this turn is rejected rather than buffered for the next turn.
    if use_mailbox:
        assert turn_mailbox is not None
        turn_mailbox.close(mailbox_key)
    typed_result = ReviewExchangeTurnResult.from_agent_dict(parsed, raw_output=None)
    result_path = _persist_turn_result(
        exchange_dir,
        round_index=cycle_index,
        role=role,
        attempt_index=attempt_index,
        result=typed_result,
    )
    response = _legacy_response_from_typed_result(typed_result)
    exit_code = session.proc.poll()
    logger.info(
        "[REVIEW_EXCHANGE] %s round produced valid response issue=%s "
        "session_name=%s round_index=%d attempt_index=%d pid=%d "
        "response_type=%s process_alive=%s exit_code=%s response_file=%s",
        role_value,
        issue_number,
        session_name,
        cycle_index,
        attempt_index,
        session.proc.pid,
        response.response_type,
        exit_code is None,
        exit_code,
        workspace.response_file,
    )
    _record_chapter(
        _ChapterRecordCommand(
            session_output=session_output,
            run_dir=run_dir,
            role=role_value,
            recording_path=recording_path,
            exchange_run_id=exchange_run_id,
            issue_number=issue_number,
            cycle_index=cycle_index,
            section=CHAPTER_SECTION_FEEDBACK,
            label=f"Round {cycle_index} {role_value} feedback",
            session_name=session_name,
            emit=emit,
            mirror=mirror,
        )
    )
    completed = _turn_completed(turn_started, result_path, response)
    _persist_turn_contract(
        exchange_dir,
        round_index=cycle_index,
        role=role,
        attempt_index=attempt_index,
        suffix="completed",
        fields=completed.to_manifest_fields(),
    )
    emit(
        EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK,
        {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": cycle_index,
            "attempt_index": attempt_index,
            "role": role_value,
            "response_type": response.response_type,
            "getting_closer": response.getting_closer,
            "artifact_refs": _event_artifact_refs(completed.artifact_refs()),
        },
    )
    owner.note_turn_completed()
    return response


def _legacy_response_from_typed_result(
    result: ReviewExchangeTurnResult,
) -> ReviewExchangeResponse:
    """Adapter for outcome code still typed as ``ReviewExchangeResponse``.

    Remove once exchange outcome/control consumers accept
    ``ReviewExchangeTurnResult`` directly.
    """
    if result.kind is TurnResultKind.PROTOCOL_ERROR:
        return ReviewExchangeResponse(
            response_type="protocol_error",
            response_text=result.response_text,
            getting_closer=result.getting_closer
            if result.getting_closer is not None
            else False,
            raw_json=result.raw_json,
            raw_output=result.raw_output,
        )
    return ReviewExchangeResponse(
        response_type=result.kind.value,
        response_text=result.response_text,
        getting_closer=result.getting_closer,
        raw_json=result.raw_json,
        raw_output=result.raw_output,
    )


def _persist_turn_packet(
    exchange_dir: Path,
    packet: ReviewExchangeTurnPacket,
) -> None:
    """Write the per-turn input packet as a session artifact.

    The artifact lives at
    ``<exchange_dir>/turns/round-<round>-<role>.packet.json`` so a
    failed exchange can be replayed/inspected from the on-disk state
    without walking the recording stream.
    """
    path = turn_artifacts.turn_packet_path(
        exchange_dir,
        round_index=packet.round_index,
        role=packet.role,
    )
    path.write_text(
        json.dumps(packet.to_manifest_fields(), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _persist_turn_result(
    exchange_dir: Path,
    *,
    round_index: int,
    role: Role,
    attempt_index: int,
    result: ReviewExchangeTurnResult,
) -> Path:
    """Write the per-attempt parsed result as a session artifact.

    Sibling to ``_persist_turn_packet``: a failed exchange leaves both
    the packet (what the orchestrator gave the agent) and the result
    (what the orchestrator parsed from the agent's response) on disk.
    """
    path = turn_artifacts.turn_artifact_path(
        exchange_dir,
        round_index=round_index,
        role=role,
        attempt_index=attempt_index,
        suffix="result.json",
        create_dir=True,
    )
    path.write_text(
        json.dumps(result.to_manifest_fields(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Outcome helpers
# ---------------------------------------------------------------------------


def _complete_with_reviewer_ok(
    *,
    run_assets: ReviewExchangeRunAssets,
    exchange_dir: Path,
    round_index: int,
    reviewer: ReviewExchangeResponse,
    decision: ReviewDecision,
    review_artifacts: list[dict[str, str]],
    emit: Callable[[EventName, dict[str, Any]], None],
    issue_number: int,
    session_name: str,
    validation_record_path: Path,
) -> ReviewExchangeOutcome:
    summary = _write_summary(
        exchange_dir, round_index,
        status=ReviewExchangeStatus.OK,
        reason=ReviewExchangeReason.REVIEWER_OK,
        reviewer_response=reviewer,
        review_artifacts=review_artifacts,
        validation_record_path=validation_record_path,
    )
    _emit_built_event(emit, make_review_exchange_round_completed_event({
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": round_index,
        "reviewer_response_type": reviewer.response_type,
        "reviewer_response_text": reviewer.response_text,
        "review_decision_verdict": decision.verdict,
        "review_nit_policy": decision.nit_policy,
        "review_abstraction_status": decision.abstraction_review.status,
        "artifacts": review_artifacts,
        "coder_response_type": None,
    }))
    _emit_built_event(emit, make_review_exchange_completed_event({
        "issue_number": issue_number,
        "session_name": session_name,
        "rounds": round_index,
        "status": ReviewExchangeStatus.OK.value,
        "reason": ReviewExchangeReason.REVIEWER_OK.value,
        "review_decision_verdict": decision.verdict,
        "review_nit_policy": decision.nit_policy,
        "review_abstraction_status": decision.abstraction_review.status,
        "artifacts": review_artifacts,
    }))
    return ReviewExchangeOutcome(
        status=ReviewExchangeStatus.OK,
        rounds=round_index,
        reason=ReviewExchangeReason.REVIEWER_OK,
        run_assets=run_assets,
        reviewer_response=reviewer,
        summary=summary,
    )


def _stop_for_no_progress(
    *,
    run_assets: ReviewExchangeRunAssets,
    exchange_dir: Path,
    round_index: int,
    reviewer: ReviewExchangeResponse,
    decision: ReviewDecision,
    review_artifacts: list[dict[str, str]],
    emit: Callable[[EventName, dict[str, Any]], None],
    issue_number: int,
    session_name: str,
    validation_record_path: Path,
) -> ReviewExchangeOutcome:
    summary = _write_summary(
        exchange_dir, round_index,
        status=ReviewExchangeStatus.STOPPED,
        reason=ReviewExchangeReason.REVIEWER_REPORTS_NO_PROGRESS,
        reviewer_response=reviewer,
        review_artifacts=review_artifacts,
        validation_record_path=validation_record_path,
    )
    _emit_built_event(emit, make_review_exchange_round_completed_event({
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": round_index,
        "reviewer_response_type": reviewer.response_type,
        "reviewer_response_text": reviewer.response_text,
        "review_decision_verdict": decision.verdict,
        "review_nit_policy": decision.nit_policy,
        "review_abstraction_status": decision.abstraction_review.status,
        "artifacts": review_artifacts,
        "coder_response_type": None,
    }))
    _emit_built_event(emit, make_review_exchange_completed_event({
        "issue_number": issue_number,
        "session_name": session_name,
        "rounds": round_index,
        "status": ReviewExchangeStatus.STOPPED.value,
        "reason": ReviewExchangeReason.REVIEWER_REPORTS_NO_PROGRESS.value,
        "review_decision_verdict": decision.verdict,
        "review_nit_policy": decision.nit_policy,
        "review_abstraction_status": decision.abstraction_review.status,
        "artifacts": review_artifacts,
    }))
    return ReviewExchangeOutcome(
        status=ReviewExchangeStatus.STOPPED,
        rounds=round_index,
        reason=ReviewExchangeReason.REVIEWER_REPORTS_NO_PROGRESS,
        run_assets=run_assets,
        reviewer_response=reviewer,
        summary=summary,
    )


def _build_outcome_for_reviewer_decision_error(
    *,
    run_assets: ReviewExchangeRunAssets,
    exchange_dir: Path,
    round_index: int,
    reviewer: ReviewExchangeResponse,
    error: ValueError,
    emit: Callable[[EventName, dict[str, Any]], None],
    issue_number: int,
    session_name: str,
    validation_record_path: Path,
) -> ReviewExchangeOutcome:
    detail = f"reviewer produced invalid decision JSON: {error}"
    summary = _write_summary(
        exchange_dir, round_index,
        status=ReviewExchangeStatus.ERROR,
        reason=ReviewExchangeReason.REVIEWER_DECISION_INVALID,
        reviewer_response=reviewer,
        validation_record_path=validation_record_path,
        detail=detail,
    )
    _emit_built_event(emit, make_review_exchange_round_completed_event({
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": round_index,
        "reviewer_response_type": reviewer.response_type,
        "reviewer_response_text": reviewer.response_text,
        "coder_response_type": None,
        "detail": detail,
    }))
    _emit_built_event(emit, make_review_exchange_completed_event({
        "issue_number": issue_number,
        "session_name": session_name,
        "rounds": round_index,
        "status": ReviewExchangeStatus.ERROR.value,
        "reason": ReviewExchangeReason.REVIEWER_DECISION_INVALID.value,
        "detail": detail,
    }))
    return ReviewExchangeOutcome(
        status=ReviewExchangeStatus.ERROR,
        rounds=round_index,
        reason=ReviewExchangeReason.REVIEWER_DECISION_INVALID,
        run_assets=run_assets,
        reviewer_response=reviewer,
        summary=summary,
    )


def _build_outcome_for_role_timeout(
    *,
    run_assets: ReviewExchangeRunAssets,
    exchange_dir: Path,
    round_index: int,
    role: Role,
    last_reviewer: ReviewExchangeResponse | None,
    emit: Callable[[EventName, dict[str, Any]], None],
    issue_number: int,
    session_name: str,
    validation_record_path: Path,
) -> ReviewExchangeOutcome:
    """Build the terminal ``error`` outcome when a role fails to complete."""
    reason = _no_completion_reason_for_role(role)
    summary = _write_summary(
        exchange_dir, round_index,
        status=ReviewExchangeStatus.ERROR,
        reason=reason,
        reviewer_response=last_reviewer,
        validation_record_path=validation_record_path,
    )
    _emit_built_event(emit, make_review_exchange_completed_event({
        "issue_number": issue_number,
        "session_name": session_name,
        "rounds": round_index,
        "status": ReviewExchangeStatus.ERROR.value,
        "reason": reason.value,
    }))
    return ReviewExchangeOutcome(
        status=ReviewExchangeStatus.ERROR,
        rounds=round_index,
        reason=reason,
        run_assets=run_assets,
        reviewer_response=last_reviewer,
        summary=summary,
    )


def _no_completion_reason_for_role(role: Role) -> ReviewExchangeReason:
    if role is Role.CODER:
        return ReviewExchangeReason.CODER_NO_COMPLETION
    if role is Role.REVIEWER:
        return ReviewExchangeReason.REVIEWER_NO_COMPLETION
    raise ValueError(f"unsupported review-exchange role: {role.value}")


def _build_outcome_for_protocol_error(
    *,
    run_assets: ReviewExchangeRunAssets,
    exchange_dir: Path,
    round_index: int,
    last_reviewer: ReviewExchangeResponse | None,
    last_coder: ReviewExchangeResponse | None,
    protocol_error: str,
    emit: Callable[[EventName, dict[str, Any]], None],
    issue_number: int,
    session_name: str,
    validation_record_path: Path,
) -> ReviewExchangeOutcome:
    """Build the ``error`` outcome when the coder fails its protocol contract.

    Mirrors the active runner's ``_stop_for_protocol_error``: emits a
    REVIEW_EXCHANGE_ROUND_COMPLETED with the partial round's data plus a
    REVIEW_EXCHANGE_COMPLETED with status=error and protocol_error reason.
    """
    summary = _write_summary(
        exchange_dir, round_index,
        status=ReviewExchangeStatus.ERROR,
        reason=ReviewExchangeReason.CODER_PROTOCOL_ERROR,
        reviewer_response=last_reviewer,
        validation_record_path=validation_record_path,
    )
    _emit_built_event(emit, make_review_exchange_round_completed_event({
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": round_index,
        "reviewer_response_type": last_reviewer.response_type if last_reviewer else None,
        "reviewer_response_text": last_reviewer.response_text if last_reviewer else None,
        "coder_response_type": "protocol_error",
        "coder_response_text": last_coder.response_text if last_coder else None,
        "detail": protocol_error,
    }))
    _emit_built_event(emit, make_review_exchange_completed_event({
        "issue_number": issue_number,
        "session_name": session_name,
        "rounds": round_index,
        "status": ReviewExchangeStatus.ERROR.value,
        "reason": ReviewExchangeReason.CODER_PROTOCOL_ERROR.value,
        "detail": protocol_error,
    }))
    return ReviewExchangeOutcome(
        status=ReviewExchangeStatus.ERROR,
        rounds=round_index,
        reason=ReviewExchangeReason.CODER_PROTOCOL_ERROR,
        run_assets=run_assets,
        reviewer_response=last_reviewer,
        summary=summary,
    )


def _write_review_exchange_manifest_header(
    session_output: SessionOutput,
    run_dir: Path,
    header: ReviewExchangeManifestHeader,
) -> None:
    """Stamp the review-exchange manifest header.

    Extracted to keep ``run_persistent_session_exchange`` under the
    C901 ceiling and to give the manifest section a typed name. The
    header itself documents the contract; this helper is the seam
    where the typed value crosses into the loose-dict
    ``update_manifest`` API.
    """
    session_output.update_manifest(run_dir, header.to_manifest_fields())


def _read_validation_facts(path: Path | None) -> tuple[str | None, bool | None]:
    """Read ``(head_sha, passed)`` from a validation-record.json.

    Returns ``(None, None)`` when the path is None, missing, or
    unreadable as JSON. ``head_sha`` is None when the field is
    absent/empty; ``passed`` is None when the field is absent or
    not a bool.

    The summary writer (and the cache loader, in a later commit)
    use this to populate ``ResumeFacts`` without leaking validation-
    record schema concerns into other modules.
    """
    if path is None or not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    head_sha = data.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        head_sha = None
    passed = data.get("passed")
    if not isinstance(passed, bool):
        passed = None
    return head_sha, passed


def _write_summary(
    exchange_dir: Path,
    round_index: int,
    *,
    status: ReviewExchangeStatus,
    reason: ReviewExchangeReason,
    reviewer_response: ReviewExchangeResponse | None,
    validation_record_path: Path | None,
    review_artifacts: list[dict[str, str]] | None = None,
    detail: str | None = None,
) -> ReviewExchangeSummaryV1:
    """Persist summary.json atomically.

    The summary records *facts* about the exchange that just ran:
    ``status``, ``reason``, ``completed_rounds``, ``response_text``,
    ``timestamp``, plus — when the validation record is readable —
    ``head_sha`` and ``validation_passed``. Policy (cacheable / halt
    / retry / stale) is NOT encoded here; the cache loader feeds
    these fields into ``ReviewExchangeResumeDecision.decide`` to
    determine the next-tick action.

    Pre-this-commit, the writer encoded policy by selectively
    omitting ``head_sha`` based on status. That dual-purpose use of
    one field (fact AND control signal) was the root cause of the
    PR #6270 review-feedback whack-a-mole: every patch that adjusted
    "which statuses cache-hit" mutated which facts got persisted,
    and downstream consumers re-inferred policy at three different
    sites. Recording facts unconditionally and centralizing policy
    in one named helper ends that drift.

    ``head_sha`` and ``validation_passed`` are still omitted (rather
    than written as None) when the validation record cannot be
    read at all — the caller should treat absence as "we don't
    know" rather than "validation explicitly failed." The cache
    loader's ``ResumeFacts`` mapping handles each case.
    """
    terminal = ReviewExchangeTerminalState(status=status, reason=reason)
    head_sha, passed = _read_validation_facts(validation_record_path)
    artifacts = tuple(
        ReviewExchangeSummaryArtifactRef.from_payload(artifact)
        for artifact in (review_artifacts or [])
    )
    summary = ReviewExchangeSummaryV1(
        completed_rounds=round_index,
        terminal=terminal,
        response_text=reviewer_response.response_text if reviewer_response else None,
        timestamp=datetime.now(timezone.utc).isoformat(),
        head_sha=head_sha,
        validation_passed=passed,
        artifacts=artifacts,
        detail=detail,
    )
    _atomic_write_json(exchange_dir / "summary.json", summary.to_payload())
    return summary


# ---------------------------------------------------------------------------
# Chapter sidecar
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ChapterRecordCommand:
    session_output: SessionOutput
    run_dir: Path
    role: str
    recording_path: Path
    exchange_run_id: str
    issue_number: int
    cycle_index: int
    section: str
    label: str
    session_name: str
    emit: Callable[[EventName, dict[str, Any]], None]
    mirror: _RoleSliceMirror | None = None


def _record_chapter(command: _ChapterRecordCommand) -> int:
    """Capture the recording's current event index, append a chapter,
    emit ``REVIEW_EXCHANGE_CHAPTER_RECORDED``, and (when ``mirror`` is
    provided) project the new events into the per-session run_dir slice.

    Returns the captured ``event_index`` so callers can chain behavior
    onto it without having to count the recording themselves. The
    returned value is **always pair-relative** so callers tracking
    exchange-wide event progression see absolute positions; the
    chapter sidecar and the SSE payload, by contrast, hold the
    slice-relative offset (when a ``mirror`` is provided) because the
    manifest points the viewer at the slice file.

    Errors propagate. Role recordings are created at session open and the
    chapter offset is the UI contract for scrubbing the persistent
    recording — a missing recording or failed sidecar write means the
    replay contract is broken, not a best-effort detail. The top-level
    ``run_persistent_session_exchange`` handler converts the propagated
    exception into a REVIEW_EXCHANGE_FAILED event and re-raises so the
    orchestrator surface treats it as a definitive exchange failure.

    The chapter event is emitted *after* the sidecar write succeeds so
    SSE/timeline consumers see the same offset that's now durable on disk;
    on failure the exception propagates and no event fires (consistent
    with the rest of the runner's emit-on-success contract).

    Per-event slice mirroring is no longer chapter-driven — the role's
    ``MirroredTerminalRecordingWriter`` writes to both the pair file
    and the per-session slice on every event drained from the PTY,
    so chapters only own offset translation here.
    """
    session_output = command.session_output
    run_dir = command.run_dir
    role = command.role
    recording_path = command.recording_path
    exchange_run_id = command.exchange_run_id
    issue_number = command.issue_number
    cycle_index = command.cycle_index
    section = command.section
    label = command.label
    session_name = command.session_name
    emit = command.emit
    mirror = command.mirror

    pair_event_index = recording_event_count(recording_path)
    # Slice-relative when a mirror is in play so the viewer can scrub
    # the manifest-pointed slice directly. Without the translation, a
    # pair recording with prior exchange content records pair-relative
    # offsets in the hundreds while the slice file only holds dozens of
    # events, and the web replay route slices ``all_events[chapter_offset:]``
    # to an empty window.
    sidecar_event_index = (
        mirror.pair_to_slice_offset(pair_event_index)
        if mirror is not None
        else pair_event_index
    )
    session_output.record_exchange_chapter(
        run_dir,
        role=role,
        exchange_run_id=exchange_run_id,
        issue_number=issue_number,
        cycle_index=cycle_index,
        section=section,
        recording_event_index=sidecar_event_index,
        recorded_at=datetime.now(timezone.utc).isoformat(),
        label=label,
    )
    emit(
        EventName.REVIEW_EXCHANGE_CHAPTER_RECORDED,
        {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": cycle_index,
            "role": role,
            "section": section,
            "recording_event_index": sidecar_event_index,
            "label": label,
        },
    )
    return pair_event_index


def _validation_record_error(
    record_path: Path,
    *,
    current_head_sha: str | None,
) -> str | None:
    if not record_path.exists():
        return "validation-record.json missing"
    try:
        data = json.loads(record_path.read_text())
    except json.JSONDecodeError:
        return "validation-record.json is not valid JSON"
    if not isinstance(data, dict):
        return "validation-record.json must be a JSON object"
    if data.get("passed") is not True:
        return "validation-record.json did not pass"
    if current_head_sha is None:
        return "cannot determine current HEAD for validation-record.json"
    record_head_sha = data.get("head_sha")
    if not isinstance(record_head_sha, str) or not record_head_sha:
        return "validation-record.json missing head_sha"
    if record_head_sha != current_head_sha:
        return (
            "validation-record.json head "
            f"{record_head_sha[:12]} does not match current HEAD "
            f"{current_head_sha[:12]}"
        )
    return None


def _validate_coder_completion(
    *,
    completion_path: Path,
    pair_validation: _PairValidationMirror,
    run_validation_record_path: Path,
    require_validation: bool,
) -> str | None:
    """Mirror of control/review_exchange_loop._validate_coder_protocol.

    The coder must produce a completion-coder.json artifact (the
    ``coding-done`` CLI's output) and, when ``require_validation`` is on,
    a passing validation-record.json. A coder that only writes the
    review-response file but skips coding-done would otherwise advance
    the exchange by accident.
    """
    if not completion_path.exists():
        return f"missing completion artifact: {completion_path}"
    if completion_path.stat().st_size <= 0:
        return f"completion artifact is empty: {completion_path}"
    try:
        payload = json.loads(completion_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return f"completion artifact is not valid JSON: {completion_path}"
    if not isinstance(payload, dict):
        return f"completion artifact must be a JSON object: {completion_path}"
    validation_source_error = pair_validation.refresh_from_completion(
        payload,
        run_validation_record_path=run_validation_record_path,
    )
    if require_validation:
        if validation_source_error is not None:
            return validation_source_error
        return pair_validation.current_validation_error()
    return None


# ``_atomic_write_json`` is the shared helper from ``infra.atomic_io``;
# re-export under the private name so the existing test that monkeypatches
# ``pse.os.replace`` continues to find the same write path.
from ..infra.atomic_io import (  # noqa: E402
    atomic_write_bytes as _atomic_write_bytes,
    atomic_write_json as _atomic_write_json,
)
