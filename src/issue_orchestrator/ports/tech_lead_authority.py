"""Port for orchestrator-owned tech_lead launch authority (ADR-0031).

The agent-writable worktree carries copies of the tech_lead assignment and PR
manifest for the *agent* to read; the orchestrator must never treat those
copies as authority (#6761 re-review F1). This port is the behavior seam the
control plane uses instead: it owns trusted launch scope plus the durable
proposal, pattern, and shipped-fix ledgers that tech_lead policy consults across
process restarts.

Constructed once at the composition root (``entrypoints/bootstrap.py``) and
injected into the session launcher, the completion processor, and the
completion action planner. Tests mock this protocol; the durable SQLite
implementation lives in ``infra/tech_lead_authority_store.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..domain.models import DiscoveredFailure
    from ..domain.tech_lead_findings import (
        PatternEvidence,
        PromotedFinding,
        PromotionState,
    )
    from ..domain.tech_lead_session import (
        StoredTechLeadOp,
        TechLeadLaunchAuthority,
        TechLeadShippedFixSummary,
    )


class TechLeadAuthorityConflictError(RuntimeError):
    """A different launch authority already exists for this session run."""


class TechLeadStormCohortConflictError(RuntimeError):
    """A different problem cohort already exists for this anchor issue."""


class TechLeadOpConflictError(RuntimeError):
    """A different stored op already exists for this proposal issue."""


class TechLeadPatternConflictError(RuntimeError):
    """A different case-file issue already exists for this pattern signature."""


class TechLeadShippedFixConflictError(RuntimeError):
    """Different shipped-fix evidence already exists for an issue."""


class TechLeadPromotionConflictError(RuntimeError):
    """A different promotion already exists for this pattern signature."""


class UnknownTechLeadPatternError(KeyError):
    """An observation was recorded for a signature with no case-file row."""


class TechLeadAuthorityStore(Protocol):
    """Durable trusted scope and operational ledgers for tech_lead."""

    def record(
        self, *, run_id: str, session_name: str, authority: "TechLeadLaunchAuthority"
    ) -> None:
        """Persist the launch authority for one session run (create-once).

        Recording an identical payload for an existing key is a no-op;
        recording a DIFFERENT payload for an existing key must raise
        :class:`TechLeadAuthorityConflictError` — the record constrains the
        session's mutation scope, so it must never silently change or
        expand after launch (#6769 round 4).
        """
        ...

    def load(
        self, *, run_id: str, session_name: str
    ) -> "TechLeadLaunchAuthority | None":
        """Return the launch authority for a session run, or None when absent."""
        ...

    def discard(self, *, run_id: str, session_name: str) -> None:
        """Remove a run's authority row. No-op if absent (retention owner)."""
        ...

    # -- Gated proposal ops (#6778, ADR-0031 §2 amendment) -----------------
    #
    # The executable payload of a gated tech_lead proposal, keyed by the
    # proposal ISSUE number. Recorded create-once when the proposal issue is
    # created; execution consumes only this record (the issue body is human
    # documentation); discarded after terminal handling so ops run at most
    # once. ``list_ops`` is the ledger read: proposal dedup per (op, target)
    # and the fact gatherer's approval classification both consult it.

    def record_op(self, *, issue_number: int, op: "StoredTechLeadOp") -> None:
        """Persist the op for one proposal issue (create-once).

        Recording an identical payload for an existing key is a no-op;
        recording a DIFFERENT payload must raise
        :class:`TechLeadOpConflictError` — the approver's consent binds to
        exactly one recorded payload, which must never silently change.
        """
        ...

    def load_op(self, *, issue_number: int) -> "StoredTechLeadOp | None":
        """Return the stored op for a proposal issue, or None when absent."""
        ...

    def discard_op(self, *, issue_number: int) -> None:
        """Remove a proposal issue's op row. No-op if absent (once-only owner)."""
        ...

    def list_ops(self) -> tuple[tuple[int, "StoredTechLeadOp"], ...]:
        """All (proposal_issue_number, op) rows — the open-proposal ledger."""
        ...

    # -- Problem-storm cohorts (#6780) --------------------------------------
    #
    # The durable cohort ledger, keyed by the health-review ANCHOR issue
    # number. A storm collapses N per-issue failure investigations into one
    # anchor, so from that moment the cohort is the only record of which
    # problems the review owns — and the pending queue that carries it is
    # in-memory. This ledger is the recoverable boundary between the two
    # orchestrator-owned facts that outlive a tick:
    #
    #   * WHAT the review may act on. Launch records
    #     ``TechLeadLaunchAuthority.problem_issue_numbers`` from the cohort the
    #     anchor owns; a restart between anchor creation and launch would
    #     otherwise rehydrate an empty cohort and strip the review of its
    #     act-level scope.
    #   * WHICH run artifacts must survive. The cleanup-hold owner holds the
    #     cohort members' worktrees while the anchor is still referenced, so
    #     ``DiscoveredFailure.artifact_hints`` cannot outlive the files.
    #
    # Recorded create-once at anchor intake, rehydrated by startup recovery,
    # and discarded by the completion retention owner. The issue body is NOT
    # the authority: it is mutable human documentation.
    #
    # Retention: a row whose anchor is neither pending nor active is inert,
    # not load-bearing — every reader intersects the ledger with live pending
    # or active tech_lead work, so a row leaked by an anchor that never reached
    # completion (e.g. dropped after exhausted launch retries) holds nothing
    # and grants nothing.

    def record_storm_cohort(
        self, *, anchor_issue_number: int, cohort: tuple["DiscoveredFailure", ...]
    ) -> None:
        """Persist the problem cohort for one anchor issue (create-once).

        Recording an identical cohort for an existing anchor is a no-op;
        recording a DIFFERENT cohort must raise
        :class:`TechLeadStormCohortConflictError` — the cohort is the review's
        act-level authority and its artifact-retention scope, so it must
        never silently change or expand after the anchor is created.
        """
        ...

    def load_storm_cohort(
        self, *, anchor_issue_number: int
    ) -> tuple["DiscoveredFailure", ...] | None:
        """Return an anchor's persisted cohort, or None when absent.

        None means "not a storm anchor" (a periodic health review has no
        cohort) — distinct from an empty tuple, which never gets recorded.
        """
        ...

    def discard_storm_cohort(self, *, anchor_issue_number: int) -> None:
        """Remove an anchor's cohort row. No-op if absent (retention owner)."""
        ...

    def list_storm_cohorts(
        self,
    ) -> tuple[tuple[int, tuple["DiscoveredFailure", ...]], ...]:
        """All (anchor_issue_number, cohort) rows — the cleanup-hold read.

        Once a health review LAUNCHES its pending queue item is removed, so
        this ledger is the only remaining carrier of the cohort its run still
        references. The hold owner reads the whole (small, storm-scoped)
        ledger once per tick rather than issuing a lookup per active session.
        """
        ...

    # -- Pattern case files (#6781) -----------------------------------------
    #
    # The durable flag_pattern ledger: one case-file issue per pattern
    # signature. Recorded create-once when the case-file issue is created;
    # subsequent flag_pattern proposals with the same signature look it up
    # and plan an evidence comment on the existing issue instead of a second
    # one. Rows are never discarded by the orchestrator — the case file IS
    # the accumulating artifact (graduation happens on GitHub).

    def record_pattern(
        self,
        *,
        signature: str,
        issue_number: int,
        observation_id: str,
        fix_class: str = "",
        area: str = "",
        diagnosis: str = "",
    ) -> None:
        """Persist the case-file issue for one signature (create-once).

        Recording the same issue for an existing signature is a no-op;
        recording a DIFFERENT issue must raise
        :class:`TechLeadPatternConflictError` — the signature keys exactly one
        evidence trail, which must never silently move.

        ``fix_class``/``area`` are the promotion facts the case file was opened
        with (#6957) and ``diagnosis`` is its original mechanism/recommended
        fix. ``observation_id`` identifies the ONE observation the issue body
        records (the creating decision's remaining observations are appended
        afterwards through :meth:`note_pattern_observation`, each with its own
        identity, so a crash mid-append cannot double-count). The observation
        COUNT is orchestrator-owned so promotion eligibility cannot be inflated
        by editing the case-file issue or by unrelated human comments on it.
        """
        ...

    def note_pattern_observation(
        self,
        *,
        signature: str,
        observation_id: str,
        fix_class: str = "",
        area: str = "",
    ) -> bool:
        """Record ONE observation of a known signature create-once (#6957).

        Returns True when *observation_id* was newly recorded (and the durable
        count therefore advanced by exactly one), False when this exact
        observation was already recorded — a replay after a crash, which must
        never advance the count a second time (review F1). The identity comes
        from :func:`~..domain.tech_lead_findings.pattern_observation_id`.

        ``fix_class``/``area`` are reconciled through
        :func:`~..domain.tech_lead_findings.reconcile_pattern_classification`:
        an empty value preserves what is recorded, an empty recorded value is
        upgraded once, and two different non-empty values raise
        :class:`~..domain.tech_lead_findings.PatternClassificationConflictError`
        rather than letting observation order decide whether a signature is
        promotable and where it routes (review F3).

        An unknown signature must raise :class:`UnknownTechLeadPatternError` —
        the caller appended evidence to a case file with no ledger row, which is
        a bug in the case-file owner, not something to silently create.
        """
        ...

    def has_pattern_observation(self, *, signature: str, observation_id: str) -> bool:
        """True when this exact observation is already recorded.

        A purely LOCAL read (no GitHub call) that lets the case-file appliers
        skip re-posting an evidence comment whose observation the previous
        attempt already committed.
        """
        ...

    def lookup_pattern(self, *, signature: str) -> int | None:
        """Return the case-file issue for a signature, or None when absent."""
        ...

    def list_patterns(self) -> tuple[tuple[str, int], ...]:
        """All (signature, case_file_issue_number) rows — the pattern ledger."""
        ...

    def list_pattern_evidence(self) -> tuple["PatternEvidence", ...]:
        """All case-file rows with their promotion facts (#6957).

        The promotion trigger's ONLY input for eligibility: durable observation
        counts plus the tech lead's fix classification, with zero GitHub reads.
        """
        ...

    # -- Promoted findings (#6957) ------------------------------------------
    #
    # One row per PROMOTED pattern signature, create-once at filing time. The
    # row is the at-most-once guarantee in both directions: a signature with a
    # row is never re-filed (later observations comment on the promoted issue),
    # and a ``declined`` row is never re-filed at all. Rows are never deleted —
    # ``declined``/``shipped`` are permanent terminal states that must survive
    # restarts, or the lane would re-file work an operator already rejected.

    def record_promotion(self, *, promotion: "PromotedFinding") -> None:
        """Persist a promoted finding (create-once by signature).

        An identical row is a no-op; a DIFFERENT target repo/issue for an
        existing signature must raise :class:`TechLeadPromotionConflictError` —
        a signature promotes to exactly one issue, ever.
        """
        ...

    def load_promotion(self, *, signature: str) -> "PromotedFinding | None":
        """Return a signature's promotion row, or None when never promoted."""
        ...

    def list_promotions(self) -> tuple["PromotedFinding", ...]:
        """All promotion rows — the dedup, cap, and loop-closure ledger read."""
        ...

    def note_promotion_reported(self, *, signature: str, observations: int) -> None:
        """Advance a promotion's reported-observation high-water mark.

        Called after later evidence is reported onto an already-promoted issue,
        so the same observations are never reported twice. Monotonic; an
        unknown signature raises :class:`UnknownTechLeadPatternError`.
        """
        ...

    def settle_promotion(
        self,
        *,
        signature: str,
        state: "PromotionState",
        shipped_pr_url: str = "",
    ) -> None:
        """Move a promotion to a terminal state (``declined``/``shipped``).

        Idempotent for a row already in *state*. An unknown signature raises
        :class:`UnknownTechLeadPatternError`.
        """
        ...

    # -- Shipped-fix operational memory (#6781 amendment) -----------------

    def record_shipped_fix(
        self, *, issue_number: int, title: str, pr_url: str, area: str
    ) -> None:
        """Record an area-tagged fix at merge time (create-once by issue)."""
        ...

    def list_recent_shipped_fixes(
        self, *, limit: int
    ) -> tuple["TechLeadShippedFixSummary", ...]:
        """Newest persisted fixes, bounded for the agent-read board snapshot."""
        ...


class InMemoryTechLeadAuthorityStore:
    """In-memory store for tests."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], "TechLeadLaunchAuthority"] = {}
        self._ops: dict[int, "StoredTechLeadOp"] = {}
        self._patterns: dict[str, int] = {}
        self._evidence: dict[str, "PatternEvidence"] = {}
        # signature -> the observation identities already counted (create-once).
        self._observations: dict[str, set[str]] = {}
        self._promotions: dict[str, "PromotedFinding"] = {}
        self._shipped_fixes: dict[int, "TechLeadShippedFixSummary"] = {}
        self._storm_cohorts: dict[int, tuple["DiscoveredFailure", ...]] = {}

    def record(
        self, *, run_id: str, session_name: str, authority: "TechLeadLaunchAuthority"
    ) -> None:
        existing = self._rows.get((run_id, session_name))
        if existing is not None:
            if existing == authority:
                return
            raise TechLeadAuthorityConflictError(
                f"launch authority already recorded for run_id={run_id!r} "
                f"session={session_name!r} with a different payload"
            )
        self._rows[(run_id, session_name)] = authority

    def load(
        self, *, run_id: str, session_name: str
    ) -> "TechLeadLaunchAuthority | None":
        return self._rows.get((run_id, session_name))

    def discard(self, *, run_id: str, session_name: str) -> None:
        self._rows.pop((run_id, session_name), None)

    def record_op(self, *, issue_number: int, op: "StoredTechLeadOp") -> None:
        existing = self._ops.get(issue_number)
        if existing is not None:
            if existing == op:
                return
            raise TechLeadOpConflictError(
                f"a different tech_lead op is already recorded for proposal"
                f" issue #{issue_number}"
            )
        self._ops[issue_number] = op

    def load_op(self, *, issue_number: int) -> "StoredTechLeadOp | None":
        return self._ops.get(issue_number)

    def discard_op(self, *, issue_number: int) -> None:
        self._ops.pop(issue_number, None)

    def list_ops(self) -> tuple[tuple[int, "StoredTechLeadOp"], ...]:
        return tuple(sorted(self._ops.items()))

    def record_storm_cohort(
        self, *, anchor_issue_number: int, cohort: tuple["DiscoveredFailure", ...]
    ) -> None:
        existing = self._storm_cohorts.get(anchor_issue_number)
        if existing is not None:
            if existing == cohort:
                return
            raise TechLeadStormCohortConflictError(
                f"a different storm cohort is already recorded for anchor"
                f" issue #{anchor_issue_number}"
            )
        self._storm_cohorts[anchor_issue_number] = cohort

    def load_storm_cohort(
        self, *, anchor_issue_number: int
    ) -> tuple["DiscoveredFailure", ...] | None:
        return self._storm_cohorts.get(anchor_issue_number)

    def discard_storm_cohort(self, *, anchor_issue_number: int) -> None:
        self._storm_cohorts.pop(anchor_issue_number, None)

    def list_storm_cohorts(
        self,
    ) -> tuple[tuple[int, tuple["DiscoveredFailure", ...]], ...]:
        return tuple(sorted(self._storm_cohorts.items()))

    def record_pattern(
        self,
        *,
        signature: str,
        issue_number: int,
        observation_id: str,
        fix_class: str = "",
        area: str = "",
        diagnosis: str = "",
    ) -> None:
        from ..domain.tech_lead_findings import PatternEvidence

        if not observation_id.strip():
            raise ValueError(
                "record_pattern requires the identity of the observation the"
                " case-file body records"
            )
        existing = self._patterns.get(signature)
        if existing is not None:
            if existing == issue_number:
                return
            raise TechLeadPatternConflictError(
                f"pattern signature {signature!r} is already recorded for"
                f" case-file issue #{existing}"
            )
        self._patterns[signature] = issue_number
        self._observations[signature] = {observation_id}
        self._evidence[signature] = PatternEvidence(
            signature=signature,
            case_file_issue_number=issue_number,
            observation_count=1,
            fix_class=fix_class,
            area=area,
            diagnosis=diagnosis,
        )

    def note_pattern_observation(
        self,
        *,
        signature: str,
        observation_id: str,
        fix_class: str = "",
        area: str = "",
    ) -> bool:
        from dataclasses import replace

        from ..domain.tech_lead_findings import reconcile_pattern_classification

        if not observation_id.strip():
            raise ValueError(
                "note_pattern_observation requires a stable observation identity"
            )
        row = self._evidence.get(signature)
        if row is None:
            raise UnknownTechLeadPatternError(
                f"no pattern case file is recorded for signature {signature!r}"
            )
        # Classification is reconciled even for a replayed observation, so a
        # conflict is reported identically on the first attempt and the retry.
        merged_fix_class = reconcile_pattern_classification(
            field="fix_class",
            signature=signature,
            existing=row.fix_class,
            incoming=fix_class,
        )
        merged_area = reconcile_pattern_classification(
            field="area", signature=signature, existing=row.area, incoming=area
        )
        recorded = self._observations.setdefault(signature, set())
        if observation_id in recorded:
            return False
        recorded.add(observation_id)
        self._evidence[signature] = replace(
            row,
            observation_count=row.observation_count + 1,
            fix_class=merged_fix_class,
            area=merged_area,
        )
        return True

    def has_pattern_observation(self, *, signature: str, observation_id: str) -> bool:
        return observation_id in self._observations.get(signature, set())

    def lookup_pattern(self, *, signature: str) -> int | None:
        return self._patterns.get(signature)

    def list_patterns(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(self._patterns.items()))

    def list_pattern_evidence(self) -> tuple["PatternEvidence", ...]:
        return tuple(self._evidence[key] for key in sorted(self._evidence))

    def record_promotion(self, *, promotion: "PromotedFinding") -> None:
        existing = self._promotions.get(promotion.signature)
        if existing is not None:
            if (
                existing.target_repo == promotion.target_repo
                and existing.target_issue_number == promotion.target_issue_number
            ):
                return
            raise TechLeadPromotionConflictError(
                f"pattern signature {promotion.signature!r} is already promoted to"
                f" {existing.target_repo}#{existing.target_issue_number}"
            )
        self._promotions[promotion.signature] = promotion

    def load_promotion(self, *, signature: str) -> "PromotedFinding | None":
        return self._promotions.get(signature)

    def list_promotions(self) -> tuple["PromotedFinding", ...]:
        return tuple(self._promotions[key] for key in sorted(self._promotions))

    def note_promotion_reported(self, *, signature: str, observations: int) -> None:
        from dataclasses import replace

        row = self._promotions.get(signature)
        if row is None:
            raise UnknownTechLeadPatternError(
                f"no promotion is recorded for signature {signature!r}"
            )
        self._promotions[signature] = replace(
            row,
            reported_observations=max(row.reported_observations, observations),
        )

    def settle_promotion(
        self,
        *,
        signature: str,
        state: "PromotionState",
        shipped_pr_url: str = "",
    ) -> None:
        from dataclasses import replace

        row = self._promotions.get(signature)
        if row is None:
            raise UnknownTechLeadPatternError(
                f"no promotion is recorded for signature {signature!r}"
            )
        self._promotions[signature] = replace(
            row, state=state, shipped_pr_url=shipped_pr_url or row.shipped_pr_url
        )

    def record_shipped_fix(
        self, *, issue_number: int, title: str, pr_url: str, area: str
    ) -> None:
        from ..domain.tech_lead_session import TechLeadShippedFixSummary

        existing = self._shipped_fixes.get(issue_number)
        if existing is not None:
            # Titles are human metadata and may be edited between a durable
            # write and a crash-retry. PR + area are the evidence identity;
            # retain the original title rather than blocking reconciliation.
            if existing.pr_url == pr_url and existing.area == area:
                return
            raise TechLeadShippedFixConflictError(
                f"different shipped-fix evidence is already recorded for"
                f" issue #{issue_number}"
            )
        self._shipped_fixes[issue_number] = TechLeadShippedFixSummary(
            issue_number=issue_number,
            title=title,
            pr_url=pr_url,
            area=area,
            merged_at=datetime.now(timezone.utc).isoformat(),
        )

    def list_recent_shipped_fixes(
        self, *, limit: int
    ) -> tuple["TechLeadShippedFixSummary", ...]:
        if limit <= 0:
            raise ValueError("shipped-fix limit must be positive")
        return tuple(
            sorted(
                self._shipped_fixes.values(),
                key=lambda item: (item.merged_at, item.issue_number),
                reverse=True,
            )[:limit]
        )
