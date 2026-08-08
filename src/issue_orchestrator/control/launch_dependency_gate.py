"""The launch-time dependency and stack-base gate.

Scheduling and launching are separated by a tick or more, and everything the
schedule was decided on can move in between: a predecessor branch advances, a
review re-opens, an issue body is rewritten. So every launch path re-asks the
work gate immediately before it commits to a worktree, and this is the one place
that asking lives.

It exists as its own owner because the answer is not a boolean. A launch needs
three distinguishable outcomes - proceed on the default base, proceed seeded
from a named predecessor branch, or fail closed - and collapsing any of them
into the others is how a stack successor silently gets rebased onto the default
branch (#6596 F1). The launcher therefore holds this gate rather than the
evaluator, the issue-refresh callback and the event sink it would otherwise have
to combine correctly at four separate call sites.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Mapping, Optional, Sequence

from ..domain.dependency_gates import Gate
from ..events import EventName
from ..ports import EventSink, Issue as IssueProtocol, make_trace_event
from .session_launch_types import LaunchResult
from .stack_base import StackBaseDecision
from .transition_log import log_transition

if TYPE_CHECKING:
    from .dependency_evaluator import DependencyEvaluator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DependencyFreshness:
    """Outcome of the just-before-launch dependency recheck (ADR-0029 / #6596).

    ``failure`` is a non-``None`` :class:`LaunchResult` when the work gate is no
    longer open (the launch must abort). ``stack_base_branch`` carries the same
    gate report's selected stack base so the launcher can seed the successor's
    worktree from the predecessor branch without re-evaluating (and re-gathering
    predecessor facts), avoiding a second round of GitHub reads.
    """

    failure: "LaunchResult | None" = None
    stack_base_branch: str | None = None


@dataclass(frozen=True)
class LaunchDependencyGate:
    """Re-asks the work gate for a launch that is about to commit resources.

    Wired with no evaluator, stack gating is off and every question answers
    "proceed" - the composition path for repos that do not use dependencies at
    all.
    """

    dependency_evaluator: Optional["DependencyEvaluator"]
    refresh_issue: Optional[Callable[[int], Optional["IssueProtocol"]]]
    events: EventSink

    def verify_fresh(self, issue: "IssueProtocol") -> DependencyFreshness:
        """CAS check: verify dependencies haven't changed since scheduling."""
        if not self.dependency_evaluator:
            return DependencyFreshness()

        # Prefer the freshest body for the just-before-launch recheck, but fall
        # back to the already-known issue body when refresh is unavailable/empty
        # so a transient read miss does not collapse a stack successor into an
        # ordinary issue. If no body can be obtained at all, the launch cannot
        # prove the slice is non-stack and must fail closed (retryable) rather
        # than seed the worktree from the default base (#6596 F1).
        fresh_issue = self.refresh_issue(issue.number) if self.refresh_issue else None
        body = (fresh_issue.body if fresh_issue else None) or issue.body
        milestone = fresh_issue.milestone if fresh_issue else issue.milestone
        if body is None:
            reason = f"could not read issue #{issue.number} body to confirm stack base"
            log_transition("issue", issue.number, "AVAILABLE", "SKIP", reason)
            self._publish_blocked(
                issue_number=issue.number,
                issue_title=issue.title,
                reason=reason,
                retryable=True,
            )
            return DependencyFreshness(
                failure=LaunchResult(
                    None, False, f"Dependencies not satisfied: {reason}"
                )
            )

        # Re-gather predecessor facts at launch time so a predecessor branch or
        # review-state change between scheduling and launch cannot start stale
        # stack work (ADR-0029 just-before-launch recheck).
        report = self.dependency_evaluator.evaluate_work_gate(
            issue_number=issue.number,
            issue_body=body,
            source_milestone=milestone,
        )
        if not report.can_start_work:
            summary = report.work_summary()
            log_transition(
                "issue", issue.number, "AVAILABLE", "SKIP",
                f"dependencies changed: {summary}"
            )
            self._publish_blocked(
                issue_number=issue.number,
                issue_title=issue.title,
                reason=summary,
                blocked_reasons=[
                    record.as_dict()
                    for record in report.gate_block_records(Gate.WORK)
                ],
            )
            return DependencyFreshness(
                failure=LaunchResult(
                    None, False, f"Dependencies not satisfied: {summary}"
                )
            )

        return DependencyFreshness(stack_base_branch=report.stack_base_branch)

    def stack_base_decision(
        self,
        issue_number: int,
        issue_body: str | None,
        source_milestone: str | None,
    ) -> StackBaseDecision:
        """Typed stack base decision for a launch (ADR-0029 / #6596).

        The single launch-side reader of stack base selection. Callers can
        distinguish a non-stack issue (proceed on the default base) from an
        allowed stack successor (seed/reset from the predecessor branch) from a
        blocked stack successor (predecessor not ready, ambiguous base, etc. —
        fail closed and do NOT reset onto the default base).

        Absence semantics mirror the publish/work gate: when stack gating is
        wired but ``issue_body`` is unavailable, the launch cannot *prove* the
        slice is non-stack, so it returns a retryable blocked decision rather
        than collapsing an unreadable issue into "ordinary issue" and seeding
        from the default base. When the evaluator is not wired at all, stack
        gating is off and the launch proceeds normally. A present body with no
        ``Stack-after:`` edge short-circuits to non-stack with no extra
        predecessor-fact I/O.
        """
        if not self.dependency_evaluator:
            return StackBaseDecision.not_stack()
        if issue_body is None:
            return StackBaseDecision.blocked(
                f"could not read issue #{issue_number} body to confirm stack base",
                retryable=True,
                is_stack=False,
            )
        if "stack-after" not in issue_body.lower():
            return StackBaseDecision.not_stack()
        report = self.dependency_evaluator.evaluate_work_gate(
            issue_number=issue_number,
            issue_body=issue_body,
            source_milestone=source_milestone,
            emit_event=False,
        )
        return StackBaseDecision.from_stack_report(report, Gate.WORK)

    def stack_base_decision_for_issue(self, issue_number: int) -> StackBaseDecision:
        """Resolve a relaunch's stack base decision from the freshest body.

        Rework reuses the existing successor branch, so its worktree must be
        reset onto the predecessor branch just like the initial launch — else the
        reuse preflight would rebase the successor onto the default branch and
        the publish ancestry gate would block it. Fails the relaunch closed
        (retryable) when stack gating is wired but the body cannot be read, never
        collapsing an unreadable issue into "ordinary issue" (#6596 F1).
        """
        fresh_issue = self.refresh_issue(issue_number) if self.refresh_issue else None
        body = fresh_issue.body if fresh_issue else None
        milestone = fresh_issue.milestone if fresh_issue else None
        return self.stack_base_decision(issue_number, body, milestone)

    def relaunch_blocked_result(
        self,
        *,
        issue_number: int,
        issue_title: str,
        decision: StackBaseDecision,
        context: str,
    ) -> LaunchResult:
        """Emit the dependency-blocked signal and build a blocked launch result.

        Shared by validation retry and rework so a closed stack work gate is
        recorded consistently before any claim/worktree work, instead of silently
        resetting the successor worktree onto the default base.
        """
        reason = decision.reason or "stack work gate blocked"
        log_transition(
            "issue", issue_number, "LAUNCHING", "SKIP", f"{context}: {reason}"
        )
        self._publish_blocked(
            issue_number=issue_number,
            issue_title=issue_title,
            reason=reason,
            retryable=decision.retryable,
        )
        return LaunchResult(None, False, f"Stack dependencies not satisfied: {reason}")

    def _publish_blocked(
        self,
        *,
        issue_number: int,
        issue_title: str,
        reason: str,
        retryable: bool | None = None,
        blocked_reasons: Sequence[Mapping[str, object]] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "issue_number": issue_number,
            "issue_title": issue_title,
            "reason": reason,
            "gate": Gate.WORK.value,
        }
        if retryable is not None:
            payload["retryable"] = retryable
        if blocked_reasons is not None:
            payload["blocked_reasons"] = list(blocked_reasons)
        self.events.publish(make_trace_event(EventName.ISSUE_DEPENDENCY_BLOCKED, payload))


__all__ = ["DependencyFreshness", "LaunchDependencyGate"]
