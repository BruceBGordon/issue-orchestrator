"""Review workflow configuration validator."""

from pathlib import Path

from typing import TYPE_CHECKING

from .base import ConfigValidator
from ..config_value_rules import validate_review_nit_policy
from ..tech_lead_promotion_activation import promotion_lane_readiness

if TYPE_CHECKING:
    from ..config import Config


class ReviewWorkflowValidator(ConfigValidator):
    """Validates review workflow configuration.

    Checks:
    - If reviews enabled, default reviewer must be set
    - Default reviewer must exist in agents
    - Tech Lead review agent must exist in agents (if set)
    - A configured tech lead agent requires tech_lead_follow_up_agent (#6779 R14)
    - tech_lead_follow_up_agent, when set, must name a real agent (#6779 R9)
    - Tech Lead authority modes are valid; act-level 'execute' rejected (#6764)
    - Tech Lead health-review interval is non-negative (0 = disabled, #6763)
    - A positive health-review interval requires a tech lead agent (#6776)
    - An ACTIVE finding-promotion lane has every dependency its routes need
      (#6957); the activation predicate itself lives in one owner
    """

    def validate(self, config: "Config") -> list[str]:
        errors: list[str] = []

        self._validate_review_defaults(config, errors)
        self._validate_tech_lead_switch(config, errors)
        self._validate_tech_lead_agent(config, errors)
        self._validate_tech_lead_follow_up_agent(config, errors)
        # Graduated tech_lead authority (ADR-0031): act-level 'execute' is a
        # startup configuration error until its executor is wired (#6764).
        errors.extend(config.tech_lead.authority.startup_errors())
        # Periodic health review (ADR-0031 §4): a negative interval is a
        # startup configuration error, never silently treated as disabled.
        errors.extend(config.tech_lead.health_review.startup_errors())
        self._validate_health_review_requires_agent(config, errors)
        # Tech-lead attention sweep (ADR-0031, #6823): own-block invariants plus
        # the cross-field "enabled requires a tech lead agent" check.
        errors.extend(config.tech_lead.stuck_sweep.startup_errors())
        self._validate_stuck_sweep_requires_agent(config, errors)
        # Expedite lane (#6870): the cap must be in range
        # 0..TECH_LEAD_MAX_EXPEDITED_LIMIT (0 disables); any out-of-range value
        # is a startup error, enforced by TechLeadConfig.startup_errors so the
        # settings-form bound (le=...) and startup agree.
        errors.extend(config.tech_lead.startup_errors())
        # Finding promotion (#6957): the lane's ACTIVATION and its remaining
        # dependencies come from one owner, the same one doctor, fact
        # gathering, and route resolution consume — so a config can never
        # pass validation and then fail on a tick (round-2 review F9).
        errors.extend(promotion_lane_readiness(config).problems)

        exchange_mode = config.review_exchange_mode
        self._validate_exchange_mode(exchange_mode, config, errors)
        self._validate_probe_schedule(config, errors)
        # Nit policy and retrospective rerun settings are review-workflow
        # concerns; they lived on Config only for historical reasons (#6939 A2).
        errors.extend(
            validate_review_nit_policy(
                config.review_nits_default_policy, config.review_nits_by_agent
            )
        )
        self._validate_internal_review(config, errors)
        self._validate_retrospective_review(config, errors)
        # Pair validation is deferred to runtime when the actual coder agent is known.

        return errors

    @staticmethod
    def _validate_internal_review(config: "Config", errors: list[str]) -> None:
        """Validate the bounded, repository-relative internal-review policy."""
        if not isinstance(config.internal_review_enabled, bool):
            errors.append("review.internal.enabled must be a boolean.")
        max_rounds = config.internal_review_max_rounds
        if not isinstance(max_rounds, int) or isinstance(max_rounds, bool):
            errors.append("review.internal.max_rounds must be an integer.")
        elif not 1 <= max_rounds <= 50:
            errors.append("review.internal.max_rounds must be between 1 and 50.")
        raw_instructions = config.internal_review_instructions
        if not isinstance(raw_instructions, str):
            errors.append("review.internal.instructions must be a string.")
            return
        instructions = raw_instructions.strip()
        if not instructions:
            errors.append("review.internal.instructions must be non-empty.")
            return
        configured_path = Path(instructions)
        if configured_path.is_absolute() or ".." in configured_path.parts:
            errors.append(
                "review.internal.instructions must be a repository-relative path "
                "that stays inside the repository root."
            )

    def _validate_retrospective_review(self, config: "Config", errors: list[str]) -> None:
        """Validate review-first existing-implementation rerun settings."""
        if not config.retrospective_review_enabled:
            return
        if not config.code_review_agent:
            errors.append("review.retrospective.enabled requires review.default to be configured")
        elif config.code_review_agent not in config.agents:
            errors.append(
                f"review.default '{config.code_review_agent}' not found in agents for"
                f" retrospective review. Available: {list(config.agents.keys())}"
            )
        for attr, yaml_path in (
            (config.retrospective_review_trigger_label, "review.retrospective.trigger_label"),
            (config.retrospective_reviewed_label, "review.retrospective.reviewed_label"),
            (
                config.retrospective_changes_requested_label,
                "review.retrospective.changes_requested_label",
            ),
        ):
            if not str(attr or "").strip():
                errors.append(
                    f"{yaml_path} must be non-empty when retrospective review is enabled"
                )

    def _validate_review_defaults(self, config: "Config", errors: list[str]) -> None:
        if not config.review_enabled:
            return
        if not config.code_review_agent:
            errors.append(
                "review.enabled is true but no default reviewer set. "
                "Add 'review: default: agent:reviewer' to config."
            )
            return
        if config.code_review_agent not in config.agents:
            errors.append(
                f"review.default '{config.code_review_agent}' not found in agents. "
                f"Available: {list(config.agents.keys())}"
            )

    def _validate_tech_lead_agent(self, config: "Config", errors: list[str]) -> None:
        if config.tech_lead.enabled is False:
            return
        if not config.tech_lead_review_agent:
            return
        if config.tech_lead_review_agent not in config.agents:
            errors.append(
                f"tech_lead_review_agent '{config.tech_lead_review_agent}' not found in agents. "
                f"Available: {list(config.agents.keys())}"
            )

    def _validate_tech_lead_follow_up_agent(
        self, config: "Config", errors: list[str]
    ) -> None:
        # Typed destination for tech_lead create_issue proposals (#6779 R9/R14).
        #
        # A configured tech lead agent makes create_issue proposals REACHABLE:
        # both execute-authority (direct create) and propose-authority (a gated
        # proposal issue that creates on approval) route the new issue to
        # review.tech_lead_follow_up_agent (see tech_lead_follow_up_agent_label). Left
        # unset, that routing RAISES at post-session planning time — a latent
        # failure. So it is REQUIRED whenever a tech lead agent is configured
        # (#6779 R14), and when set it MUST name a real agent so routing can
        # never fall back to dict order and hand new work to a
        # reviewer/tech_lead/goal-pilot agent (#6779 R9).
        if config.tech_lead.enabled is False:
            return
        if not config.tech_lead_follow_up_agent:
            if config.tech_lead_review_agent:
                errors.append(
                    "review.tech_lead_follow_up_agent is required when a tech_lead"
                    " agent is configured: a tech_lead create_issue proposal routes"
                    " the new issue to it, and leaving it unset fails at"
                    " post-session planning. Set it to a worker agent in `agents`"
                    f" (available: {list(config.agents.keys())}) (#6779 R14)"
                )
            return
        if config.tech_lead_follow_up_agent not in config.agents:
            errors.append(
                f"review.tech_lead_follow_up_agent '{config.tech_lead_follow_up_agent}' "
                f"not found in agents. Available: {list(config.agents.keys())}"
            )

    def _validate_health_review_requires_agent(
        self, config: "Config", errors: list[str]
    ) -> None:
        # Cross-field invariant (#6776): a positive health-review interval with
        # no tech lead agent is silently disabled at runtime
        # (health_review_interval_minutes() returns 0). Reject the pair so the
        # misconfiguration fails loudly rather than degrading; 0/absent is the
        # documented disable value and a positive interval needs an agent.
        if config.tech_lead.enabled is False:
            return
        interval = config.tech_lead.health_review.interval_minutes
        if interval > 0 and not config.tech_lead_review_agent:
            errors.append(
                f"tech_lead.health_review.interval_minutes is {interval} but no "
                "tech lead agent is configured. Set review.tech_lead_review_agent, or "
                "use 0 to disable the periodic health review."
            )

    def _validate_stuck_sweep_requires_agent(
        self, config: "Config", errors: list[str]
    ) -> None:
        # Cross-field invariant (#6823): the sweep re-injects stuck issues into
        # the reactive-tech-lead pipeline, so an enabled sweep with no tech lead agent
        # (or tech-lead-on-failure off) is silently inert at runtime. Reject the
        # pair so the misconfiguration fails loudly instead of degrading.
        if (
            config.tech_lead.enabled is False
            or not config.tech_lead.stuck_sweep.enabled
        ):
            return
        if not config.tech_lead_review_agent:
            errors.append(
                "tech_lead.stuck_sweep.enabled is true but no tech lead agent is "
                "configured. Set review.tech_lead_review_agent, or disable the "
                "stuck sweep."
            )
        if not config.tech_lead_review_on_failure:
            errors.append(
                "tech_lead.stuck_sweep.enabled is true but "
                "review.tech_lead_review_on_failure is false; the sweep feeds the "
                "reactive tech-lead-on-failure pipeline and would be inert. Enable "
                "tech_lead_review_on_failure, or disable the stuck sweep."
            )

    @staticmethod
    def _validate_tech_lead_switch(config: "Config", errors: list[str]) -> None:
        """An explicitly enabled workflow must declare its agent dependency.

        Omission is the backwards-compatible legacy mode, where no agent means
        disabled. Explicit ``false`` is always an escape hatch from dormant
        cross-field dependencies without deleting their configured values.
        """
        if config.tech_lead.enabled is True and not config.tech_lead_review_agent:
            errors.append(
                "tech_lead.enabled is true but review.tech_lead_review_agent is "
                "not configured. Configure the agent, or set tech_lead.enabled: false."
            )

    def _validate_exchange_mode(
        self,
        exchange_mode: str,
        config: "Config",
        errors: list[str],
    ) -> None:
        allowed_modes = {"via-draft-pr", "via-mcp", "via-local-loop", "auto"}
        if exchange_mode not in allowed_modes:
            errors.append(
                f"review.exchange.mode '{exchange_mode}' is invalid. "
                f"Allowed: {sorted(allowed_modes)}"
            )

    def _validate_probe_schedule(self, config: "Config", errors: list[str]) -> None:
        schedule = config.review_exchange_probe_schedule
        allowed_schedules = {"startup", "daily", "interval", "manual"}
        if schedule not in allowed_schedules:
            errors.append(
                f"review.exchange.probe.schedule '{schedule}' is invalid. "
                f"Allowed: {sorted(allowed_schedules)}"
            )
        if schedule == "interval" and config.review_exchange_probe_interval_days < 1:
            errors.append(
                "review.exchange.probe.interval_days must be >= 1 when schedule=interval."
            )

        if config.review_exchange_max_rounds < 1:
            errors.append("review.exchange.loop.max_rounds must be >= 1.")
        if config.review_exchange_max_no_progress < 1:
            errors.append("review.exchange.loop.max_no_progress must be >= 1.")

    def _validate_supported_exchange_pair(
        self,
        exchange_mode: str,
        config: "Config",
        errors: list[str],
    ) -> None:
        if exchange_mode != "via-mcp" or not config.review_enabled:
            return
        from ..review_exchange_registry import SUPPORTED_MCP_PAIRS
        if not config.code_review_agent:
            errors.append(
                "review.exchange.mode is via-mcp but review.default is not set."
            )
            return

        pairs = self._collect_exchange_pairs(config)
        if not pairs:
            return

        unsupported_pairs = self._unsupported_exchange_pairs(
            pairs,
            config,
            SUPPORTED_MCP_PAIRS,
        )
        if unsupported_pairs:
            errors.append(
                "review.exchange.mode is via-mcp but unsupported ai_system pair(s) configured: "
                f"{unsupported_pairs}. Use via-local-loop or update the MCP allowlist."
            )

    @staticmethod
    def _collect_exchange_pairs(config: "Config") -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for label, agent in config.agents.items():
            if config.tech_lead_review_agent and label == config.tech_lead_review_agent:
                continue
            if agent.skip_review:
                continue
            reviewer_label = config.get_reviewer_for_agent(label)
            if not reviewer_label or reviewer_label not in config.agents:
                continue
            pairs.append((label, reviewer_label))
        return pairs

    @staticmethod
    def _unsupported_exchange_pairs(
        pairs: list[tuple[str, str]],
        config: "Config",
        supported_pairs,
    ) -> list[str]:
        unsupported_pairs = []
        for coder_label, reviewer_label in pairs:
            coder_system = config.agents[coder_label].ai_system
            reviewer_system = config.agents[reviewer_label].ai_system
            if (coder_system, reviewer_system) not in supported_pairs:
                unsupported_pairs.append(
                    f"{coder_label}->{reviewer_label} ({coder_system}->{reviewer_system})"
                )
        return unsupported_pairs
