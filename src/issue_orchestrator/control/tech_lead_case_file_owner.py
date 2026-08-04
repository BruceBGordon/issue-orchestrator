"""The bounded owner of pattern case-file identity and evidence (#6781/#6957).

A case file is the durable evidence ledger for one pattern signature, and two
invariants make it trustworthy:

* **At most one case file per signature, ever.** Promotion reads the accrued
  observation count, so a second case file would split the evidence that gates
  it — or orphan half of it.
* **Every observation counted exactly once.** The count is orchestrator-owned
  precisely so it cannot be inflated by editing the issue, and it must not be
  inflated by the orchestrator's own retries either.

Both invariants span an authority-store write AND a GitHub write, in an order
that matters. Callers used to coordinate that themselves — the applier's
creation path checked the ledger, created remotely, then recorded locally;
the append path commented, then reconciled classification — which left two
externally visible holes (a duplicate case file after a crash between create
and record; a comment published before a rejected classification). This module
is the owner that closes both, so callers ask for an OUTCOME instead of
sequencing store and GitHub calls themselves (#6957 round-2 review A1/F10).

Three operations, one owner:

* :meth:`PatternCaseFileOwner.resolve` — "does a case file already exist for
  this signature?" The local ledger first (free); then, only when it does not,
  a marker-based remote recovery, which is what makes creation safe across the
  create/record crash window. A recovered issue is written back to the ledger
  before it is returned, so the recovery happens at most once.
* :meth:`PatternCaseFileOwner.append_observations` — post each observation's
  evidence and count it create-once, skipping identities already recorded.
* :meth:`PatternCaseFileOwner.open` — record the new case file's ledger row and
  append the creating decision's remaining observations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:
    from ..domain.tech_lead_findings import PatternObservation
    from ..ports import RepositoryHost
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .actions import CreateTechLeadCaseFileIssueAction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ObservationAppendOutcome:
    """What an append actually did: newly counted vs. already-recorded."""

    recorded: int
    skipped: int

    @property
    def deduplicated(self) -> bool:
        """True when nothing new was counted — every observation was a replay."""
        return self.recorded == 0 and self.skipped > 0


class PatternCaseFileOwner:
    """Owns case-file identity, remote recovery, and observation accrual."""

    def __init__(
        self,
        *,
        authority: "TechLeadAuthorityStore",
        repository_host: "RepositoryHost",
        add_comment: Callable[[int, str], str],
    ) -> None:
        self._authority = authority
        self._repository_host = repository_host
        self._add_comment = add_comment

    def resolve(self, action: "CreateTechLeadCaseFileIssueAction") -> int | None:
        """The existing case file for this signature, or None to create one.

        The ledger is the authority and is consulted first, so the common path
        costs no GitHub call. Only when it has no row does this ask GitHub
        whether the issue nevertheless EXISTS — which happens exactly when a
        previous attempt created it and then died before recording the row
        (#6957 round-2 review F10). Recovering it writes the ledger row, so the
        signature is never filed twice and the evidence never splits.

        A recovery lookup that FAILS propagates: "unknown" must never be
        mistaken for "no case file exists", because that is precisely the
        mistake that files a duplicate.
        """
        recorded = self._authority.lookup_pattern(signature=action.pattern_signature)
        if recorded is not None:
            return recorded
        recovered = self._repository_host.find_issue_by_marker(
            title=action.title, marker=action.idempotency_marker
        )
        if recovered is None:
            return None
        logger.warning(
            "[tech_lead] Recovered orphaned case file #%d for signature %r: the"
            " issue existed on GitHub with no ledger row (interrupted creation);"
            " adopting it instead of filing a second one",
            recovered,
            action.pattern_signature,
        )
        self.open(action, issue_number=recovered)
        return recovered

    def open(
        self, action: "CreateTechLeadCaseFileIssueAction", *, issue_number: int
    ) -> None:
        """Record the ledger row for a case file and append its observations.

        The issue BODY is the first observation, so the row is created carrying
        exactly that one identity; every further observation from the same
        decision is appended one at a time (comment, then count create-once)
        rather than pre-counted. Pre-counting them claimed evidence a crash
        might never post, and the retry counted it all again.

        Correct for a RECOVERED issue too: its body was written by the process
        that died, so the body observation is already visible there and only
        its identity needs recording — never re-posting.
        """
        self._authority.record_pattern(
            signature=action.pattern_signature,
            issue_number=issue_number,
            observation_id=action.body_observation.observation_id,
            fix_class=action.fix_class,
            area=action.area or "",
            diagnosis=action.diagnosis,
        )
        self.append_observations(
            signature=action.pattern_signature,
            issue_number=issue_number,
            observations=action.additional_observations,
            fix_class=action.fix_class,
            area=action.area or "",
        )

    def adopt(self, action: "CreateTechLeadCaseFileIssueAction", *, issue_number: int) -> None:
        """Reconcile a whole creation action onto an ALREADY-existing case file.

        Every observation the action carried becomes a repeat observation on the
        existing issue — comment AND durable count, exactly like the planned
        repeat path — so evidence is never silently dropped when a concurrent
        tick or a crash-retry got there first.
        """
        self.append_observations(
            signature=action.pattern_signature,
            issue_number=issue_number,
            observations=action.observations,
            fix_class=action.fix_class,
            area=action.area or "",
        )

    def append_observations(
        self,
        *,
        signature: str,
        issue_number: int,
        observations: Iterable["PatternObservation"],
        fix_class: str,
        area: str,
    ) -> "ObservationAppendOutcome":
        """Post and count each observation, skipping ones already recorded.

        The ordering is deliberate and shared by every caller:

        1. an identity ALREADY recorded means a previous attempt completed —
           do nothing (a purely local ledger read, no GitHub call);
        2. otherwise comment FIRST, then record create-once. A crash between
           the two repeats one comment on retry (cosmetic, and the comment
           carries the observation marker), but the durable count can never
           move twice for one observation.

        Evidence is therefore never lost and never inflated — the two failure
        modes that matter, since the count alone gates promotion.

        Returns what actually happened, so callers report a replay honestly
        instead of inferring it from the absence of an error.
        """
        recorded = 0
        skipped = 0
        for observation in observations:
            if self._authority.has_pattern_observation(
                signature=signature, observation_id=observation.observation_id
            ):
                skipped += 1
                continue
            self._add_comment(issue_number, observation.comment)
            self._authority.note_pattern_observation(
                signature=signature,
                observation_id=observation.observation_id,
                fix_class=fix_class,
                area=area,
            )
            recorded += 1
        return ObservationAppendOutcome(recorded=recorded, skipped=skipped)
