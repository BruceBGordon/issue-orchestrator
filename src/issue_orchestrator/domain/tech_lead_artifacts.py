"""Tech Lead artifact pair contract.

Tech Lead (tech-lead) agent sessions produce two related artifacts in their
``tech-lead-data`` directory:

* ``tech-lead-report.md`` — human-readable tech-lead narrative.
* ``tech-lead-decision.json`` — strict orchestration/audit data.

The JSON is authoritative for workflow. The markdown explains the findings for
humans. Stable finding/action IDs tie the two together without making markdown
parsing a policy dependency.

ID contract: finding ids are canonical ``T<n>`` and action ids are canonical
``A<n>`` (``n`` a positive integer without leading zeros, e.g. ``T1``,
``A12``). Ids are unique across the combined finding+action namespace, and
the report must mention every id as an exact token (``T1`` is not satisfied
by ``T10``). Every finding must carry at least one non-empty string evidence
reference into the inputs the agent was given.

Unlike the review exchange (where the orchestrator persists the pair from an
exchange payload), the tech lead agent writes both files itself; the orchestrator
loads and validates them as **untrusted input** at session completion
(ADR-0031). Proposed actions express agent *intent* only — the orchestrator
decides execution per the configured authority mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal


TechLeadActionType = Literal[
    "post_comment",
    "create_issue",
    "escalate_to_human",
    "flag_pattern",
    "reset_retry",
    "kill_hung_session",
]
TechLeadFindingClassification = Literal["infra", "task", "agent", "systemic"]
TechLeadAuthorityMode = Literal["execute", "propose"]

TECH_LEAD_REPORT_ARTIFACT = "tech_lead_report"
TECH_LEAD_DECISION_ARTIFACT = "tech_lead_decision"
TECH_LEAD_REPORT_FILENAME = "tech-lead-report.md"
TECH_LEAD_DECISION_FILENAME = "tech-lead-decision.json"

VALID_TECH_LEAD_ACTION_TYPES: frozenset[str] = frozenset(
    (
        "post_comment",
        "create_issue",
        "escalate_to_human",
        "flag_pattern",
        "reset_retry",
        "kill_hung_session",
    )
)
_VALID_CLASSIFICATIONS = frozenset(("infra", "task", "agent", "systemic"))

# Act-level intents mutate orchestrator runtime state. reset_retry is wired to
# the reset+retry-from-scratch owner (#6764, first slice) and may be granted
# "execute"; the UNWIRED subset has no executor yet — config validation must
# reject authority "execute" for those until they are wired, never no-op.
ACT_LEVEL_TECH_LEAD_ACTIONS: frozenset[str] = frozenset(
    ("reset_retry", "kill_hung_session")
)
UNWIRED_ACT_LEVEL_TECH_LEAD_ACTIONS: frozenset[str] = frozenset(("kill_hung_session",))

# Canonical id forms (see module docstring). Leading zeros are rejected so
# every id has exactly one canonical spelling; the forms are disjoint, which
# structurally prevents a finding and an action from sharing an id.
TECH_LEAD_FINDING_ID_FORM = "T<n>"
TECH_LEAD_ACTION_ID_FORM = "A<n>"
_FINDING_ID_RE = re.compile(r"T[1-9][0-9]*\Z")
_ACTION_ID_RE = re.compile(r"A[1-9][0-9]*\Z")

# Sentinel distinguishing an ABSENT mapping key (valid default) from an
# explicitly present ``null`` (a contract violation). ``dict.get(key)`` collapses
# both to ``None``; callers pass ``dict.get(key, _MISSING)`` so a present ``null``
# reaches the strict parser and is rejected. See ``_required_bool``.
_MISSING: Any = object()

# Untrusted-input bounds. The decision file is agent-authored; violating any
# bound is a contract violation, not something to silently truncate.
MAX_TECH_LEAD_FINDINGS = 50
MAX_TECH_LEAD_ACTIONS = 20
MAX_ACTION_BODY_CHARS = 20_000
MAX_TITLE_CHARS = 300
MAX_SUMMARY_CHARS = 5_000
MAX_EVIDENCE_REFS = 20
MAX_LABELS_PER_ACTION = 10
MAX_LABEL_CHARS = 100
# The pattern signature is a dedup KEY (the case-file ledger keys on it,
# #6781) and lands in an issue title, so it is bounded tighter than bodies.
MAX_PATTERN_SIGNATURE_CHARS = 200
MAX_AREA_CHARS = 50


@dataclass(frozen=True)
class TechLeadFinding:
    """One diagnosed problem in the machine decision."""

    id: str
    title: str
    classification: TechLeadFindingClassification
    evidence: tuple[str, ...] = ()
    details: str | None = None

    @classmethod
    def from_mapping(cls, data: Any, *, index: int) -> "TechLeadFinding":
        if not isinstance(data, dict):
            raise ValueError(f"finding #{index} must be an object, got {type(data).__name__}")
        finding_id = _required_str(data, "id", f"finding #{index}")
        _validate_finding_id(finding_id)
        title = _required_str(data, "title", f"finding {finding_id}")
        classification = data.get("classification")
        if classification not in _VALID_CLASSIFICATIONS:
            raise ValueError(
                f"finding {finding_id} has invalid classification: {classification!r}"
                f" (expected one of {sorted(_VALID_CLASSIFICATIONS)})"
            )
        evidence = _evidence_tuple(data.get("evidence"), context=f"finding {finding_id}")
        if len(evidence) > MAX_EVIDENCE_REFS:
            raise ValueError(
                f"finding {finding_id} has {len(evidence)} evidence refs"
                f" (max {MAX_EVIDENCE_REFS})"
            )
        finding = cls(
            id=finding_id,
            title=_bounded(title, MAX_TITLE_CHARS, f"finding {finding_id} title"),
            classification=classification,
            evidence=evidence,
            details=_optional_str(data.get("details")),
        )
        return finding

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "classification": self.classification,
            "evidence": list(self.evidence),
        }
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class ProposedTechLeadAction:
    """One action the tech lead agent proposes; the orchestrator decides execution.

    Field requirements vary by ``action_type`` and are enforced by
    ``validate()``:

    * ``post_comment`` — ``target_number`` + ``body`` (``target_is_pr`` selects
      the comment surface).
    * ``create_issue`` — ``title`` + ``body`` (+ optional ``labels``).
    * ``escalate_to_human`` — ``target_number`` + ``body`` (the reason).
    * ``flag_pattern`` — ``body`` describing the cross-job pattern PLUS a
      REQUIRED ``pattern_signature``: a short stable slug keying the durable
      case-file ledger (#6781) — the same signature always names the same
      pattern, so repeated observations accrue as evidence comments on one
      case-file issue. Optional ``area`` names the component/seam the
      pattern clusters on (it becomes the case file's ``area:*`` tag).
    * ``reset_retry`` / ``kill_hung_session`` — ``target_number`` + ``body``
      (the rationale); act-level, gated by ``tech_lead.authority``. Under
      ``propose`` the op is filed as a gated proposal that runs on operator
      approval (#6778); ``reset_retry`` is also wired for direct ``execute``
      (#6764, first slice), while ``kill_hung_session``'s direct ``execute`` is
      unwired and rejected at startup — it runs only via gated approval.
    """

    id: str
    action_type: TechLeadActionType
    target_number: int | None = None
    target_is_pr: bool = False
    title: str | None = None
    body: str | None = None
    labels: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    pattern_signature: str | None = None
    area: str | None = None
    # Urgency signal for the expedite lane (#6870): when the tech lead wants a
    # follow-up worked SOONER, it sets ``expedite: true`` on a ``create_issue``
    # action. Only meaningful for ``create_issue`` — ``validate()`` rejects it
    # on any other action type. It composes with the ADR-0031 authority gate:
    # under ``execute`` the created issue jumps the worker lane immediately;
    # under ``propose`` it jumps only once the ``proposed-tech-lead`` gate is
    # removed. The orchestrator (never the agent) performs the queue write.
    expedite: bool = False
    # Dedup intent (#6878): when the tech lead recognizes that its proposed
    # ``create_issue`` follow-up already exists as an open issue, it sets
    # ``duplicate_of`` to that issue number rather than filing a new one. This is
    # INTENT only — the orchestrator (never the agent) decides what to do with it
    # (route the observation onto the existing issue), and independently
    # cross-checks it. Only meaningful for ``create_issue``; ``validate()``
    # rejects it on any other action type.
    duplicate_of: int | None = None

    @classmethod
    def from_mapping(cls, data: Any, *, index: int) -> "ProposedTechLeadAction":
        if not isinstance(data, dict):
            raise ValueError(f"proposed action #{index} must be an object, got {type(data).__name__}")
        action_id = _required_str(data, "id", f"proposed action #{index}")
        action_type = data.get("action_type")
        if action_type not in VALID_TECH_LEAD_ACTION_TYPES:
            raise ValueError(
                f"proposed action {action_id} has invalid action_type:"
                f" {action_type!r} (expected one of {sorted(VALID_TECH_LEAD_ACTION_TYPES)})"
            )
        target_number = data.get("target_number")
        if target_number is not None and (
            not isinstance(target_number, int)
            or isinstance(target_number, bool)
            or target_number <= 0
        ):
            raise ValueError(
                f"proposed action {action_id} target_number must be a positive"
                f" integer, got {target_number!r}"
            )
        labels = _string_tuple(data.get("labels"))
        if len(labels) > MAX_LABELS_PER_ACTION:
            raise ValueError(
                f"proposed action {action_id} has {len(labels)} labels"
                f" (max {MAX_LABELS_PER_ACTION})"
            )
        for label in labels:
            if not _LABEL_ALLOWED(label):
                raise ValueError(
                    f"proposed action {action_id} label {label!r} contains"
                    " disallowed characters"
                )
        body = _optional_bounded_str(
            data.get("body"), MAX_ACTION_BODY_CHARS, f"proposed action {action_id} body"
        )
        title = _optional_bounded_str(
            data.get("title"), MAX_TITLE_CHARS, f"proposed action {action_id} title"
        )
        signature = _optional_bounded_str(
            data.get("pattern_signature"),
            MAX_PATTERN_SIGNATURE_CHARS,
            f"proposed action {action_id} pattern_signature",
        )
        area = _optional_bounded_str(
            data.get("area"), MAX_AREA_CHARS, f"proposed action {action_id} area"
        )
        duplicate_of = data.get("duplicate_of")
        if duplicate_of is not None and not _is_valid_issue_number(duplicate_of):
            raise ValueError(
                f"proposed action {action_id} duplicate_of must be a positive"
                f" integer, got {duplicate_of!r}"
            )
        action = cls(
            id=action_id,
            action_type=action_type,
            target_number=target_number,
            target_is_pr=bool(data.get("target_is_pr", False)),
            title=title,
            body=body,
            labels=labels,
            finding_ids=_string_tuple(data.get("finding_ids")),
            pattern_signature=signature,
            area=area,
            expedite=_required_bool(
                data.get("expedite", _MISSING),
                f"proposed action {action_id} expedite",
            ),
            duplicate_of=duplicate_of,
        )
        action.validate()
        return action

    @property
    def is_act_level(self) -> bool:
        return self.action_type in ACT_LEVEL_TECH_LEAD_ACTIONS

    def validate(self) -> None:
        context = f"proposed action {self.id} ({self.action_type})"
        _validate_action_id(self.id)
        # Expedite is a create_issue-only urgency signal (#6870): urgency for a
        # comment, escalation, pattern flag or act-level op is meaningless, so a
        # decision that sets it elsewhere is a contract violation, never silently
        # honored. Mirrors the per-action-type field discipline below.
        if self.expedite and self.action_type != "create_issue":
            _require(
                False,
                f"{context} sets expedite=true, which is only valid on"
                " create_issue actions (#6870)",
            )
        # duplicate_of is a create_issue-only dedup intent (#6878): "this
        # proposal already exists as #N". Meaningless — and a contract
        # violation — on any other action type. A positive-int bound is enforced
        # at parse time; direct construction is re-checked here.
        if self.duplicate_of is not None:
            _require(
                self.action_type == "create_issue",
                f"{context} sets duplicate_of, which is only valid on"
                " create_issue actions (#6878)",
            )
            _require(
                _is_valid_issue_number(self.duplicate_of),
                f"{context} duplicate_of must be a positive issue number",
            )
        # pattern_signature/area must be meaningful whenever present —
        # direct construction bypasses from_mapping's normalization, and the
        # area lands in an `area:*` label, so it obeys label constraints.
        if self.pattern_signature is not None:
            _require(
                bool(self.pattern_signature.strip())
                and len(self.pattern_signature) <= MAX_PATTERN_SIGNATURE_CHARS,
                f"{context} pattern_signature must be non-empty and at most"
                f" {MAX_PATTERN_SIGNATURE_CHARS} characters when present",
            )
        if self.area is not None:
            _require(
                bool(self.area.strip())
                and len(self.area) <= MAX_AREA_CHARS
                and _LABEL_ALLOWED(self.area),
                f"{context} area must be a non-empty label-safe string"
                f" of at most {MAX_AREA_CHARS} characters when present",
            )
        if self.action_type == "post_comment":
            _require(self.target_number is not None, f"{context} requires target_number")
            _require(bool(self.body), f"{context} requires body")
        elif self.action_type == "create_issue":
            _require(bool(self.title), f"{context} requires title")
            _require(bool(self.body), f"{context} requires body")
        elif self.action_type == "escalate_to_human":
            _require(self.target_number is not None, f"{context} requires target_number")
            _require(bool(self.body), f"{context} requires body")
        elif self.action_type == "flag_pattern":
            _require(bool(self.body), f"{context} requires body")
            # Contract change (#6781): the signature keys the durable
            # case-file ledger — a flag_pattern without one cannot accrue.
            _require(
                self.pattern_signature is not None,
                f"{context} requires pattern_signature (the case-file"
                " ledger key, #6781)",
            )
        elif self.action_type in ACT_LEVEL_TECH_LEAD_ACTIONS:
            _require(self.target_number is not None, f"{context} requires target_number")
            _require(bool(self.body), f"{context} requires body (rationale)")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "action_type": self.action_type,
        }
        # Emit each optional field only when set — target_number/duplicate_of are
        # None-or-positive and the flags/collections/strings are None/empty when
        # unset, so a single truthiness pass matches the per-field ``if`` chain
        # without its branch-per-field complexity.
        optional: tuple[tuple[str, Any], ...] = (
            ("target_number", self.target_number),
            ("target_is_pr", self.target_is_pr),
            ("title", self.title),
            ("body", self.body),
            ("labels", list(self.labels)),
            ("finding_ids", list(self.finding_ids)),
            ("pattern_signature", self.pattern_signature),
            ("area", self.area),
            ("expedite", self.expedite),
            ("duplicate_of", self.duplicate_of),
        )
        for key, value in optional:
            if value:
                payload[key] = value
        return payload


@dataclass(frozen=True)
class TechLeadDecision:
    """Machine-readable tech_lead decision.

    Records tech-lead intent: what was diagnosed (findings) and what should
    happen about it (proposed actions). Execution is decided orchestrator-side
    per the configured authority mode; the decision itself never encodes
    authority.
    """

    summary: str
    findings: tuple[TechLeadFinding, ...] = ()
    proposed_actions: tuple[ProposedTechLeadAction, ...] = ()
    schema_version: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_agent_payload(cls, payload: Any) -> "TechLeadDecision":
        """Parse an agent-authored decision payload. Raises ValueError loudly."""
        if not isinstance(payload, dict):
            raise ValueError(
                f"tech_lead decision must be a JSON object, got {type(payload).__name__}"
            )
        decision_section = payload.get("decision")
        data = decision_section if isinstance(decision_section, dict) else payload
        schema_version = data.get("schema_version", 1)
        if schema_version != 1:
            raise ValueError(f"unsupported tech_lead decision schema_version: {schema_version!r}")
        summary = _required_str(data, "summary", "tech_lead decision")
        raw_findings = data.get("findings", [])
        if not isinstance(raw_findings, list):
            raise ValueError("tech_lead decision findings must be a list")
        if len(raw_findings) > MAX_TECH_LEAD_FINDINGS:
            raise ValueError(
                f"tech_lead decision has {len(raw_findings)} findings (max {MAX_TECH_LEAD_FINDINGS})"
            )
        findings = tuple(
            TechLeadFinding.from_mapping(item, index=index)
            for index, item in enumerate(raw_findings, start=1)
        )
        raw_actions = data.get("proposed_actions", [])
        if not isinstance(raw_actions, list):
            raise ValueError("tech_lead decision proposed_actions must be a list")
        if len(raw_actions) > MAX_TECH_LEAD_ACTIONS:
            raise ValueError(
                f"tech_lead decision has {len(raw_actions)} proposed actions"
                f" (max {MAX_TECH_LEAD_ACTIONS})"
            )
        actions = tuple(
            ProposedTechLeadAction.from_mapping(item, index=index)
            for index, item in enumerate(raw_actions, start=1)
        )
        decision = cls(
            summary=_bounded(summary, MAX_SUMMARY_CHARS, "tech_lead decision summary"),
            findings=findings,
            proposed_actions=actions,
            schema_version=1,
            extra={
                key: value
                for key, value in data.items()
                if key
                not in {"schema_version", "summary", "findings", "proposed_actions"}
            },
        )
        decision.validate()
        return decision

    def validate(self) -> None:
        finding_ids = [finding.id for finding in self.findings]
        for finding in self.findings:
            _validate_finding_id(finding.id)
            # Direct construction bypasses from_mapping; re-check the
            # runtime types of the untrusted evidence refs.
            if not _is_valid_evidence(finding.evidence):
                raise ValueError(
                    f"finding {finding.id} requires at least one non-empty"
                    " string evidence reference"
                )
        duplicates = _duplicates(finding_ids)
        if duplicates:
            raise ValueError(f"duplicate finding ids: {', '.join(sorted(duplicates))}")
        action_ids = [action.id for action in self.proposed_actions]
        duplicates = _duplicates(action_ids)
        if duplicates:
            raise ValueError(f"duplicate proposed action ids: {', '.join(sorted(duplicates))}")
        # Combined-namespace uniqueness. The canonical T<n>/A<n> forms make a
        # cross-namespace collision structurally impossible for parsed input,
        # but directly-constructed decisions must not bypass the invariant.
        duplicates = _duplicates(finding_ids + action_ids)
        if duplicates:
            raise ValueError(
                "finding and proposed action ids share a namespace;"
                f" duplicate ids: {', '.join(sorted(duplicates))}"
            )
        known = set(finding_ids)
        act_level_action_by_target: dict[int, str] = {}
        for action in self.proposed_actions:
            action.validate()
            unknown = [ref for ref in action.finding_ids if ref not in known]
            if unknown:
                raise ValueError(
                    f"proposed action {action.id} references unknown finding ids:"
                    f" {', '.join(unknown)}"
                )
            if action.is_act_level:
                assert action.target_number is not None  # enforced by validate()
                prior_action_id = act_level_action_by_target.get(action.target_number)
                if prior_action_id is not None:
                    raise ValueError(
                        "multiple act-level proposed actions target"
                        f" #{action.target_number}: {prior_action_id}, {action.id};"
                        " exactly one act-level command per target is allowed"
                    )
                act_level_action_by_target[action.target_number] = action.id

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "summary": self.summary,
            "findings": [finding.to_dict() for finding in self.findings],
            "proposed_actions": [action.to_dict() for action in self.proposed_actions],
        }
        payload.update(self.extra)
        return payload


def validate_tech_lead_report_links(decision: TechLeadDecision, report_text: str) -> None:
    """Every decision finding/action id must appear in tech-lead-report.md.

    Matching is exact-token (word-boundary): ``T1`` is NOT satisfied by a
    report that only mentions ``T10``.
    """
    missing = [
        item_id
        for item_id in (
            *(finding.id for finding in decision.findings),
            *(action.id for action in decision.proposed_actions),
        )
        if item_id
        and re.search(rf"\b{re.escape(item_id)}\b", report_text) is None
    ]
    if missing:
        raise ValueError(
            "tech-lead-report.md must mention every tech-lead-decision item id"
            " as an exact token: " + ", ".join(missing)
        )


def _LABEL_ALLOWED(label: str) -> bool:
    return bool(label) and all(
        ch.isalnum() or ch in "-_:. " for ch in label
    ) and len(label) <= MAX_LABEL_CHARS


def _validate_finding_id(finding_id: str) -> None:
    if _FINDING_ID_RE.fullmatch(finding_id) is None:
        raise ValueError(
            f"finding id {finding_id!r} is not canonical"
            f" (expected {TECH_LEAD_FINDING_ID_FORM}, e.g. T1)"
        )


def _validate_action_id(action_id: str) -> None:
    if _ACTION_ID_RE.fullmatch(action_id) is None:
        raise ValueError(
            f"proposed action id {action_id!r} is not canonical"
            f" (expected {TECH_LEAD_ACTION_ID_FORM}, e.g. A1)"
        )


def _is_valid_evidence(evidence: Any) -> bool:
    """Runtime check for directly-constructed findings (no static trust)."""
    return bool(evidence) and all(
        isinstance(ref, str) and ref.strip() for ref in evidence
    )


def _evidence_tuple(value: Any, *, context: str) -> tuple[str, ...]:
    """Strictly-typed evidence: a non-empty list of non-empty strings."""
    if not isinstance(value, list) or not value:
        raise ValueError(
            f"{context} requires a non-empty evidence list of string references"
        )
    items: list[str] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"{context} evidence #{index} must be a non-empty string,"
                f" got {item!r}"
            )
        items.append(item.strip())
    return tuple(items)


def _duplicates(ids: list[str]) -> set[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for item in ids:
        if item in seen:
            dupes.add(item)
        seen.add(item)
    return dupes


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _is_valid_issue_number(value: object) -> bool:
    """A positive int, excluding bool (an int subclass). Takes ``object`` so the
    runtime type check is real at both the parse and the direct-construction
    boundary (``validate``), not narrowed away by a declared ``int`` field."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _required_str(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} requires non-empty string {key!r}")
    return value.strip()


def _bounded(value: str, limit: int, context: str) -> str:
    if len(value) > limit:
        raise ValueError(f"{context} exceeds {limit} characters ({len(value)})")
    return value


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, list):
        raise ValueError(f"expected a list of strings, got {type(value).__name__}")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _optional_bounded_str(value: Any, limit: int, context: str) -> str | None:
    """Normalize an optional agent-authored string, enforcing its bound."""
    normalized = _optional_str(value)
    return _bounded(normalized, limit, context) if normalized is not None else None


def _required_bool(value: Any, context: str) -> bool:
    """Strictly parse an optional agent-authored JSON boolean.

    An ABSENT key defaults to False; callers signal absence by passing
    ``_MISSING`` (via ``dict.get(key, _MISSING)``). Any PRESENT value that is not
    a boolean — including an explicit JSON ``null`` — is a contract violation:
    the decision file is untrusted input, so ``null``, ``"false"``, ``1``, ``[]``
    and ``{}`` must fail loudly rather than be coerced (``bool("false")`` is
    True). ``bool`` is a subclass of ``int``; ``isinstance(1, bool)`` is False,
    so integers are correctly rejected.
    """
    if value is _MISSING:
        return False
    if not isinstance(value, bool):
        rendered = "null" if value is None else type(value).__name__
        raise ValueError(
            f"{context} must be a JSON boolean when present, got {rendered}"
        )
    return value
