"""Operator-facing gate-note rendering for tech-lead ``create_issue`` decisions.

The planner (`tech_lead_decision_actions.py`) translates each typed
``ProposalDedupGate`` outcome into an action; when an outcome gates the create,
the *presence* of a reason string is what gates it (never a bare boolean), so
every gated issue explains itself. This module owns the single mapping from a
typed dedup outcome to that explanation, plus the intra-decision batch note and
the composition of the two, so both the standalone gate path and the
sibling-dedup path render evidence identically and no candidate/score/reason is
ever lost (#6883 review).
"""

from __future__ import annotations

from typing import assert_never

from .proposal_dedup_gate import (
    CommentExisting,
    DedupOutcome,
    FileNew,
    GateDedupUnavailable,
    GateSuspectedDuplicate,
    GateUnverifiedDuplicate,
    RejectCandidate,
)

_PROPOSE_AUTHORITY_NOTE = (
    "Gated with the proposed-tech-lead label under `propose` authority (#6778):"
    " remove the label to approve."
)


def _suspected_note(outcome: GateSuspectedDuplicate) -> str:
    # A lexical match names its score (once); an agent-confirmed-but-uncommentable
    # duplicate has no score and its reason names the blocking mode(s).
    if outcome.score is not None:
        headline = (
            f"SUSPECTED DUPLICATE of #{outcome.issue_number}"
            f" (lexical score {outcome.score:.2f})"
        )
    else:
        headline = f"DUPLICATE of #{outcome.issue_number}"
    return (
        f"Gated as a {headline}: {outcome.reason}. Confirm and dedup onto that"
        " issue, or remove the proposed-tech-lead label to file this as a new"
        " issue."
    )


def _unavailable_note(outcome: GateDedupUnavailable) -> str:
    return (
        f"Gated for review: {outcome.reason}. Filed nothing automatically —"
        " remove the proposed-tech-lead label once checked, or dedup by hand."
    )


def _unverified_note(outcome: GateUnverifiedDuplicate) -> str:
    return (
        f"Gated as a possible DUPLICATE of #{outcome.issue_number}:"
        f" {outcome.reason}. Verify against #{outcome.issue_number}, then dedup"
        " onto it, or remove the proposed-tech-lead label to file this as new."
    )


def _rejected_note(outcome: RejectCandidate) -> str:
    return (
        f"Gated for review: the agent cited #{outcome.issue_number} as a duplicate"
        f" but {outcome.reason}. Filed as a new issue pending confirmation; remove"
        " the proposed-tech-lead label to approve."
    )


def _comment_existing_note(outcome: CommentExisting) -> str:
    # A CommentExisting outcome routes onto a verified, granted duplicate. When
    # such a proposal is ALSO an intra-decision sibling it cannot take that
    # comment action (its sibling already did), so this note carries the cited
    # issue forward as gate evidence on the filed-but-gated fallback.
    return (
        f"The proposal cited #{outcome.issue_number} as a confirmed duplicate"
        f" ({outcome.reason})."
    )


def batch_duplicate_note(sibling_action_id: str) -> str:
    return (
        f"Gated as an intra-decision duplicate of proposal {sibling_action_id} in"
        " the same tech-lead decision — only the first of identical sibling"
        " create_issue proposals takes a primary action (filed, or routed onto an"
        " existing issue); the rest are gated. Remove the proposed-tech-lead label"
        " to file this as a separate issue."
    )


def outcome_gate_note(outcome: DedupOutcome, *, execute: bool) -> str | None:
    """Single owner of the create_issue outcome → gate-note mapping.

    Returns the note a proposal's OWN typed outcome carries, independent of
    intra-decision batching. ``None`` means "no gate" (a clean FileNew under
    execute authority). Every duplicate-flavored outcome names its
    candidate/score/reason so a composed batch note never loses that evidence
    (#6883 review — composition, not replacement).

    Typed to the ``DedupOutcome`` union and matched exhaustively: ``FileNew`` is
    the ONLY value that may reach the ungated ``None`` path, and it is handled
    explicitly. Any other value — a bad caller or a future ``DedupOutcome``
    variant added without extending this mapper — hits ``assert_never`` and
    fails fast (a static exhaustiveness error at type-check time, an
    ``AssertionError`` at runtime) rather than silently degrading to an ungated
    create (#6883 review — fail-fast, never fail-open).
    """
    match outcome:
        case FileNew():
            # Novel — gated only when create_issue authority is propose (#6778).
            return None if execute else _PROPOSE_AUTHORITY_NOTE
        case CommentExisting():
            return _comment_existing_note(outcome)
        case GateSuspectedDuplicate():
            return _suspected_note(outcome)
        case GateDedupUnavailable():
            return _unavailable_note(outcome)  # fail closed
        case GateUnverifiedDuplicate():
            return _unverified_note(outcome)
        case RejectCandidate():
            return _rejected_note(outcome)
        case _:
            assert_never(outcome)


def compose_gate_note(batch_note: str, typed_note: str | None) -> str:
    """Compose an intra-decision sibling gate reason with the proposal's own
    typed outcome note. The batch reason leads (it is why the primary action was
    withheld); the typed note is appended so a sibling that ALSO trips the
    corpus/authority gate keeps its candidate/score/reason (#6883 review)."""
    if typed_note is None:
        return batch_note
    return f"{batch_note}\n>\n> It is independently flagged as well — {typed_note}"
