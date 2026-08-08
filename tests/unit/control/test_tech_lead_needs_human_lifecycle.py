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

import pytest

from issue_orchestrator.control.actions import (
    AddCommentAction,
    AddLabelAction,
    RemoveLabelAction,
)
from issue_orchestrator.control.claim_quarantine import QuarantineSubject
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.needs_human_block import (
    BlockOutcome,
    NeedsHumanBlock,
    NeedsHumanCause,
)


class _LiveLabelWriter:
    """The narrow label port the shared-block owner writes through."""

    def __init__(self, live: dict[int, set[str]]) -> None:
        self._live = live

    def add_label(self, issue_number: int, label: str) -> None:
        self._live.setdefault(issue_number, set()).add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self._live.setdefault(issue_number, set()).discard(label)
from issue_orchestrator.execution.pending_work_claim_store import (
    SqlitePendingWorkClaimStore,
)
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
            needs_human_block=NeedsHumanBlock(
                needs_human_label=labels.needs_human,
                tech_lead_marker=labels.tech_lead_needs_human,
                labels=_LiveLabelWriter(live),
                read_labels=lambda issue_number: list(live[issue_number]),
                quarantined_issue_numbers=lambda: frozenset({903}),
                causes=SqlitePendingWorkClaimStore.for_repo(tmp_path),
            ),
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
            needs_human_block=NeedsHumanBlock(
                needs_human_label=labels.needs_human,
                tech_lead_marker=labels.tech_lead_needs_human,
                labels=_LiveLabelWriter(live),
                read_labels=lambda issue_number: list(live[issue_number]),
                quarantined_issue_numbers=frozenset,
                causes=SqlitePendingWorkClaimStore.for_repo(tmp_path),
            ),
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


class TestTheSharedBlockIsNotOneOwnersToRetract:
    """Two independent causes, one label, and no way to lose either (#6999 F4).

    ``needs-human`` is a single label with several independent causes. Each
    owner used to reason about it from its OWN provenance alone, which is not
    enough to decide a removal - and the gap was not theoretical. A quarantine
    acquired the label; a tech-lead escalation then became required, saw a bare
    label it had not applied, and deliberately declined to claim it by recording
    no marker; the quarantine later resolved and took "its" label off. The
    escalation was left with neither the block nor the marker its own recovery
    reads, so an issue a human had been told to look at went quietly back on the
    board.
    """

    def _owners(self, sample_config, tmp_path, live):
        from unittest.mock import MagicMock

        from issue_orchestrator.control.claim_quarantine import (
            build_claim_quarantine_owner,
        )
        from issue_orchestrator.execution.pending_work_claim_store import (
            SqlitePendingWorkClaimStore,
        )

        labels = LabelManager(sample_config)
        applier = _RecordingApplier(live=live)
        claims = SqlitePendingWorkClaimStore.for_repo(tmp_path)
        block = NeedsHumanBlock(
            needs_human_label=labels.needs_human,
            tech_lead_marker=labels.tech_lead_needs_human,
            labels=_LiveLabelWriter(live),
            read_labels=lambda issue_number: list(live.get(issue_number, set())),
            quarantined_issue_numbers=claims.quarantined_issue_numbers,
            causes=claims,
        )
        quarantine = build_claim_quarantine_owner(
            store=claims,
            action_applier=applier,
            label_manager=labels,
            events=MagicMock(),
            needs_human_block=block,
        )
        lifecycle = TechLeadNeedsHumanLifecycle(
            labels=labels,
            events=MagicMock(),
            read_labels=lambda issue_number: list(live.get(issue_number, set())),
            discover_marked_issue_numbers=lambda: (),
            apply_actions=lambda actions, context: all(
                applier.apply(action).success for action in actions
            ),
            needs_human_block=block,
        )
        return labels, applier, quarantine, lifecycle

    def _quarantine_run(self, quarantine, tmp_path, issue_number=903):
        from issue_orchestrator.control.in_flight_work import QuarantinedSession

        quarantined = QuarantinedSession(
            _session(issue_number, tmp_path),
            "payload unreadable",
            f"/runs/{issue_number}",
            f"/runs/{issue_number}@t1",
        )
        quarantine.quarantine(
            QuarantineSubject.live_run_with_unreadable_claim(quarantined)
        )
        return quarantined

    def test_a_resolving_quarantine_leaves_a_live_tech_lead_block_standing(
        self, sample_config, tmp_path
    ):
        """The whole cross-owner sequence, end to end."""
        live: dict[int, set[str]] = {903: set()}
        labels, _applier, quarantine, lifecycle = self._owners(
            sample_config, tmp_path, live
        )

        quarantined = self._quarantine_run(quarantine, tmp_path)
        assert labels.needs_human in live[903]

        # A tech-lead investigation exhausts its launch budget while the
        # quarantine's block is standing. The label is present but it is not a
        # human's, so this escalation still records its own provenance.
        assert lifecycle.escalate(
            issue_number=903,
            reason="tech_lead launch retries exhausted",
            comment="dropped after 3 launch failures",
            context="tech_lead_launch_retry_exhausted",
            event_data={"issue_number": 903},
        ) is True
        assert labels.tech_lead_needs_human in live[903]

        # The quarantine's own cause is repaired and it releases.
        quarantine.reconcile_released(frozenset())

        # The issue stays durably blocked for the cause that is still live, with
        # no repair tick required, and the quarantine keeps nothing of its own.
        assert labels.needs_human in live[903]
        assert labels.tech_lead_needs_human in live[903]
        assert quarantine.store.list_quarantines() == ()
        assert quarantine.store.quarantined_issue_numbers() == frozenset()
        del quarantined

    def test_a_resolving_quarantine_still_clears_a_block_nobody_else_wants(
        self, sample_config, tmp_path
    ):
        """The guard is scoped: with no second cause, release behaves as before."""
        live: dict[int, set[str]] = {903: set()}
        labels, _applier, quarantine, _lifecycle_ = self._owners(
            sample_config, tmp_path, live
        )

        self._quarantine_run(quarantine, tmp_path)
        assert labels.needs_human in live[903]

        quarantine.reconcile_released(frozenset())

        assert labels.needs_human not in live[903]
        assert quarantine.store.list_quarantines() == ()

    def test_a_human_applied_block_is_still_never_claimed(
        self, sample_config, tmp_path
    ):
        """Only ANOTHER ORCHESTRATOR cause defeats the human-intent rule.

        A bare needs-human that nothing else accounts for is a human's, and this
        lifecycle must keep refusing to stamp its marker on it - otherwise its
        own supersede path would later remove a label it never applied.
        """
        labels = LabelManager(sample_config)
        live: dict[int, set[str]] = {903: {labels.needs_human}}
        _labels, applier, _quarantine, lifecycle = self._owners(
            sample_config, tmp_path, live
        )

        assert lifecycle.escalate(
            issue_number=903,
            reason="tech_lead launch retries exhausted",
            comment="dropped after 3 launch failures",
            context="tech_lead_launch_retry_exhausted",
            event_data={"issue_number": 903},
        ) is True

        assert labels.tech_lead_needs_human not in live[903]
        assert [
            action.label
            for action in applier.applied
            if isinstance(action, AddLabelAction)
        ] == []


class TestEveryOrchestratorCauseOwnsTheSharedBlock:
    """The other causes, through the seam every label mutation passes (#6999 F2 r2).

    Naming only the tech-lead escalation and the quarantine left the same loss
    open for every OTHER orchestrator cause. Several controllers add the shared
    label directly - a session that ended without a completion record, publish
    failures past their bound, an invalid completion record, a stuck sweep - and
    none of them recorded that they needed it. So: quarantine acquires the
    label, a planner escalation later needs the very same label and finds it
    already present, the quarantine resolves, and the remover sees no cause it
    recognises and takes the block off underneath a lifecycle that still
    requires it.

    These exercise the REAL ``ActionApplier``, because that is the single seam
    every acquisition and release passes through and therefore the only place
    the rule can be made to hold for call sites nobody has converted - the ten
    that exist today and the eleventh someone adds tomorrow.
    """

    def _wiring(self, sample_config, tmp_path, live):
        from unittest.mock import MagicMock

        from issue_orchestrator.control.action_applier import ActionApplier
        from issue_orchestrator.control.claim_quarantine import (
            build_claim_quarantine_owner,
        )

        labels = LabelManager(sample_config)

        class _LabelSet:
            """Live labels, with the production add/remove/has semantics."""

            def add_label(self, number: int, label: str) -> None:
                live.setdefault(number, set()).add(label)

            def remove_label(self, number: int, label: str) -> None:
                live.setdefault(number, set()).discard(label)

            def has_label(self, issue_number: int, label: str) -> bool:
                return label in live.get(issue_number, set())

        claims = SqlitePendingWorkClaimStore.for_repo(tmp_path)
        label_set = _LabelSet()
        block = NeedsHumanBlock(
            needs_human_label=labels.needs_human,
            tech_lead_marker=labels.tech_lead_needs_human,
            labels=label_set,
            read_labels=lambda number: list(live.get(number, set())),
            quarantined_issue_numbers=claims.quarantined_issue_numbers,
            causes=claims,
        )
        applier = ActionApplier(
            labels=label_set,
            sessions=MagicMock(),
            events=MagicMock(),
            label_manager=labels,
            needs_human_block=block,
        )
        quarantine = build_claim_quarantine_owner(
            store=claims,
            action_applier=applier,
            label_manager=labels,
            events=MagicMock(),
            needs_human_block=block,
        )
        return labels, applier, quarantine, block

    def _quarantine_run(self, quarantine, tmp_path, issue_number=903):
        from issue_orchestrator.control.in_flight_work import QuarantinedSession

        quarantine.quarantine(
            QuarantineSubject.live_run_with_unreadable_claim(
                QuarantinedSession(
                    _session(issue_number, tmp_path),
                    "payload unreadable",
                    f"/runs/{issue_number}",
                    f"/runs/{issue_number}@t1",
                )
            )
        )

    def _session_escalation(self, labels, issue_number=903):
        """Exactly what the completion planner emits - a plain label add."""
        return AddLabelAction(
            issue_number=issue_number,
            label=labels.needs_human,
            reason="Session terminated without calling completion command (mandatory)",
            needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
        )

    def test_a_resolving_quarantine_leaves_a_planner_escalation_blocked(
        self, sample_config, tmp_path
    ):
        """The reviewer's scenario, end to end, with no tech-lead marker in sight."""
        live: dict[int, set[str]] = {903: set()}
        labels, applier, quarantine, _block = self._wiring(
            sample_config, tmp_path, live
        )

        self._quarantine_run(quarantine, tmp_path)
        assert labels.needs_human in live[903]

        # A session for the same issue dies without a completion record while
        # the quarantine's block is standing. The planner's add is a no-op on
        # the label - and that no-op is exactly the moment the cause has to be
        # recorded, because it is the only trace this lifecycle ever leaves.
        result = applier.apply(self._session_escalation(labels))
        assert result.success
        assert result.details.get("no_op") is True

        quarantine.reconcile_released(frozenset())

        assert labels.needs_human in live[903], (
            "the planner escalation still requires the block"
        )
        assert labels.tech_lead_needs_human not in live[903]
        assert quarantine.store.list_quarantines() == ()

    def test_the_last_cause_out_still_clears_the_block(
        self, sample_config, tmp_path
    ):
        """Provenance must not become a one-way ratchet.

        A cause ledger that can only accumulate strands issues in needs-human
        forever, which is a worse failure than the one it fixes. The planner
        cause withdraws through the same seam, and once it is the last one the
        label comes off.
        """
        live: dict[int, set[str]] = {903: set()}
        labels, applier, quarantine, _block = self._wiring(
            sample_config, tmp_path, live
        )

        self._quarantine_run(quarantine, tmp_path)
        applier.apply(self._session_escalation(labels))
        quarantine.reconcile_released(frozenset())
        assert labels.needs_human in live[903]

        # The planner's own clear - the last cause standing.
        cleared = applier.apply(
            RemoveLabelAction(
                issue_number=903,
                label=labels.needs_human,
                reason="post-publish state now reworkable; clearing needs-human",
                needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
            )
        )

        assert cleared.success
        assert not cleared.details.get("blocked_by_other_cause")
        assert labels.needs_human not in live[903]

    def test_a_withdrawal_that_is_not_the_last_cause_keeps_the_label(
        self, sample_config, tmp_path
    ):
        """The backstop fires even for a remover that never asked."""
        live: dict[int, set[str]] = {903: set()}
        labels, applier, quarantine, _block = self._wiring(
            sample_config, tmp_path, live
        )

        self._quarantine_run(quarantine, tmp_path)
        applier.apply(self._session_escalation(labels))

        # A planner clear while the quarantine is still live. The withdrawal is
        # real - this cause is discharged - but the label is not this remover's
        # to take.
        cleared = applier.apply(
            RemoveLabelAction(
                issue_number=903,
                label=labels.needs_human,
                reason="post-publish state now reworkable; clearing needs-human",
                needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
            )
        )

        assert cleared.success, "a discharged obligation is not a failure"
        assert cleared.details.get("blocked_by_other_cause") is True
        assert labels.needs_human in live[903]

    def test_a_human_clearing_the_label_ends_every_recorded_cause(
        self, sample_config, tmp_path
    ):
        """The label stays authoritative over the rows, never the reverse.

        A human who removes ``needs-human`` ends every cause at once and tells
        nobody. A row found standing over an absent label is stale by
        definition; leaving it would hold the block open for a cause that no
        longer exists and strand the issue the next time anything asked.
        """
        from issue_orchestrator.control.needs_human_block import NeedsHumanCause

        live: dict[int, set[str]] = {903: set()}
        labels, applier, _quarantine, block = self._wiring(
            sample_config, tmp_path, live
        )

        applier.apply(self._session_escalation(labels))
        assert block.held_by_another_cause(
            903, excluding=NeedsHumanCause.CLAIM_QUARANTINE
        )

        live[903].discard(labels.needs_human)  # a human, out of band

        assert not block.held_by_another_cause(
            903, excluding=NeedsHumanCause.CLAIM_QUARANTINE
        )


class TestTheBlockOwnerIsNotBypassableInProduction:
    """The four paths that reached around the owner (#6999 F2 round 3).

    Recording provenance beside a mutation performed elsewhere is not a
    boundary: it holds only for callers that choose to report, and four real
    production paths did not. Agent-authored ``needs_human`` completions went
    straight to the label adapter; merge escalation labelled a PR directly;
    recovered-terminal shedding and operator retry/dismiss removed the label
    without ending the causes standing on it. The label now goes on and comes
    off inside the owner, so those paths cannot mutate it any other way.

    These drive the REAL production objects - ``CompletionProcessor``,
    ``ActionApplier`` handlers, the operator route helper - rather than the
    generic label actions, because the generic actions were never the gap.
    """

    def _wiring(self, sample_config, tmp_path, live):
        from unittest.mock import MagicMock

        from issue_orchestrator.control.action_applier import ActionApplier
        from issue_orchestrator.control.claim_quarantine import (
            build_claim_quarantine_owner,
        )

        labels = LabelManager(sample_config)

        class _LabelSet(_LiveLabelWriter):
            def has_label(self, number: int, label: str) -> bool:
                return label in live.get(number, set())

        label_set = _LabelSet(live)
        claims = SqlitePendingWorkClaimStore.for_repo(tmp_path)
        block = NeedsHumanBlock(
            needs_human_label=labels.needs_human,
            tech_lead_marker=labels.tech_lead_needs_human,
            labels=label_set,
            read_labels=lambda number: list(live.get(number, set())),
            quarantined_issue_numbers=claims.quarantined_issue_numbers,
            causes=claims,
        )


        class _FreshReader:
            """Live labels, so the recovery shed can see what to shed."""

            def read_issue_labels(self, number: int) -> list[str]:
                return sorted(live.get(number, set()))

        applier = ActionApplier(
            labels=label_set,
            sessions=MagicMock(),
            events=MagicMock(),
            label_manager=labels,
            fresh_issue_reader=_FreshReader(),
            reconcile=True,
            needs_human_block=block,
        )
        quarantine = build_claim_quarantine_owner(
            store=claims,
            action_applier=applier,
            label_manager=labels,
            events=MagicMock(),
            needs_human_block=block,
        )
        return labels, applier, quarantine, block, claims

    def _quarantine_run(self, quarantine, tmp_path, issue_number=903):
        from issue_orchestrator.control.in_flight_work import QuarantinedSession

        quarantine.quarantine(
            QuarantineSubject.live_run_with_unreadable_claim(
                QuarantinedSession(
                    _session(issue_number, tmp_path),
                    "payload unreadable",
                    f"/runs/{issue_number}",
                    f"/runs/{issue_number}@t1",
                )
            )
        )

    def _process_completion(self, tmp_path, block, labels, record_actions, *,
                            issue_number=903, pr_labels=()):
        """Drive the REAL public completion path with a real record on disk."""
        import json
        from unittest.mock import MagicMock

        from issue_orchestrator.control.completion_processor import (
            CompletionProcessor,
        )
        from issue_orchestrator.control.governed_label_set import GovernedLabelSet
        from issue_orchestrator.execution import FileSystemSessionOutput

        worktree = tmp_path / f"wt-{issue_number}"
        worktree.mkdir(parents=True, exist_ok=True)
        session_output = FileSystemSessionOutput()
        run_assets = session_output.start_run(
            worktree.resolve(),
            f"issue-{issue_number}",
            issue_number=issue_number,
            agent_label="agent:test",
            backend="subprocess",
        )
        completion = {
            "session_id": "session-1",
            "timestamp": "2026-08-08T00:00:00Z",
            "outcome": "needs_human",
            "summary": "needs a human",
            "requested_actions": list(record_actions),
            "question": "what now?",
        }
        if pr_labels:
            completion["pr_labels"] = list(pr_labels)
        (run_assets.run_dir / "completion.json").write_text(json.dumps(completion))

        adapter = MagicMock()
        processor = CompletionProcessor(
            # Wired exactly as the composition root wires it: the governed
            # label is refused at the capability, and the typed outcome routes
            # through the owner.
            label_adapter=GovernedLabelSet(
                labels=adapter, governed_label=labels.needs_human
            ),
            pr_adapter=MagicMock(),
            git_adapter=MagicMock(),
            session_output=session_output,
            agent_callback_endpoint=MagicMock(),
            label_config={"needs_human": labels.needs_human},
            needs_human_block=block,
        )
        result = processor.process(
            worktree.resolve(),
            issue_number,
            "Test issue",
            run_assets=run_assets,
            completion_path=(
                f".issue-orchestrator/sessions/{run_assets.run_dir.name}/"
                "completion.json"
            ),
            agent_label="agent:test",
        )
        return result, adapter

    def test_an_agent_requested_needs_human_survives_a_quarantine_release(
        self, sample_config, tmp_path
    ):
        """The MAIN completion path, end to end through ``process()``.

        ``coding-done needs_human`` writes a real completion record requesting
        ADD_NEEDS_HUMAN_LABEL, and the processor applied it straight through
        the label adapter. With a quarantine already holding the same label the
        agent's request left no trace, so the quarantine resolved and took away
        the block the session had just asked for.
        """
        live: dict[int, set[str]] = {903: set()}
        labels, _applier, quarantine, block, _claims = self._wiring(
            sample_config, tmp_path, live
        )
        self._quarantine_run(quarantine, tmp_path)
        assert labels.needs_human in live[903]

        result, adapter = self._process_completion(
            tmp_path, block, labels, ["add_needs_human_label"]
        )

        assert result.success, result.errors
        # The raw adapter was never asked for this label: the owner is the only
        # writer, and the capability would have refused it anyway.
        assert [
            call for call in adapter.add_label.call_args_list
            if call.args[1] == labels.needs_human
        ] == []

        quarantine.reconcile_released(frozenset())

        assert labels.needs_human in live[903], (
            "the agent's own needs_human request must survive the quarantine"
        )

    def test_an_agent_cannot_mint_a_cause_free_block_through_pr_labels(
        self, sample_config, tmp_path
    ):
        """``pr_labels`` is agent-supplied, so it is untrusted input.

        Nothing in record validation stops an agent naming the configured
        shared label there, and applied it would create a block with no cause -
        which a later typed release then takes away from whoever did record
        one. The agent already has the typed needs_human outcome for this, and
        that one goes through the owner.
        """
        live: dict[int, set[str]] = {903: set(), 77: set()}
        labels, _applier, _quarantine, block, claims = self._wiring(
            sample_config, tmp_path, live
        )

        _result, adapter = self._process_completion(
            tmp_path,
            block,
            labels,
            [],
            pr_labels=[labels.needs_human, "size:small"],
        )

        # Refused by VALUE at the capability, whatever the record said, and the
        # ordinary label beside it is unaffected.
        refused = [
            call for call in adapter.add_label.call_args_list
            if call.args[1] == labels.needs_human
        ]
        assert refused == []
        assert claims.needs_human_causes(77) == frozenset()
        assert claims.needs_human_causes(903) == frozenset()

    def test_recovered_terminal_shedding_ends_the_causes_with_the_label(
        self, sample_config, tmp_path
    ):
        """A committed removal must not leave provenance behind.

        Recovery sheds ``needs-human`` deliberately, and it did so with a bare
        remove. The session cause standing on that label survived it, so a
        LATER quarantine acquiring the same label inherited a cause nothing was
        asserting - and could then never release its own block.
        """
        from issue_orchestrator.control.actions import RecoverTerminalIssueAction

        live: dict[int, set[str]] = {903: set()}
        labels, applier, quarantine, block, _claims = self._wiring(
            sample_config, tmp_path, live
        )

        applier.apply(
            AddLabelAction(
                issue_number=903,
                label=labels.needs_human,
                reason="Session terminated without calling completion command",
                needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
            )
        )
        assert block.held_by_another_cause(
            903, excluding=NeedsHumanCause.CLAIM_QUARANTINE
        )

        # Through the owner command that owns the shed, exactly as terminal
        # recovery reaches it in production.
        applier.apply(
            RecoverTerminalIssueAction(
                issue_number=903,
                pr_number=77,
                status="merged",
                reason="terminal recovery",
            )
        )

        assert labels.needs_human not in live[903]
        assert not block.held_by_another_cause(
            903, excluding=NeedsHumanCause.CLAIM_QUARANTINE
        ), "the shed cleared the label, so every cause standing on it is stale"

        # ...and a later quarantine inherits nothing: it acquires, resolves,
        # and takes its own label off again.
        self._quarantine_run(quarantine, tmp_path)
        assert labels.needs_human in live[903]
        quarantine.reconcile_released(frozenset())
        assert labels.needs_human not in live[903]

    def test_merge_escalation_records_provenance_against_its_pr(
        self, sample_config, tmp_path
    ):
        """The target is the PR, not the issue the escalation belongs to.

        ``EscalateToHumanAction`` labels ``pr_number``. Recording that cause
        against ``issue_number`` would leave the PR's block with no owner at
        all, and put a phantom cause on an issue nothing had blocked.
        """
        from issue_orchestrator.control.actions import EscalateToHumanAction

        live: dict[int, set[str]] = {903: set(), 77: set()}
        labels, applier, _quarantine, block, claims = self._wiring(
            sample_config, tmp_path, live
        )

        applier.apply(
            EscalateToHumanAction(
                issue_number=903,
                pr_number=77,
                needs_human_label=labels.needs_human,
                needs_rework_label=labels.needs_rework,
                rework_cycles=3,
                reason="merge attempts exhausted",
            )
        )

        assert labels.needs_human in live[77]
        assert labels.needs_human not in live[903]
        assert claims.needs_human_causes(77) == frozenset(
            {NeedsHumanCause.MERGE_ESCALATION.value}
        )
        assert claims.needs_human_causes(903) == frozenset()
        # ...and the post-publish clear that pairs with it withdraws exactly
        # that cause, on that PR.
        applier.apply(
            RemoveLabelAction(
                issue_number=77,
                label=labels.needs_human,
                reason="post-publish state now reworkable; clearing needs-human",
                needs_human_cause=NeedsHumanCause.MERGE_ESCALATION,
            )
        )
        assert labels.needs_human not in live[77]
        assert claims.needs_human_causes(77) == frozenset()

    def test_an_operator_clear_is_refused_while_a_quarantine_holds_the_block(
        self, sample_config, tmp_path
    ):
        """A force-clear must not promise what it cannot deliver (#6999 F3 r4).

        A quarantine re-applies the block on EVERY scan, so clearing the label
        around it is undone within a tick - after the operator has been told
        the issue was requeued and the queue has acted on that. Relaunching
        work whose quarantined terminal is still alive is precisely the
        duplicate execution the quarantine exists to prevent. The command
        refuses instead, and says which owner is holding it.
        """
        live: dict[int, set[str]] = {903: set()}
        labels, applier, quarantine, block, claims = self._wiring(
            sample_config, tmp_path, live
        )

        self._quarantine_run(quarantine, tmp_path)
        applier.apply(
            AddLabelAction(
                issue_number=903,
                label=labels.needs_human,
                reason="publish failures exhausted",
                needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
            )
        )
        assert claims.needs_human_causes(903) == frozenset(
            {NeedsHumanCause.SESSION_LIFECYCLE.value}
        )

        outcome = block.force_clear(903, "operator retry")

        assert outcome is BlockOutcome.HELD_BY_ANOTHER_CAUSE
        assert not outcome.committed
        assert labels.needs_human in live[903], (
            "the quarantine would have put it straight back anyway"
        )

        # ...and once the quarantine resolves, the same command clears the
        # label AND every cause this owner records.
        quarantine.reconcile_released(frozenset())
        applier.apply(
            AddLabelAction(
                issue_number=903,
                label=labels.needs_human,
                reason="publish failures exhausted",
                needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
            )
        )
        cleared = block.force_clear(903, "operator retry")

        assert cleared is BlockOutcome.CLEARED
        assert labels.needs_human not in live[903]
        assert claims.needs_human_causes(903) == frozenset()

    def test_an_operator_clear_is_refused_while_tech_lead_provenance_stands(
        self, sample_config, tmp_path
    ):
        """The other cause a force-clear cannot settle.

        The tech-lead lifecycle keeps its own marker label, which this owner
        never removes, so its reconcile recovers the block from that marker on
        the next pass. Clearing the shared label without settling the marker
        would not stick either.
        """
        live: dict[int, set[str]] = {903: {LabelManager(sample_config).tech_lead_needs_human}}
        labels, _applier, _quarantine, block, _claims = self._wiring(
            sample_config, tmp_path, live
        )

        outcome = block.force_clear(903, "operator dismiss")

        assert outcome is BlockOutcome.HELD_BY_ANOTHER_CAUSE
        assert labels.tech_lead_needs_human in live[903]

    def test_a_generic_action_with_no_cause_is_refused(
        self, sample_config, tmp_path
    ):
        """Fail-fast beats a silent default.

        The catch-all default made every uncaused call site look correct while
        collapsing independent assertions onto one row, where a single release
        erased them all. An action that touches the governed label without
        naming a lifecycle is now refused outright, and the static guardrail
        stops one being written in the first place.
        """
        live: dict[int, set[str]] = {903: set()}
        labels, applier, _quarantine, _block, _claims = self._wiring(
            sample_config, tmp_path, live
        )

        result = applier.apply(
            AddLabelAction(
                issue_number=903, label=labels.needs_human, reason="no cause"
            )
        )

        assert not result.success
        assert labels.needs_human not in live[903]


class TestTheOwnerSurvivesAHalfWrittenTransition:
    """Label and provenance live in two systems, so either write can fail.

    Grouping the two writes in one method does not make them one transaction;
    only their ORDER decides what a failure between them leaves behind (#6999
    F4 round 4). Every ordering here is chosen so the surviving state is the
    self-healing one, and each is exercised by making the real collaborator
    fail rather than by reasoning about it.
    """

    def _block(self, sample_config, tmp_path, live, *, labels_writer=None,
               causes=None):
        labels = LabelManager(sample_config)
        claims = causes or SqlitePendingWorkClaimStore.for_repo(tmp_path)
        return labels, NeedsHumanBlock(
            needs_human_label=labels.needs_human,
            tech_lead_marker=labels.tech_lead_needs_human,
            labels=labels_writer or _LiveLabelWriter(live),
            read_labels=lambda number: list(live.get(number, set())),
            quarantined_issue_numbers=frozenset,
            causes=claims,
        ), claims

    def _request(self, labels, cause=None):
        from issue_orchestrator.control.needs_human_block import HumanBlockRequest

        return HumanBlockRequest(
            target=903,
            cause=cause or NeedsHumanCause.SESSION_LIFECYCLE,
            reason="publish failures exhausted",
        )

    def test_a_failed_label_write_leaves_no_cause_claiming_a_block(
        self, sample_config, tmp_path
    ):
        """Acquisition records provenance first, then compensates on failure.

        Labelling first left a window where the external write had committed
        and the cause store had not: a LIVE block nobody owns, which the next
        remover takes away because it can find no reason for it.
        """
        live: dict[int, set[str]] = {903: set()}

        class _RefusingWriter(_LiveLabelWriter):
            def add_label(self, issue_number: int, label: str) -> None:
                raise RuntimeError("github write failed")

        labels, block, claims = self._block(
            sample_config, tmp_path, live, labels_writer=_RefusingWriter(live)
        )

        outcome = block.acquire(self._request(labels))

        assert outcome is BlockOutcome.FAILED
        assert labels.needs_human not in live[903]
        assert claims.needs_human_causes(903) == frozenset(), (
            "a cause must never outlive the block it claims to explain"
        )

    def test_a_cause_recorded_without_its_label_is_pruned_on_sight(
        self, sample_config, tmp_path
    ):
        """The other half of that ordering: the survivable state self-heals.

        A crash between the durable record and the label write leaves a row
        over an absent label. The label is authoritative, so a reader drops it
        rather than letting it hold a block open for a cause that never landed.
        """
        live: dict[int, set[str]] = {903: set()}
        _labels, block, claims = self._block(sample_config, tmp_path, live)
        claims.record_needs_human_cause(
            903, NeedsHumanCause.SESSION_LIFECYCLE.value, reason="crashed"
        )

        held = block.held_by_another_cause(
            903, excluding=NeedsHumanCause.CLAIM_QUARANTINE
        )

        assert held is False
        assert claims.needs_human_causes(903) == frozenset()

    def test_a_failed_removal_keeps_the_last_cause_recorded(
        self, sample_config, tmp_path
    ):
        """Release must not discharge a cause it could not act on.

        Withdrawing first meant a failed removal returned FAILED with the label
        still on the issue and its last cause already erased - the unowned live
        block this owner exists to make impossible, produced by the owner.
        """
        live: dict[int, set[str]] = {903: set()}

        class _RefusingRemove(_LiveLabelWriter):
            def remove_label(self, issue_number: int, label: str) -> None:
                raise RuntimeError("github write failed")

        labels, block, claims = self._block(
            sample_config, tmp_path, live, labels_writer=_RefusingRemove(live)
        )
        assert block.acquire(self._request(labels)) is BlockOutcome.HELD
        assert labels.needs_human in live[903]

        outcome = block.release(self._request(labels))

        assert outcome is BlockOutcome.FAILED
        assert labels.needs_human in live[903]
        assert claims.needs_human_causes(903) == frozenset(
            {NeedsHumanCause.SESSION_LIFECYCLE.value}
        ), "the cause must survive so the next attempt can retry the removal"

    def test_a_cause_store_failure_after_removal_cannot_reattach(
        self, sample_config, tmp_path
    ):
        """Stale rows left by a failed clear must not revive on a re-add.

        The removal commits before the rows are dropped, so a store fault can
        leave them behind. They are only ever read against the live label, so
        the moment the label is gone they are stale - and a LATER acquisition
        by a different cause does not inherit them.
        """
        live: dict[int, set[str]] = {903: set()}
        real = SqlitePendingWorkClaimStore.for_repo(tmp_path)

        class _RefusingClear:
            def __init__(self) -> None:
                self.refuse = False

            def record_needs_human_cause(self, issue_number, cause, *, reason):
                real.record_needs_human_cause(issue_number, cause, reason=reason)

            def needs_human_causes(self, issue_number):
                return real.needs_human_causes(issue_number)

            def withdraw_needs_human_cause(self, issue_number, cause):
                real.withdraw_needs_human_cause(issue_number, cause)

            def clear_needs_human_causes(self, issue_number):
                if self.refuse:
                    raise RuntimeError("sqlite write failed")
                real.clear_needs_human_causes(issue_number)

        causes = _RefusingClear()
        labels, block, _claims = self._block(
            sample_config, tmp_path, live, causes=causes
        )
        assert block.acquire(self._request(labels)) is BlockOutcome.HELD

        causes.refuse = True
        with pytest.raises(RuntimeError):
            block.release(self._request(labels))

        # The label is gone; the row survived the fault.
        assert labels.needs_human not in live[903]
        assert real.needs_human_causes(903) != frozenset()

        # A different cause now needs the block. It must not inherit the stale
        # row - the reader prunes it against the absent label first.
        causes.refuse = False
        assert not block.held_by_another_cause(
            903, excluding=NeedsHumanCause.MERGE_ESCALATION
        )
        assert real.needs_human_causes(903) == frozenset()
