"""Action application results — the Apply/report boundary.

Split out of ``actions.py``: an :class:`ActionResult` describes what happened
when an action was applied, which is a distinct concern from the action
vocabulary itself. Re-exported from ``control.actions`` so existing
``from .actions import ActionResult`` importers are unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional, Protocol

if TYPE_CHECKING:
    from .action_base import Action


class ActionResultType(Enum):
    """Result of applying an action."""

    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"  # Already applied or not applicable


@dataclass(frozen=True)
class ActionResult:
    """Result of applying an action.

    Attributes:
        action: The action that was applied
        result_type: Success, failure, or skipped
        error: Error message if failed
        details: Additional details about the result
    """

    action: "Action"
    result_type: ActionResultType
    error: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """Check if the action succeeded."""
        return self.result_type == ActionResultType.SUCCESS

    @property
    def issue_number(self) -> int | None:
        """Canonical issue number this result carries, or ``None`` if it carries
        none (e.g. the session_launcher-less fallback launch, which has no
        ``Session`` and thus no canonical issue identity).

        A typed accessor over the untyped ``details`` bag: it fails fast on a
        present-but-non-``int`` value (a producer/contract bug, ``bool`` included
        since ``bool`` is an ``int`` subclass) rather than letting a malformed
        identity flow silently downstream. Consumers that *require* the number
        (e.g. blocked->front launch cleanup for an issue launch) enforce presence
        themselves; see #6873 N4.
        """
        value = self.details.get("issue_number")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"ActionResult.issue_number must be int, got {value!r}")
        return value

    @classmethod
    def ok(cls, action: "Action", **details: str | int | bool | list[str] | None) -> "ActionResult":
        """Create a successful result."""
        return cls(
            action=action,
            result_type=ActionResultType.SUCCESS,
            details=details,
        )

    @classmethod
    def fail(cls, action: "Action", error: str, **details: str | int | bool | list[str] | None) -> "ActionResult":
        """Create a failed result."""
        return cls(
            action=action,
            result_type=ActionResultType.FAILURE,
            error=error,
            details=details,
        )

    @classmethod
    def skip(
        cls,
        action: "Action",
        reason: str,
        **details: str | int | bool | list[str] | None,
    ) -> "ActionResult":
        """Create a skipped result."""
        return cls(
            action=action,
            result_type=ActionResultType.SKIPPED,
            details={"skip_reason": reason, **details},
        )


class SupportsApplyAction(Protocol):
    """The single-action apply seam a control owner drives.

    Named structurally so an owner reuses the tick's real apply path (the
    concrete ``ActionApplier``) without importing the infra facade, and a test
    can supply a lightweight fake returning a canned ``ActionResult``.

    It lives beside ``ActionResult`` because the RESULT is what makes the seam
    worth having: owners that decide from ``details`` - quarantine provenance
    reads ``no_op`` to tell "I added this label" from "it was already there"
    (#6999 F12) - cannot be given a boolean applier.
    """

    def apply(self, action: "Action") -> ActionResult: ...
