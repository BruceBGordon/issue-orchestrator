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

import hashlib
from dataclasses import dataclass
from typing import Literal, Sequence

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


class PatternClassificationConflictError(ValueError):
    """Two observations disagree about a signature's ``fix_class``/``area``.

    Raised by :func:`reconcile_pattern_classification`, the SINGLE owner of the
    upgrade rule. A signature's classification decides whether it is promotable
    at all (``fix:human`` is excluded) and which repo it routes to, so a later
    observation must never silently overwrite an established value — that would
    make a human-gated finding runnable, or reroute one signature's fix to a
    different repository depending on observation order (#6957 review F3).
    """


def reconcile_pattern_classification(
    *, field: str, signature: str, existing: str, incoming: str
) -> str:
    """Merge an incoming classification value into the recorded one.

    The whole rule, in one place, applied identically by the durable store, the
    in-memory port fake, and same-decision coalescing in the planner:

    * an EMPTY incoming value preserves what is recorded (an unclassified later
      observation never erases an established classification);
    * an empty recorded value is UPGRADED once by a non-empty incoming value;
    * identical values (case-insensitively — these ride GitHub label text) are
      idempotent;
    * two different non-empty values are a CONFLICT and raise. Reclassification
      is a reviewed decision, not a side effect of the next observation.
    """
    if not incoming:
        return existing
    if not existing:
        return incoming
    if existing.casefold() == incoming.casefold():
        return existing
    raise PatternClassificationConflictError(
        f"pattern signature {signature!r} is already recorded with"
        f" {field}={existing!r}; a later observation claims {incoming!r}."
        " Promotion classification and routing are immutable once recorded —"
        " reclassify deliberately instead of letting observation order decide"
    )


def pattern_observation_id(
    *, source_run_id: str, source_session_name: str, action_id: str
) -> str:
    """Stable identity for ONE ``flag_pattern`` observation.

    The durable observation count is what ``min_evidence`` reads, so it must be
    keyed by WHICH observation produced it, not by how many times the
    orchestrator replayed the write. A tech-lead decision action is observed
    exactly once by exactly one session run, so (run, session, action) is that
    identity — replaying a completed action after a crash reproduces the same
    id and the store records it create-once (#6957 review F1).
    """
    parts = (source_run_id.strip(), source_session_name.strip(), action_id.strip())
    if not all(parts):
        raise ValueError(
            "a pattern observation identity requires a non-empty source run id,"
            f" session name, and decision action id, got {parts!r}"
        )
    return ":".join(parts)


def pattern_observation_marker(observation_id: str) -> str:
    """Provenance marker embedded in an observation's case-file comment.

    Makes a comment traceable to the observation identity that owns it, so a
    duplicate posted by a crash-retry is identifiable as the same observation
    rather than looking like fresh evidence. A digest keeps arbitrary
    agent-authored ids out of HTML-comment syntax.
    """
    digest = hashlib.sha256(observation_id.encode()).hexdigest()
    return f"<!-- issue-orchestrator:tech-lead-observation:v1:{digest} -->"


@dataclass(frozen=True)
class PatternObservation:
    """One observation of a signature: its identity and its evidence comment.

    Carried on the case-file actions so the applier can record the observation
    create-once and post its comment under one owner. ``comment`` already
    embeds :func:`pattern_observation_marker`.
    """

    observation_id: str
    comment: str

    def __post_init__(self) -> None:
        if not self.observation_id.strip():
            raise ValueError("a PatternObservation requires a non-empty identity")
        if not self.comment.strip():
            raise ValueError(
                "a PatternObservation requires its non-empty evidence comment"
            )


@dataclass(frozen=True)
class PromotionRoute:
    """The full queue contract of the repo a finding's area routes to (#6957).

    A route identifies more than a repository: an issue is only RUNNABLE in its
    target when it carries that target's scheduling labels. The managed repo's
    own discovery, for instance, queries ``filtering.label`` AND a worker agent
    label, so a promotion missing the scope label is invisible to the scheduler
    even after the operator removes the gate — approval that actuates nothing
    (#6957 review F2). This value object owns that whole contract, so the label
    set is derived in ONE place for self-routed and foreign targets alike.

    ``scope_label`` may legitimately be empty (a target whose queue has no scope
    filter); ``agent_label`` never is — an issue with no agent label is
    unschedulable everywhere.
    """

    target_repo: str
    agent_label: str
    scope_label: str = ""
    is_self: bool = False

    def __post_init__(self) -> None:
        if not self.target_repo.strip():
            raise ValueError("a PromotionRoute requires a target repository")
        if not self.agent_label.strip():
            raise ValueError(
                f"the promotion route to {self.target_repo!r} has no worker agent"
                " label; a promoted issue with no agent label is never picked up"
                " by any pipeline"
            )

    def issue_labels(self, *, area: str, gate_label: str = "") -> tuple[str, ...]:
        """Every label a promoted issue must carry to be runnable in this target.

        Order is stable for the sake of readable diffs and assertions: worker
        agent, target scope, area tag, then the approval gate (when gated) —
        which is the ONLY blocking one, so removing it is the operator's whole
        approval.
        """
        return _deduped_labels(
            (
                self.agent_label,
                self.scope_label,
                f"{_AREA_LABEL_PREFIX}{area}" if area else "",
                gate_label,
            )
        )


def _deduped_labels(labels: Sequence[str]) -> tuple[str, ...]:
    """Order-preserving, case-insensitive dedup of non-empty label names."""
    seen: set[str] = set()
    result: list[str] = []
    for label in labels:
        folded = label.casefold()
        if label and folded not in seen:
            seen.add(folded)
            result.append(label)
    return tuple(result)


# Duplicated from ``tech_lead_session.TECH_LEAD_AREA_LABEL_PREFIX``
# deliberately: importing it here would close a cycle (tech_lead_session ->
# tech_lead_artifacts -> tech_lead_findings) for one string constant. The
# guardrail test ``test_area_label_prefix_agrees_with_session_vocabulary`` pins
# the two spellings together.
_AREA_LABEL_PREFIX = "area:"


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
    # Original tech-lead diagnosis/recommended fix from the first flag_pattern
    # observation. Stored with the trigger facts so a routed promotion carries
    # the actionable mechanism instead of only pointing back to the case file.
    diagnosis: str = ""

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
    # High-water mark of the signature's observation count that has already been
    # reported onto the promoted issue. Later observations are DEDUPED against
    # this rather than re-filed: the gap between it and the live count is
    # exactly the new evidence the promoted issue has not been told about yet.
    reported_observations: int = 0

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
class PromotionUpdate:
    """New evidence accrued on a signature with an in-flight promotion.

    The dedup mirror of :class:`PromotableFinding`: one promoted issue per
    signature, so further observations are reported ONTO that issue instead of
    filing a second one. ``observation_count`` is the signature's live count,
    which becomes the promotion's new high-water mark once reported.

    Terminal promotions are deliberately excluded: after a finding ships or an
    operator declines it, later evidence continues to accrue on the case file
    without reviving a closed target issue.
    """

    promotion: PromotedFinding
    observation_count: int

    @property
    def new_observations(self) -> int:
        return self.observation_count - self.promotion.reported_observations


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


def case_file_issue_marker(signature: str) -> str:
    """Stable remote provenance key for crash-safe CASE-FILE filing (#6781).

    The mirror of :func:`promotion_issue_marker`, for the same reason and the
    same crash window: the case file is created on GitHub first and its ledger
    row written second, so a process that dies in between leaves a real issue
    with no row — and the next attempt would file a SECOND case file for one
    signature, splitting the evidence that gates promotion (#6957 round-2
    review F10). This marker is what makes the already-created issue
    recoverable instead.

    A digest keeps arbitrary agent-authored signatures out of HTML-comment
    syntax while keeping the key deterministic across restarts.
    """
    digest = hashlib.sha256(signature.encode()).hexdigest()
    return f"<!-- issue-orchestrator:tech-lead-case-file:v1:{digest} -->"


def promotion_issue_marker(*, source_repo: str, signature: str) -> str:
    """Stable remote provenance key for crash-safe promotion filing.

    The local ledger is recorded after GitHub confirms creation.  If the
    process dies between those two writes, the next attempt uses this marker
    to recover the already-created issue instead of filing a duplicate.  A
    digest keeps arbitrary agent-authored signatures out of HTML-comment
    syntax while still making the key deterministic across restarts.
    """
    identity = f"{source_repo.casefold()}\0{signature}".encode()
    digest = hashlib.sha256(identity).hexdigest()
    return f"<!-- issue-orchestrator:tech-lead-promotion:v1:{digest} -->"
