"""The governed label, refused at the capability rather than by spelling.

An AST check sees how a label is WRITTEN. It cannot see a value that arrives at
runtime - an agent's ``pr_labels`` entry, a member of a planner-assembled sync
collection - and both were real paths to a shared block with no cause recorded
against it (#6999 F2 round 4). These pin the half of the rule the checker
cannot express.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.control.governed_label_set import (
    GovernedLabelError,
    GovernedLabelSet,
)


class _RecordingLabels:
    def __init__(self) -> None:
        self.added: list[tuple[int, str]] = []
        self.removed: list[tuple[int, str]] = []
        self.present: set[tuple[int, str]] = set()

    def add_label(self, issue_number: int, label: str) -> None:
        self.added.append((issue_number, label))

    def remove_label(self, issue_number: int, label: str) -> None:
        self.removed.append((issue_number, label))

    def has_label(self, issue_number: int, label: str) -> bool:
        return (issue_number, label) in self.present


def _guarded() -> tuple[_RecordingLabels, GovernedLabelSet]:
    raw = _RecordingLabels()
    return raw, GovernedLabelSet(labels=raw, governed_label="blocked-needs-human")


def test_a_dynamic_value_is_refused_exactly_like_a_spelled_one() -> None:
    """The whole point: enforcement by VALUE, not by how it was written."""
    raw, guarded = _guarded()
    agent_supplied = ["size:small", "blocked-needs-human"]

    applied: list[str] = []
    for label in agent_supplied:
        try:
            guarded.add_label(77, label)
        except GovernedLabelError:
            continue
        applied.append(label)

    assert applied == ["size:small"]
    assert raw.added == [(77, "size:small")]


def test_a_direct_removal_is_refused_too() -> None:
    """Removal is the half that loses a block somebody else still needs."""
    raw, guarded = _guarded()

    with pytest.raises(GovernedLabelError):
        guarded.remove_label(903, "blocked-needs-human")

    assert raw.removed == []


def test_the_refusal_names_what_it_refused() -> None:
    """A batch caller has to be able to report the one label it could not apply."""
    _raw, guarded = _guarded()

    with pytest.raises(GovernedLabelError) as caught:
        guarded.add_label(903, "blocked-needs-human")

    assert caught.value.label == "blocked-needs-human"
    assert caught.value.issue_number == 903
    assert caught.value.operation == "add"


def test_every_other_label_passes_straight_through() -> None:
    """Safe to give to every writer: it withholds one label and nothing else."""
    raw, guarded = _guarded()

    guarded.add_label(903, "in-progress")
    guarded.remove_label(903, "pr-pending")

    assert raw.added == [(903, "in-progress")]
    assert raw.removed == [(903, "pr-pending")]


def test_reads_are_never_refused() -> None:
    """Nobody's exclusive right: a read cannot lose a block."""
    raw, guarded = _guarded()
    raw.present.add((903, "blocked-needs-human"))

    assert guarded.has_label(903, "blocked-needs-human") is True


class TestTheCollectionWritersCannotSmuggleIt:
    """The two public surfaces that write labels from a COLLECTION.

    Both were named as bypasses because their values are computed, not spelled:
    ``SyncLabelsAction`` carries a planner-assembled tuple and ``LabelSync``
    computes its own add/remove sets. Wiring them to the guarded capability is
    what closes them, and these drive the public surfaces so a composition that
    forgot the wrapper cannot pass while the decorator's own tests still do.
    """

    def _live_labels(self):
        live: dict[int, set[str]] = {}

        class _Labels:
            def add_label(self, issue_number: int, label: str) -> None:
                live.setdefault(issue_number, set()).add(label)

            def remove_label(self, issue_number: int, label: str) -> None:
                live.setdefault(issue_number, set()).discard(label)

            def has_label(self, issue_number: int, label: str) -> bool:
                return label in live.get(issue_number, set())

        return live, GovernedLabelSet(
            labels=_Labels(), governed_label="blocked-needs-human"
        )

    def test_a_sync_action_cannot_add_the_reserved_label(self) -> None:
        from unittest.mock import MagicMock

        from issue_orchestrator.control.action_applier import ActionApplier
        from issue_orchestrator.control.actions import SyncLabelsAction

        live, guarded = self._live_labels()
        applier = ActionApplier(
            labels=guarded, sessions=MagicMock(), events=MagicMock()
        )

        result = applier.apply(
            SyncLabelsAction(
                issue_number=903,
                add_labels=("blocked-needs-human", "in-progress"),
                remove_labels=(),
                reason="sync",
            )
        )

        assert not result.success
        assert "blocked-needs-human" not in live.get(903, set())
        # ...and the ordinary label in the same collection still landed.
        assert "in-progress" in live[903]

    def test_a_sync_action_cannot_remove_the_reserved_label(self) -> None:
        from unittest.mock import MagicMock

        from issue_orchestrator.control.action_applier import ActionApplier
        from issue_orchestrator.control.actions import SyncLabelsAction

        live, guarded = self._live_labels()
        live[903] = {"blocked-needs-human", "pr-pending"}
        applier = ActionApplier(
            labels=guarded, sessions=MagicMock(), events=MagicMock()
        )

        result = applier.apply(
            SyncLabelsAction(
                issue_number=903,
                add_labels=(),
                remove_labels=("blocked-needs-human", "pr-pending"),
                reason="sync",
            )
        )

        assert not result.success
        assert "blocked-needs-human" in live[903], (
            "removing it here would retract a block with no owner consulted"
        )
        assert "pr-pending" not in live[903]

    def test_label_sync_cannot_add_the_reserved_label(self) -> None:
        from unittest.mock import MagicMock

        from issue_orchestrator.control.label_sync import (
            DesiredLabels,
            LabelSync,
        )

        live, guarded = self._live_labels()
        sync = LabelSync(labels=guarded, events=MagicMock())

        result = sync.sync(
            903,
            current=set(),
            desired=DesiredLabels.add("blocked-needs-human", "in-progress"),
        )

        assert "blocked-needs-human" not in live.get(903, set())
        assert "blocked-needs-human" in result.errors
        assert "in-progress" in live[903]
