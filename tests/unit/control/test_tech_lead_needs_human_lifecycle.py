"""Expected-state guards for the tech_lead needs-human lifecycle (#6785 F2).

``TechLeadNeedsHumanLifecycle`` owns marker-provenance escalation and stale-label
clearing.  Its fresh read is only a hint: a concurrent human or orchestrator
path can change the labels between that read and the applier's write.  These
tests exercise that race by giving the lifecycle a stale ``read_labels`` while
a faithful fake applier — one that enforces each action's ``expected`` against
independent live labels exactly as the production ``ActionApplier`` does —
raises ``ReconciliationRequired`` on drift.  The lifecycle must never clear or
complete against state that no longer holds.
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.control.actions import (
    AddCommentAction,
    AddLabelAction,
    RemoveLabelAction,
)
from issue_orchestrator.control.claim_quarantine import QuarantineSubject
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.reconciliation import (
    ExternalSnapshot,
    ReconciliationRequired,
)
from issue_orchestrator.control.tech_lead_needs_human_reconcile import (
    TechLeadNeedsHumanLifecycle,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    Session,
    SessionKey,
    TaskKind,
)
from issue_orchestrator.events import EventName
from tests.unit.session_run_helpers import make_session_run_assets


class GuardEnforcingApplier:
    """Fake ``apply_actions`` that mimics ``ActionApplier``'s expected gate.

    Holds live labels per issue independently of whatever the lifecycle read.
    Before each write it verifies the action's ``expected`` against those live
    labels using the real reconciliation types and raises
    ``ReconciliationRequired`` on drift — the production contract.  Successful
    writes mutate the live labels / record comments so ordering effects are
    observable.  ``on_apply`` fires just before the guard check for each action,
    letting a test land a concurrent mutation mid-sequence.
    """

    def __init__(self, live: dict[int, set[str]], *, on_apply=None) -> None:
        self.live = live
        self.applied: list = []
        self.comments: list[AddCommentAction] = []
        self._on_apply = on_apply

    def __call__(self, actions: list, context: str) -> bool:
        for action in actions:
            number = getattr(action, "issue_number", None)
            if number is None:
                number = action.number  # AddCommentAction
            if self._on_apply is not None:
                self._on_apply(action, self.live)
            labels = self.live.setdefault(number, set())
            if action.expected is not None:
                snapshot = ExternalSnapshot.for_issue(number, labels)
                satisfied, reason = action.expected.is_satisfied_by(snapshot)
                if not satisfied:
                    raise ReconciliationRequired(
                        entity_type="issue",
                        entity_id=number,
                        expected=ExternalSnapshot.for_issue(
                            number, set(action.expected.required_labels)
                        ),
                        actual=snapshot,
                        reason=reason,
                    )
            if isinstance(action, AddLabelAction):
                labels.add(action.label)
            elif isinstance(action, RemoveLabelAction):
                labels.discard(action.label)
            elif isinstance(action, AddCommentAction):
                self.comments.append(action)
            self.applied.append(action)
        return True


class _RecordingApplier:
    """An ``ActionApplier``-shaped fake with the production label semantics.

    Adding a label that is already present is a SUCCESS carrying ``no_op=True``
    (#6999 F12) — the detail the quarantine owner's provenance rule reads, and
    the reason a boolean applier could not be kept.
    """

    def __init__(self, live: dict[int, set[str]]) -> None:
        self.live = live
        self.applied: list = []

    def apply(self, action):
        from issue_orchestrator.control.action_results import ActionResult

        self.applied.append(action)
        if isinstance(action, AddLabelAction):
            labels = self.live.setdefault(action.issue_number, set())
            already = action.label in labels
            labels.add(action.label)
            return ActionResult.ok(action, no_op=already)
        if isinstance(action, RemoveLabelAction):
            self.live.setdefault(action.issue_number, set()).discard(action.label)
        return ActionResult.ok(action)


def _session(issue_number: int, tmp_path: Path) -> Session:
    """A real active-session shape; reconcile only reads ``issue.number``."""
    return Session(
        key=SessionKey(issue=FakeIssueKey(str(issue_number)), task=TaskKind.CODE),
        issue=Issue(
            number=issue_number,
            title=f"Issue {issue_number}",
            labels=[],
            repo="test/repo",
        ),
        agent_config=AgentConfig(prompt_path=tmp_path / "p.md", timeout_minutes=45),
        terminal_id=f"issue-{issue_number}",
        worktree_path=tmp_path,
        branch_name=f"{issue_number}-branch",
        run_assets=make_session_run_assets(
            tmp_path, session_name=f"issue-{issue_number}"
        ),
    )


def _lifecycle(config, events, live, *, stale_read, on_apply=None):
    """Build a lifecycle whose fresh read is intentionally stale."""
    applier = GuardEnforcingApplier(live, on_apply=on_apply)
    lifecycle = TechLeadNeedsHumanLifecycle(
        labels=LabelManager(config),
        events=events,
        read_labels=lambda issue_number: list(stale_read.get(issue_number, [])),
        discover_marked_issue_numbers=lambda: (),
        apply_actions=applier,
    )
    return lifecycle, applier


# ---------------------------------------------------------------------------
# escalate()
# ---------------------------------------------------------------------------


class TestEscalateGuards:
    def _escalate(self, lifecycle) -> bool:
        return lifecycle.escalate(
            issue_number=903,
            reason="failure_investigation exhausted",
            comment="failure_investigation could not launch",
            context="tech_lead_exhausted",
            event_data={"issue_number": 903, "reason": "failure_investigation"},
        )

    def test_completes_under_real_expected_enforcement(
        self, sample_config, mock_event_sink
    ):
        """Happy path: the read-after-write chain satisfies every guard."""
        live: dict[int, set[str]] = {903: set()}
        lifecycle, applier = _lifecycle(
            sample_config, mock_event_sink, live, stale_read={}
        )
        labels = LabelManager(sample_config)

        assert self._escalate(lifecycle) is True
        assert live[903] == {labels.tech_lead_needs_human, labels.needs_human}
        assert [c.number for c in applier.comments] == [903]
        assert [str(e.name) for e in mock_event_sink.events] == [
            str(EventName.ISSUE_NEEDS_HUMAN)
        ]

    def test_needs_human_add_requires_marker_still_present(
        self, sample_config, mock_event_sink
    ):
        """If the marker is stripped before needs-human lands, stop.

        needs-human must never exist without its provenance marker, and no
        comment or event may claim a transition that did not happen.
        """
        labels = LabelManager(sample_config)
        live: dict[int, set[str]] = {903: set()}

        def strip_marker_before_needs_human(action, live_labels):
            if (
                isinstance(action, AddLabelAction)
                and action.label == labels.needs_human
            ):
                live_labels[903].discard(labels.tech_lead_needs_human)

        lifecycle, applier = _lifecycle(
            sample_config,
            mock_event_sink,
            live,
            stale_read={},
            on_apply=strip_marker_before_needs_human,
        )

        assert self._escalate(lifecycle) is False
        assert labels.needs_human not in live[903]
        assert applier.comments == []
        assert not any(
            str(e.name) == str(EventName.ISSUE_NEEDS_HUMAN)
            for e in mock_event_sink.events
        )

    def test_comment_and_event_withheld_when_state_cleared_first(
        self, sample_config, mock_event_sink
    ):
        """A concurrent clear before the comment stops the durable record."""
        live: dict[int, set[str]] = {903: set()}

        def clear_before_comment(action, live_labels):
            if isinstance(action, AddCommentAction):
                live_labels[903].clear()

        lifecycle, applier = _lifecycle(
            sample_config,
            mock_event_sink,
            live,
            stale_read={},
            on_apply=clear_before_comment,
        )

        assert self._escalate(lifecycle) is False
        assert applier.comments == []
        assert not any(
            str(e.name) == str(EventName.ISSUE_NEEDS_HUMAN)
            for e in mock_event_sink.events
        )

    def test_paused_issue_blocks_the_escalation(
        self, sample_config, mock_event_sink
    ):
        """The reconcile pause label fails the escalation closed."""
        labels = LabelManager(sample_config)
        live: dict[int, set[str]] = {903: {labels.needs_reconcile}}
        lifecycle, applier = _lifecycle(
            sample_config, mock_event_sink, live, stale_read={}
        )

        assert self._escalate(lifecycle) is False
        assert labels.tech_lead_needs_human not in live[903]
        assert applier.comments == []
        assert mock_event_sink.events == []

    def test_preexisting_needs_human_is_not_claimed(
        self, sample_config, mock_event_sink, tmp_path
    ):
        """A bare human/session-owned label remains outside this lifecycle."""
        labels = LabelManager(sample_config)
        live = {903: {labels.needs_human}}
        lifecycle, applier = _lifecycle(
            sample_config,
            mock_event_sink,
            live,
            stale_read={903: {labels.needs_human}},
        )

        assert self._escalate(lifecycle) is True
        assert live[903] == {labels.needs_human}
        assert not any(
            isinstance(action, AddLabelAction)
            and action.label == labels.tech_lead_needs_human
            for action in applier.applied
        )

        lifecycle.reconcile([_session(903, tmp_path)])

        assert live[903] == {labels.needs_human}
        assert not any(
            isinstance(action, RemoveLabelAction) for action in applier.applied
        )

    def test_concurrent_needs_human_blocks_marker_claim(
        self, sample_config, mock_event_sink
    ):
        """The write guard closes the race after an initially empty read."""
        labels = LabelManager(sample_config)
        live = {903: {labels.needs_human}}
        lifecycle, applier = _lifecycle(
            sample_config,
            mock_event_sink,
            live,
            stale_read={903: set()},
        )

        assert self._escalate(lifecycle) is False
        assert live[903] == {labels.needs_human}
        assert applier.applied == []
        assert applier.comments == []
        assert mock_event_sink.events == []

    def test_guards_carry_marker_provenance_contract(
        self, sample_config, mock_event_sink
    ):
        """Document the exact required/forbidden set on each escalate step."""
        labels = LabelManager(sample_config)
        live: dict[int, set[str]] = {903: set()}
        lifecycle, applier = _lifecycle(
            sample_config, mock_event_sink, live, stale_read={}
        )

        assert self._escalate(lifecycle) is True
        by_kind = {
            (type(a).__name__, getattr(a, "label", "comment")): a.expected
            for a in applier.applied
        }
        marker_add = by_kind[("AddLabelAction", labels.tech_lead_needs_human)]
        needs_human_add = by_kind[("AddLabelAction", labels.needs_human)]
        comment = by_kind[("AddCommentAction", "comment")]

        # Pause label is always forbidden (fail-closed), prefix-resolved.
        for guard in (marker_add, needs_human_add, comment):
            assert labels.needs_reconcile in guard.forbidden_labels

        assert marker_add.required_labels == frozenset()
        assert labels.tech_lead_needs_human in marker_add.forbidden_labels
        assert labels.needs_human in marker_add.forbidden_labels
        assert needs_human_add.required_labels == frozenset(
            {labels.tech_lead_needs_human}
        )
        assert comment.required_labels == frozenset(
            {labels.tech_lead_needs_human, labels.needs_human}
        )


# ---------------------------------------------------------------------------
# reconcile()
# ---------------------------------------------------------------------------


class TestReconcileGuards:
    def test_clears_both_labels_when_state_holds(
        self, sample_config, mock_event_sink, tmp_path
    ):
        """Happy path: marker-owned escalation superseded by active work."""
        labels = LabelManager(sample_config)
        present = {labels.tech_lead_needs_human, labels.needs_human}
        live: dict[int, set[str]] = {903: set(present)}
        lifecycle, _ = _lifecycle(
            sample_config,
            mock_event_sink,
            live,
            stale_read={903: set(present)},
        )

        lifecycle.reconcile([_session(903, tmp_path)])

        assert live[903] == set()

    def test_preserves_needs_human_when_it_vanished_before_apply(
        self, sample_config, mock_event_sink, tmp_path
    ):
        """Stale read said needs-human present; live drifted to marker-only.

        The remove must be refused (its guard requires needs-human), and the
        marker must survive so the next tick re-evaluates against fresh state
        rather than stripping provenance on a stale assumption.
        """
        labels = LabelManager(sample_config)
        live: dict[int, set[str]] = {903: {labels.tech_lead_needs_human}}
        lifecycle, applier = _lifecycle(
            sample_config,
            mock_event_sink,
            live,
            stale_read={903: {labels.tech_lead_needs_human, labels.needs_human}},
        )

        lifecycle.reconcile([_session(903, tmp_path)])

        # Marker preserved; needs-human removal attempted but reconciled away.
        assert live[903] == {labels.tech_lead_needs_human}
        assert not any(isinstance(a, RemoveLabelAction) for a in applier.applied)

    def test_skips_when_issue_paused_before_apply(
        self, sample_config, mock_event_sink, tmp_path
    ):
        """A pause landing after the read stops all reconcile mutations."""
        labels = LabelManager(sample_config)
        live: dict[int, set[str]] = {
            903: {
                labels.tech_lead_needs_human,
                labels.needs_human,
                labels.needs_reconcile,
            }
        }
        lifecycle, applier = _lifecycle(
            sample_config,
            mock_event_sink,
            live,
            stale_read={903: {labels.tech_lead_needs_human, labels.needs_human}},
        )

        lifecycle.reconcile([_session(903, tmp_path)])

        # Nothing removed while paused.
        assert live[903] == {
            labels.tech_lead_needs_human,
            labels.needs_human,
            labels.needs_reconcile,
        }
        assert not any(isinstance(a, RemoveLabelAction) for a in applier.applied)

    def test_preserves_marker_when_needs_human_reappears_before_marker_removal(
        self, sample_config, mock_event_sink, tmp_path
    ):
        """Marker-only cleanup must not strip the marker if needs-human returns.

        The read shows marker-only (needs-human already cleared), so the
        lifecycle skips straight to marker removal.  But a human re-escalated
        needs-human before the write: the marker-removal guard forbids
        needs-human, so the marker is kept and provenance is not lost.
        """
        labels = LabelManager(sample_config)
        live: dict[int, set[str]] = {
            903: {labels.tech_lead_needs_human, labels.needs_human}
        }
        lifecycle, applier = _lifecycle(
            sample_config,
            mock_event_sink,
            live,
            stale_read={903: {labels.tech_lead_needs_human}},
        )

        lifecycle.reconcile([_session(903, tmp_path)])

        assert labels.tech_lead_needs_human in live[903]
        assert not any(isinstance(a, RemoveLabelAction) for a in applier.applied)

    def test_read_failure_is_isolated_per_issue(
        self, sample_config, mock_event_sink, tmp_path
    ):
        """A fresh-read error on one issue never blocks reconciling others."""
        labels = LabelManager(sample_config)
        live: dict[int, set[str]] = {
            904: {labels.tech_lead_needs_human, labels.needs_human}
        }

        def read_labels(issue_number: int) -> list[str]:
            if issue_number == 903:
                raise RuntimeError("github read failed")
            return list(live.get(issue_number, set()))

        applier = GuardEnforcingApplier(live)
        lifecycle = TechLeadNeedsHumanLifecycle(
            labels=labels,
            events=mock_event_sink,
            read_labels=read_labels,
            discover_marked_issue_numbers=lambda: (),
            apply_actions=applier,
        )

        lifecycle.reconcile([_session(903, tmp_path), _session(904, tmp_path)])

        assert live[904] == set()

    def test_fresh_process_recovers_marker_without_queue_or_active_session(
        self, sample_config, mock_event_sink
    ):
        """A marker-only crash remains discoverable and becomes blocking."""
        labels = LabelManager(sample_config)
        live = {903: {labels.tech_lead_needs_human}}
        applier = GuardEnforcingApplier(live)
        lifecycle = TechLeadNeedsHumanLifecycle(
            labels=labels,
            events=mock_event_sink,
            read_labels=lambda issue_number: list(live[issue_number]),
            discover_marked_issue_numbers=lambda: (903,),
            apply_actions=applier,
        )

        lifecycle.reconcile([])

        assert live[903] == {labels.tech_lead_needs_human, labels.needs_human}
        assert any(
            isinstance(action, AddLabelAction) and action.label == labels.needs_human
            for action in applier.applied
        )
        assert len(applier.comments) == 1
        assert "recovered an interrupted tech_lead" in applier.comments[0].comment
        assert [str(event.name) for event in mock_event_sink.events] == [
            str(EventName.ISSUE_NEEDS_HUMAN)
        ]


class TestPrefixResolvedPauseLabel:
    def test_forbidden_pause_label_honors_configured_prefix(
        self, sample_config, mock_event_sink
    ):
        """The pause label in the guard is resolved through LabelManager."""
        sample_config.label_prefix = "bot"
        labels = LabelManager(sample_config)
        assert labels.needs_reconcile == "bot:needs-reconcile"

        live: dict[int, set[str]] = {903: set()}
        lifecycle, applier = _lifecycle(
            sample_config, mock_event_sink, live, stale_read={}
        )
        lifecycle.escalate(
            issue_number=903,
            reason="exhausted",
            comment="c",
            context="tech_lead_exhausted",
            event_data={"issue_number": 903},
        )

        assert applier.applied, "escalation should have produced guarded actions"
        assert all(
            "bot:needs-reconcile" in a.expected.forbidden_labels
            for a in applier.applied
        )


class TestQuarantineProvenanceIsRespected:
    """A quarantined claim holds the shared needs-human label open (#6999 F12).

    ``ClaimQuarantineOwner`` keeps a terminal OUT of ``active_sessions`` on
    purpose - its work cannot be named, so letting it complete would discard
    that work. This lifecycle clears its own escalation whenever a session for
    the issue looks active, so without knowing about the quarantine a healthy
    sibling session on the SAME issue silently retracts the operator's warning
    about a live agent nobody can account for.
    """

    def test_a_healthy_sibling_session_does_not_lift_a_quarantine_block(
        self, sample_config, mock_event_sink, tmp_path
    ):
        labels = LabelManager(sample_config)
        live = {903: {labels.tech_lead_needs_human, labels.needs_human}}
        applier = GuardEnforcingApplier(live)
        lifecycle = TechLeadNeedsHumanLifecycle(
            labels=labels,
            events=mock_event_sink,
            read_labels=lambda issue_number: list(live[issue_number]),
            discover_marked_issue_numbers=lambda: (903,),
            apply_actions=applier,
            quarantined_issue_numbers=lambda: frozenset({903}),
        )

        # A perfectly healthy session for the same issue is running.
        lifecycle.reconcile([_session(903, tmp_path)])

        assert labels.needs_human in live[903]
        assert labels.tech_lead_needs_human in live[903]

    def test_without_a_quarantine_the_sibling_still_clears_as_before(
        self, sample_config, mock_event_sink, tmp_path
    ):
        """The guard is scoped to quarantined issues and changes nothing else."""
        labels = LabelManager(sample_config)
        live = {903: {labels.tech_lead_needs_human, labels.needs_human}}
        applier = GuardEnforcingApplier(live)
        lifecycle = TechLeadNeedsHumanLifecycle(
            labels=labels,
            events=mock_event_sink,
            read_labels=lambda issue_number: list(live[issue_number]),
            discover_marked_issue_numbers=lambda: (903,),
            apply_actions=applier,
            quarantined_issue_numbers=frozenset,
        )

        lifecycle.reconcile([_session(903, tmp_path)])

        assert live[903] == set()

    def test_a_removed_block_is_reasserted_on_the_next_quarantine_scan(
        self, sample_config, tmp_path
    ):
        """Even if something else removes it, the next sweep puts it back.

        Two owners disagreeing once is survivable; the label staying gone while
        an unaccountable terminal runs is not.
        """
        from unittest.mock import MagicMock

        from issue_orchestrator.control.claim_quarantine import (
            build_claim_quarantine_owner,
        )
        from issue_orchestrator.control.in_flight_work import QuarantinedSession
        from issue_orchestrator.execution.pending_work_claim_store import (
            SqlitePendingWorkClaimStore,
        )

        labels = LabelManager(sample_config)
        applier = _RecordingApplier(live={903: set()})
        owner = build_claim_quarantine_owner(
            store=SqlitePendingWorkClaimStore.for_repo(tmp_path),
            action_applier=applier,
            label_manager=labels,
            events=MagicMock(),
        )
        quarantined = QuarantinedSession(
            _session(903, tmp_path), "payload unreadable", "/runs/903", "/runs/903@t1"
        )
        owner.quarantine(QuarantineSubject.live_run_with_unreadable_claim(quarantined))
        applier.applied.clear()
        # Something else takes the shared label off while the terminal runs on.
        applier.live[903].discard(labels.needs_human)

        owner.quarantine(QuarantineSubject.live_run_with_unreadable_claim(quarantined))  # a later scan

        assert [
            action.label
            for action in applier.applied
            if isinstance(action, AddLabelAction)
        ] == [labels.needs_human]
        assert labels.needs_human in applier.live[903]
        # ...and the operator is not re-told about a quarantine already reported.
        assert not [a for a in applier.applied if isinstance(a, AddCommentAction)]
