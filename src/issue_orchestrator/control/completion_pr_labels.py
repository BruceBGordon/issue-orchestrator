"""What an agent may ask to have labelled on its own PR (#6999 F2).

``pr_labels`` is the one label set that arrives from OUTSIDE the orchestrator's
own planning: an agent writes it into its completion record and the processor
applies it. That makes it untrusted input, and the shared ``needs-human`` block
is not among the things it may hand itself. Applied there it would create a
block with no cause recorded against it, which a later typed release then takes
away from whoever DID record one - the exact loss the shared-block owner exists
to prevent. The agent already has the typed ``needs_human`` completion outcome
for this, and that one goes through the owner.

The rule is enforced twice, on purpose, and neither is redundant:

* at the DOOR, by :func:`reserved_pr_label_error`, which rejects the whole
  completion record before any side effect - no push, no PR, no labels;
* at the WRITE, by the governed label capability, which refuses the value.

They consult different objects (the block owner, then the label capability), so
a composition can wire one without the other. When that happens the write-side
refusal FAILS the completion rather than skipping the entry: a request for a
human block that is silently dropped is one nothing downstream can see.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from .completion_types import ERROR_PREFIX_GOVERNED_LABEL
from .governed_label_set import GovernedLabelError

if TYPE_CHECKING:
    from ..domain.models import CompletionRecord
    from ..ports.pull_request_tracker import PRInfo
    from .needs_human_block import SharedNeedsHumanBlock

logger = logging.getLogger(__name__)

#: PR numbers the E2E dry run invents. They exist nowhere, so labelling them
#: would be a call against a PR GitHub has never heard of.
_DRY_RUN_PR_NUMBERS = range(90000, 100000)


class _LabelWriter(Protocol):
    def add_label(self, issue_number: int, label: str) -> None: ...


def reserved_pr_label_error(
    record: "CompletionRecord", block: "SharedNeedsHumanBlock"
) -> str | None:
    """The door check: why this record's ``pr_labels`` is not acceptable.

    Asks the OWNER whether it governs the label rather than comparing against a
    hard-coded name, so a repo that configures a different shared block is
    governed just the same.
    """
    reserved = [label for label in (record.pr_labels or ()) if block.owns(label)]
    if not reserved:
        return None
    logger.error(
        "[COMPLETION] Rejecting completion record: pr_labels names the reserved "
        "shared block %s. Use the needs_human completion outcome, which records "
        "the lifecycle that requires it.",
        reserved,
    )
    return (
        f"pr_labels may not contain the reserved shared block label(s) "
        f"{reserved}: use the needs_human completion outcome instead"
    )


def apply_pr_labels(
    *,
    pr: "PRInfo",
    record: "CompletionRecord",
    labels: _LabelWriter,
    actions_taken: list[str],
    errors: list[str],
) -> bool:
    """Apply the record's extra PR labels. False when one was REFUSED.

    A refusal appends a ``governed_label`` error, which forces the completion
    to fail with no "but the push worked" escape - see
    :mod:`.completion_result_artifacts`.
    """
    actions_taken.append(f"Created PR #{pr.number}")
    logger.info("Created PR #%d: %s", pr.number, pr.url)

    if not record.pr_labels:
        return True
    if pr.number in _DRY_RUN_PR_NUMBERS:
        logger.info(
            "[E2E_DRY_RUN] Skipping PR label addition for fake PR #%d", pr.number
        )
        return True

    applied: list[str] = []
    refused: str | None = None
    for label in record.pr_labels:
        try:
            labels.add_label(pr.number, label)
        except GovernedLabelError:
            refused = (
                f"{ERROR_PREFIX_GOVERNED_LABEL}: pr_labels entry {label!r} is the "
                f"shared needs-human block, which is not the agent's to apply on "
                f"PR #{pr.number}; use the needs_human completion outcome"
            )
            logger.error("[COMPLETION] %s", refused)
            errors.append(refused)
            break
        applied.append(label)
        logger.info("Added label '%s' to PR #%d", label, pr.number)
    if applied:
        actions_taken.append(f"Added labels to PR: {applied}")
    return refused is None


__all__ = ["apply_pr_labels", "reserved_pr_label_error"]
