"""Tech-lead attention sweep for terminally-stuck issues (#6823).

Single policy owner for the reactive backstop that the normal control loop
cannot reach: an open issue that carries a terminal blocking label but is NOT
in any active session, pending tech_lead queue, or storm cohort has fallen out of
the loop entirely — no completion will ever re-discover it, so it sits stuck
forever. This module finds those issues on a bounded, timer-gated cadence and
re-injects each one into the EXISTING reactive-tech-lead pipeline as a recovered
:class:`DiscoveredFailure`, so the tech-lead reaction model investigates it and
the tech lead agent proposes/executes ``reset_retry`` (or closes it if the work is
actually done).

Design boundaries (kept deliberately narrow, ADR-0031):

* **All policy lives here.** The planner, ``fact_gatherer`` and
  ``tech_lead_reaction`` gain no new branchy control vocabulary — the fact
  gatherer only arms this owner and records what it returns.
* **Observation, not mutation.** :func:`run_stuck_sweep` reads GitHub and
  mutates only orchestrator STATE (the durable per-issue recovery counter, the
  injected discovered failures, and the escalation buffer). It never writes a
  GitHub label directly — recovered failures ride the Observer -> Planner ->
  Applier chain, and exhausted issues are escalated to needs-human through the
  Planner/Applier too (#6824 F1), so every GitHub write stays orchestrator-
  authoritative.
* **Budget spent on OUTCOMES, not injections (#6824 F1).** The durable per-issue
  counter (:attr:`OrchestratorState.recovery_attempts`) counts *failed recovery
  cycles*, not re-injections. First detection injects and records ``0`` (an
  outstanding recovery, no failure yet); a LATER sweep that re-discovers the
  same issue still stuck and NOT owned means the prior cycle's remedy did not
  stick — that is the failure that spends one unit of budget. Once the budget is
  exhausted the issue is escalated to needs-human ONCE and never re-injected; a
  genuine recovery (the blocking label cleared) clears the counter so a later
  unrelated incident starts fresh.
* **``failure_reason`` is always ``timed_out``.** The reaction model's
  ``_disposition`` only applies the "no downstream dependents -> IGNORE" gate
  when ``failure_reason == "blocked"``; ``timed_out`` always yields
  INVESTIGATE. The recovered failure preserves the issue's REAL terminal label
  in ``blocking_label`` for context, but reports ``timed_out`` so a leaf stuck
  issue is still investigated rather than silently dropped.
* **Ownership vs eligibility (#6824 F2).** An issue is ELIGIBLE when it carries a
  recoverable stuck label — including ``blocked:provider-unavailable`` and the
  needs-human labels, which #6823 wants re-examined, not permanently ignored. It
  is SKIPPED this sweep only while a dedicated owner is actively handling it: an
  active session / pending tech_lead work, an open gated proposal (the ledger), a
  provider whose circuit is still open (the resilience manager will resume it),
  or a ``tech-lead-needs-human`` marker (the escalation reconciler owns it). Only
  ``proposed-tech-lead`` / ``tech-lead-observation`` are true machinery labels never
  treated as work items.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..domain.models import DiscoveredFailure, SessionStatus
from ..domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL, TECH_LEAD_OBSERVATION_LABEL
from ..ports.repository_host import (
    RepositoryHostError,
    RepositoryScanIncompleteError,
)
from .needs_human_block import NeedsHumanCause

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..infra.config import Config
    from ..domain.models import OrchestratorState
    from ..ports import Issue, RepositoryHost
    from ..ports.queue_cache_store import QueueCacheStore
    from .actions import Action
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

# One timer-gated, EXHAUSTIVE scan of open blocking-labeled issues. The scan is
# ``exhaustive`` (like the health-review anchor scan, #6779 R17): the multi-page
# walk fails loud rather than returning a silently partial set. This is the
# authoritative recovery backstop, so a truncated read is NOT "defer the
# straggler to the next cadence" — a non-exhaustive fixed page returns the SAME
# newest N every cadence, so any eligible issue beyond page N is starved forever
# (never re-swept). Failing loud on a backlog larger than the (generous) cap is
# the correct signal: 1000+ terminally-stuck issues is itself an anomaly worth
# blocking the tick over, not silently under-recovering. Gated by
# ``stuck_sweep_due`` so a disabled/not-due sweep makes ZERO GitHub calls.
STUCK_SWEEP_SCAN_LIMIT = 1000

# How long a FAILED sweep waits before trying again.
#
# A failure must not stamp the last-success timestamp — the sweep genuinely did
# not run — but `stuck_sweep_due` reads only that stamp, and the engine ticks
# every ~10s. A deterministic failure (a repo whose scoped open issues exceed
# STUCK_SWEEP_SCAN_LIMIT fails on every attempt) would therefore walk up to ten
# issue pages roughly 360 times an hour, forever: thousands of GitHub calls for
# a scan that cannot succeed, crowding out the orchestration traffic that can.
#
# Well under the normal interval so a transient outage still recovers quickly,
# far above the tick so a permanent failure is loud without being a hammer.
STUCK_SWEEP_FAILURE_RETRY_SECONDS = 15 * 60


@dataclass(frozen=True)
class StuckSweepResult:
    """The sweep's outcome for the fact gatherer to consume.

    ``recovered`` are the failures to inject into ``discovered_failures`` (each
    an outstanding recovery whose budget has NOT yet been spent). ``exhausted``
    are the issue numbers that spent their recovery budget on failed cycles and
    must be escalated to needs-human through the Planner/Applier — emitted ONCE,
    on the transition to the ceiling, so the escalation comment is not re-posted
    every sweep (the durable counter keeps the issue skipped thereafter).
    """

    recovered: tuple[DiscoveredFailure, ...] = ()
    exhausted: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class _StuckScan:
    """One scan's eligibility split (computed once, no extra GitHub reads)."""

    # (issue, stuck_label) for issues eligible AND not currently owned.
    candidates: tuple[tuple["Issue", str], ...]
    # Open issues still carrying ANY blocking label (recovered => absent here).
    blocked_numbers: frozenset[int]
    # Issues currently OWNED by a dedicated reconciler / open proposal / active
    # work — skipped this sweep, and never treated as "recovered" by the clear.
    owned_numbers: frozenset[int]
    # Open issues that now carry the needs-human label — a landed escalation
    # (the acknowledgement that drops it from the durable pending set, #6824 R1).
    needs_human_numbers: frozenset[int]


def stuck_sweep_due(config: "Config", state: "OrchestratorState", now: float) -> bool:
    """True when the timer-gated tech-lead sweep should run this tick.

    Pure state/config math — ZERO GitHub calls — so an disabled or not-yet-due
    sweep never touches the network (mirrors ``health_review_due``). The sweep
    requires a configured tech lead agent AND tech-lead-on-failure, because the whole
    point is to feed the reactive tech_lead pipeline; without either there is
    nothing to re-inject into.
    """
    sweep = config.tech_lead.stuck_sweep
    if not sweep.enabled:
        return False
    if not (config.tech_lead_enabled and config.tech_lead_review_on_failure):
        return False
    interval_seconds = sweep.interval_minutes * 60
    if interval_seconds <= 0:
        # Defense-in-depth against the every-tick-scan trap: config validation
        # already rejects interval_minutes < 1 when enabled (#6823), but a
        # 0/negative interval reaching here must NOT be treated as "always due".
        return False
    # Due at the LATER of the success cadence and the failure backoff. Two
    # deadlines, because a failed sweep must not stamp the success timestamp —
    # it genuinely did not sweep — yet without a deadline of its own that
    # silently means "rescan on every tick", i.e. hundreds of full scans an
    # hour for a failure that cannot clear. See
    # STUCK_SWEEP_FAILURE_RETRY_SECONDS.
    due_at = max(
        state.last_stuck_sweep_at + interval_seconds,
        state.last_stuck_sweep_failure_at + STUCK_SWEEP_FAILURE_RETRY_SECONDS,
    )
    return now >= due_at


def run_stuck_sweep(
    config: "Config",
    state: "OrchestratorState",
    repository_host: "RepositoryHost",
    label_manager: "LabelManager",
    now: float,
    *,
    open_proposal_targets: frozenset[int] = frozenset(),
    provider_circuit_open: "Callable[[Issue], bool] | None" = None,
) -> StuckSweepResult:
    """Find stuck issues and return recovered failures + exhausted numbers.

    Mutates ``state.recovery_attempts`` but performs no GitHub writes. The caller
    injects ``recovered`` via ``state.record_discovered_failure`` and routes
    ``exhausted`` through the Planner's needs-human escalation.

    ``open_proposal_targets`` are issues with an OPEN gated proposal (the ledger)
    — owned by the human who must delabel the proposal, so the sweep must not
    re-investigate them or spend their budget (this is what stops ``reset_retry:
    propose`` from exhausting an issue that never had a remedy applied, #6824 F1).
    ``provider_circuit_open(issue)`` reports whether the issue's provider circuit
    is still open (the resilience manager owns it); ``None`` conservatively treats
    every ``provider-unavailable`` issue as owned (the pre-#6824 behaviour).
    """
    max_attempts = config.tech_lead.stuck_sweep.max_recovery_attempts
    scan = _scan_stuck_issues(
        config,
        repository_host,
        label_manager,
        base_owned=_owned_issue_numbers(state) | open_proposal_targets,
        provider_circuit_open=provider_circuit_open,
    )
    _clear_recovered_counters(state, scan)
    _ack_landed_escalations(state, scan)
    recovered: list[DiscoveredFailure] = []
    exhausted: list[int] = []
    for issue, blocking_label in scan.candidates:
        attempts = state.recovery_attempts.get(issue.number)
        if attempts is None:
            # First detection: an OUTSTANDING recovery, no failed cycle yet.
            # Injection alone never spends the budget (#6824 F1).
            state.recovery_attempts[issue.number] = 0
            recovered.append(_recovered_failure(issue, blocking_label, now))
            _log_reinject(issue, 0, max_attempts, blocking_label)
        elif attempts >= max_attempts:
            # Budget already spent + escalated: leave it for the human. Do NOT
            # re-inject or re-escalate; the counter clears when it recovers.
            continue
        elif attempts + 1 >= max_attempts:
            # This re-detection is the failing cycle that exhausts the budget:
            # escalate to needs-human exactly ONCE.
            state.recovery_attempts[issue.number] = max_attempts
            exhausted.append(issue.number)
            _log_exhausted(issue, max_attempts, blocking_label)
        else:
            # A prior recovery cycle failed (still stuck, not owned): spend one
            # unit of budget and re-inject.
            state.recovery_attempts[issue.number] = attempts + 1
            recovered.append(_recovered_failure(issue, blocking_label, now))
            _log_reinject(issue, attempts + 1, max_attempts, blocking_label)
    # Newly exhausted issues join the durable pending-escalation set; they stay
    # there (re-labelled every sweep) until the needs-human label is observed
    # present, so a crash or apply failure never loses the escalation (#6824 R1).
    state.pending_stuck_sweep_escalations.update(exhausted)
    return StuckSweepResult(recovered=tuple(recovered), exhausted=tuple(exhausted))


def _ack_landed_escalations(state: "OrchestratorState", scan: "_StuckScan") -> None:
    """Drop escalations that LANDED (needs-human present) or whose issue RECOVERED.

    The durable pending set is the escalation retry queue (#6824 R1): an entry
    survives until its needs-human label is observed on the issue (the
    acknowledged outcome) — through any intervening crash or apply failure — or
    until the issue is no longer blocked (recovered), which supersedes it.
    """
    state.pending_stuck_sweep_escalations = {
        number
        for number in state.pending_stuck_sweep_escalations
        if number in scan.blocked_numbers and number not in scan.needs_human_numbers
    }


def _clear_recovered_counters(state: "OrchestratorState", scan: "_StuckScan") -> None:
    """Drop the recovery budget for issues that genuinely RECOVERED (#6824 F1).

    An issue no longer carrying ANY blocking label (its ``blocked-failed`` was
    cleared by a successful reset, or it was closed) and not currently owned
    mid-recovery has recovered; its lifetime budget must reset so a later
    unrelated incident on the same number starts fresh instead of inheriting a
    stale (possibly already-exhausted) count.
    """
    for number in list(state.recovery_attempts):
        if number not in scan.blocked_numbers and number not in scan.owned_numbers:
            del state.recovery_attempts[number]


def hydrate_stuck_sweep_state(
    state: "OrchestratorState",
    store: "QueueCacheStore | None",
) -> None:
    """Restore the durable sweep timer and recovery counters at startup.

    The recovery counter is crash-safe truth: without it a restart would reset
    every issue's budget to 0 and re-inject an already-exhausted issue forever.
    Loaded unconditionally when a store is present (even when the sweep is
    disabled) so the counters survive an enable/disable toggle.
    """
    if store is None:
        return
    state.last_stuck_sweep_at = store.load_last_stuck_sweep_at()
    state.recovery_attempts = store.load_recovery_attempts()
    # Unacknowledged escalations survive a restart so an exhausted issue is
    # re-escalated until its needs-human label lands (#6824 R1).
    state.pending_stuck_sweep_escalations = store.load_pending_escalations()


def persist_stuck_sweep_state(
    state: "OrchestratorState",
    store: "QueueCacheStore | None",
) -> None:
    """Persist the sweep timer + recovery counters; degrade on failure.

    A persist failure is logged, not raised: the sweep already mutated
    in-memory state and a restart re-hydrates from the store, so a lost write
    at worst re-sweeps one issue early or under-counts one attempt — never a
    crash on the observation path (mirrors ``record_health_review_creation``).
    """
    if store is None:
        return
    try:
        store.save_last_stuck_sweep_at(state.last_stuck_sweep_at)
        store.save_recovery_attempts(state.recovery_attempts)
        store.save_pending_escalations(state.pending_stuck_sweep_escalations)
    except Exception:
        logger.warning(
            "[STUCK_SWEEP] failed to persist recovery counters; a restart "
            "re-hydrates them from the queue-cache store",
            exc_info=True,
        )


def _scan_stuck_issues(
    config: "Config",
    repository_host: "RepositoryHost",
    label_manager: "LabelManager",
    *,
    base_owned: set[int],
    provider_circuit_open: "Callable[[Issue], bool] | None",
) -> "_StuckScan":
    """Scan open issues and split them into eligible candidates vs owned.

    ONE bounded scoped query (server-side ``filtering.label`` when configured;
    GitHub label filtering is AND-semantics so blocking labels — an OR set —
    are filtered client-side). An issue is a candidate only when it STILL carries
    a recoverable stuck label AND is not currently owned by a dedicated
    reconciler (an active session / open proposal / open provider circuit /
    needs-human marker). Provider-unavailable and needs-human are recoverable
    labels (#6824 F2), not blanket exclusions; only tech_lead machinery
    (proposed-tech-lead / observation case files) is never a work item.
    """
    scope = [value for value in (config.filtering.label,) if value] or None
    issues = repository_host.list_issues(
        labels=scope,
        state="open",
        limit=STUCK_SWEEP_SCAN_LIMIT,
        # Authoritative recovery scan: a truncated read must fail loud (#6779 R17),
        # never silently drop older stuck issues (which a fixed page would starve).
        exhaustive=True,
    )
    scoped = _scope_filtered(issues, config.filtering.label)
    machinery = _machinery_blocking_folded()
    preferred = label_manager.blocked_failed.casefold()
    needs_human = label_manager.needs_human.casefold()
    candidates: list[tuple["Issue", str]] = []
    blocked: set[int] = set()
    owned: set[int] = set(base_owned)
    needs_human_numbers: set[int] = set()
    for issue in scoped:
        if issue.state != "open":
            continue
        folded = {name.casefold() for name in issue.labels}
        if label_manager.get_blocking(issue.labels):
            blocked.add(issue.number)
        if needs_human in folded:
            needs_human_numbers.add(issue.number)
        if issue.number in base_owned:
            continue
        if _reconciler_owns(issue, label_manager, provider_circuit_open):
            owned.add(issue.number)
            continue
        blocker = _stuck_blocking_label(issue, label_manager, machinery, preferred)
        if blocker is None:
            continue
        candidates.append((issue, blocker))
    return _StuckScan(
        candidates=tuple(candidates),
        blocked_numbers=frozenset(blocked),
        owned_numbers=frozenset(owned),
        needs_human_numbers=frozenset(needs_human_numbers),
    )


def _scope_filtered(
    issues: list["Issue"], filter_label: str | None
) -> list["Issue"]:
    """Enforce the ``filtering.label`` scope client-side (casefolded).

    The scope is also part of the server query, but adapters/fakes may not
    filter; eligibility is a policy rule and must hold regardless of transport.
    """
    folded = (filter_label or "").casefold()
    return [
        issue
        for issue in issues
        if not folded or any(name.casefold() == folded for name in issue.labels)
    ]


def _stuck_blocking_label(
    issue: "Issue",
    label_manager: "LabelManager",
    machinery_folded: frozenset[str],
    preferred_folded: str,
) -> str | None:
    """The issue's recoverable stuck label, or None when not stuck.

    Prefers the ``blocked-failed`` label (the canonical failed-session signal)
    when present, else the first remaining recoverable blocking label. Returns
    None when the issue carries no blocking label, or only machinery labels
    (proposed-tech-lead / observation) — the done guard. Ownership (provider
    circuit / needs-human marker / open proposal) is decided BEFORE this by the
    scan; here every non-machinery blocking label is eligible (#6824 F2).
    """
    blockers = [
        name
        for name in label_manager.get_blocking(issue.labels)
        if name.casefold() not in machinery_folded
    ]
    if not blockers:
        return None
    for name in blockers:
        if name.casefold() == preferred_folded:
            return name
    return blockers[0]


def _machinery_blocking_folded() -> frozenset[str]:
    """Casefolded blocking labels that are tech_lead MACHINERY, never work items.

    ``proposed-tech-lead`` gates an awaiting-approval proposal issue and
    ``tech-lead-observation`` marks an evidence case file — re-injecting either
    would launch a tech_lead session on tech_lead's own bookkeeping. Unlike
    provider-unavailable and needs-human (which are recoverable stuck states
    gated by ownership, #6824 F2), these are ALWAYS excluded.
    """
    return frozenset(
        {
            PROPOSED_TECH_LEAD_LABEL.casefold(),
            TECH_LEAD_OBSERVATION_LABEL.casefold(),
        }
    )


def _reconciler_owns(
    issue: "Issue",
    label_manager: "LabelManager",
    provider_circuit_open: "Callable[[Issue], bool] | None",
) -> bool:
    """True while a dedicated reconciler is actively handling this issue (#6824 F2).

    * ``provider-unavailable``: owned WHILE its provider circuit is open — the
      resilience manager will resume it when the circuit closes. ``None`` (no
      circuit reader wired) conservatively treats it as owned, preserving the
      pre-#6824 skip. When the circuit is CLOSED the issue is orphaned (the
      planner only clears the label for issues still in active work) and the
      sweep re-examines it.
    * ``tech-lead-needs-human`` marker: the escalation reconciler owns a stranded
      needs-human issue (it re-asserts the block on restart). A BARE needs-human
      (operator escalation, no marker) is left ELIGIBLE for re-examination —
      re-injecting it is exactly how a superseding investigation is created.
    """
    folded = {name.casefold() for name in issue.labels}
    if label_manager.provider_unavailable.casefold() in folded:
        if provider_circuit_open is None or provider_circuit_open(issue):
            return True
    if label_manager.tech_lead_needs_human.casefold() in folded:
        return True
    return False


def _owned_issue_numbers(state: "OrchestratorState") -> set[int]:
    """Issue numbers the tech lead must NOT compete for (dedup + cooldown).

    An issue already worked or queued — an active session, a pending tech_lead
    review, a pending storm cohort member, or discovered this very tick — is
    covered by the normal loop; re-injecting it would double-queue. The
    pending-tech-lead membership is also the cooldown: a recovered issue stays a
    pending failure investigation until it completes, so it is skipped on every
    intervening sweep.
    """
    owned = {session.issue.number for session in state.active_sessions}
    owned.update(item.issue_number for item in state.pending_tech_lead_reviews)
    for item in state.pending_tech_lead_reviews:
        owned.update(problem.issue_number for problem in item.problem_cohort)
    owned.update(failure.issue_number for failure in state.discovered_failures)
    return owned


def _recovered_failure(
    issue: "Issue", blocking_label: str, now: float
) -> DiscoveredFailure:
    """Build the recovered failure fact re-injected into reactive tech_lead.

    ``failure_reason`` is ``timed_out`` (never ``blocked``) so the reaction
    model always INVESTIGATES: its no-dependents IGNORE gate only fires for a
    ``blocked`` reason, and a leaf stuck issue must still be investigated. The
    real terminal label rides along in ``blocking_label`` for context.
    """
    return DiscoveredFailure(
        issue_number=issue.number,
        issue_title=issue.title,
        failure_reason=SessionStatus.TIMED_OUT.value,
        blocking_label=blocking_label,
        issue_body=issue.body or "",
        issue_milestone=issue.milestone,
        observed_at=now,
        artifact_hints=(),
    )


def _log_reinject(
    issue: "Issue", attempts: int, max_attempts: int, blocking_label: str
) -> None:
    logger.info(
        "[STUCK_SWEEP] re-injecting stuck issue #%d as a recovered failure "
        "(failed cycles %d/%d, label=%s) (#6823)",
        issue.number,
        attempts,
        max_attempts,
        blocking_label,
    )


def _log_exhausted(issue: "Issue", max_attempts: int, blocking_label: str) -> None:
    logger.warning(
        "[STUCK_SWEEP] issue #%d exhausted recovery budget (%d failed cycles, "
        "label=%s); escalating to needs-human via the planner (#6824 F1)",
        issue.number,
        max_attempts,
        blocking_label,
    )


def build_stuck_sweep_escalation_actions(
    label_issue_numbers: "tuple[int, ...]",
    needs_human_label: str,
) -> "list[Action]":
    """The authoritative needs-human escalation for exhausted issues (#6824 R1).

    The escalation is deliberately **label-only**: the ``needs-human`` label IS
    the authoritative, durable escalation. It is re-emitted each sweep for the
    FULL durable pending set — an idempotent no-op once present — so a crash or
    an apply failure never loses it; it retries until the label lands and is then
    acknowledged (the label observed on the issue). An explaining comment was
    dropped because it could NOT be made retry-safe/deduplicated without a
    per-effect durable outbox, so promising a required comment would reintroduce
    the very lost-on-crash gap R1 flagged. The Planner routes this through the
    Applier so the GitHub write stays orchestrator-authoritative.
    """
    from .actions import AddLabelAction
    from .reconciliation import build_expected_for_mutation

    return [
        AddLabelAction(
            issue_number=issue_number,
            label=needs_human_label,
            reason="stuck-sweep recovery budget exhausted (#6824)",
            needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
            expected=build_expected_for_mutation(),
        )
        for issue_number in label_issue_numbers
    ]


def run_stuck_sweep_cycle(
    config: "Config",
    state: "OrchestratorState",
    repository_host: "RepositoryHost",
    now: float,
    *,
    open_proposal_targets: frozenset[int],
    provider_circuit_open: "Callable[[Issue], bool] | None",
    queue_cache_store: "QueueCacheStore | None",
    on_result: "Callable[[StuckSweepResult], None]",
    on_scan_incomplete: "Callable[[Exception], None]",
) -> None:
    """Arm the sweep, absorb what it is allowed to absorb, record the rest.

    The whole cycle lives beside the sweep's policy rather than in the general
    fact gatherer: arming, the two failure classifications, recording recovered
    failures, stamping the timer, and persisting are one concern, and splitting
    them across modules is what let the classifications drift apart.

    Three outcomes, deliberately different:

    * **Success** — record, stamp, persist, report.
    * **Outage** (``RepositoryHostError``) — skip. The sweep issues its own
      exhaustive read OUTSIDE the resilience guard wrapping the queue fetch, so
      a transient blip absorbed by that fetch used to escape from here into the
      tick's error budget; three in a row tripped the breaker. Safe to swallow
      because the sweep is not the auth detector — the queue fetch runs the same
      credentials every cycle and still fails fast on a permanent failure.
    * **Unprovable scan** (``RepositoryScanIncompleteError``) — neither
      swallowed nor fatal. The timer is NOT stamped (so it stays due) and the
      caller is told, loudly. Raising instead would abort the whole tick, and a
      repo with more open issues than ``STUCK_SWEEP_SCAN_LIMIT`` raises every
      cycle — stopping all orchestration over one recovery backstop.

    In every failure case ``last_stuck_sweep_at`` is left alone, so a failed
    sweep can never be mistaken for a clean one.
    """
    # Local import: label_manager imports back into this package at module
    # scope, so a top-level import here would close the cycle.
    from .label_manager import LabelManager

    if not stuck_sweep_due(config, state, now):
        return
    try:
        result = run_stuck_sweep(
            config,
            state,
            repository_host,
            LabelManager(config),
            now,
            open_proposal_targets=open_proposal_targets,
            provider_circuit_open=provider_circuit_open,
        )
    except RepositoryScanIncompleteError as error:
        logger.error(
            "[STUCK_SWEEP] Scan could not prove completeness: %s. The sweep is "
            "SKIPPED and stays due; stuck issues are not being reclaimed. If "
            "this repeats every cycle the scoped open-issue count likely "
            "exceeds the sweep's scan cap — narrow filtering.label.",
            error,
        )
        state.last_stuck_sweep_failure_at = now
        on_scan_incomplete(error)
        return
    except RepositoryHostError as error:
        logger.warning(
            "[STUCK_SWEEP] Skipping sweep — repository host unavailable: %s. "
            "Sweep retries after the failure backoff.",
            error,
        )
        state.last_stuck_sweep_failure_at = now
        return
    for failure in result.recovered:
        state.record_discovered_failure(failure)
    # Escalate to needs-human through the Planner/Applier (authoritative,
    # label-only). Re-emit the idempotent label for EVERY unacknowledged
    # escalation (the durable pending set) so a crash/apply failure retries
    # until it lands (#6824 R1). The durable set itself is persisted below.
    state.stuck_sweep_escalations = list(state.pending_stuck_sweep_escalations)
    state.last_stuck_sweep_at = now
    state.last_stuck_sweep_failure_at = 0.0
    persist_stuck_sweep_state(state, queue_cache_store)
    on_result(result)
