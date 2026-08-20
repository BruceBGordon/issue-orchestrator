"""On-demand tech_lead dispatch — the tech lead, aimed by hand (ADR-0031).

The reactive tech_lead triggers all *discover* the issue to investigate from the
board: the stuck-sweep backstop (:mod:`.stuck_sweep`), the per-failure reaction
model (:mod:`.tech_lead_reaction`), and the periodic / problem-storm health
review. This owner is their manual counterpart — an operator names one or more
specific issues and the tech lead (a ``failure_investigation``) is dispatched
at each, on demand.

It reuses the REAL tech-lead-launch machinery
(``orchestrator.launch_tech_lead_session``) so evidence-map staging, authority
recording, and agent sandboxing are byte-for-byte identical to a reactive
launch: this module builds the same :class:`PendingTechLeadReview` a discovered
failure would and hands it to the same facade method — which since #6994
round 2 routes through the single
:class:`~.tech_lead_launch_authority.TechLeadLaunchAuthority`, so this path
cannot start a run the scope matrix or the shared ledger would refuse. It then drives the
launched session to completion with the planner PAUSED, so no OTHER board work
starts while the investigation runs — a paused planner returns an empty plan,
yet ``tick()`` still drives already-active sessions to completion and drains
background completion jobs. Completion is applied under ``tech_lead.authority``
exactly as usual, so ``propose``-gated actions stay gated (an ``--advise-only``
run pins every dial to ``propose``).

Boundaries kept deliberately narrow:

* **Owner only.** No CLI / argparse here — the entrypoint injects the clock and
  sleep and prints the results.
* **Injected time.** ``now`` / ``sleep`` are passed in so the drive loop is
  deterministic under test; this owner never touches ``time`` directly.
* **Reuse, don't reimplement.** The failure fact is modelled on
  :func:`.stuck_sweep._recovered_failure` (``timed_out`` reason so the reaction
  model always INVESTIGATES a leaf issue); the launch goes through the one
  facade method the planner already uses.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

from ..domain.models import PendingTechLeadReview
from ..domain.pause_state import PauseActor, PauseReason
from ..domain.tech_lead_run import (
    GlobalHealthReviewScope,
    IssueInvestigationScope,
    TechLeadRunAdmission,
    TechLeadRunRequest,
    TechLeadRunTrigger,
)
from .tech_lead_run_admission import run_key_of_pending

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, Session
    from ..ports import RepositoryHost

logger = logging.getLogger(__name__)

# Fallback blocking-label context when the operator aims at an issue carrying no
# ``blocked*`` label (e.g. a plain open issue they want the tech lead to look
# at). It rides along in ``DiscoveredFailure.blocking_label`` for evidence-map
# context only; ``failure_reason`` is always ``timed_out`` so the reaction model
# investigates regardless of the label. Re-exported from the run-admission owner
# (#6994) so both hand-aimed surfaces label their context identically.
_BLOCKED_LABEL_PREFIX = "blocked"


class TechLeadDispatchHost(Protocol):
    """The orchestrator-facade surface an on-demand dispatch drives.

    Named structurally rather than importing the concrete ``Orchestrator`` so
    this control-layer owner stays decoupled from the infra facade and a test
    can supply a lightweight fake. ``launch_tech_lead_session`` is the same public
    facade method the reactive planner path uses to launch a tech_lead session —
    reusing it verbatim is the whole point (identical evidence-map staging +
    authority recording).
    """

    @property
    def repository_host(self) -> "RepositoryHost": ...

    @property
    def state(self) -> "OrchestratorState": ...

    def launch_tech_lead_session(
        self, tech_lead: "PendingTechLeadReview"
    ) -> "Session | None": ...

    def request_tech_lead_run(
        self, request: "TechLeadRunRequest"
    ) -> "TechLeadRunAdmission": ...

    def pause(
        self,
        *,
        reason: "PauseReason" = ...,
        actor: "PauseActor" = ...,
        detail: str = ...,
    ) -> None: ...

    def tick(self) -> bool: ...

    def terminate_tech_lead_session(
        self, session: "Session"
    ) -> "TechLeadTerminationOutcome": ...


@dataclass(frozen=True)
class TechLeadTerminationOutcome:
    """Per-effect result of terminating a tech_lead session (#6824 R7).

    Each field is True when its effect SUCCEEDED or was not needed, and False
    when it was attempted and FAILED. ``clean`` is False when any effect failed —
    so a caller never reports a leak-free termination after, say, the disposable
    scratch worktree failed to remove. This outcome is the SOLE owner of a failed
    one-shot cleanup: the on-demand driver runs no further engine tick, so a
    failed removal is surfaced HERE (via ``leaked_worktree``) for explicit
    operator action, not deferred to a tick-based retry that would never run.
    """

    terminal_stopped: bool = True
    machine_removed: bool = True
    claim_released: bool = True
    # The repository-wide SCOPE hold, which is a different coordination layer
    # from the per-issue claim above. Termination owns both, or a timed-out
    # one-shot leaves every conflicting tech-lead run blocked until the lease
    # expires — there is no later tick to reconcile it (#6994 round 2 F10/A7).
    run_released: bool = True
    worktree_removed: bool = True
    # Path of the disposable scratch worktree that could NOT be removed (a leak
    # requiring explicit operator action); None when there was nothing to remove
    # or removal succeeded.
    leaked_worktree: str | None = None

    @property
    def clean(self) -> bool:
        return (
            self.terminal_stopped
            and self.machine_removed
            and self.claim_released
            and self.run_released
            and self.worktree_removed
        )

    def failures(self) -> tuple[str, ...]:
        """Human-readable names of the effects that failed (for the result detail)."""
        names = {
            "terminal_stopped": "terminal stop",
            "machine_removed": "state-machine removal",
            "claim_released": "claim release",
            "run_released": "tech-lead run release",
            "worktree_removed": "scratch-worktree removal",
        }
        return tuple(
            label for attr, label in names.items() if not getattr(self, attr)
        )


class TechLeadOutcomeStatus(Enum):
    """The discriminated lifecycle of one on-demand tech_lead dispatch (#6824 R7).

    A dispatch is in exactly ONE of these states, which makes the previously
    representable — and lying — ``launched-but-not-completed with no
    termination`` combination impossible to construct:

    * ``NOT_LAUNCHED`` — the session never started (issue not found, no anchor,
      or the launch path declined). No ``termination``.
    * ``COMPLETED`` — the launched session drained before the timeout. No
      ``termination``.
    * ``TIMED_OUT`` — the launched session hit ``timeout_s`` still running and
      was terminated; ``termination`` is REQUIRED and records whether that
      cleanup was clean.
    """

    NOT_LAUNCHED = "not_launched"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"


class _DispatchOutcome:
    """Shared derived view + invariant for the two on-demand dispatch results.

    Both :class:`InvestigationResult` and :class:`HealthReviewResult` carry the
    same ``status``/``termination`` lifecycle; this mixin owns the single source
    of truth for the derived ``launched``/``completed`` booleans (so every
    existing consumer keeps working) and the ``termination``-presence invariant,
    rather than duplicating either across the two dataclasses.
    """

    status: "TechLeadOutcomeStatus"
    termination: "TechLeadTerminationOutcome | None"

    @property
    def launched(self) -> bool:
        """True once a session actually started (COMPLETED or TIMED_OUT)."""
        return self.status is not TechLeadOutcomeStatus.NOT_LAUNCHED

    @property
    def completed(self) -> bool:
        """True only when the launched session drained before the timeout."""
        return self.status is TechLeadOutcomeStatus.COMPLETED

    def _validate_status(self) -> None:
        """Enforce the single invariant: ``termination`` present IFF TIMED_OUT.

        Fail fast (fail-fast design) so a producer can neither build a timeout
        result without its cleanup outcome nor attach a stray termination to a
        completed / not-launched result. One invariant, one check — the two
        failure directions are the same XOR violation, distinguished at runtime
        by the state named in the message.
        """
        has_termination = self.termination is not None
        expects_termination = self.status is TechLeadOutcomeStatus.TIMED_OUT
        if has_termination != expects_termination:
            raise ValueError(
                "TechLeadTerminationOutcome must be attached iff status is TIMED_OUT"
                f" (status={self.status.name},"
                f" termination={'present' if has_termination else 'None'})"
            )


@dataclass(frozen=True)
class InvestigationResult(_DispatchOutcome):
    """The outcome of one on-demand tech-lead dispatch.

    ``status`` is the discriminated lifecycle (:class:`TechLeadOutcomeStatus`);
    the ``launched``/``completed`` booleans consumers read are DERIVED from it
    (see :class:`_DispatchOutcome`), so an invalid launched-but-incomplete state
    with no termination cannot be constructed.
    """

    issue_number: int
    status: TechLeadOutcomeStatus
    detail: str
    # The structured termination outcome, REQUIRED iff ``status`` is TIMED_OUT
    # (None when it completed or never launched); the command surfaces its
    # cleanliness rather than always printing "session terminated" (#6824 R7).
    termination: "TechLeadTerminationOutcome | None" = None

    def __post_init__(self) -> None:
        self._validate_status()


def run_targeted_investigations(
    orchestrator: TechLeadDispatchHost,
    issue_numbers: Sequence[int],
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    poll_interval: float = 3.0,
    timeout_s: float = 1800.0,
) -> list[InvestigationResult]:
    """Dispatch the tech lead at each issue and drive it to completion.

    Admission runs BEFORE the pause, then the planner is paused ONCE so no other
    board work launches while the targeted investigations run. That order is
    deliberate: every run — dashboard, reactive, or CLI — is admitted by the one
    :class:`~.tech_lead_run_admission.TechLeadRunCoordinator`, and its matrix
    rightly refuses a paused engine, so the CLI must ask while the engine is
    still live and only then halt the planner it is standing in for (#6994
    round 1 F2). Each admitted run is launched through the real facade path and
    ticked until its session leaves ``active_sessions`` or ``timeout_s`` elapses.

    Returns one :class:`InvestigationResult` per requested issue, in order.
    """
    admissions = [
        (
            issue_number,
            orchestrator.request_tech_lead_run(
                TechLeadRunRequest(
                    scope=IssueInvestigationScope(issue_number),
                    trigger=TechLeadRunTrigger.CLI,
                )
            ),
        )
        for issue_number in issue_numbers
    ]
    orchestrator.pause(
        reason=PauseReason.TECH_LEAD_INVESTIGATION,
        actor=PauseActor.CLI,
        detail=f"on-demand investigation of {len(issue_numbers)} issue(s): "
               f"{', '.join(f'#{n}' for n in issue_numbers)}",
    )
    logger.info(
        "[TECH_LEAD_TRIGGER] planner paused for %d on-demand investigation(s) (#6823)",
        len(issue_numbers),
    )
    return [
        _investigate_one(
            orchestrator,
            issue_number,
            admission,
            now=now,
            sleep=sleep,
            poll_interval=poll_interval,
            timeout_s=timeout_s,
        )
        for issue_number, admission in admissions
    ]


@dataclass(frozen=True)
class HealthReviewResult(_DispatchOutcome):
    """The outcome of an on-demand whole-board health review.

    ``anchor_issue_number`` is the health-review anchor that was launched, or
    ``None`` when none could be prepared (e.g. no tech lead agent configured, or
    anchor creation failed). ``status`` is the same discriminated lifecycle as
    :class:`InvestigationResult`, with ``launched``/``completed`` derived from it.
    """

    anchor_issue_number: int | None
    status: TechLeadOutcomeStatus
    detail: str
    # Structured termination outcome, REQUIRED iff ``status`` is TIMED_OUT (#6824 R7).
    termination: "TechLeadTerminationOutcome | None" = None

    def __post_init__(self) -> None:
        self._validate_status()


def run_health_review(
    orchestrator: TechLeadDispatchHost,
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    poll_interval: float = 3.0,
    timeout_s: float = 1800.0,
) -> HealthReviewResult:
    """Force a whole-board health review NOW and drive it to completion.

    The on-demand counterpart of the timer-based periodic review (ADR-0031 §4).
    Like :func:`run_targeted_investigations` it pauses the planner up front so no
    other board work starts, then reuses the anchor lifecycle: an already-open
    anchor is reused, otherwise one is created + queued through the SAME owner
    the timer path uses (``ensure_health_review_anchor`` bypasses only the
    interval/fingerprint debounce, never the fingerprint recording). The queued
    anchor is launched through the real tech-lead-launch path and ticked to
    completion, exactly as a targeted investigation is.
    """
    # Admit BEFORE pausing: the shared admission matrix refuses a paused engine,
    # so a CLI that halted the planner first would be refused by the very owner
    # it is meant to share with the dashboard (#6994 round 1 F2).
    # The anchor decision (board fingerprint + interval) is sourced on the
    # WALL clock by the facade, matching the timer path (fact_gatherer uses
    # ``time.time()``); the injected ``now`` here is the monotonic drive-loop
    # clock, used only to bound the wait below.
    admission = orchestrator.request_tech_lead_run(
        TechLeadRunRequest(
            scope=GlobalHealthReviewScope(), trigger=TechLeadRunTrigger.CLI
        )
    )
    orchestrator.pause(
        reason=PauseReason.TECH_LEAD_HEALTH_REVIEW,
        actor=PauseActor.CLI,
        detail="on-demand whole-board health review",
    )
    logger.info("[TECH_LEAD_TRIGGER] planner paused for an on-demand health review")
    tech_lead = _admitted_queue_item(orchestrator, admission)
    if tech_lead is None:
        return HealthReviewResult(
            admission.issue_number, status=TechLeadOutcomeStatus.NOT_LAUNCHED,
            detail=f"health review not admitted: {admission.reason} ({admission.detail})",
        )
    session = orchestrator.launch_tech_lead_session(tech_lead)
    if session is None:
        logger.warning(
            "[TECH_LEAD_TRIGGER] health-review launch declined for anchor #%d",
            tech_lead.issue_number,
        )
        return HealthReviewResult(
            tech_lead.issue_number, status=TechLeadOutcomeStatus.NOT_LAUNCHED,
            detail=(
                f"health review not started for anchor #{tech_lead.issue_number}:"
                " the launch authority held or refused it (scope exclusivity,"
                " cross-engine ownership, or a launch failure) — it stays queued"
            ),
        )
    identity = _session_identity(session)
    logger.info(
        "[TECH_LEAD_TRIGGER] dispatched health review at anchor #%d (session %s)",
        tech_lead.issue_number,
        identity,
    )
    termination = _drive_session_to_completion(
        orchestrator,
        identity,
        now=now,
        sleep=sleep,
        poll_interval=poll_interval,
        timeout_s=timeout_s,
    )
    if termination is not None:
        return HealthReviewResult(
            tech_lead.issue_number, status=TechLeadOutcomeStatus.TIMED_OUT,
            detail=_timeout_detail("health review", timeout_s, termination),
            termination=termination,
        )
    logger.info(
        "[TECH_LEAD_TRIGGER] on-demand health review completed (anchor #%d)",
        tech_lead.issue_number,
    )
    return HealthReviewResult(
        tech_lead.issue_number, status=TechLeadOutcomeStatus.COMPLETED,
        detail=f"health review completed for anchor #{tech_lead.issue_number}",
    )


def _investigate_one(
    orchestrator: TechLeadDispatchHost,
    issue_number: int,
    admission: TechLeadRunAdmission,
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    poll_interval: float,
    timeout_s: float,
) -> InvestigationResult:
    """Launch and drive a single already-admitted on-demand investigation."""
    tech_lead = _admitted_queue_item(orchestrator, admission)
    if tech_lead is None:
        logger.warning(
            "[TECH_LEAD_TRIGGER] investigation of #%d not admitted: %s",
            issue_number,
            admission.reason,
        )
        return InvestigationResult(
            issue_number, status=TechLeadOutcomeStatus.NOT_LAUNCHED,
            detail=(
                f"investigation of issue #{issue_number} not admitted:"
                f" {admission.reason} ({admission.detail})"
            ),
        )
    session = orchestrator.launch_tech_lead_session(tech_lead)
    if session is None:
        logger.warning(
            "[TECH_LEAD_TRIGGER] tech_lead launch declined for issue #%d", issue_number
        )
        return InvestigationResult(
            issue_number, status=TechLeadOutcomeStatus.NOT_LAUNCHED,
            detail=(
                f"investigation of issue #{issue_number} not started: the launch"
                " authority held or refused it (scope exclusivity, cross-engine"
                " ownership, subject no longer eligible, or a launch failure)"
            ),
        )

    identity = _session_identity(session)
    logger.info(
        "[TECH_LEAD_TRIGGER] dispatched tech lead at issue #%d (session %s)",
        issue_number,
        identity,
    )
    return _drive_to_completion(
        orchestrator,
        issue_number,
        identity,
        now=now,
        sleep=sleep,
        poll_interval=poll_interval,
        timeout_s=timeout_s,
    )


def _drive_to_completion(
    orchestrator: TechLeadDispatchHost,
    issue_number: int,
    identity: str,
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    poll_interval: float,
    timeout_s: float,
) -> InvestigationResult:
    """Drive a launched failure investigation to completion and label the result."""
    termination = _drive_session_to_completion(
        orchestrator,
        identity,
        now=now,
        sleep=sleep,
        poll_interval=poll_interval,
        timeout_s=timeout_s,
    )
    if termination is not None:
        return InvestigationResult(
            issue_number, status=TechLeadOutcomeStatus.TIMED_OUT,
            detail=_timeout_detail("investigation", timeout_s, termination),
            termination=termination,
        )
    logger.info(
        "[TECH_LEAD_TRIGGER] issue #%d investigation completed", issue_number
    )
    return InvestigationResult(
        issue_number, status=TechLeadOutcomeStatus.COMPLETED,
        detail=f"investigation completed for issue #{issue_number}",
    )


def _timeout_detail(
    what: str, timeout_s: float, termination: "TechLeadTerminationOutcome"
) -> str:
    """Timeout result detail that is HONEST about a failed cleanup (#6824 R7).

    A clean termination reports "session terminated"; if any effect failed the
    detail says so, so a caller never reports a leak-free outcome after, e.g.,
    the disposable scratch worktree failed to remove.
    """
    detail = f"{what} timed out after {timeout_s:.0f}s (session terminated"
    if not termination.clean:
        detail += f"; cleanup INCOMPLETE — {', '.join(termination.failures())} failed"
    return detail + ")"


def _drive_session_to_completion(
    orchestrator: TechLeadDispatchHost,
    identity: str,
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    poll_interval: float,
    timeout_s: float,
) -> "TechLeadTerminationOutcome | None":
    """Tick until the launched session leaves ``active_sessions`` or times out.

    Shared drive loop for every on-demand tech_lead flavor (targeted failure
    investigations and the whole-board health review). The planner is already
    paused, so ``tick()`` starts no new work — it only drives the active session
    and drains background completion jobs. The session is matched by its stable
    :class:`SessionKey` identity (``tech_lead:N``) rather than by raw issue number,
    so a restored session with the same slot still counts as the one we
    launched. Returns None when the session drained (completed); on timeout it
    terminates the session and returns the :class:`TechLeadTerminationOutcome`.
    """
    deadline = now() + timeout_s
    while _session_active(orchestrator, identity):
        if now() >= deadline:
            logger.warning(
                "[TECH_LEAD_TRIGGER] session %s still active after %.0fs;"
                " terminating it and giving up the drive",
                identity,
                timeout_s,
            )
            return _terminate_session(orchestrator, identity)
        orchestrator.tick()
        sleep(poll_interval)
    return None


def _terminate_session(
    orchestrator: TechLeadDispatchHost, identity: str
) -> "TechLeadTerminationOutcome":
    """Terminate the timed-out session so ownership of the timeout is EXPLICIT.

    Routes through the reconciling ``terminate_tech_lead_session`` owner (#6824
    F7/R7), which returns a typed outcome — so the drive loop's
    ``_session_active`` check is immediately false, a multi-issue batch does not
    co-drive a dead session, and the caller learns whether cleanup was clean.
    Iterates a COPY because the reconciling terminate mutates ``active_sessions``.
    """
    outcome: "TechLeadTerminationOutcome | None" = None
    for session in list(orchestrator.state.active_sessions):
        if _session_identity(session) == identity:
            outcome = orchestrator.terminate_tech_lead_session(session)
    return outcome if outcome is not None else TechLeadTerminationOutcome()


def _session_active(orchestrator: TechLeadDispatchHost, identity: str) -> bool:
    """True while a session with the launched slot identity is still active."""
    return any(
        _session_identity(session) == identity
        for session in orchestrator.state.active_sessions
    )


def _session_identity(session: "Session") -> str:
    """Stable slot identity for the launched tech_lead session.

    ``SessionKey.stable_id()`` is the domain identity (``tech_lead:<issue>``) — it
    is value-based and survives session restore, so it is preferred over a raw
    issue-number match that could not distinguish a tech_lead slot from any other
    session on the same issue.
    """
    return session.key.stable_id()


def _admitted_queue_item(
    orchestrator: TechLeadDispatchHost, admission: TechLeadRunAdmission
) -> "PendingTechLeadReview | None":
    """The queued run this admission refers to, or None when there is no run.

    The one-shot CLI launches a run it did not construct: admission owns
    identity, eligibility, cross-instance ownership, and the typed failure
    context, and leaves the queue entry behind. Reading it back — rather than
    building a second :class:`PendingTechLeadReview` here — is what stops the
    CLI from being a path where those rules do not apply (#6994 round 1 F2).

    ``ALREADY_QUEUED``/``ALREADY_RUNNING`` are as launchable as ``QUEUED``: the
    caller asked for the run to happen, and it exists.
    """
    if not admission.outcome.has_run:
        return None
    return next(
        (
            item
            for item in orchestrator.state.pending_tech_lead_reviews
            if run_key_of_pending(item) == admission.run_key
        ),
        None,
    )
