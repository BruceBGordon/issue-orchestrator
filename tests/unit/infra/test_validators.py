"""Unit tests for config validators.

Tests each validator in isolation with mock config objects.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from issue_orchestrator.infra.validators import (
    AgentValidator,
    IsolationValidator,
    ReviewWorkflowValidator,
    TemplateValidator,
    UnknownFieldsValidator,
    WorktreeValidator,
)


class TestWorktreeValidator:
    """Tests for WorktreeValidator."""

    def _make_config(self, tmp_path, worktree_base=None, recreate_mode="delete"):
        """Create a mock config with worktree settings."""
        config = MagicMock()
        config.worktree_base = worktree_base or tmp_path
        config.worktree_branch_on_recreate = recreate_mode
        config.worktree_remediation_pr_collision = "new_branch"
        config.worktree_base_branch_override = None
        return config

    def test_valid_config(self, tmp_path):
        """Verify no errors for valid worktree config."""
        config = self._make_config(tmp_path, worktree_base=tmp_path)
        errors = WorktreeValidator().validate(config)
        assert errors == []

    def test_relative_path_error(self, tmp_path):
        """Verify error for relative worktree base."""
        config = self._make_config(tmp_path, worktree_base=Path("relative/path"))
        errors = WorktreeValidator().validate(config)
        assert len(errors) == 1
        assert "must be absolute path" in errors[0]

    def test_nonexistent_path_error(self, tmp_path):
        """Verify error for nonexistent worktree base."""
        config = self._make_config(tmp_path, worktree_base=tmp_path / "nonexistent")
        errors = WorktreeValidator().validate(config)
        assert len(errors) == 1
        assert "does not exist" in errors[0]

    def test_file_not_dir_error(self, tmp_path):
        """Verify error when worktree base is a file, not directory."""
        file_path = tmp_path / "file.txt"
        file_path.touch()
        config = self._make_config(tmp_path, worktree_base=file_path)
        errors = WorktreeValidator().validate(config)
        assert len(errors) == 1
        assert "not a directory" in errors[0]

    def test_invalid_recreate_mode(self, tmp_path):
        """Verify error for invalid worktree_branch_on_recreate."""
        config = self._make_config(tmp_path, recreate_mode="invalid_mode")
        errors = WorktreeValidator().validate(config)
        assert len(errors) == 1
        assert "worktree_branch_on_recreate" in errors[0]

    def test_valid_recreate_modes(self, tmp_path):
        """Verify valid recreate modes are accepted."""
        for mode in ["delete", "create_new_branch"]:
            config = self._make_config(tmp_path, recreate_mode=mode)
            errors = WorktreeValidator().validate(config)
            assert errors == [], f"Mode '{mode}' should be valid"


class TestIsolationValidator:
    """Tests for IsolationValidator."""

    def _make_config(self, mode):
        """Create a mock config with isolation settings."""
        config = MagicMock()
        config.isolation = MagicMock()
        config.isolation.mode = mode
        return config

    def test_standard_mode_valid(self):
        """Verify standard isolation mode is valid."""
        config = self._make_config("standard")
        errors = IsolationValidator().validate(config)
        assert errors == []

    def test_hardened_mode_valid(self):
        """Verify hardened isolation mode is valid."""
        config = self._make_config("hardened")
        errors = IsolationValidator().validate(config)
        assert errors == []

    def test_invalid_mode_error(self):
        """Verify error for unknown isolation mode."""
        config = self._make_config("paranoid")
        errors = IsolationValidator().validate(config)
        assert len(errors) == 1
        assert "isolation.mode" in errors[0]


class TestReviewWorkflowValidator:
    """Tests for ReviewWorkflowValidator."""

    def _make_config(
        self,
        review_enabled=False,
        code_review_agent=None,
        tech_lead_review_agent=None,
        review_exchange_mode="via-draft-pr",
        review_exchange_probe_schedule="daily",
        review_exchange_probe_interval_days=1,
        review_exchange_max_rounds=10,
        review_exchange_max_no_progress=2,
        review_exchange_require_validation=True,
        agents=None,
        health_review_interval_minutes=0,
        tech_lead_follow_up_agent=None,
        tech_lead_enabled=None,
    ):
        """Create a mock config with review settings."""
        config = MagicMock()
        config.review_enabled = review_enabled
        config.code_review_agent = code_review_agent
        config.tech_lead_review_agent = tech_lead_review_agent
        config.tech_lead.enabled = tech_lead_enabled
        config.tech_lead_enabled = (
            bool(tech_lead_review_agent)
            if tech_lead_enabled is None
            else bool(tech_lead_enabled and tech_lead_review_agent)
        )
        # Explicit None default so the typed follow-up-agent invariant (#6779 R9)
        # is exercised deterministically (a bare MagicMock is truthy).
        config.tech_lead_follow_up_agent = tech_lead_follow_up_agent
        # Concrete int so the cross-field health-review invariant (#6776) is
        # exercised deterministically (a bare MagicMock compares > 0 truthy).
        config.tech_lead.health_review.interval_minutes = health_review_interval_minutes
        # Nit policy and retrospective rerun moved onto this validator (#6939 A2),
        # so a bare MagicMock now trips checks this fixture does not target.
        # Concrete valid defaults keep each test exercising its own invariant.
        config.review_nits_default_policy = "address"
        config.review_nits_by_agent = {}
        config.retrospective_review_enabled = False
        config.internal_review_enabled = False
        config.internal_review_max_rounds = 5
        config.internal_review_instructions = ".io/internal-review.md"
        # Concrete False so the stuck-sweep cross-field invariant (#6823) is off
        # by default here (a bare MagicMock attribute is truthy).
        config.tech_lead.stuck_sweep.enabled = False
        config.review_exchange_mode = review_exchange_mode
        config.review_exchange_probe_schedule = review_exchange_probe_schedule
        config.review_exchange_probe_interval_days = review_exchange_probe_interval_days
        config.review_exchange_max_rounds = review_exchange_max_rounds
        config.review_exchange_max_no_progress = review_exchange_max_no_progress
        config.review_exchange_require_validation = review_exchange_require_validation
        config.agents = agents or {}
        config.get_reviewer_for_agent = lambda _label: config.code_review_agent
        for agent in config.agents.values():
            agent.skip_review = False
        return config

    def test_reviews_disabled_valid(self):
        """Verify no errors when reviews are disabled."""
        config = self._make_config(review_enabled=False)
        errors = ReviewWorkflowValidator().validate(config)
        assert errors == []

    def test_reviews_enabled_no_reviewer_error(self):
        """Verify error when reviews enabled but no reviewer set."""
        config = self._make_config(review_enabled=True)
        errors = ReviewWorkflowValidator().validate(config)
        assert len(errors) == 1
        assert "no default reviewer" in errors[0]

    def test_exchange_mode_requires_ai_system(self):
        """Verify exchange modes pass when ai_system is already validated elsewhere."""
        agent = MagicMock()
        agent.ai_system = "claude-code"
        agent.command = "claude"
        config = self._make_config(
            review_enabled=True,
            code_review_agent="agent:reviewer",
            review_exchange_mode="via-mcp",
            agents={"agent:reviewer": agent},
        )
        errors = ReviewWorkflowValidator().validate(config)
        assert errors == []

    def test_reviews_enabled_invalid_reviewer_error(self):
        """Verify error when reviewer doesn't exist in agents."""
        config = self._make_config(
            review_enabled=True,
            code_review_agent="agent:reviewer",
            agents={"agent:developer": MagicMock()},
        )
        errors = ReviewWorkflowValidator().validate(config)
        assert len(errors) == 1
        assert "not found in agents" in errors[0]

    def test_reviews_enabled_valid_reviewer(self):
        """Verify no error when reviewer exists in agents."""
        config = self._make_config(
            review_enabled=True,
            code_review_agent="agent:reviewer",
            agents={"agent:reviewer": MagicMock()},
        )
        errors = ReviewWorkflowValidator().validate(config)
        assert errors == []

    def test_invalid_tech_lead_agent_error(self):
        """Verify error when tech lead agent doesn't exist."""
        config = self._make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_follow_up_agent="agent:developer",
            agents={"agent:developer": MagicMock()},
        )
        errors = ReviewWorkflowValidator().validate(config)
        assert len(errors) == 1
        assert "tech_lead_review_agent" in errors[0]

    def test_unknown_tech_lead_follow_up_agent_error(self):
        """R9: a follow-up worker naming an agent absent from `agents` is rejected."""
        config = self._make_config(
            tech_lead_follow_up_agent="agent:ghost",
            agents={"agent:developer": MagicMock()},
        )
        errors = ReviewWorkflowValidator().validate(config)
        assert len(errors) == 1
        assert "tech_lead_follow_up_agent" in errors[0]

    def test_valid_tech_lead_follow_up_agent_ok(self):
        """R9: a follow-up worker that names a real agent passes."""
        config = self._make_config(
            tech_lead_follow_up_agent="agent:developer",
            agents={"agent:developer": MagicMock()},
        )
        errors = ReviewWorkflowValidator().validate(config)
        assert errors == []

    def test_unset_tech_lead_follow_up_agent_ok_without_tech_lead_agent(self):
        """R14: unset is valid ONLY when no tech lead agent is configured — then a
        create_issue proposal can never be reached, so no destination is needed."""
        config = self._make_config(
            tech_lead_review_agent=None,
            tech_lead_follow_up_agent=None,
            agents={"agent:developer": MagicMock()},
        )
        errors = ReviewWorkflowValidator().validate(config)
        assert errors == []

    def test_tech_lead_agent_requires_follow_up_agent(self):
        """R14: a configured tech lead agent makes create_issue proposals reachable
        (execute-create or a gated propose-create), both of which route to
        tech_lead_follow_up_agent — so leaving it unset is a startup error rather
        than a latent post-session planning failure."""
        config = self._make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_follow_up_agent=None,
            agents={"agent:tech-lead": MagicMock(), "agent:developer": MagicMock()},
        )
        errors = ReviewWorkflowValidator().validate(config)
        assert any(
            "tech_lead_follow_up_agent is required" in e for e in errors
        ), errors

    def test_tech_lead_agent_with_follow_up_agent_ok(self):
        """R14: a tech lead agent plus a follow-up worker naming a real agent is the
        valid enabled pair — no create_issue routing failure lurking."""
        config = self._make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_follow_up_agent="agent:developer",
            agents={"agent:tech-lead": MagicMock(), "agent:developer": MagicMock()},
        )
        errors = ReviewWorkflowValidator().validate(config)
        assert not any("tech_lead_follow_up_agent" in e for e in errors), errors

    def test_explicitly_enabled_tech_lead_requires_agent(self):
        """The master switch fails fast when its runtime dependency is absent."""
        config = self._make_config(tech_lead_enabled=True)

        errors = ReviewWorkflowValidator().validate(config)

        assert any("tech_lead.enabled is true" in error for error in errors), errors

    def test_explicitly_disabled_tech_lead_preserves_dormant_settings(self):
        """Turning off the master switch does not require deleting sub-settings."""
        config = self._make_config(
            tech_lead_enabled=False,
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_follow_up_agent=None,
            health_review_interval_minutes=60,
            agents={},
        )
        config.tech_lead.stuck_sweep.enabled = True
        config.tech_lead_review_on_failure = False

        errors = ReviewWorkflowValidator().validate(config)

        assert not any("tech_lead_review_agent" in error for error in errors), errors
        assert not any("tech_lead_follow_up_agent" in error for error in errors), errors
        assert not any("no tech lead agent" in error for error in errors), errors
        assert not any("stuck_sweep.enabled" in error for error in errors), errors

    def test_negative_health_review_interval_error(self):
        """The validator surfaces the health-review interval invariant so a
        negative value fails startup, not silently disables (#6763 finding 8)."""
        from issue_orchestrator.infra.config_models import (
            TechLeadHealthReviewConfig,
        )

        config = self._make_config()
        config.tech_lead.authority.startup_errors.return_value = []
        config.tech_lead.health_review = TechLeadHealthReviewConfig(interval_minutes=-5)

        errors = ReviewWorkflowValidator().validate(config)

        assert any(
            "tech_lead.health_review.interval_minutes" in e for e in errors
        ), errors

    def test_positive_health_review_interval_without_agent_error(self):
        """Cross-field invariant (#6776): a positive interval with no tech_lead
        agent is a startup config error, not a silent disable."""
        config = self._make_config(
            tech_lead_review_agent=None, health_review_interval_minutes=60
        )
        errors = ReviewWorkflowValidator().validate(config)
        assert any("no tech lead agent is configured" in e for e in errors), errors

    def test_zero_interval_without_agent_ok(self):
        """0 + no agent is the documented disabled state — no invariant error."""
        config = self._make_config(
            tech_lead_review_agent=None, health_review_interval_minutes=0
        )
        errors = ReviewWorkflowValidator().validate(config)
        assert not any("no tech lead agent is configured" in e for e in errors), errors

    def test_positive_interval_with_agent_ok(self):
        """Positive interval + configured agent is the valid enabled pair."""
        config = self._make_config(
            tech_lead_review_agent="agent:tech-lead",
            agents={"agent:tech-lead": MagicMock()},
            health_review_interval_minutes=60,
        )
        errors = ReviewWorkflowValidator().validate(config)
        assert not any("no tech lead agent is configured" in e for e in errors), errors


class TestTemplateValidator:
    """Tests for TemplateValidator."""

    def _make_config(self, agents):
        """Create a mock config with agents."""
        config = MagicMock()
        config.agents = agents
        return config

    def _make_agent(self, initial_prompt="", command=""):
        """Create a mock agent config."""
        agent = MagicMock()
        agent.initial_prompt = initial_prompt
        agent.command = command
        return agent

    def test_valid_initial_prompt_vars(self):
        """Verify valid template variables in initial_prompt."""
        agent = self._make_agent(
            initial_prompt="Issue #{issue_number}: {issue_title} in {worktree}"
        )
        config = self._make_config({"agent:dev": agent})
        errors = TemplateValidator().validate(config)
        assert errors == []

    def test_invalid_initial_prompt_var_error(self):
        """Verify error for unknown variable in initial_prompt."""
        agent = self._make_agent(initial_prompt="Fix {unknown_var}")
        config = self._make_config({"agent:dev": agent})
        errors = TemplateValidator().validate(config)
        assert len(errors) == 1
        assert "unknown_var" in errors[0]
        assert "initial_prompt" in errors[0]

    def test_valid_command_vars(self):
        """Verify valid template variables in command."""
        agent = self._make_agent(command="claude -p {initial_prompt} --model {model}")
        config = self._make_config({"agent:dev": agent})
        errors = TemplateValidator().validate(config)
        assert errors == []

    def test_invalid_command_var_error(self):
        """Verify error for unknown variable in command."""
        agent = self._make_agent(command="run {bad_variable}")
        config = self._make_config({"agent:dev": agent})
        errors = TemplateValidator().validate(config)
        assert len(errors) == 1
        assert "bad_variable" in errors[0]
        assert "command" in errors[0]

    def test_multiple_invalid_vars(self):
        """Verify multiple invalid variables are reported."""
        agent = self._make_agent(
            initial_prompt="{foo} and {bar}",
            command="{baz}",
        )
        config = self._make_config({"agent:dev": agent})
        errors = TemplateValidator().validate(config)
        assert len(errors) == 2


class TestUnknownFieldsValidator:
    """Tests for UnknownFieldsValidator."""

    def _make_config(self, raw_data=None, raw_agents=None):
        """Create a mock config with raw YAML data."""
        config = MagicMock()
        config.raw_data = raw_data or {}
        config.raw_agents = raw_agents or {}
        return config

    def test_no_unknown_fields_valid(self):
        """Verify no errors when all fields are known."""
        config = self._make_config(
            raw_data={"repo": {}, "agents": {}},  # Known top-level fields
            raw_agents={},
        )

        errors = UnknownFieldsValidator().validate(config)
        assert errors == []

    def test_unknown_top_level_field_is_error(self):
        """Verify error for unknown top-level field."""
        config = self._make_config(
            raw_data={"repo": {}, "unknown_field": "value"},
        )
        errors = UnknownFieldsValidator().validate(config)
        assert len(errors) == 1
        assert "unknown_field" in errors[0]

    def test_removed_config_strict_field_is_error(self):
        """Unknown fields are always errors."""
        config = self._make_config(
            raw_data={"config": {"strict": False}},
        )
        errors = UnknownFieldsValidator().validate(config)
        assert errors == ["Unknown config field: 'config'"]

    def test_unknown_agent_field_is_error(self):
        """Verify error for unknown agent field."""
        config = self._make_config(
            raw_data={
                "agents": {
                    "agent:dev": {
                        "prompt": "prompt.md",
                        "unknown_agent_field": "value",
                    }
                }
            },
        )
        errors = UnknownFieldsValidator().validate(config)
        assert len(errors) == 1
        assert "agent:dev" in errors[0]
        assert "unknown_agent_field" in errors[0]

    def test_unknown_nested_e2e_field_is_error(self):
        """Misplaced filtering under e2e must not be silently ignored."""
        config = self._make_config(
            raw_data={
                "e2e": {
                    "enabled": True,
                    "filtering": {"label": "review-audit-358"},
                }
            },
        )

        errors = UnknownFieldsValidator().validate(config)

        assert errors == ["Unknown config field: 'e2e.filtering'"]

    def test_unknown_nested_worktree_remediation_field_is_error(self):
        """Nested validation applies beyond the e2e symptom path."""
        config = self._make_config(
            raw_data={
                "worktrees": {
                    "remediation": {
                        "pr_collision": "new_branch",
                        "bogus": True,
                    }
                }
            },
        )

        errors = UnknownFieldsValidator().validate(config)

        assert errors == ["Unknown config field: 'worktrees.remediation.bogus'"]

    def test_open_maps_allow_user_defined_keys(self):
        """Provider args and sort strategy config are intentionally open maps."""
        config = self._make_config(
            raw_data={
                "agents": {
                    "agent:dev": {
                        "prompt": "prompt.md",
                        "provider_args": {
                            "approval_mode": "full-auto",
                            "nested": {"custom": True},
                        },
                    }
                },
                "milestones": {
                    "sort_config": {
                        "pattern": "M(\\d+)",
                        "nested": {"custom": True},
                    }
                },
            },
        )

        errors = UnknownFieldsValidator().validate(config)

        assert errors == []


class TestAgentValidator:
    """Tests for AgentValidator."""

    def _make_config(
        self,
        agents=None,
        default_agent=None,
        raw_agents=None,
    ):
        """Create a mock config with agent settings."""
        config = MagicMock()
        config.agents = agents or {}
        config.default_agent = default_agent
        config.raw_agents = raw_agents or {}
        config.ai_systems_allowed = []
        config.repo_root = Path("/tmp")
        return config

    def _make_agent(
        self,
        prompt_path=None,
        provider=None,
        model="sonnet",
        reviewer=None,
        prompt_exists=True,
        ai_system: str | None = "claude-code",
        sandbox: bool = False,
    ):
        """Create a mock agent config."""
        agent = MagicMock()
        if prompt_path is not None:
            # Use real Path object
            agent.prompt_path = prompt_path
        else:
            # Use mock that can have exists() mocked
            mock_path = MagicMock()
            mock_path.exists.return_value = prompt_exists
            agent.prompt_path = mock_path
        agent.provider = provider
        agent.model = model
        agent.reviewer = reviewer
        agent.ai_system = ai_system
        # Explicit so a MagicMock default doesn't read as a truthy opt-in.
        agent.sandbox = sandbox
        return agent

    def test_no_agents_error(self):
        """Verify error when no agents configured."""
        config = self._make_config(agents={})
        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert len(errors) == 1
        assert "No agents configured" in errors[0]

    def test_valid_agent(self, tmp_path):
        """Verify no errors for valid agent config."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.touch()
        agent = self._make_agent(prompt_path=prompt_file, provider="claude-code")
        config = self._make_config(agents={"agent:dev": agent})

        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert errors == []

    def test_custom_command_sandbox_optin_rejected(self, tmp_path):
        """sandbox: true on a provider-less custom-command agent must be rejected."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.touch()
        agent = self._make_agent(prompt_path=prompt_file, provider=None, sandbox=True)
        config = self._make_config(
            agents={"agent:custom": agent},
            raw_agents={"agent:custom": {"command": "claude --permission-mode bypassPermissions"}},
        )
        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert any("sandbox: true is not supported" in e for e in errors), errors

    def test_provider_agent_sandbox_optin_allowed(self, tmp_path):
        """sandbox: true is fine with a provider adapter (claude-code)."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.touch()
        agent = self._make_agent(prompt_path=prompt_file, provider="claude-code", sandbox=True)
        config = self._make_config(agents={"agent:dev": agent})
        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert errors == []

    def test_missing_ai_system_error(self, tmp_path):
        """Verify error when ai_system is missing."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.touch()
        agent = self._make_agent(
            prompt_path=prompt_file,
            provider="claude-code",
            ai_system=None,
        )
        config = self._make_config(agents={"agent:dev": agent})

        with patch("agent_runner.is_valid_provider", return_value=True), \
             patch("agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert any("ai_system" in e for e in errors)

    def test_custom_ai_system_allowlist(self, tmp_path):
        """Verify allowlist accepts custom ai_system values."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.touch()
        agent = self._make_agent(
            prompt_path=prompt_file,
            provider="claude-code",
            ai_system="custom-ai",
        )
        config = self._make_config(agents={"agent:dev": agent})
        config.ai_systems_allowed = ["custom-ai"]

        with patch("agent_runner.is_valid_provider", return_value=True), \
             patch("agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert errors == []

    def test_missing_prompt_file_error(self):
        """Verify error when prompt file doesn't exist."""
        agent = self._make_agent(provider="claude-code", prompt_exists=False)
        config = self._make_config(agents={"agent:dev": agent})

        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert any("prompt file not found" in e for e in errors)

    def test_unknown_model_error(self):
        """Verify error for unknown Claude model."""
        agent = self._make_agent(model="gpt-4", provider="claude-code")
        config = self._make_config(agents={"agent:dev": agent})

        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert any("unknown model" in e for e in errors)

    def test_invalid_reviewer_reference_error(self):
        """Verify error when reviewer references non-existent agent."""
        agent = self._make_agent(provider="claude-code", reviewer="agent:nonexistent")
        config = self._make_config(agents={"agent:dev": agent})

        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert any("reviewer" in e and "not found" in e for e in errors)

    def test_no_provider_and_no_command_error(self):
        """Verify error when agent has no provider and no custom command."""
        agent = self._make_agent(provider=None)
        config = self._make_config(
            agents={"agent:dev": agent},
            raw_agents={"agent:dev": {}},  # No command override
        )

        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert any("no provider specified" in e for e in errors)

    def test_custom_command_bypasses_provider_check(self):
        """Verify no error when agent has custom command instead of provider."""
        agent = self._make_agent(provider=None, model="haiku")  # Valid model
        config = self._make_config(
            agents={"agent:dev": agent},
            raw_agents={"agent:dev": {"command": "custom-agent {prompt}"}},
        )

        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        # Should not have "no provider specified" error
        assert not any("no provider specified" in e for e in errors)
