"""The governed label, enforced at the capability rather than by spelling.

An AST check can only see how a label is WRITTEN. It cannot see
``for label in record.pr_labels: labels.add_label(pr.number, label)``, where the
value arrives from an agent's completion record at runtime, or
``for label in action.add_labels``, where it arrives from a collection the
planner assembled. Both are real paths, and both could put the shared
``needs-human`` block on an issue or a PR with no cause recorded against it
(#6999 F2 round 4) — after which a typed release sees no independent cause and
takes away a block somebody still needed.

So the rule lives where the mutation actually happens. Every holder of a
:class:`~..ports.label_set.LabelSet` except the block owner receives this
wrapper, which refuses the configured shared label BY VALUE. The owner keeps the
raw capability, which is what makes it the only writer in fact and not merely by
convention: there is no spelling, no dynamic value and no new call site that can
route around a capability the caller was never given.

The refusal is an exception rather than a silent skip. A caller that tried to
apply this label wanted a human to look at the issue; swallowing that would turn
a mis-routed block into no block at all, which is the failure mode this whole
boundary exists to prevent. Callers that legitimately handle collections catch
it and report the label they could not apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class LabelWriter(Protocol):
    """The mutating half of a label port - all this wrapper needs to govern.

    Narrower than :class:`~..ports.label_set.LabelSet` on purpose: the
    completion pipeline holds a port declared without ``has_label``, and a
    wrapper that demanded reads it never performs would exclude exactly the
    caller whose agent-supplied labels most need governing.
    """

    def add_label(self, issue_number: int, label: str) -> None: ...

    def remove_label(self, issue_number: int, label: str) -> None: ...


class GovernedLabelError(RuntimeError):
    """A non-owner tried to mutate the shared ``needs-human`` block directly.

    Carries the label and target so the caller can report exactly what it
    refused, rather than failing a whole batch with an opaque message.
    """

    def __init__(self, issue_number: int, label: str, operation: str) -> None:
        self.issue_number = issue_number
        self.label = label
        self.operation = operation
        super().__init__(
            f"refusing to {operation} the shared block label {label!r} on "
            f"#{issue_number} directly: it is owned by NeedsHumanBlock, which "
            "records the lifecycle that requires it. Use its typed "
            "acquire/release/force_clear commands."
        )


@dataclass(frozen=True, slots=True)
class GovernedLabelSet:
    """A :class:`LabelSet` that refuses the one label it does not own.

    Deliberately a wrapper rather than a check inside each caller: a check is
    something a new caller can forget, and the four bypasses this replaced were
    all callers that forgot. A capability cannot be forgotten — a holder either
    has the raw writer or it does not.
    """

    labels: LabelWriter
    #: The one label this wrapper withholds. Everything else passes straight
    #: through, so the wrapper is safe to give to every label writer.
    governed_label: str

    def add_label(self, issue_number: int, label: str) -> None:
        self._refuse_if_governed(issue_number, label, "add")
        self.labels.add_label(issue_number, label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self._refuse_if_governed(issue_number, label, "remove")
        self.labels.remove_label(issue_number, label)

    def has_label(self, issue_number: int, label: str) -> bool:
        """Delegate a read. Reads are not mutations and govern nothing.

        Resolved dynamically because the wrapped port is only required to
        WRITE; a holder that calls this is one whose port has always offered
        it, and it fails exactly as the unwrapped port would if not.
        """
        return getattr(self.labels, "has_label")(issue_number, label)

    def governs(self, label: str) -> bool:
        return label == self.governed_label

    def _refuse_if_governed(
        self, issue_number: int, label: str, operation: str
    ) -> None:
        if self.governs(label):
            raise GovernedLabelError(issue_number, label, operation)


__all__ = ["GovernedLabelError", "GovernedLabelSet", "LabelWriter"]
