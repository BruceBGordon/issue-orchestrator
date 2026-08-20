"""FactGatherer - creates immutable snapshots for planning.

This module extracts fact-gathering logic from the orchestrator,
making it a pure read-only component that:
1. Reads current state (OrchestratorState)
2. Fetches external data via ports (RepositoryHost)
3. Returns immutable facts for the Planner

The FactGatherer makes NO decisions and plans no mutations - all state
transitions happen in the orchestrator based on Plan execution. Its only
outputs besides the snapshot are fire-and-forget observation sinks: trace
events (EventSink) and the tech_lead board projection (TechLeadBoardPublisher,
#6781), both projections of what was observed, never policy.

Usage:
    gatherer = FactGatherer(
        config=config,
        repository_host=github_adapter,
    )
    snapshot = gatherer.create_snapshot(state, issues)
"""

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TYPE_CHECKING

from ..infra.config import Config
from ..events import EventName
from ..ports.repository_host import RepositoryHost, RepositoryHostError
from ..ports import EventSink,  make_trace_event
from .provider_launch_readiness import ProviderLaunchReadiness
from .health_review_trigger import (
    classify_tech_lead_anchor_issues,
    discover_open_tech_lead_anchor_issues,
    health_review_decision,
    health_review_interval_minutes,
)
from .tech_lead_finding_promotion import (
    PromotionReadBudget,
    gather_finding_promotion_facts,
)
from .tech_lead_artifact_retention import (
    clear_discovered_facts as _clear_discovered_facts,
    tech_lead_problem_artifact_hold_issue_numbers,
)
from .tech_lead_reaction import storm_possible

# Compatibility export: this policy lived in fact_gatherer before it gained a
# dedicated owner module. Keep existing callers stable while new code imports
# from tech_lead_artifact_retention directly.
clear_discovered_facts = _clear_discovered_facts

if TYPE_CHECKING:
    from ..ports.issue import Issue
    from ..ports.promotion_target import PromotionTargetHost
    from ..ports.queue_cache_store import QueueCacheStore
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from ..domain.models import (
        OrchestratorState,
        TechLeadFacts,
        CleanupFacts,
    )
    from ..domain.tech_lead_session import (
        ApprovedTechLeadOp,
        StoredTechLeadOp,
        TechLeadCaseFileSummary,
    )
    from .planner_types import E2ESlotSignals
    from .planner_types import OrchestratorSnapshot
    from .tech_lead_board import TechLeadBoardPublisher

logger = logging.getLogger(__name__)


def _pr_labels(pr: Any) -> list[str]:
    labels = getattr(pr, "labels", None)
    if labels is None and isinstance(pr, dict):
        labels = pr.get("labels", [])
    return labels or []


@dataclass
class FactGatherer:
    """Gathers facts from state and external sources for planning.

    This is a read-only component that creates immutable snapshots.
    It does not modify any state.
    """

    config: Config
    repository_host: RepositoryHost
    events: Optional[EventSink] = None
    # Orchestrator-owned gated-proposal ledger (#6778). Optional so unrelated
    # tests need not wire it; without it the anchor scan classifies no
    # approved ops (gate-labeled proposals are still excluded from anchors).
    tech_lead_authority: Optional["TechLeadAuthorityStore"] = None
    # Fire-and-forget projection sink for tech_lead facts (#6781): like the
    # event sink, it observes gathered facts (retaining the latest case-file
    # projection + refreshing the tech_lead board file) and makes no decisions.
    # Optional so unrelated tests need not wire it.
    board_publisher: Optional["TechLeadBoardPublisher"] = None
    # Cross-repo filing/read seam for the finding-promotion lane (#6957).
    # Optional so unrelated tests need not wire it; without it the lane gathers
    # no loop-closure facts (promotions simply stay in flight).
    promotion_target: Optional["PromotionTargetHost"] = None
    # Durable store for the tech-lead stuck sweep's timer + recovery counters
    # (#6823). Optional so unrelated tests need not wire it; without it the
    # sweep still runs but its counters do not survive a restart.
    queue_cache_store: Optional["QueueCacheStore"] = None
    # Observation feed for the first-class E2E workload
    # (``e2e.occupies_session_slot``). Returns whether an E2E run occupies a
    # worker slot right now, or is due to claim one. Optional so unrelated
    # tests need not wire it; without it (or with the flag off) both snapshot
    # facts stay False and the default scheduling path is unchanged.
    e2e_slot_reader: Optional[Callable[[], "E2ESlotSignals"]] = None
    # Predicate answering "is this issue's provider circuit still open?" for the
    # stuck sweep's ownership check (#6824 F2): a provider-unavailable issue is
    # owned by the resilience manager WHILE its circuit is open. Optional so
    # unrelated tests need not wire it; without it every provider-unavailable
    # issue is conservatively treated as owned (the pre-#6824 skip).
    provider_circuit_open: Optional[Callable[["Issue"], bool]] = None
    # Per-target read budget for finding-promotion loop closure (#6957 F5). Owned
    # here because the budget is a fact-gathering concern (it bounds this
    # component's cross-repo reads per tick) and rotates across ticks, so it must
    # outlive a single call.
    promotion_read_budget: PromotionReadBudget = field(
        default_factory=PromotionReadBudget
    )

    def fetch_issues(
        self,
        labels_for_agent: list[str],
        milestone: Optional[str] = None,
        required_stable_ids: set[str] | None = None,
        fetch_limit: int | None = None,
    ) -> list["Issue"]:
        """Fetch all issues for configured agents from GitHub."""
        milestones = self.config.get_filter_milestones() or [milestone]
        limit = fetch_limit if fetch_limit is not None else self.config.filtering.fetch_limit
        all_issues, seen, still_needed = [], set(), set(required_stable_ids) if required_stable_ids else None

        for agent_label in self.config.agents.keys():
            labels = list(labels_for_agent) + [agent_label]
            for milestone_name in milestones:
                issues = self.repository_host.list_issues(
                    labels=labels, milestone=milestone_name,
                    limit=limit, required_stable_ids=still_needed,
                )
                self._process_fetched_issues(issues, all_issues, seen, still_needed, agent_label, labels, milestone_name)

        return self._apply_issue_filter(all_issues)

    def _process_fetched_issues(
        self,
        issues: list["Issue"],
        all_issues: list["Issue"],
        seen: set[int],
        still_needed: set[str] | None,
        agent_label: str,
        labels: list[str],
        milestone_name: str | None,
    ) -> None:
        """Process fetched issues and emit events."""
        for issue in issues:
            if issue.number in seen:
                continue
            seen.add(issue.number)
            all_issues.append(issue)
            if still_needed and issue.key.stable_id() in still_needed:
                still_needed.discard(issue.key.stable_id())

        if self.events is not None:
            self._emit_issues_fetched_events(issues, agent_label, labels, milestone_name)

    def _emit_issues_fetched_events(self, issues: list["Issue"], agent_label: str, labels: list[str], milestone_name: str | None) -> None:
        """Emit events for fetched issues."""
        self.events.publish(make_trace_event(EventName.ISSUES_FETCHED, {
            "agent": agent_label, "labels": labels, "milestone": milestone_name,
            "count": len(issues), "issue_numbers": [i.number for i in issues],
        }))

    def _apply_issue_filter(self, all_issues: list["Issue"]) -> list["Issue"]:
        """Apply exclusion filter to issues."""
        issue_filter = self.config.get_issue_filter()
        if issue_filter.is_empty():
            return all_issues
        before_count = len(all_issues)
        filtered = issue_filter.apply(all_issues)
        if before_count != len(filtered):
            logger.debug("Excluded %d issues via filter %s", before_count - len(filtered), issue_filter)
        return filtered

    def create_snapshot(
        self,
        state: "OrchestratorState",
        issues: list["Issue"],
        stale_in_progress_issues: list["Issue"] | None = None,
        stale_claim_issues: list["Issue"] | None = None,
        provider_launch: ProviderLaunchReadiness | None = None,
    ) -> "OrchestratorSnapshot":
        """Create an immutable snapshot for planning.

        Args:
            state: Current orchestrator state
            issues: Current list of issues from GitHub
            stale_in_progress_issues: Issues with in-progress label but no running session
            stale_claim_issues: Issues with io:claimed label but expired/invalid claim
            provider_launch: Provider launch eligibility the tick sampled before
                planning (#6999 A3). Passed in rather than sampled here because
                sampling probes a CLI and writes circuit state, which this
                read-only gatherer must not do.

        Returns:
            Immutable snapshot of orchestrator state for Planner
        """
        from .planner_types import OrchestratorSnapshot

        # Gather tech_lead facts FIRST: gather_tech_lead_facts runs the tech-lead
        # stuck sweep (#6823), which injects recovered failures into
        # state.discovered_failures. That mutation must land before this tick's
        # discovered_failures is captured below, so the reaction model sees the
        # recovered failures this tick (a next-tick capture would be dropped by
        # the end-of-tick discovered-fact clear).
        tech_lead_facts = self.gather_tech_lead_facts(state)
        tech_lead_subjects = self.gather_tech_lead_subject_facts(state, issues)
        cleanup_facts = self.gather_cleanup_facts(state)
        e2e_occupies_slot, e2e_due = self._read_e2e_slot_facts()

        return OrchestratorSnapshot(
            issues=tuple(issues),
            active_sessions=tuple(state.active_sessions),
            pending_reviews=tuple(state.pending_reviews),
            pending_retrospective_reviews=tuple(state.pending_retrospective_reviews),
            pending_reworks=tuple(state.pending_reworks),
            pending_tech_lead=tuple(state.pending_tech_lead_reviews),
            pending_validation_retries=tuple(state.pending_validation_retries),
            paused=state.paused,
            priority_queue=tuple(state.priority_queue),
            issues_started_count=state.issues_started_count,
            max_issues_to_start=self.config.filtering.max_to_start if self.config.filtering.max_to_start > 0 else None,
            discovered_reviews=tuple(state.discovered_reviews),
            discovered_retrospective_reviews=tuple(
                state.discovered_retrospective_reviews
            ),
            discovered_awaiting_merge_reconciliations=tuple(
                state.discovered_awaiting_merge_reconciliations
            ),
            discovered_awaiting_merge_drifts=tuple(
                state.discovered_awaiting_merge_drifts
            ),
            discovered_reworks=tuple(state.discovered_reworks),
            discovered_escalations=tuple(state.discovered_escalations),
            discovered_awaiting_merge_escalations=tuple(
                state.discovered_awaiting_merge_escalations
            ),
            discovered_merge_queue_enqueues=tuple(
                state.discovered_merge_queue_enqueues
            ),
            discovered_failures=tuple(state.discovered_failures),
            stuck_sweep_escalations=tuple(state.stuck_sweep_escalations),
            tech_lead_facts=tech_lead_facts,
            tech_lead_subjects=tech_lead_subjects,
            cleanup_facts=cleanup_facts,
            stale_in_progress_issues=tuple(stale_in_progress_issues or []),
            stale_claim_issues=tuple(stale_claim_issues or []),
            failed_this_cycle=frozenset(state.failed_this_cycle),
            session_history_issue_numbers=frozenset(e.issue_number for e in state.session_history),
            e2e_occupies_slot=e2e_occupies_slot,
            e2e_due=e2e_due,
            provider_launch=provider_launch or ProviderLaunchReadiness.empty(),
        )

    def _read_e2e_slot_facts(self) -> tuple[bool, bool]:
        """Read the ``(e2e_occupies_slot, e2e_due)`` worker-slot facts.

        Returns ``(False, False)`` without invoking the reader when the reader
        is unwired OR ``e2e.occupies_session_slot`` is off — the reader itself
        guards the flag, but short-circuiting here means the default path pays
        no reader call at all. At most one of the two facts is ever True.
        """
        if self.e2e_slot_reader is None or not self.config.e2e.occupies_session_slot:
            return False, False
        signals = self.e2e_slot_reader()
        return signals.occupies_slot, signals.due

    def gather_tech_lead_subject_facts(
        self,
        state: "OrchestratorState",
        board: list["Issue"],
    ) -> tuple["Issue", ...]:
        """Authoritative lifecycle reads for queued investigation subjects (#6994).

        Launch-time revalidation must be able to see a subject that was CLOSED
        while its investigation waited behind the global barrier. It cannot get
        that from ``board``: the board fetch is filtered by agent label,
        milestone, and ``filtering.exclude_labels``, and it asks GitHub only for
        OPEN issues — so a closed subject is simply ABSENT, and absence is the
        one signal revalidation must never act on (a filtered-out issue is
        absent too). Before round 1 F4 that made the closed-while-queued rule
        unreachable in production.

        So the gap is closed HERE, where fact gathering belongs, and only for
        the subjects that actually need it: a queued FAILURE_INVESTIGATION whose
        subject the board did not carry. GitHub API discipline is why the read
        is scoped that narrowly — a tick with no queued investigations, or whose
        subjects are all on the board, makes ZERO extra calls, and the queue is
        bounded by ``tech_lead.max_concurrent`` plus its backlog.
        """
        from ..domain.tech_lead_session import TechLeadSessionFlavor

        on_board = {issue.number for issue in board}
        wanted = sorted(
            {
                item.issue_number
                for item in state.pending_tech_lead_reviews
                if item.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
                and item.issue_number not in on_board
            }
        )
        if not wanted:
            return ()
        read: list["Issue"] = []
        for number in wanted:
            try:
                issue = self.repository_host.get_issue(number)
            except Exception as exc:  # pragma: no cover - transport-specific
                logger.warning(
                    "[TECH_LEAD] Could not re-read queued subject #%d: %s",
                    number,
                    exc,
                )
                continue
            # A subject that cannot be read yields NO fact: revalidation then
            # keeps the run, which is the same conservative direction absence
            # from the filtered board takes.
            if issue is not None:
                read.append(issue)
        return tuple(read)

    def gather_tech_lead_facts(
        self,
        state: "OrchestratorState",
        now: float | None = None,
    ) -> Optional["TechLeadFacts"]:
        """Gather facts for the tech_lead batch and health-review triggers.

        Three independent triggers can each produce facts (only the case where
        none is active yields None):
          * BATCH fields, gated by ``tech_lead_review_threshold`` (via the watch
            label);
          * HEALTH-REVIEW fields, gated by
            ``tech_lead.health_review.interval_minutes`` when the periodic review
            is due, and independently by :func:`storm_possible` so an
            unscheduled storm escalation can dedup its anchor on a tick the
            interval is not due (including ``interval_minutes=0``, which
            disables only the periodic trigger);
          * PROPOSAL fields (approved-op execution + terminal-op cleanup
            candidates), armed by the tech lead agent's local op ledger and
            reconciled whenever it holds an op — INDEPENDENT of the batch
            review threshold (#6779 R12), so a manual-approval / default
            (threshold=0) proposal still advances and self-heals.

        GitHub API discipline shapes every read here: due-ness and storm
        possibility are pure state/config math computed FIRST, so a health-only
        configuration that is neither due nor holding enough problems to storm
        makes ZERO GitHub calls (no anchor fact can affect planning until one
        of the two can fire); the exhaustive tech-lead-agent scan runs only when
        the batch trigger is armed OR the local op ledger has proposals to
        reconcile (an empty ledger has nothing to approve or clean up, so no
        scan is worth making). A due health review also uses that exhaustive scan
        so its snapshot includes open case files while still deduplicating its
        anchor, and a possible storm uses it for that same dedup on a tick the
        interval is not due. Observation only: milestone ASSEMBLY policy
        (strategy choice, explicit name -> number resolution) belongs to
        planning and the create-issue applier boundary (#6769 round 3) — no
        milestone API reads happen here.
        """
        from ..domain.models import TechLeadFacts

        # The master switch is the outermost boundary: disabled means zero
        # tech-lead scans, ledger reads, proposal reconciliation, or promotion
        # polling. Durable state remains untouched for a later re-enable.
        if not self.config.tech_lead_enabled:
            return None

        now_ts = time.time() if now is None else now
        # Tech-lead attention sweep (#6823): an independent, timer-gated trigger
        # that re-injects terminally-stuck issues into the reactive-tech-lead
        # pipeline. Runs regardless of the batch/health/storm arming below (it
        # feeds discovered_failures, not TechLeadFacts). stuck_sweep_due is pure
        # state/config math, so a disabled/not-due sweep makes ZERO GitHub calls.
        self._run_stuck_sweep_if_due(state, now_ts)

        watch_label = self._get_tech_lead_watch_label()
        batch_armed = bool(watch_label)
        tech_lead_workflow_enabled = self.config.tech_lead_enabled
        health_armed = health_review_interval_minutes(self.config) > 0
        # A storm can fire an anchor on a tick the interval is NOT due — and a
        # storm-only configuration (interval_minutes=0) is never due at all.
        # Arming the scan on the storm predicate is what keeps
        # ``existing_health_review_issue`` trustworthy on those ticks; without
        # it the dedup fact is unconditionally None and every storm mints a
        # duplicate anchor. Pure state/config math, so it costs no API call.
        storm_armed = storm_possible(state, self.config)

        # The act-level PROPOSAL machinery is armed by having a tech lead agent, so
        # it reconciles INDEPENDENT of the batch review threshold (#6779 R12):
        # approved gated proposals must execute and terminal/absent proposals
        # must be surfaced for cleanup even when threshold=0 (batch disabled).
        # The local op ledger (no GitHub call) says whether there is anything to
        # reconcile — an empty ledger produces no facts and no scan.
        ops = (
            dict(self.tech_lead_authority.list_ops())
            if tech_lead_workflow_enabled and self.tech_lead_authority is not None
            else {}
        )
        # The finding-promotion lane (#6957) arms INDEPENDENTLY of the batch,
        # health, storm, and proposal triggers: it reads the durable pattern and
        # promotion ledgers, not the anchor scan. Eligibility is pure local math
        # (zero GitHub calls, so a board with nothing promotable costs nothing);
        # only loop closure reads, and only for promotions actually in flight.
        promotable, promotion_updates, settled = gather_finding_promotion_facts(
            self.config,
            authority=self.tech_lead_authority,
            target=self.promotion_target,
            read_budget=self.promotion_read_budget,
        )
        if (
            not batch_armed
            and not health_armed
            and not ops
            and not storm_armed
            and not promotable
            and not promotion_updates
            and not settled
        ):
            return None

        # The decision carries the board it was decided on, so anchor creation
        # can stamp that exact value instead of recomputing a board that has
        # moved on by then (#6793).
        health_decision = health_review_decision(self.config, state, now_ts)
        due = health_decision.due

        existing_tech_lead_issue: Optional[int] = None
        existing_health_review_issue: Optional[int] = None
        approved_ops: tuple["ApprovedTechLeadOp", ...] = ()
        absent_op_candidates: tuple[int, ...] = ()
        case_files: tuple["TechLeadCaseFileSummary", ...] = ()
        # Distinguishes "scan ran and observed no case files" from "scan was
        # skipped this tick" so the board projection is only replaced when the
        # anchor scan actually observed the ledger (#6781 R2). A frugal tick
        # (health armed but not due, no batch, empty ledger) leaves this False
        # and its empty ``case_files`` must NOT wipe the retained projection.
        case_files_scanned = False
        if batch_armed or ops or due or storm_armed:
            # The ONE exhaustive open tech-lead-agent scan classifies batch +
            # health anchors, open proposals, approved ops, and absent-ledger
            # cleanup candidates in a single reconcile (#6778/#6779).
            # It runs when the batch trigger is armed OR the ledger has ops to
            # reconcile — decoupling proposal advancement from the batch
            # threshold. A due health review also needs this scan so its board
            # snapshot includes every open pattern case file (#6781), and a
            # possible storm needs it to dedup its anchor (#6780).
            (
                batch_anchor,
                existing_health_review_issue,
                approved_ops,
                absent_op_candidates,
                case_files,
            ) = self._classify_tech_lead_anchor_scan(ops)
            case_files_scanned = True
            # Batch anchor classification stays gated on batch_armed: a batch
            # anchor is meaningless while the batch trigger is off.
            if batch_armed:
                existing_tech_lead_issue = batch_anchor
        prs = self._fetch_tech_lead_prs(watch_label) if batch_armed else []
        all_labels, source_milestones = self._collect_pr_metadata(prs)

        facts = TechLeadFacts(
            pr_count=len(prs),
            threshold=self.config.tech_lead_review_threshold,
            existing_tech_lead_issue=existing_tech_lead_issue,
            watch_label=watch_label or "",
            prs=tuple((pr.number, pr.title) for pr in prs),
            source_labels=frozenset(all_labels),
            source_milestones=tuple(source_milestones),
            health_review_due=due,
            health_review_fingerprint=health_decision.fingerprint,
            existing_health_review_issue=existing_health_review_issue,
            approved_tech_lead_ops=approved_ops,
            absent_proposal_op_candidates=absent_op_candidates,
            open_case_files=case_files,
            case_files_scanned=case_files_scanned,
            promotable_findings=promotable,
            promotion_updates=promotion_updates,
            settled_promotions=settled,
        )
        if self.board_publisher is not None:
            self.board_publisher.publish(
                facts, last_health_review_at=state.last_health_review_at
            )
        return facts

    def _run_stuck_sweep_if_due(
        self, state: "OrchestratorState", now: float
    ) -> None:
        """Run the tech-lead stuck sweep and record what it recovered (#6823).

        All policy lives in the ``stuck_sweep`` owner; this seam only arms it,
        records the recovered failures through the state owner method, stamps
        and persists the timer, and emits an observation event. No new control
        vocabulary enters this module.
        """
        from .label_manager import LabelManager
        from .stuck_sweep import (
            persist_stuck_sweep_state,
            run_stuck_sweep,
            stuck_sweep_due,
        )

        if not stuck_sweep_due(self.config, state, now):
            return
        try:
            result = run_stuck_sweep(
                self.config,
                state,
                self.repository_host,
                LabelManager(self.config),
                now,
                # Issues with an OPEN gated proposal are owned by the human who must
                # delabel it — the sweep must not re-investigate them or spend their
                # budget (stops propose-mode from exhausting a never-remedied issue,
                # #6824 F1). The ledger rows ARE the open-proposal set.
                open_proposal_targets=self._open_proposal_targets(),
                provider_circuit_open=self.provider_circuit_open,
            )
        except RepositoryHostError as error:
            # The sweep issues its own exhaustive `list_issues` read, OUTSIDE the
            # resilience guard that wraps the queue fetch. So a transient network
            # blip used to be absorbed by the fetch layer ("keeping cached queue")
            # and then, in the very same tick, escape from here into the loop
            # error budget — three of those in a row tripped the breaker and
            # halted the engine. Observed repeatedly: a DNS drop during host
            # sleep paused a healthy orchestrator for days.
            #
            # Swallowing every RepositoryHostError here is safe because the
            # sweep is not the system's auth detector: the queue fetch runs the
            # same credentials through IssueFetchResilience every cycle, and a
            # genuinely permanent auth/not-found failure still shuts the engine
            # down cleanly from there. Only the duplicate signal is dropped.
            #
            # The sweep is best-effort recovery machinery, so a failed scan is
            # simply skipped. `last_stuck_sweep_at` is deliberately NOT stamped,
            # leaving the sweep due so the next tick retries it.
            logger.warning(
                "[STUCK_SWEEP] Skipping sweep — repository host unavailable: %s. "
                "Sweep stays due and retries next tick.",
                error,
            )
            return
        for failure in result.recovered:
            state.record_discovered_failure(failure)
        # Escalate to needs-human through the Planner/Applier (authoritative,
        # label-only). Re-emit the idempotent label for EVERY unacknowledged
        # escalation (the durable pending set) so a crash/apply failure retries
        # until it lands (#6824 R1). The durable set itself is persisted below.
        state.stuck_sweep_escalations = list(state.pending_stuck_sweep_escalations)
        state.last_stuck_sweep_at = now
        persist_stuck_sweep_state(state, self.queue_cache_store)
        self._emit_stuck_sweep(result)

    def _open_proposal_targets(self) -> frozenset[int]:
        """Target issue numbers with an OPEN gated proposal in the ledger (#6824)."""
        if self.tech_lead_authority is None:
            return frozenset()
        return frozenset(
            op.target_issue_number for _, op in self.tech_lead_authority.list_ops()
        )

    def _emit_stuck_sweep(self, result: object) -> None:
        """Fire-and-forget observation of a sweep that acted (#6823)."""
        if self.events is None:
            return
        recovered = [failure.issue_number for failure in result.recovered]
        exhausted = list(result.exhausted)
        if not recovered and not exhausted:
            return
        self.events.publish(
            make_trace_event(
                EventName.TECH_LEAD_STUCK_SWEEP,
                {"recovered": recovered, "exhausted": exhausted},
            )
        )

    def _get_tech_lead_watch_label(self) -> str | None:
        """Get the label to watch for tech_lead review (None = trigger disabled)."""
        if (
            not self.config.tech_lead_enabled
            or self.config.tech_lead_review_threshold <= 0
        ):
            return None
        return self.config.tech_lead_watch_label

    def _fetch_tech_lead_prs(self, watch_label: str) -> list[Any]:
        """Fetch PRs that are current tech_lead batch candidates.

        Eligibility comes from the shared :class:`TechLeadCandidatePolicy` — the
        same predicate the manifest builder applies — so terminally-triaged
        PRs never count toward the threshold that the manifest then filters
        out (#6768 round 5: that divergence created empty-batch loops).
        """
        from .tech_lead_manifest_builder import TechLeadCandidatePolicy

        policy = TechLeadCandidatePolicy.from_config(self.config)
        prs = self.repository_host.get_prs_with_label(watch_label, state="all")
        return [pr for pr in prs if policy.is_candidate(_pr_labels(pr))]

    def _classify_tech_lead_anchor_scan(
        self,
        ops: Mapping[int, "StoredTechLeadOp"],
    ) -> tuple[
        int | None,
        int | None,
        tuple["ApprovedTechLeadOp", ...],
        tuple[int, ...],
        tuple["TechLeadCaseFileSummary", ...],
    ]:
        """Classify the ONE shared, exhaustive open tech-lead-agent scan.

        The scoped/exhaustive anchor-discovery owner backs both this path and
        startup recovery, so both apply ONE eligibility rule (#6763 finding 7)
        over the COMPLETE open set (#6779 R4). Gated proposal issues carry the
        tech lead agent label, so the SAME scan that finds batch/health anchors
        classifies them (#6778): gate-labeled issues are open proposals
        (excluded from anchor classification), and op-backed issues WITHOUT
        the gate label were approved by the operator. A backlog of proposals
        can never hide an older approved op or an anchor.

        ``ops`` is the caller-provided local authority-store ledger (the caller
        already read it to decide whether a scan is worthwhile — #6779 R12), so
        no extra GitHub call is made here beyond the single anchor scan.

        Fact gathering is READ-ONLY (#6779 R10): reconciliation only
        CLASSIFIES ledger rows absent from the scan as terminal-cleanup
        CANDIDATES; the numbers are returned as a fact for the planner to turn
        into a confirm-and-discard action, never mutated here.
        Observation-labeled issues are pattern case files (#6781), summarized
        for the board snapshot and excluded before anchor classification.
        """
        from .tech_lead_case_files import split_tech_lead_case_file_issues
        from .tech_lead_proposals import reconcile_tech_lead_proposals

        if not self.config.tech_lead_enabled:
            return None, None, (), (), ()
        existing = discover_open_tech_lead_anchor_issues(
            self.repository_host, self.config
        )
        reconciled = reconcile_tech_lead_proposals(existing, ops=ops)
        remaining, case_files = split_tech_lead_case_file_issues(
            reconciled.anchor_candidate_issues
        )
        batch, health = classify_tech_lead_anchor_issues(
            remaining, self.config.filtering.label
        )
        return (
            batch,
            health,
            reconciled.approved,
            reconciled.absent_op_issue_numbers,
            case_files,
        )

    def _collect_pr_metadata(self, prs: list[Any]) -> tuple[set[str], list[tuple[int, str]]]:
        """Collect labels and milestones from PRs and their linked issues."""
        all_labels: set[str] = set()
        source_milestones: list[tuple[int, str]] = []

        for pr in prs:
            all_labels.update(_pr_labels(pr))
            self._collect_linked_issue_metadata(pr, all_labels, source_milestones)

        return all_labels, source_milestones

    def _collect_linked_issue_metadata(
        self,
        pr: object,
        all_labels: set[str],
        source_milestones: list[tuple[int, str]],
    ) -> None:
        """Collect metadata from issues linked to a PR."""
        matches = re.findall(r'#(\d+)', (getattr(pr, 'body', '') or "") + " " + pr.title)
        for match in matches:
            issue_num = int(match)
            issue = self.repository_host.get_issue(issue_num)
            if not issue:
                continue
            all_labels.update(issue.labels)
            if issue.milestone and issue.milestone_number:
                milestone_tuple = (issue.milestone_number, issue.milestone)
                if milestone_tuple not in source_milestones:
                    source_milestones.append(milestone_tuple)

    def gather_cleanup_facts(
        self,
        state: "OrchestratorState",
    ) -> Optional["CleanupFacts"]:
        """Gather facts for cleanup decision.

        Returns immutable facts for the Planner to decide which cleanups to process.
        Does NOT perform cleanup - that's the Planner's job.

        Handles two types of cleanups:
        1. Deferred cleanups (pending_cleanups) - waiting for review label
        2. Immediate cleanups (immediate_cleanups) - ready to execute now

        Args:
            state: Current orchestrator state with pending_cleanups and immediate_cleanups

        Returns:
            CleanupFacts if there are any cleanups to process, else None
        """
        from ..domain.models import CleanupFacts

        # Check if there's anything to clean up
        has_pending = bool(state.pending_cleanups)
        has_immediate = bool(state.immediate_cleanups)

        if not has_pending and not has_immediate:
            return None

        # Determine cleanup settings based on workflow
        if self.config.tech_lead_enabled:
            cleanup_label = self.config.tech_lead_reviewed_label
            close_tabs = self.config.cleanup.with_tech_lead.close_ai_session_tabs
            remove_wt = self.config.cleanup.with_tech_lead.remove_worktrees
        elif self.config.code_review_agent:
            cleanup_label = self.config.code_reviewed_label
            close_tabs = self.config.cleanup.without_tech_lead.close_ai_session_tabs
            remove_wt = self.config.cleanup.without_tech_lead.remove_worktrees
        else:
            # No review workflow - use defaults for immediate cleanups
            cleanup_label = None
            close_tabs = self.config.cleanup.without_tech_lead.close_ai_session_tabs
            remove_wt = self.config.cleanup.without_tech_lead.remove_worktrees

        # Get reviewed PRs for deferred cleanups (only if we have pending cleanups)
        reviewed_pr_numbers: frozenset[int] = frozenset()
        if has_pending and cleanup_label:
            try:
                reviewed_prs = self.repository_host.get_prs_with_label(cleanup_label)
                reviewed_pr_numbers = frozenset(pr.number for pr in reviewed_prs)
            except RepositoryHostError:
                raise
            except Exception as e:
                logger.warning(f"[CLEANUP] Failed to fetch PRs with label {cleanup_label}: {e}")

        # Build immutable tuples of pending cleanup info
        pending_tuples = tuple(
            (c.issue_number, c.pr_number, c.terminal_id, str(c.worktree_path))
            for c in state.pending_cleanups
        )

        # Build immutable tuple of immediate cleanups
        immediate_tuples = tuple(state.immediate_cleanups)

        return CleanupFacts(
            pending_cleanups=pending_tuples,
            reviewed_pr_numbers=reviewed_pr_numbers,
            close_tabs=close_tabs,
            remove_worktrees=remove_wt,
            immediate_cleanups=immediate_tuples,
            held_issue_numbers=tech_lead_problem_artifact_hold_issue_numbers(
                state, self.config, self.tech_lead_authority
            ),
        )
