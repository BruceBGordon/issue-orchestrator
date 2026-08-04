"""The bounded owner of pattern case-file identity and evidence (#6781/#6957).

A case file is the durable evidence ledger for one pattern signature, and three
invariants make it trustworthy:

* **At most one case file per signature, ever.** Promotion reads the accrued
  observation count, so a second case file would split the evidence that gates
  it — or orphan half of it.
* **Every observation counted exactly once**, under its own identity. The count
  is orchestrator-owned precisely so it cannot be inflated by editing the issue,
  and it must not be inflated by the orchestrator's own retries either.
* **The recorded classification is what the recorded body says.** ``fix_class``
  and ``area`` decide whether a finding is promotable at all and which repo it
  routes to, so they must come from the command that actually wrote the issue.

All three span an authority-store write AND a GitHub write, in an order that
matters, and this module is the owner of that transaction.

**The creation transaction.** The GitHub issue is created before its ledger row
exists, so a crash in between leaves an issue nothing knows about. A marker
lookup can find that issue again — but it cannot say WHICH command wrote it, and
the retry is not guaranteed to be the same command: an ordinary case-file
finalization failure is returned as an ``ActionResult`` failure, so the next
observation of that signature can be the one that recovers it. Adopting the
orphan with the retrying action's metadata therefore recorded the wrong
observation identity and could rewrite a ``fix:human`` finding as ``fix:code``,
with no durable row for the classification preflight to defend (#6957 round-3
review F10). So the owner writes a durable :class:`PendingCaseFile` BEFORE the
create and finalizes a recovered issue FROM THAT INTENT; the retrying action is
then handled separately, as an ordinary append.

Four operations, one owner:

* :meth:`PatternCaseFileOwner.resolve` — typed :class:`CaseFileResolution`:
  already committed, recovered from its intent just now, or absent (create it).
* :meth:`PatternCaseFileOwner.begin` — record the creation intent; call
  immediately before the GitHub create.
* :meth:`PatternCaseFileOwner.open` — commit the ledger row for an issue this
  process just created, and append the rest of its decision's observations.
* :meth:`PatternCaseFileOwner.adopt` / :meth:`append_observations` — reconcile
  an action's observations onto an existing case file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable, Iterable

from ..domain.tech_lead_findings import (
    PendingCaseFile,
    reconcile_pattern_classification,
)

if TYPE_CHECKING:
    from ..domain.tech_lead_findings import PatternObservation
    from ..ports import RepositoryHost
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .actions import CreateTechLeadCaseFileIssueAction

logger = logging.getLogger(__name__)


class OrphanedCaseFileError(RuntimeError):
    """A case file exists on GitHub with no ledger row and no creation intent.

    Unreachable through the ordinary crash path — the intent is written before
    every create — so this means the orchestrator's own durable state was lost
    while the remote issue survived. Neither available answer is safe to guess:
    adopting the orphan would invent its observation identity and classification
    from an unrelated action, and creating another would split the signature's
    evidence. So the lane stops and says so.
    """


class CaseFileState(Enum):
    """What :meth:`PatternCaseFileOwner.resolve` found."""

    #: The ledger already holds this signature.
    COMMITTED = "committed"
    #: An in-flight creation was found on GitHub and finalized from its intent.
    RECOVERED = "recovered"
    #: No case file exists; the caller must create one.
    ABSENT = "absent"


@dataclass(frozen=True)
class CaseFileResolution:
    """Where a case-file creation action should land."""

    state: CaseFileState
    issue_number: int | None = None

    def __post_init__(self) -> None:
        exists = self.state is not CaseFileState.ABSENT
        if exists != (self.issue_number is not None):
            raise ValueError(
                f"a {self.state.value} case-file resolution must"
                f"{'' if exists else ' not'} carry an issue number"
            )


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
    """Owns case-file identity, its creation transaction, and evidence accrual."""

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

    def resolve(
        self, action: "CreateTechLeadCaseFileIssueAction"
    ) -> CaseFileResolution:
        """Does a case file already exist for this signature, and where?

        The ledger is the authority and is consulted first, so the common path
        costs no GitHub call. Otherwise the durable creation intent decides
        whether an in-flight create could exist at all:

        * an intent present means one might — look it up, and if it is there,
          COMMIT ITS LEDGER ROW FROM THE INTENT. The intent knows the body
          observation, classification, area, and diagnosis that were actually
          written; the action in hand may be a later, different observation of
          the same signature and must not lend its own metadata (#6957 round-3
          review F10);
        * an intent present but no remote issue means the create never
          happened. The intent is stale, so it is discarded explicitly rather
          than silently overwritten, and the caller creates fresh;
        * no intent at all means nothing was in flight. An issue nevertheless
          existing is a durable-state loss, not a crash window, and raises
          :class:`OrphanedCaseFileError` rather than being guessed at.

        A lookup that FAILS propagates: "unknown" must never be mistaken for
        "no case file exists", because that is what files a duplicate.
        """
        committed = self._authority.lookup_pattern(signature=action.pattern_signature)
        if committed is not None:
            # Belt and braces: an intent left behind by a crash between the
            # ledger write and its discard is inert, but it should not linger.
            self._authority.discard_pending_case_file(
                signature=action.pattern_signature
            )
            return CaseFileResolution(CaseFileState.COMMITTED, committed)

        pending = self._authority.load_pending_case_file(
            signature=action.pattern_signature
        )
        found = self._repository_host.find_issue_by_marker(
            title=action.title, marker=action.idempotency_marker
        )
        if pending is None:
            if found is None:
                return CaseFileResolution(CaseFileState.ABSENT)
            raise OrphanedCaseFileError(
                f"case file #{found} exists for signature"
                f" {action.pattern_signature!r} but the orchestrator has neither a"
                " ledger row nor a record of creating it. Its observation identity"
                " and fix classification cannot be reconstructed safely; recover"
                " the authority store, or close that issue to let the signature"
                " open a fresh case file"
            )
        if found is None:
            logger.info(
                "[tech_lead] Discarding a stale case-file creation intent for"
                " signature %r: no issue carries its marker, so the create never"
                " happened",
                action.pattern_signature,
            )
            self._authority.discard_pending_case_file(
                signature=action.pattern_signature
            )
            return CaseFileResolution(CaseFileState.ABSENT)

        logger.warning(
            "[tech_lead] Recovered orphaned case file #%d for signature %r from its"
            " creation intent (interrupted creation); adopting it instead of filing"
            " a second one",
            found,
            action.pattern_signature,
        )
        self._commit(
            signature=action.pattern_signature,
            issue_number=found,
            body_observation_id=pending.body_observation_id,
            fix_class=pending.fix_class,
            area=pending.area,
            diagnosis=pending.diagnosis,
        )
        return CaseFileResolution(CaseFileState.RECOVERED, found)

    def begin(self, action: "CreateTechLeadCaseFileIssueAction") -> None:
        """Record the creation intent. Call immediately BEFORE the GitHub create.

        This is the whole point of the transaction: whatever happens next, the
        orchestrator can say which command wrote the issue and what it meant.
        """
        self._authority.record_pending_case_file(
            pending=PendingCaseFile(
                signature=action.pattern_signature,
                title=action.title,
                idempotency_marker=action.idempotency_marker,
                body_observation_id=action.body_observation.observation_id,
                fix_class=action.fix_class,
                area=action.area or "",
                diagnosis=action.diagnosis,
            )
        )

    def open(
        self, action: "CreateTechLeadCaseFileIssueAction", *, issue_number: int
    ) -> None:
        """Commit the ledger row for an issue THIS action just created.

        The issue BODY is the first observation, so the row is created carrying
        exactly that one identity; every further observation from the same
        decision is appended one at a time (comment, then count create-once)
        rather than pre-counted. Pre-counting them claimed evidence a crash
        might never post, and the retry counted it all again.
        """
        self._commit(
            signature=action.pattern_signature,
            issue_number=issue_number,
            body_observation_id=action.body_observation.observation_id,
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

    def _commit(
        self,
        *,
        signature: str,
        issue_number: int,
        body_observation_id: str,
        fix_class: str,
        area: str,
        diagnosis: str,
    ) -> None:
        """Write the ledger row and retire the creation intent."""
        self._authority.record_pattern(
            signature=signature,
            issue_number=issue_number,
            observation_id=body_observation_id,
            fix_class=fix_class,
            area=area,
            diagnosis=diagnosis,
        )
        self._authority.discard_pending_case_file(signature=signature)

    def adopt(
        self, action: "CreateTechLeadCaseFileIssueAction", *, issue_number: int
    ) -> "ObservationAppendOutcome":
        """Reconcile a whole creation action onto an ALREADY-existing case file.

        Every observation the action carried becomes a repeat observation on the
        existing issue — comment AND durable count, exactly like the planned
        repeat path — so evidence is never silently dropped when a concurrent
        tick, a crash-retry, or a recovery got there first. When the action IS
        the one that created the issue, its body observation is already recorded
        and is skipped rather than re-posted.
        """
        return self.append_observations(
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

        0. RECONCILE the incoming classification against the recorded row first.
           A conflict raises before anything is published — the apply-time
           mirror of the planner's preflight, and the one that matters on the
           recovery path, where the durable row appears only moments earlier
           and planning could not have seen it (#6957 round-3 review F10).
        1. an identity ALREADY recorded means a previous attempt completed —
           do nothing (a purely local ledger read, no GitHub call);
        2. otherwise comment FIRST, then record create-once. A crash between
           the two repeats one comment on retry (cosmetic, and the comment
           carries the observation marker), but the durable count can never
           move twice for one observation.

        Evidence is therefore never lost, never inflated, and never published
        under a classification the ledger disagrees with.

        Returns what actually happened, so callers report a replay honestly
        instead of inferring it from the absence of an error.
        """
        recorded_row = self._authority.load_pattern_evidence(signature=signature)
        if recorded_row is not None:
            reconcile_pattern_classification(
                field="fix_class",
                signature=signature,
                existing=recorded_row.fix_class,
                incoming=fix_class,
            )
            reconcile_pattern_classification(
                field="area",
                signature=signature,
                existing=recorded_row.area,
                incoming=area,
            )
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
