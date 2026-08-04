"""Finding-promotion domain vocabulary (#6957).

Pattern case files (#6781) accrue evidence but had no actuation lane: a
diagnosed, reproducible orchestrator bug sat in a case file until a human
hand-carried it into a fix. This module is the domain vocabulary for the lane
that closes that gap — case file -> gated runnable issue in the repo that owns
the fix -> merged fix -> ``tech_lead_shipped_fixes`` row.

Two durable facts drive it, both orchestrator-owned (never issue bodies):

* :class:`PatternEvidence` — one row per pattern signature: its case file, how
  many observations have accrued, and the tech lead's ``fix:code`` /
  ``fix:human`` classification. Only ``fix:code`` is ever promotable — a
  human-gated problem made runnable manufactures doomed rework.
* :class:`PromotedFinding` — one row per PROMOTED signature: which repo it was
  filed in, which issue, and its terminal state. ``declined`` is permanent
  (closing a gated promotion is a rejection, and it must never be re-filed);
  ``shipped`` records the merged fix that closed the loop.

The types are infrastructure-agnostic: no GitHub, no config, no ports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# The tech lead's fix classification on a ``flag_pattern`` action. A finding
# with NO classification is unclassified and never promotable — promotion is
# opt-in evidence of "a code change in some repo fixes this", not a default.
FINDING_FIX_CLASS_CODE = "code"
FINDING_FIX_CLASS_HUMAN = "human"
VALID_FINDING_FIX_CLASSES: frozenset[str] = frozenset(
    (FINDING_FIX_CLASS_CODE, FINDING_FIX_CLASS_HUMAN)
)

# Route value meaning "the managed repo itself owns this fix". Self-routed
# promotions are ordinary issues in the source repo and face its own scope
# gates exactly like any other issue — promotion never bypasses them.
PROMOTION_ROUTE_SELF = "self"

# The route table's catch-all key.
PROMOTION_ROUTE_DEFAULT_KEY = "default"

# Promotion modes for ``tech_lead.findings.promote``.
FINDING_PROMOTION_OFF = "off"
FINDING_PROMOTION_GATED = "gated"
FINDING_PROMOTION_AUTO = "auto"
VALID_FINDING_PROMOTION_MODES: tuple[str, ...] = (
    FINDING_PROMOTION_OFF,
    FINDING_PROMOTION_GATED,
    FINDING_PROMOTION_AUTO,
)

PromotionState = Literal["promoted", "declined", "shipped"]

PROMOTION_STATE_PROMOTED: PromotionState = "promoted"
PROMOTION_STATE_DECLINED: PromotionState = "declined"
PROMOTION_STATE_SHIPPED: PromotionState = "shipped"

VALID_PROMOTION_STATES: frozenset[str] = frozenset(
    (
        PROMOTION_STATE_PROMOTED,
        PROMOTION_STATE_DECLINED,
        PROMOTION_STATE_SHIPPED,
    )
)


@dataclass(frozen=True)
class PatternEvidence:
    """Accrued evidence for ONE pattern signature (the promotion input).

    ``observation_count`` is orchestrator-owned and durable: it is incremented
    by the case-file owner every time a ``flag_pattern`` observation lands, so
    it cannot be inflated by editing the case-file issue or by unrelated human
    comments on it (the tamper boundary the case file itself documents).
    """

    signature: str
    case_file_issue_number: int
    observation_count: int
    # "" = unclassified. Only FINDING_FIX_CLASS_CODE is promotable.
    fix_class: str = ""
    area: str = ""

    @property
    def is_code_fix(self) -> bool:
        """True iff the tech lead classified this as fixable by code."""
        return self.fix_class == FINDING_FIX_CLASS_CODE


@dataclass(frozen=True)
class PromotedFinding:
    """One promoted signature's durable ledger row.

    Create-once at filing time keyed by ``signature``: at most one promoted
    issue ever exists per signature, in either direction (a later observation
    comments on the promoted issue instead of re-filing, and a declined
    signature is never re-filed at all).
    """

    signature: str
    case_file_issue_number: int
    target_repo: str
    target_issue_number: int
    state: PromotionState = PROMOTION_STATE_PROMOTED
    area: str = ""
    title: str = ""
    # Merged PR that closed the loop; set only in the ``shipped`` state.
    shipped_pr_url: str = ""
    recorded_at: str = ""

    @property
    def is_open(self) -> bool:
        """True while the promotion is still in flight (counts toward the cap)."""
        return self.state == PROMOTION_STATE_PROMOTED


@dataclass(frozen=True)
class PromotableFinding:
    """A signature eligible for promotion this tick, with its resolved route."""

    evidence: PatternEvidence
    target_repo: str


@dataclass(frozen=True)
class SettledPromotion:
    """A promoted issue observed terminal in its target repo.

    ``shipped`` distinguishes "closed by a merged PR" (the loop closed: record
    the shipped fix, comment and close the case file) from "closed while still
    gated" (the operator DECLINED it: mark the signature declined so it is
    never re-filed, and leave the case file open to keep accruing).
    """

    promotion: PromotedFinding
    shipped: bool
    merged_pr_url: str = ""


def promotion_issue_title(*, source_repo: str, signature: str) -> str:
    """Title for a promoted finding issue: ``[tech-lead:<repo>] <signature>``.

    The source repo NAME (not ``owner/repo``) keeps the title readable when a
    promotion lands in a foreign repo whose maintainers need to know which
    managed repo diagnosed it.
    """
    name = source_repo.rsplit("/", 1)[-1] if source_repo else "unknown"
    return f"[tech-lead:{name}] {signature}"
