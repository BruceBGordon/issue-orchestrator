"""Tech-lead configuration sub-models (ADR-0031).

Split out of ``config_models`` for cohesion — and for its line budget — as the
``tech_lead:`` section grew its own authority, dedup, health-review, stuck-sweep
and finding-promotion blocks. Mirrors the parsing split that already exists
(``config_sections_tech_lead``). ``config_models`` re-exports every name here,
so importers are unaffected.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from ..domain.tech_lead_artifacts import UNWIRED_ACT_LEVEL_TECH_LEAD_ACTIONS
from ..domain.tech_lead_findings import (
    FINDING_PROMOTION_GATED,
    FINDING_PROMOTION_OFF,
    PROMOTION_ROUTE_DEFAULT_KEY,
    PROMOTION_ROUTE_SELF,
    VALID_FINDING_PROMOTION_MODES,
)


@dataclass
class MilestoneStrategyConfig:
    """Milestone assignment strategy for tech_lead issues."""

    inherit_from_issues: Optional[str] = "latest"  # "earliest" | "latest" | None
    explicit: Optional[str] = None  # Explicit milestone name


TECH_LEAD_AUTHORITY_MODES = ("execute", "propose")

# Action types whose authority mode is configurable. escalate_to_human is
# deliberately absent: it is the non-configurable floor and always executes.
TECH_LEAD_AUTHORITY_CONFIGURABLE_ACTIONS = (
    "post_comment",
    "create_issue",
    "flag_pattern",
    "reset_retry",
    "kill_hung_session",
)


@dataclass
class TechLeadAuthorityConfig:
    """Per-action-type authority modes for tech_lead decision proposals (ADR-0031).

    ``execute`` — the orchestrator performs the proposed action directly.
    ``propose`` — for ``post_comment``/``flag_pattern``: shadow mode (the
    proposal is surfaced as would-have-done). For ``create_issue`` and
    act-level actions: a GATED ISSUE (#6778) — the proposal is created as a
    GitHub issue carrying ``proposed-tech-lead``; removing that label is
    per-instance operator approval. Per-instance approval and config-level
    trust coexist.

    ``escalate_to_human`` is intentionally not a field: it is the
    non-configurable floor and always executes. Act-level actions
    (``reset_retry``, ``kill_hung_session``) default to ``propose``.
    ``reset_retry: execute`` is honored — it is wired to the
    reset+retry-from-scratch owner with execution-time re-validation
    (#6764, first slice). ``kill_hung_session: execute`` remains a startup
    error: its DIRECT tier is not wired yet — it ships as gated proposal
    issues (#6778) — see ``Config.validate``.
    """

    post_comment: str = "execute"
    create_issue: str = "execute"
    flag_pattern: str = "execute"
    reset_retry: str = "propose"
    kill_hung_session: str = "propose"

    @classmethod
    def from_mapping(cls, data: dict) -> "TechLeadAuthorityConfig":
        """Parse the ``tech_lead.authority`` YAML section, validating modes."""
        defaults = cls()
        values: dict[str, str] = {}
        for key in TECH_LEAD_AUTHORITY_CONFIGURABLE_ACTIONS:
            value = data.get(key, getattr(defaults, key))
            if value not in TECH_LEAD_AUTHORITY_MODES:
                raise ValueError(
                    f"tech_lead.authority.{key} must be one of"
                    f" {list(TECH_LEAD_AUTHORITY_MODES)}, got {value!r}"
                )
            values[key] = value
        return cls(**values)

    def mode_for(self, action_type: str) -> str:
        """Return the authority mode for a proposed tech_lead action type.

        ``escalate_to_human`` ALWAYS returns ``execute`` — routing to a
        human is the fail-safe floor and cannot be configured away.
        Unknown action types raise: authority for an unrecognized action
        must never be silently guessed.
        """
        if action_type == "escalate_to_human":
            return "execute"
        if action_type not in TECH_LEAD_AUTHORITY_CONFIGURABLE_ACTIONS:
            raise ValueError(f"unknown tech_lead action type: {action_type!r}")
        return getattr(self, action_type)

    def to_event_dict(self) -> dict:
        """All five graduated-authority modes, for config event payloads."""
        return {
            key: getattr(self, key) for key in TECH_LEAD_AUTHORITY_CONFIGURABLE_ACTIONS
        }

    def startup_errors(self) -> list[str]:
        """Startup configuration errors for this authority block (ADR-0031).

        ``execute`` on an act-level action whose DIRECT executor is not
        wired yet must be a startup configuration error, never a silent
        no-op (#6764). ``reset_retry`` is wired and no longer rejected; the
        unwired set lives in ``UNWIRED_ACT_LEVEL_TECH_LEAD_ACTIONS``. The
        rejection is deliberate even though ``kill_hung_session`` ships as
        GATED PROPOSAL ISSUES under ``propose`` (#6778): the gated tier is
        the point — per-instance approval, not config-level trust.
        """
        errors: list[str] = []
        for key in TECH_LEAD_AUTHORITY_CONFIGURABLE_ACTIONS:
            mode = getattr(self, key)
            if mode not in TECH_LEAD_AUTHORITY_MODES:
                errors.append(
                    f"tech_lead.authority.{key} must be one of"
                    f" {list(TECH_LEAD_AUTHORITY_MODES)}, got {mode!r}"
                )
        for key in sorted(UNWIRED_ACT_LEVEL_TECH_LEAD_ACTIONS):
            if getattr(self, key) == "execute":
                errors.append(
                    f"tech_lead.authority.{key}: direct 'execute' is not wired"
                    " yet (#6764); use 'propose' — proposals surface as"
                    " gated issues awaiting per-instance approval (#6778)"
                )
        return errors


@dataclass
class TechLeadDedupConfig:
    """Trusted open-issue deduplication settings for ``create_issue`` proposals."""

    enabled: bool = True
    similarity_threshold: float = 0.72

    @classmethod
    def from_mapping(cls, data: dict) -> "TechLeadDedupConfig":
        return cls(
            enabled=bool(data.get("enabled", True)),
            similarity_threshold=float(data.get("similarity_threshold", 0.72)),
        )

    def startup_errors(self) -> list[str]:
        if not 0.0 < self.similarity_threshold <= 1.0:
            return [
                "tech_lead.dedup.similarity_threshold must be > 0.0 and <= 1.0, "
                f"got {self.similarity_threshold}"
            ]
        return []


@dataclass
class TechLeadHealthReviewConfig:
    """Periodic and problem-storm health-review trigger settings (ADR-0031).

    ``interval_minutes`` drives the planner-side trigger: every N minutes
    the orchestrator creates a health-review anchor issue for the tech_lead
    agent to walk the board snapshot. 0 (the default) disables the trigger.

    ``storm_threshold`` is the number of recent blocked/failed problem issues
    that replaces per-issue investigations with one unscheduled health review;
    0 disables storm escalation. ``storm_window_minutes`` defines "recent".
    """

    interval_minutes: int = 0
    storm_threshold: int = 3
    storm_window_minutes: int = 5

    @classmethod
    def from_mapping(cls, data: dict) -> "TechLeadHealthReviewConfig":
        """Parse the ``tech_lead.health_review`` YAML sub-dict."""
        return cls(
            interval_minutes=int(data.get("interval_minutes", 0)),
            storm_threshold=int(data.get("storm_threshold", 3)),
            storm_window_minutes=int(data.get("storm_window_minutes", 5)),
        )

    def startup_errors(self) -> list[str]:
        """Startup configuration errors for the health-review block.

        The documented disable value is exactly 0; a negative interval is a
        misconfiguration that must fail startup loudly, never be silently
        treated as disabled (#6763 finding 8).
        """
        errors: list[str] = []
        if self.interval_minutes < 0:
            errors.append(
                "tech_lead.health_review.interval_minutes must be >= 0 "
                f"(0 disables the trigger), got {self.interval_minutes}"
            )
        if self.storm_threshold < 0:
            errors.append(
                "tech_lead.health_review.storm_threshold must be >= 0 "
                f"(0 disables storm escalation), got {self.storm_threshold}"
            )
        if self.storm_window_minutes <= 0:
            errors.append(
                "tech_lead.health_review.storm_window_minutes must be > 0, got "
                f"{self.storm_window_minutes}"
            )
        return errors


@dataclass
class StuckSweepConfig:
    """Tech-lead attention sweep trigger settings (ADR-0031, #6823).

    A bounded, timer-gated backstop that re-injects open issues stuck in a
    terminal blocking state (that the normal loop cannot re-discover) into the
    reactive-tech-lead pipeline. ``interval_minutes`` is the cadence;
    ``max_recovery_attempts`` bounds re-injection per issue before the sweep
    surfaces it as exhausted (needs human attention) instead of looping.
    ``enabled`` is False (off) by default. The default cadence is 4h: the sweep
    is a reconcile-for-strays BACKSTOP, not a hot path, and its scan is a broad
    exhaustive open-issue read — a slow interval keeps that off the frequent
    paths (stranded issues are quiescent, so a few hours of recovery latency is
    fine; lower it when faster reclamation is worth the extra scans).
    """

    enabled: bool = False
    interval_minutes: int = 240
    max_recovery_attempts: int = 3

    @classmethod
    def from_mapping(cls, data: dict) -> "StuckSweepConfig":
        """Parse the ``tech_lead.stuck_sweep`` YAML sub-dict."""
        return cls(
            enabled=bool(data.get("enabled", False)),
            interval_minutes=int(data.get("interval_minutes", 240)),
            max_recovery_attempts=int(data.get("max_recovery_attempts", 3)),
        )

    def startup_errors(self) -> list[str]:
        """Own-block invariants; the enabled-requires-tech-lead-agent cross-field
        check lives in the review validator (it reads other config sections)."""
        errors: list[str] = []
        if self.interval_minutes < 1:
            errors.append(
                "tech_lead.stuck_sweep.interval_minutes must be >= 1 — a zero (or "
                "negative) interval makes stuck_sweep_due true every tick, i.e. an "
                "unthrottled GitHub scan on every loop, which #6823 forbids; a "
                "cadence of 0 is meaningless, so set enabled: false to turn the "
                f"sweep off instead. Got {self.interval_minutes}"
            )
        if self.max_recovery_attempts < 1:
            errors.append(
                "tech_lead.stuck_sweep.max_recovery_attempts must be >= 1 "
                f"(bounds re-injection before escalation), got "
                f"{self.max_recovery_attempts}"
            )
        return errors


def _promote_mode(raw: Any) -> str:
    """Normalize ``tech_lead.findings.promote``, undoing YAML's boolean coercion.

    YAML 1.1 parses a bare ``off`` as the boolean ``False`` — so the documented
    disable value, written exactly as the ADR and the example config show it,
    arrived here as ``'False'``: not a valid mode, so startup rejected the one
    spelling operators are told to use. Map it back; ``True`` has no
    corresponding mode and is deliberately left to fail validation loudly.
    """
    if raw is False:
        return FINDING_PROMOTION_OFF
    return str(raw).strip()


@dataclass(frozen=True)
class PromotionRouteTarget:
    """One ``tech_lead.findings.route`` entry: a repo AND its queue contract.

    A promoted issue is only runnable in its target when it carries that
    target's scheduling labels, so a route entry declares them (#6957 review
    F2). Two YAML spellings:

    * a bare string — ``self`` or ``owner/repo``;
    * a mapping — ``{repo:, scope_label:, agent_label:}`` for a foreign target
      whose queue filters on labels this repo's config knows nothing about.

    ``None`` on ``scope_label``/``agent_label`` means "not declared, derive it";
    an explicit empty string means "this target has no such label". The
    derivation itself belongs to the route resolver in the promotion owner, not
    here — config models stay pure data.
    """

    repo: str
    scope_label: Optional[str] = None
    agent_label: Optional[str] = None

    @property
    def is_self(self) -> bool:
        return self.repo == PROMOTION_ROUTE_SELF

    @classmethod
    def from_value(cls, *, area: str, value: Any) -> "PromotionRouteTarget":
        """Parse one route value, rejecting every shape that is not a route."""
        if isinstance(value, str):
            return cls(repo=value.strip())
        if not isinstance(value, dict):
            raise ValueError(
                f"tech_lead.findings.route[{area!r}] must be 'self', 'owner/repo',"
                " or a mapping with a 'repo' key, got "
                f"{type(value).__name__}"
            )
        unknown = sorted(set(value) - {"repo", "scope_label", "agent_label"})
        if unknown:
            raise ValueError(
                f"tech_lead.findings.route[{area!r}] has unknown key(s)"
                f" {', '.join(unknown)}; supported keys are repo, scope_label,"
                " agent_label"
            )
        raw_repo = value.get("repo")
        if not isinstance(raw_repo, str) or not raw_repo.strip():
            raise ValueError(
                f"tech_lead.findings.route[{area!r}] requires a non-empty 'repo'"
                " ('self' or 'owner/repo')"
            )
        return cls(
            repo=raw_repo.strip(),
            scope_label=_optional_label(area=area, key="scope_label", value=value),
            agent_label=_optional_label(area=area, key="agent_label", value=value),
        )

    def to_event_dict(self) -> dict:
        return {
            "repo": self.repo,
            "scope_label": self.scope_label,
            "agent_label": self.agent_label,
        }


def _optional_label(*, area: str, key: str, value: dict) -> Optional[str]:
    """Parse an optional route label: absent -> None, present -> exact string."""
    if key not in value:
        return None
    raw = value[key]
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(
            f"tech_lead.findings.route[{area!r}].{key} must be a label string"
            f" (use '' for 'this target has none'), got {type(raw).__name__}"
        )
    return raw.strip()


@dataclass
class TechLeadFindingsConfig:
    """Finding-promotion lane settings (#6957): case file -> gated issue.

    ``promote`` is the master switch. ``off`` disables the whole lane (no
    promotion issues, no loop-closure reads); ``gated`` (the default) files the
    promotion carrying the ``proposed-tech-lead`` gate, so the operator's
    approval is exactly one action — removing the label; ``auto`` files it
    ungated, i.e. immediately runnable in the target repo's own pipeline.

    ``min_evidence`` is how many observations a signature must accrue before it
    is eligible, ``max_open_promoted`` bounds in-flight promoted issues PER
    TARGET REPO (storm backpressure: excess eligible signatures queue behind
    merges rather than flooding a repo), and ``route`` maps an area label to the
    :class:`PromotionRouteTarget` that owns the fix. ``route['default']`` is the
    catch-all; ``self`` means the managed repo itself.
    """

    promote: str = FINDING_PROMOTION_GATED
    min_evidence: int = 2
    max_open_promoted: int = 3
    route: dict[str, PromotionRouteTarget] = field(
        default_factory=lambda: {
            PROMOTION_ROUTE_DEFAULT_KEY: PromotionRouteTarget(repo=PROMOTION_ROUTE_SELF)
        }
    )

    @classmethod
    def from_mapping(cls, data: dict) -> "TechLeadFindingsConfig":
        """Parse the ``tech_lead.findings`` YAML sub-dict.

        The route table is validated STRICTLY by shape, not by truthiness: a
        falsy non-mapping (``route: []``, ``route: ""``, ``route: false``,
        ``route: 0``) is an explicitly supplied invalid value, and silently
        replacing it with ``default: self`` would redirect a cross-repo routing
        feature into the managed repo without a word (#6957 review F6). Only an
        absent key or an explicit ``null`` counts as omission.
        """
        raw_route = data.get("route", None)
        if raw_route is None:
            raw_route = {}
        if not isinstance(raw_route, dict):
            raise ValueError(
                "tech_lead.findings.route must be a mapping of area -> repo, got "
                f"{type(raw_route).__name__} ({raw_route!r}); remove the key or set"
                " it to null to accept the default 'default: self' route"
            )
        route: dict[str, PromotionRouteTarget] = {}
        folded_areas: set[str] = set()
        for raw_area, raw_target in raw_route.items():
            area = str(raw_area).strip()
            if not area:
                raise ValueError(
                    "tech_lead.findings.route keys must be non-empty area names"
                )
            folded = area.casefold()
            if folded in folded_areas:
                raise ValueError(
                    "tech_lead.findings.route contains duplicate case-insensitive"
                    f" area {area!r}"
                )
            folded_areas.add(folded)
            if folded == PROMOTION_ROUTE_DEFAULT_KEY:
                area = PROMOTION_ROUTE_DEFAULT_KEY
            target = PromotionRouteTarget.from_value(area=area, value=raw_target)
            if not target.repo:
                raise ValueError(
                    f"tech_lead.findings.route[{area!r}] must not be empty; use"
                    " 'self' or 'owner/repo'"
                )
            route[area] = target
        route.setdefault(
            PROMOTION_ROUTE_DEFAULT_KEY, PromotionRouteTarget(repo=PROMOTION_ROUTE_SELF)
        )
        return cls(
            promote=_promote_mode(data.get("promote", FINDING_PROMOTION_GATED)),
            min_evidence=int(data.get("min_evidence", 2)),
            max_open_promoted=int(data.get("max_open_promoted", 3)),
            route=route,
        )

    @property
    def enabled(self) -> bool:
        """False when ``promote: off`` — the lane makes no reads and no writes."""
        return self.promote != FINDING_PROMOTION_OFF

    @property
    def gated(self) -> bool:
        """True when promoted issues carry the operator-approval gate label."""
        return self.promote == FINDING_PROMOTION_GATED

    def route_for(self, area: str | None) -> PromotionRouteTarget:
        """The route entry that owns the fix for *area*.

        Area matching is case-insensitive because the area rides an ``area:*``
        GitHub label, and GitHub folds label names.
        """
        folded = (area or "").strip().casefold()
        if folded:
            for key, target in self.route.items():
                if key.casefold() == folded:
                    return target
        return self.route.get(
            PROMOTION_ROUTE_DEFAULT_KEY, PromotionRouteTarget(repo=PROMOTION_ROUTE_SELF)
        )

    def target_repos(self) -> tuple[str, ...]:
        """Distinct non-``self`` route repos, for startup writability checks."""
        seen: dict[str, str] = {}
        for target in self.route.values():
            if target.repo and not target.is_self:
                seen.setdefault(target.repo.casefold(), target.repo)
        return tuple(seen.values())

    def startup_errors(self) -> list[str]:
        """Fail-loud own-block validation (#6957 guardrail).

        A misconfigured route/threshold must fail startup, never degrade into a
        lane that silently never promotes (or promotes into the wrong repo).
        """
        errors: list[str] = []
        if self.promote not in VALID_FINDING_PROMOTION_MODES:
            errors.append(
                "tech_lead.findings.promote must be one of "
                f"{', '.join(VALID_FINDING_PROMOTION_MODES)}, got {self.promote!r}"
            )
        if self.min_evidence < 1:
            errors.append(
                "tech_lead.findings.min_evidence must be >= 1 (observations before "
                f"a signature is promotable), got {self.min_evidence}"
            )
        if self.max_open_promoted < 1:
            errors.append(
                "tech_lead.findings.max_open_promoted must be >= 1 (set promote: off "
                f"to disable the lane), got {self.max_open_promoted}"
            )
        for area, target in sorted(self.route.items()):
            if target.is_self:
                # A ``self`` route's queue contract is the managed repo's own
                # config (filtering.label + the follow-up worker agent).
                # Declaring it twice invites silent drift between the two, and
                # a promotion that carries a scope label the scheduler does not
                # query is invisible work.
                declared = [
                    key
                    for key, value in (
                        ("scope_label", target.scope_label),
                        ("agent_label", target.agent_label),
                    )
                    if value is not None
                ]
                if declared:
                    errors.append(
                        f"tech_lead.findings.route[{area!r}] routes to 'self', so"
                        f" it must not declare {', '.join(declared)} — the managed"
                        " repo's scheduling contract comes from filtering.label"
                        " and review.tech_lead_follow_up_agent"
                    )
                continue
            if target.repo.count("/") != 1 or not all(target.repo.split("/")):
                errors.append(
                    f"tech_lead.findings.route[{area!r}] must be 'self' or "
                    f"'owner/repo', got {target.repo!r}"
                )
            if target.agent_label == "":
                errors.append(
                    f"tech_lead.findings.route[{area!r}].agent_label must name the"
                    " target's worker agent label; an issue with no agent label is"
                    " never picked up by any pipeline"
                )
        return errors

    def to_event_dict(self) -> dict:
        return {
            "promote": self.promote,
            "min_evidence": self.min_evidence,
            "max_open_promoted": self.max_open_promoted,
            "route": {
                area: target.to_event_dict() for area, target in self.route.items()
            },
        }


# Upper bound on the expedite-lane cap (#6870). The single source of truth for
# BOTH the runtime config validation (TechLeadConfig.startup_errors) and the
# settings-form schema (settings_schema le=...), so the two layers can never
# accept/reject a value inconsistently.
TECH_LEAD_MAX_EXPEDITED_LIMIT = 20


@dataclass
class TechLeadConfig:
    """Tech Lead issue configuration.

    Controls whether new tech-lead work is admitted, how labels and milestones
    are assigned to orchestrator-created tech_lead issues, which tech_lead
    decision proposals the orchestrator executes versus surfaces (ADR-0031),
    and the periodic health-review trigger (ADR-0031 §4).

    ``enabled`` is optional only for backwards compatibility. ``None`` means
    the YAML key was omitted, so :attr:`Config.tech_lead_enabled` preserves the
    legacy rule (configured agent => enabled). A persisted true/false value is
    the authoritative master switch.
    """

    enabled: Optional[bool] = None

    # Labels to inherit from source issues (if any source issue has the label)
    inherit_labels: list[str] = field(default_factory=list)

    # Labels always applied to tech_lead issues
    explicit_labels: list[str] = field(default_factory=list)

    # Milestone assignment strategy
    milestone_strategy: MilestoneStrategyConfig = field(
        default_factory=MilestoneStrategyConfig
    )

    # Optional explicit priority label
    priority: Optional[str] = None

    # Reserved concurrency for tech_lead sessions. None (the default) = tech_lead
    # shares the worker budget (``max_concurrent_sessions``): tech_lead counts
    # against it and is planned from the shared capacity, exactly as before.
    # An int = a SEPARATE additive tech_lead budget: tech_lead sessions run from
    # their own ``tech_lead.max_concurrent`` slots and are NOT subtracted from
    # the worker ``max_concurrent_sessions``, so the tech lead can run even
    # when the worker budget is saturated. Total live agents are then bounded
    # at ``max_concurrent_sessions + tech_lead.max_concurrent``.
    max_concurrent: Optional[int] = None

    # Expedite lane cap (#6870). Bounds how many OUTSTANDING tech-lead-expedited
    # issues can sit at the front of the worker queue at once, so a noisy tech
    # lead cannot starve normal work. The default is small; 0 disables the lane
    # entirely (an expedite request then falls back to normal priority).
    max_expedited: int = 3

    # Per-action-type graduated authority for tech_lead decision proposals
    authority: TechLeadAuthorityConfig = field(default_factory=TechLeadAuthorityConfig)

    # Trusted open-issue corpus and lexical backstop for create_issue proposals
    dedup: TechLeadDedupConfig = field(default_factory=TechLeadDedupConfig)

    # Periodic health-review trigger (ADR-0031 §4)
    health_review: TechLeadHealthReviewConfig = field(
        default_factory=TechLeadHealthReviewConfig
    )

    # Tech-lead attention sweep for stuck issues (ADR-0031, #6823)
    stuck_sweep: StuckSweepConfig = field(default_factory=StuckSweepConfig)

    # Finding-promotion lane: pattern case file -> gated runnable issue (#6957)
    findings: TechLeadFindingsConfig = field(default_factory=TechLeadFindingsConfig)

    def to_event_dict(self, *, enabled: Optional[bool] = None) -> dict:
        """Serialized ``tech_lead`` section for config event payloads."""
        return {
            "enabled": self.enabled if enabled is None else enabled,
            "inherit_labels": list(self.inherit_labels),
            "explicit_labels": list(self.explicit_labels),
            "milestone_strategy": {
                "inherit_from_issues": self.milestone_strategy.inherit_from_issues,
                "explicit": self.milestone_strategy.explicit,
            },
            "priority": self.priority,
            "max_concurrent": self.max_concurrent,
            "max_expedited": self.max_expedited,
            "authority": self.authority.to_event_dict(),
            "dedup": {
                "enabled": self.dedup.enabled,
                "similarity_threshold": self.dedup.similarity_threshold,
            },
            "health_review": {
                "interval_minutes": self.health_review.interval_minutes,
                "storm_threshold": self.health_review.storm_threshold,
                "storm_window_minutes": self.health_review.storm_window_minutes,
            },
            "stuck_sweep": {
                "enabled": self.stuck_sweep.enabled,
                "interval_minutes": self.stuck_sweep.interval_minutes,
                "max_recovery_attempts": self.stuck_sweep.max_recovery_attempts,
            },
            "findings": self.findings.to_event_dict(),
        }

    def startup_errors(self) -> list[str]:
        """Own-block invariants for the ``tech_lead`` section (#6870).

        The documented disable value is exactly 0; a value outside
        ``0..TECH_LEAD_MAX_EXPEDITED_LIMIT`` is a misconfiguration that must fail
        startup loudly, never be silently treated as disabled (mirrors the
        health-review/stuck-sweep blocks). The upper bound is shared verbatim
        with the settings-form schema so both layers agree on the ceiling.
        """
        errors: list[str] = []
        if not 0 <= self.max_expedited <= TECH_LEAD_MAX_EXPEDITED_LIMIT:
            errors.append(
                "tech_lead.max_expedited must be between 0 and "
                f"{TECH_LEAD_MAX_EXPEDITED_LIMIT} (0 disables the expedite lane), "
                f"got {self.max_expedited}"
            )
        errors.extend(self.dedup.startup_errors())
        errors.extend(self.findings.startup_errors())
        return errors


class TechLeadActivationOwner:
    """Own the effective master-switch rule while retaining legacy configs."""

    tech_lead: TechLeadConfig
    tech_lead_review_agent: Optional[str]

    @property
    def tech_lead_explicitly_disabled(self) -> bool:
        """Whether the operator persisted the authoritative off choice."""
        return self.tech_lead.enabled is False

    @property
    def tech_lead_enabled(self) -> bool:
        """Admit new work only when enabled and backed by an agent."""
        return (
            bool(self.tech_lead_review_agent) and not self.tech_lead_explicitly_disabled
        )

    @tech_lead_enabled.setter
    def tech_lead_enabled(self, enabled: bool) -> None:
        """Persist the operator's explicit master-switch choice."""
        self.tech_lead.enabled = enabled

    def explicit_tech_lead_section(self) -> dict[str, dict[str, bool]]:
        """Serialize only an explicit choice, preserving legacy omission."""
        if self.tech_lead.enabled is None:
            return {}
        return {"tech_lead": {"enabled": self.tech_lead.enabled}}
