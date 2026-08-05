"""FreshIssueReader port for correctness-critical issue reads.

This port exists because some decisions may NOT be made against cached state:
the reconciliation gate that refuses to mutate a paused issue, the publish-retry
gate, and the session-outcome classification all need what GitHub says right
now. So the one thing this port must never do is make "I could not read the
issue" look like an observation.

The GitHub adapter used to swallow every read failure and return ``[]``, which
is indistinguishable from "this issue genuinely has no labels" — and an empty
label set SATISFIES the expectation the tech-lead mutation gate checks (it
forbids ``io:needs-reconcile`` and requires nothing). A timeout, a rate limit,
or an auth failure therefore let the control plane walk straight through an
explicit operator pause and file issues, comment cross-repo, settle promotions,
reset work, or kill sessions (#6957 round-2 review F4/A4). ADR-0006 requires the
opposite: fail closed when fresh observations cannot be obtained.

So the contract is unambiguous, and every consumer is expected to honor it:

* a successful read returns the observed labels, which MAY be empty;
* a failed read raises :class:`FreshIssueReadError` and never returns.
"""

from typing import Protocol


class FreshIssueReadError(RuntimeError):
    """A fresh issue read did not complete, so its result is UNKNOWN.

    Deliberately not a subclass of anything callers already swallow. Each
    consumer has to decide what "unknown" means for its own decision — pause the
    mutation, reject the retry, fall back to last-known labels — instead of
    inheriting a silent empty list that reads as fact.
    """


class FreshIssueReader(Protocol):
    """Protocol for fresh issue reads (no cache, no ETag)."""

    def read_issue_labels(self, issue_number: int) -> list[str]:
        """Labels on *issue_number* right now, bypassing every cache.

        Returns the observed labels, which may legitimately be empty.

        Raises:
            FreshIssueReadError: the read did not complete. Implementations must
                NOT degrade to an empty list — "unknown" is not an observation,
                and the callers of this port act on the difference.
        """
        ...
