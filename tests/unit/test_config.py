"""Unit tests for configuration loading and management."""

import pytest
import yaml
from pathlib import Path
from issue_orchestrator.infra.config import Config, ConfigSectionError
from issue_orchestrator.infra.config_models import PromotionRouteTarget


class TestConfig:
    """Test the Config class."""

    @pytest.mark.parametrize(
        "config_path",
        [
            "examples/config.example.yaml",
            ".issue-orchestrator/config/maintenance/hooks-validate.yaml",
            ".issue-orchestrator/config/modes/default/main.yaml",
            ".issue-orchestrator/config/modes/default/z-codespaces.yaml",
        ],
    )
    def test_shipped_configs_validate_clean(self, tmp_path, config_path):
        """Existing shipped configs must continue to load and validate."""
        repo_root = Path(__file__).resolve().parents[2]
        path = repo_root / config_path
        if config_path.startswith("examples/"):
            example_data = yaml.safe_load(path.read_text(encoding="utf-8"))
            for agent_data in example_data.get("agents", {}).values():
                prompt = agent_data.get("prompt")
                if isinstance(prompt, str):
                    prompt_path = tmp_path / prompt
                    prompt_path.parent.mkdir(parents=True, exist_ok=True)
                    prompt_path.write_text("Prompt\n", encoding="utf-8")
            installed_path = (
                tmp_path / ".issue-orchestrator" / "config" / "default.yaml"
            )
            installed_path.parent.mkdir(parents=True, exist_ok=True)
            installed_path.write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            path = installed_path

        config = Config.load(path)

        assert config.validate() == [], (
            f"{config_path} failed validation; schema likely missing fields"
        )

    def test_merge_queue_defaults_off(self):
        """Merge queue is optional and disabled by default."""
        config = Config()
        assert config.merge_queue.enabled is False
        assert config.merge_queue.provider == "github"
        assert config.merge_queue.enqueue_after == "code-reviewed"
        assert config.merge_queue.failure_action == "rework"

    def test_merge_queue_parses_from_yaml(self, tmp_path):
        """A merge_queue section is parsed onto Config."""
        path = tmp_path / ".issue-orchestrator" / "config" / "default.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "repo:\n  name: owner/repo\n"
            "merge_queue:\n"
            "  enabled: true\n"
            "  failure_action: needs_human\n",
            encoding="utf-8",
        )
        config = Config.load(path)
        assert config.merge_queue.enabled is True
        assert config.merge_queue.failure_action == "needs_human"
        # Round-trips through the event/serialization views.
        assert config.to_event_dict()["merge_queue"]["failure_action"] == "needs_human"
        assert config.to_dict()["merge_queue"]["enabled"] is True

    def test_agent_sandbox_optin_round_trips_and_is_not_inherited(self, tmp_path):
        """Per-agent ``sandbox`` (ADR-0034) parses, serializes, and is NOT inherited.

        Unlike ``provider``/``model``/``provider_args`` (which DO inherit from
        ``default_agent``), the ``sandbox`` security opt-in is strictly
        per-agent: an agent that omits it stays ``False`` even when
        ``default_agent`` (wrongly) sets ``sandbox: true``.
        """
        path = tmp_path / ".issue-orchestrator" / "config" / "default.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "repo:\n  name: owner/repo\n"
            "default_agent:\n"
            "  provider: claude-code\n"
            "  sandbox: true\n"  # must NOT leak into agents that don't opt in
            "agents:\n"
            '  "agent:sandboxed":\n'
            "    prompt: .prompts/a.md\n"
            "    sandbox: true\n"
            '  "agent:plain":\n'
            "    prompt: .prompts/b.md\n",
            encoding="utf-8",
        )
        config = Config.load(path)

        # Opt-in parses; the other agent stays False despite default_agent.
        assert config.agents["agent:sandboxed"].sandbox is True
        assert config.agents["agent:plain"].sandbox is False
        # provider DID inherit — proving default_agent inheritance is live and
        # that sandbox is deliberately excluded from it.
        assert config.agents["agent:plain"].provider == "claude-code"

        # Serialize: only the opted-in agent carries the flag (default off is
        # omitted), and re-parsing preserves both sides.
        serialized = config.to_dict()
        assert serialized["agents"]["agent:sandboxed"]["sandbox"] is True
        assert "sandbox" not in serialized["agents"]["agent:plain"]

        reparsed_path = tmp_path / "reparsed.yaml"
        reparsed_path.write_text(yaml.safe_dump(serialized), encoding="utf-8")
        reparsed = Config.load(reparsed_path)
        assert reparsed.agents["agent:sandboxed"].sandbox is True
        assert reparsed.agents["agent:plain"].sandbox is False

    def test_merge_queue_rejects_unknown_failure_action(self, tmp_path):
        """A typo in an enum field fails loud at load time."""
        path = tmp_path / ".issue-orchestrator" / "config" / "default.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "repo:\n  name: owner/repo\nmerge_queue:\n  failure_action: explode\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="merge_queue.failure_action"):
            Config.load(path)

    def test_config_creation(self):
        """Test basic Config creation with defaults."""
        config = Config()

        assert config.agents == {}
        assert config.max_concurrent_sessions == 3
        assert config.session_timeout_minutes == 45
        assert config.label_in_progress == "in-progress"
        assert config.label_blocked == "blocked"
        assert config.label_needs_human == "needs-human"
        assert config.repo is None
        assert config.web_port == 0
        assert config.control_api_port == 0
        assert config.session_interactions.enabled is False

    def test_github_auth_kwargs(self):
        """GitHub auth helper exposes the repo-scoped auth settings."""
        config = Config(
            github_token="tok",
            github_token_env="TIXMEUP_GITHUB_TOKEN",
            github_keyring_service="tixmeup-github",
            github_keyring_username="bruce",
        )

        assert config.github_auth_kwargs() == {
            "configured_token": "tok",
            "configured_env": "TIXMEUP_GITHUB_TOKEN",
            "configured_keyring_service": "tixmeup-github",
            "configured_keyring_username": "bruce",
            "configured_app_client_id": None,
            "configured_app_id": None,
            "configured_app_installation_id": None,
            "configured_app_private_key_path": None,
            "configured_app_private_key_env": None,
        }

    def test_config_load_ui_ports_default_to_auto_assign(self, tmp_path):
        """Missing ui port config should preserve 0=auto-assign defaults."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
ui:
  mode: web
""")

        config = Config.load(config_file)

        assert config.web_port == 0
        assert config.control_api_port == 0

    def test_config_load_rejects_agent_level_permission_mode(self, tmp_path):
        """The top-level agent permission_mode spelling is not supported;
        the error points at provider_args.permission_mode."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
    permission_mode: bypassPermissions
worktrees:
  base: {worktree_base}
""")

        with pytest.raises(
            ValueError,
            match=r"agents\.agent:web\.permission_mode is not supported.*provider_args\.permission_mode",
        ):
            Config.load(config_file)

    def test_repo_owned_configs_load(self):
        """Every checked-in config (orchestrator + E2E fixtures) must parse.

        Guards against config-contract changes (like the agent-level
        permission_mode rejection) silently breaking repo-owned YAML."""
        repo_root = Path(__file__).resolve().parents[2]
        config_paths = sorted(
            list((repo_root / ".issue-orchestrator" / "config").glob("*.yaml"))
            + list((repo_root / "tests" / "e2e" / "configs").glob("*.yaml"))
        )
        assert config_paths, "expected repo-owned configs to exist"
        for path in config_paths:
            Config.load(path)

    def test_config_load_codex_agent_without_model_uses_provider_default(
        self, tmp_path
    ):
        """Codex agents without a model should not inherit Claude's sonnet fallback."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:dev:
    prompt: {prompt}
    provider: codex
    ai_system: codex
worktrees:
  base: {worktree_base}
""")

        config = Config.load(config_file)

        assert config.agents["agent:dev"].provider == "codex"
        assert config.agents["agent:dev"].model == ""

    def test_config_load_default_agent_codex_without_model_uses_provider_default(
        self, tmp_path
    ):
        """Default-agent Codex configs should also preserve the provider CLI default model."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
default_agent:
  provider: codex

agents:
  agent:dev:
    prompt: {prompt}
    ai_system: codex
worktrees:
  base: {worktree_base}
""")

        config = Config.load(config_file)

        assert config.agents["agent:dev"].provider == "codex"
        assert config.agents["agent:dev"].model == ""

    def test_config_load_from_yaml(self, mock_config_yaml, tmp_path):
        """Test loading config from YAML file."""
        # Create temporary prompt files
        prompt_web = tmp_path / "web_prompt.txt"
        prompt_web.write_text("Web prompt content")

        prompt_mobile = tmp_path / "mobile_prompt.txt"
        prompt_mobile.write_text("Mobile prompt content")

        # Create config YAML with absolute paths
        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {tmp_path}

agents:
  agent:web:
    prompt: {prompt_web}
    model: sonnet
    timeout_minutes: 45
  agent:mobile:
    prompt: {prompt_mobile}
    model: haiku
    timeout_minutes: 60

execution:
  concurrency:
    max_concurrent_sessions: 4
    session_timeout_minutes: 60

labels:
  in_progress: working
  blocked: blocked-on
  needs_human: needs-review
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Check agents
        assert len(config.agents) == 2
        assert "agent:web" in config.agents
        assert "agent:mobile" in config.agents

        web_config = config.agents["agent:web"]
        assert web_config.model == "sonnet"
        assert web_config.timeout_minutes == 45
        assert web_config.prompt_path == prompt_web

        mobile_config = config.agents["agent:mobile"]
        assert mobile_config.model == "haiku"
        assert mobile_config.timeout_minutes == 60
        assert mobile_config.prompt_path == prompt_mobile

        # Check concurrency settings
        assert config.max_concurrent_sessions == 4
        assert config.session_timeout_minutes == 60

    def test_worktree_branch_on_recreate_default(self, tmp_path):
        """Default worktree_branch_on_recreate should be delete."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
""")

        config = Config.load(config_file)

        assert config.worktree_branch_on_recreate == "delete"
        assert config.worktree_base_branch_override is None

    def test_worktree_base_branch_override_configured_main(self, tmp_path):
        """Config can set worktrees.base_branch_override."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
  base_branch_override: main
""")

        config = Config.load(config_file)

        assert config.worktree_base_branch_override == "main"

    def test_worktree_base_branch_override_default(self, tmp_path):
        """Default worktree_base_branch_override should be unset."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
""")

        config = Config.load(config_file)

        assert config.worktree_base_branch_override is None

    def test_worktree_seed_ref_configured(self, tmp_path):
        """Config can set worktrees.seed_ref."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
  seed_ref: HEAD
""")

        config = Config.load(config_file)

        assert config.worktree_seed_ref == "HEAD"

    def test_worktree_seed_ref_default(self, tmp_path):
        """Default worktree_seed_ref should be unset."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
""")

        config = Config.load(config_file)

        assert config.worktree_seed_ref is None

    def test_example_config_e2e_section_shows_pytest_junit_contract(self):
        """The checked-in example should show the migrated pytest/JUnit shape."""
        example_path = (
            Path(__file__).resolve().parents[2] / "examples" / "config.example.yaml"
        )
        data = yaml.safe_load(example_path.read_text(encoding="utf-8"))

        e2e = data["e2e"]
        assert e2e["runner_kind"] == "pytest"
        assert (
            "--junitxml=.issue-orchestrator/e2e-results/pytest-junit.xml"
            in e2e["pytest_args"]
        )
        assert e2e["junit_xml_paths"] == [
            ".issue-orchestrator/e2e-results/pytest-junit.xml"
        ]

    def test_codespaces_config_e2e_section_shows_pytest_junit_contract(self):
        """Codespaces config should emit and ingest JUnit explicitly too."""
        codespaces_path = (
            Path(__file__).resolve().parents[2]
            / ".issue-orchestrator"
            / "config"
            / "modes"
            / "default"
            / "z-codespaces.yaml"
        )
        data = yaml.safe_load(codespaces_path.read_text(encoding="utf-8"))

        e2e = data["e2e"]
        assert e2e["runner_kind"] == "pytest"
        assert (
            "--junitxml=.issue-orchestrator/e2e-results/issue-orchestrator-e2e.xml"
            in e2e["pytest_args"]
        )
        assert e2e["junit_xml_paths"] == [
            ".issue-orchestrator/e2e-results/issue-orchestrator-e2e.xml"
        ]

    def test_validate_rejects_filtering_misnested_under_e2e(self, tmp_path):
        """A YAML indentation mistake under e2e must fail validation."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
e2e:
  enabled: true
  filtering:
    label: review-audit-358
""")

        config = Config.load(config_file)

        assert config.filtering.label is None
        assert any("e2e.filtering" in error for error in config.validate())

    def test_worktree_branch_on_recreate_configured(self, tmp_path):
        """Config can set worktree_branch_on_recreate."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
  worktree_branch_on_recreate: create_new_branch
""")

        config = Config.load(config_file)

        assert config.worktree_branch_on_recreate == "create_new_branch"

    def test_worktree_base_branch_override_configured_master(self, tmp_path):
        """Config can set worktree_base_branch_override."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
  base_branch_override: master
""")

        config = Config.load(config_file)

        assert config.worktree_base_branch_override == "master"

    def test_worktree_branch_on_recreate_invalid(self, tmp_path):
        """Invalid worktree_branch_on_recreate value should fail validation."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
    ai_system: claude-code
worktrees:
  base: {worktree_base}
  worktree_branch_on_recreate: nope
""")

        config = Config.load(config_file)

        errors = config.validate()
        assert any("worktree_branch_on_recreate" in err for err in errors)

    def test_worktree_base_branch_override_invalid(self, tmp_path):
        """Invalid worktree_base_branch_override values should fail validation."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
  base_branch_override: origin/main
""")

        config = Config.load(config_file)
        errors = config.validate()
        assert any("worktrees.base_branch_override" in err for err in errors)

    def test_allow_no_verify_dry_run_preflight_default(self, tmp_path):
        """Default allow_no_verify_dry_run_preflight should be True."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
""")

        config = Config.load(config_file)

        assert config.allow_no_verify_dry_run_preflight is True

    def test_allow_no_verify_dry_run_preflight_configured(self, tmp_path):
        """Config can disable allow_no_verify_dry_run_preflight."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
  allow_no_verify_dry_run_preflight: false
""")

        config = Config.load(config_file)

        assert config.allow_no_verify_dry_run_preflight is False

    def test_config_load_with_defaults(self, tmp_path):
        """Test loading config with minimal YAML uses defaults."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:simple:
    prompt: /path/to/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Check that defaults were applied
        simple_config = config.agents["agent:simple"]
        assert simple_config.model == "sonnet"
        assert simple_config.timeout_minutes == 45

        # Global defaults
        assert config.max_concurrent_sessions == 3
        assert config.session_timeout_minutes == 45
        assert config.label_in_progress == "in-progress"
        assert config.label_blocked == "blocked"
        assert config.label_needs_human == "needs-human"
        assert config.milestone_sort == "milestone_number"

    def test_config_load_file_not_found(self, tmp_path):
        """Test that FileNotFoundError is raised for missing config."""
        config_file = tmp_path / "nonexistent.yaml"

        with pytest.raises(FileNotFoundError):
            Config.load(config_file)

    def test_config_load_invalid_yaml(self, tmp_path):
        """Test that invalid YAML raises an error."""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text("{ invalid yaml [ : }")

        with pytest.raises(Exception):  # YAML parse error
            Config.load(config_file)

    def test_config_empty_agents(self, tmp_path):
        """Test loading config with no agents defined."""
        config_content = """
execution:
  concurrency:
    max_concurrent_sessions: 2
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.agents == {}
        assert config.max_concurrent_sessions == 2

    def test_config_find_and_load_current_dir(self, tmp_path, monkeypatch):
        """Test finding config in current directory."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        # Config is now in .issue-orchestrator/config/default.yaml
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
        config_file.write_text(config_content)

        monkeypatch.chdir(tmp_path)

        config = Config.find_and_load()

        assert "agent:test" in config.agents

    def test_config_find_and_load_parent_dir(self, tmp_path, monkeypatch):
        """Test finding config in parent directory."""
        config_content = """
agents:
  agent:parent:
    prompt: /tmp/prompt.txt
"""
        # Config is now in .issue-orchestrator/config/default.yaml
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
        config_file.write_text(config_content)

        # Create a subdirectory
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        monkeypatch.chdir(subdir)

        config = Config.find_and_load()

        assert "agent:parent" in config.agents

    def test_config_find_and_load_in_hidden_dir(self, tmp_path, monkeypatch):
        """Test finding config in .issue-orchestrator/config subdirectory."""
        # Config is now in .issue-orchestrator/config/default.yaml
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)

        config_content = """
agents:
  agent:hidden:
    prompt: /tmp/prompt.txt
"""
        config_file = config_dir / "default.yaml"
        config_file.write_text(config_content)

        monkeypatch.chdir(tmp_path)

        config = Config.find_and_load()

        assert "agent:hidden" in config.agents

    def test_config_find_and_load_not_found(self, tmp_path, monkeypatch):
        """Test that FileNotFoundError is raised when config not found."""
        monkeypatch.chdir(tmp_path)

        with pytest.raises(FileNotFoundError):
            Config.find_and_load()

    def test_config_find_uses_standard_location(self, tmp_path, monkeypatch):
        """Test that config is loaded from .issue-orchestrator/config/ directory."""
        # Config must be in .issue-orchestrator/config/default.yaml
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)

        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = config_dir / "default.yaml"
        config_file.write_text(config_content)

        monkeypatch.chdir(tmp_path)

        config = Config.find_and_load()

        assert "agent:test" in config.agents
        assert config.config_path == config_file

    def test_config_with_custom_repo(self, tmp_path):
        """Test config with custom repo specified."""
        config_content = """
repo:
  name: owner/private-repo

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.repo == "owner/private-repo"

    def test_config_agent_with_all_fields(self, tmp_path):
        """Test agent config with all fields specified."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:full:
    prompt: {prompt_file}
    model: opus
    timeout_minutes: 120
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        agent = config.agents["agent:full"]
        assert agent.model == "opus"
        assert agent.timeout_minutes == 120
        assert agent.prompt_path == prompt_file
        assert config.worktree_base == tmp_path  # Now top-level

    def test_default_agent_claude_opus_effort_reaches_command(self, tmp_path):
        """Default Claude model/provider args flow through YAML into launch argv."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt")

        config_content = f"""
worktrees:
  base: {tmp_path}

default_agent:
  provider: claude-code
  model: opus
  provider_args:
    effort: xhigh

agents:
  agent:full:
    prompt: {prompt_file}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        command = config.agents["agent:full"].get_command(
            issue_number=1,
            issue_title="Title",
            worktree=tmp_path,
        )

        import shlex

        tokens = shlex.split(command)
        assert tokens[:5] == ["claude", "--model", "opus", "--effort", "xhigh"]

    def test_config_state_file_default(self):
        """Test default state file path."""
        config = Config()
        assert config.state_file == Path(".issue-orchestrator/state.json")

    def test_sqlite_backup_defaults(self):
        """Test default sqlite backup settings."""
        config = Config()
        assert config.sqlite_backup.enabled is True
        assert config.sqlite_backup.cadence_hours == 24
        assert config.sqlite_backup.check_interval_minutes == 60
        assert config.sqlite_backup.retention_daily == 14
        assert config.sqlite_backup.retention_weekly == 8
        assert config.sqlite_backup.enforce_on_startup is True

    def test_sqlite_backup_loaded_from_yaml(self, tmp_path):
        """Test sqlite backup settings from YAML."""
        config_content = """
sqlite_backup:
  enabled: false
  cadence_hours: 6
  check_interval_minutes: 15
  retention_daily: 7
  retention_weekly: 4
  enforce_on_startup: false
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.sqlite_backup.enabled is False
        assert config.sqlite_backup.cadence_hours == 6
        assert config.sqlite_backup.check_interval_minutes == 15
        assert config.sqlite_backup.retention_daily == 7
        assert config.sqlite_backup.retention_weekly == 4
        assert config.sqlite_backup.enforce_on_startup is False

    def test_timeline_defaults(self):
        """Test default timeline settings."""
        config = Config()
        assert config.timeline.max_records == 5000

    def test_timeline_loaded_from_yaml(self, tmp_path):
        """Test timeline settings from YAML."""
        config_content = """
timeline:
  max_records: 1200
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.timeline.max_records == 1200

    def test_config_multiple_agents(self, tmp_path):
        """Test loading config with multiple agents."""
        prompt1 = tmp_path / "prompt1.txt"
        prompt1.write_text("Prompt 1")
        prompt2 = tmp_path / "prompt2.txt"
        prompt2.write_text("Prompt 2")
        prompt3 = tmp_path / "prompt3.txt"
        prompt3.write_text("Prompt 3")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:web:
    prompt: {prompt1}
  agent:mobile:
    prompt: {prompt2}
  agent:backend:
    prompt: {prompt3}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert len(config.agents) == 3
        assert all(
            agent in config.agents
            for agent in ["agent:web", "agent:mobile", "agent:backend"]
        )

    def test_config_with_filter_milestone(self, tmp_path):
        """Test config with filtering.milestone specified."""
        config_content = """
filtering:
  milestone: "v1.0"
agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.filtering.milestone == "v1.0"
        assert config.get_filter_milestones() == ["v1.0"]

    def test_config_with_filter_milestones(self, tmp_path):
        """Test config with filtering.milestones specified."""
        config_content = """
filtering:
  milestones:
    - "M1"
    - "M2"
agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.filtering.milestones == ["M1", "M2"]
        assert config.get_filter_milestones() == ["M1", "M2"]

    def test_config_with_milestone_order(self, tmp_path):
        """Test config with milestones.order specified."""
        config_content = """
milestones:
  order: ["M2", "M1"]
agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.milestone_order == ["M2", "M1"]

    def test_config_filter_milestone_default(self):
        """Test default filtering.milestone is None."""
        config = Config()
        assert config.filtering.milestone is None
        assert config.filtering.milestones == []
        assert config.get_filter_milestones() == []

    def test_label_prefix_not_configured(self):
        """Test that labels are not prefixed when label_prefix is not set."""
        config = Config()

        assert config.label_prefix is None
        assert config.get_label_in_progress() == "in-progress"
        assert config.get_label_blocked() == "blocked"
        assert config.get_label_needs_human() == "needs-human"

    def test_label_prefix_configured(self, tmp_path):
        """Test that labels are prefixed when label_prefix is set."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

labels:
  prefix: bot
  in_progress: working
  blocked: blocked-on
  needs_human: needs-review
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.label_prefix == "bot"
        assert config.label_in_progress == "working"
        assert config.label_blocked == "blocked-on"
        assert config.label_needs_human == "needs-review"

        # Test prefixed versions
        assert config.get_label_in_progress() == "bot:working"
        assert config.get_label_blocked() == "bot:blocked-on"
        assert config.get_label_needs_human() == "bot:needs-review"

    def test_prefixed_label_helper(self):
        """Test the prefixed_label helper method."""
        config = Config()

        # Without prefix
        assert config.prefixed_label("test-label") == "test-label"

        # With prefix
        config.label_prefix = "bot"
        assert config.prefixed_label("test-label") == "bot:test-label"
        assert config.prefixed_label("another") == "bot:another"

    def test_label_prefix_with_defaults(self, tmp_path):
        """Test label prefix with default label names."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

labels:
  prefix: orchestrator
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.label_prefix == "orchestrator"
        assert config.label_in_progress == "in-progress"
        assert config.label_blocked == "blocked"
        assert config.label_needs_human == "needs-human"

        # Test prefixed versions with defaults
        assert config.get_label_in_progress() == "orchestrator:in-progress"
        assert config.get_label_blocked() == "orchestrator:blocked"
        assert config.get_label_needs_human() == "orchestrator:needs-human"

    def test_queue_refresh_seconds_default(self):
        """Test that queue_refresh_seconds defaults to 600."""
        config = Config()
        assert config.queue_refresh_seconds == 600
        assert config.session_interactions.enabled is False
        assert config.fetch_layer_enabled is True
        assert config.fetch_layer_network_sync_seconds == 60
        assert config.fetch_layer_full_scan_interval_seconds == 1800
        assert config.fetch_layer_discovery_limit == 25
        assert config.fetch_layer_max_hot_issues_per_cycle == 40
        assert config.fetch_layer_pr_scan_every_n_refreshes == 2
        assert config.fetch_layer_dependency_scan_every_n_refreshes == 1
        assert config.fetch_layer_visibility_aware_enabled is False
        assert config.fetch_layer_selective_sync_planner_enabled is False

    def test_browser_session_defaults(self):
        """Session config defaults match the security-stack baseline."""
        config = Config()
        assert config.browser_session_ttl_seconds == 8 * 3600
        assert config.browser_session_max == 1024
        assert config.sse_token_ttl_seconds == 60

    def test_browser_session_yaml_overrides(self, tmp_path):
        """Values in ``ui.browser_session`` override the Config defaults."""
        from issue_orchestrator.infra.config import Config

        yaml_path = tmp_path / ".issue-orchestrator.yaml"
        yaml_path.write_text(
            "ui:\n"
            "  mode: web\n"
            "  browser_session:\n"
            "    ttl_seconds: 900\n"
            "    max: 32\n"
            "    sse_token_ttl_seconds: 15\n"
        )
        config = Config.load(yaml_path)

        assert config.browser_session_ttl_seconds == 900
        assert config.browser_session_max == 32
        assert config.sse_token_ttl_seconds == 15

    def test_session_interactions_enabled_from_execution_config(self, tmp_path):
        """Execution config can enable runner-managed session interactions."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
execution:
  session_interactions:
    enabled: true
worktrees:
  base: {worktree_base}
""")

        config = Config.load(config_file)

        assert config.session_interactions.enabled is True

    def test_session_interactions_omitted_from_to_event_dict_when_disabled(self):
        config = Config()

        result = config.to_event_dict()

        assert "session_interactions" not in result["execution"]

    def test_session_interactions_included_in_to_event_dict_when_enabled(self):
        config = Config()
        config.session_interactions.enabled = True

        result = config.to_event_dict()

        assert result["execution"]["session_interactions"] == {"enabled": True}

    def test_flow_refresh_defaults(self):
        """Test flow refresh defaults for lazy visible refresh."""
        config = Config()
        assert config.flow_freshness_mode == "balanced"
        assert config.flow_api_budget == "medium"
        assert config.flow_attention_priority == "strict"
        assert config.flow_refresh_enabled is True
        assert config.flow_refresh_stale_seconds == 900
        assert config.flow_refresh_cooldown_seconds == 120

    def test_github_cache_ttl_seconds_default(self):
        """Test that github_cache_ttl_seconds defaults to 300."""
        config = Config()
        assert config.github_cache_ttl_seconds == 300

    def test_github_write_verify_defaults(self):
        """Test that gh write-verify defaults are set."""
        config = Config()
        assert config.github_token is None
        assert config.github_token_env is None
        assert config.github_api_url == "https://api.github.com"
        assert config.github_http_timeout_seconds == 20.0
        assert config.github_required_scopes == []
        assert config.github_allowed_scopes == []
        assert config.gh_write_verify_timeout_seconds == 20
        assert config.gh_write_verify_initial_delay_ms == 250
        assert config.gh_write_verify_max_delay_ms == 2000
        assert config.gh_write_verify_backoff == 1.5
        assert config.gh_write_verify_jitter_ms == 0

    def test_max_issues_to_start_default(self):
        """Test that filtering.max_to_start defaults to 0 (unlimited)."""
        config = Config()
        assert config.filtering.max_to_start == 0

    def test_yaml_overrides_apply_to_nested_keys(self, tmp_path):
        """Test CLI overrides apply to nested YAML settings."""
        config_content = """
labels:
  in_progress: in-progress
review:
  default: "agent:reviewer"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(
            config_file,
            overrides=[
                "labels.in_progress=claimed",
                "review.default=agent:code-review",
                "ui.queue_refresh_seconds=120",
                'filtering.milestones=["M1", "M2"]',
            ],
        )

        assert config.get_label_in_progress() == "claimed"
        assert config.code_review_agent == "agent:code-review"
        assert config.queue_refresh_seconds == 120
        assert config.filtering.milestones == ["M1", "M2"]

    def test_github_scopes_parse_from_strings(self, tmp_path):
        config_content = """
repo:
  name: owner/repo
  github:
    required_scopes: "repo, read:org"
    allowed_scopes: "repo, read:org, read:user"

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.github_required_scopes == ["repo", "read:org"]
        assert config.github_allowed_scopes == ["repo", "read:org", "read:user"]

    def test_queue_refresh_seconds_from_yaml(self, tmp_path):
        """Test loading queue_refresh_seconds from YAML."""
        config_content = """
ui:
  queue_refresh_seconds: 300
  fetch_layer:
    enabled: false
    network_sync_seconds: 45
    full_scan_interval_seconds: 900
    discovery_limit: 12
    max_hot_issues_per_cycle: 18
    pr_scan_every_n_refreshes: 4
    dependency_scan_every_n_refreshes: 3
    visibility_aware_enabled: true
    selective_sync_planner_enabled: true

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.queue_refresh_seconds == 300
        assert config.fetch_layer_enabled is False
        assert config.fetch_layer_network_sync_seconds == 45
        assert config.fetch_layer_full_scan_interval_seconds == 900
        assert config.fetch_layer_discovery_limit == 12
        assert config.fetch_layer_max_hot_issues_per_cycle == 18
        assert config.fetch_layer_pr_scan_every_n_refreshes == 4
        assert config.fetch_layer_dependency_scan_every_n_refreshes == 3
        assert config.fetch_layer_visibility_aware_enabled is True
        assert config.fetch_layer_selective_sync_planner_enabled is True

    def test_flow_refresh_from_yaml(self, tmp_path):
        """Test loading ui.flow_refresh settings from YAML."""
        config_content = """
ui:
  flow_refresh:
    freshness_mode: aggressive
    api_budget: low
    attention_priority: normal
    enabled: false
    stale_seconds: 1800
    cooldown_seconds: 45

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        assert config.flow_freshness_mode == "aggressive"
        assert config.flow_api_budget == "low"
        assert config.flow_attention_priority == "normal"
        assert config.flow_refresh_enabled is False
        assert config.flow_refresh_stale_seconds == 1800
        assert config.flow_refresh_cooldown_seconds == 45

    def test_flow_refresh_high_level_defaults_drive_advanced_defaults(self, tmp_path):
        """High-level flow refresh dials set default low-level values when not explicitly provided."""
        config_content = """
ui:
  flow_refresh:
    freshness_mode: economy
    api_budget: low
    attention_priority: strict

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        assert config.flow_freshness_mode == "economy"
        assert config.flow_api_budget == "low"
        assert config.flow_attention_priority == "strict"
        assert config.flow_refresh_enabled is True
        assert config.flow_refresh_stale_seconds > 900
        assert config.flow_refresh_cooldown_seconds > 120

    def test_github_cache_ttl_seconds_from_yaml(self, tmp_path):
        """Test loading github_cache_ttl_seconds from YAML."""
        config_content = """
repo:
  github:
    cache_ttl_seconds: 120

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.github_cache_ttl_seconds == 120

    def test_session_output_settings_from_yaml(self, tmp_path):
        """Test loading session output settings from YAML."""
        config_content = """
observability:
  session_no_output_seconds: 180
  session_no_output_tail_lines: 25
  session_no_output_max_bytes: 5000
  session_no_output_repeat_seconds: 300
  session_output_retention_days: 14
  session_output_retention_tier: cold

repo:
  github:
    write_verify:
      timeout_seconds: 30
      initial_delay_ms: 300
      max_delay_ms: 2500
      backoff: 1.8
      jitter_ms: 50

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.session_no_output_seconds == 180
        assert config.session_no_output_tail_lines == 25
        assert config.session_no_output_max_bytes == 5000
        assert config.session_no_output_repeat_seconds == 300
        assert config.session_output_retention_days == 14
        assert config.session_output_retention_tier == "cold"
        assert config.gh_write_verify_timeout_seconds == 30
        assert config.gh_write_verify_initial_delay_ms == 300
        assert config.gh_write_verify_max_delay_ms == 2500
        assert config.gh_write_verify_backoff == 1.8
        assert config.gh_write_verify_jitter_ms == 50

    def test_max_issues_to_start_from_yaml(self, tmp_path):
        """Test loading filtering.max_to_start from YAML."""
        config_content = """
filtering:
  max_to_start: 5

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.filtering.max_to_start == 5

    def test_session_output_retention_tier_invalid_fails(self, tmp_path):
        """Invalid observability.session_output_retention_tier should fail load."""
        config_content = """
observability:
  session_output_retention_tier: warm

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        with pytest.raises(ValueError, match="session_output_retention_tier"):
            Config.load(config_file)

    def test_queue_refresh_seconds_zero_disables_auto_refresh(self, tmp_path):
        """Test that queue_refresh_seconds=0 means manual refresh only."""
        config_content = """
ui:
  queue_refresh_seconds: 0

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.queue_refresh_seconds == 0

    def test_both_new_settings_from_yaml(self, tmp_path):
        """Test loading both queue_refresh_seconds and filtering.max_to_start from YAML."""
        config_content = """
ui:
  queue_refresh_seconds: 120
filtering:
  max_to_start: 10

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.queue_refresh_seconds == 120
        assert config.filtering.max_to_start == 10

    def test_review_workflow_defaults(self):
        """Test that review workflow options default to disabled."""
        config = Config()
        # Code review defaults (all None when not configured)
        assert config.code_review_agent is None
        assert config.code_review_label is None
        assert config.code_reviewed_label is None
        assert config.review_exchange_mode == "via-local-loop"
        assert config.review_exchange_probe_schedule == "daily"
        assert config.review_exchange_probe_interval_days == 1
        assert config.review_exchange_max_rounds == 10
        assert config.review_exchange_max_no_progress == 2
        assert config.review_exchange_require_validation is True
        assert config.review_nits_default_policy == "surface"
        assert config.review_nits_by_agent == {}
        assert (
            config.review_keep_current_approach_label
            == "reviewer-keep-current-approach"
        )
        assert config.review_run_audit_min_runtime_minutes == 20
        assert config.review_run_audit_on_timeout is True
        assert config.retrospective_review_enabled is False
        assert config.retrospective_review_trigger_label == "retrospective-review"
        assert config.retrospective_reviewed_label == "retrospective-reviewed"
        assert (
            config.retrospective_changes_requested_label
            == "retrospective-changes-requested"
        )
        assert config.internal_review_enabled is False
        assert config.internal_review_max_rounds == 5
        assert config.internal_review_instructions == ".io/internal-review.md"
        # tech_lead review defaults
        assert config.tech_lead_review_agent is None
        assert config.tech_lead_review_label is None
        assert config.tech_lead_reviewed_label == "tech-lead-reviewed"
        assert config.tech_lead_failed_label == "tech-lead-failed"
        assert config.tech_lead_review_threshold == 0
        assert config.tech_lead_review_on_failure is True

    def test_tech_lead_watch_label_single_owner(self):
        """tech_lead_watch_label is the one derivation every consumer must share.

        Fact gathering, manifest building, and prompt generation all key PR
        selection off this property; drift between them lets the threshold
        trigger on one PR set while the session audits another (#6768 B3).
        """
        config = Config()
        assert config.tech_lead_watch_label == "code-reviewed"
        config.code_reviewed_label = "my-reviewed"
        assert config.tech_lead_watch_label == "my-reviewed"
        config.tech_lead_review_label = "audit-me"
        assert config.tech_lead_watch_label == "audit-me"

    def test_goal_pilot_defaults(self):
        """Goal Pilot defaults to disabled with journeys-only approvals."""
        config = Config()
        assert config.goal_pilot.enabled is False
        assert config.goal_pilot.agent is None
        assert config.goal_pilot.approval_policy == "journeys_only"
        assert config.goal_pilot.approval_batch_size == 10
        assert config.goal_pilot.approval_batch_window_minutes == 60

    def test_goal_pilot_from_yaml(self, tmp_path):
        """Test loading goal pilot config from YAML."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:goal-pilot:
    prompt: /tmp/prompt.txt

goal_pilot:
  enabled: true
  agent: agent:goal-pilot
  approval_policy: batch
  approval_batch_size: 20
  approval_batch_window_minutes: 120
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.goal_pilot.enabled is True
        assert config.goal_pilot.agent == "agent:goal-pilot"
        assert config.goal_pilot.approval_policy == "batch"
        assert config.goal_pilot.approval_batch_size == 20
        assert config.goal_pilot.approval_batch_window_minutes == 120

    def test_goal_pilot_requires_agent_when_enabled(self, tmp_path):
        """Goal Pilot enabled requires a valid agent."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

goal_pilot:
  enabled: true
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()

        assert any("goal_pilot.enabled is true" in e for e in errors)

    def test_review_workflow_from_yaml(self, tmp_path):
        """Test loading review workflow config from YAML."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
    ai_system: claude-code
  agent:coder:
    prompt: /tmp/prompt.txt
    ai_system: claude-code
  agent:reviewer:
    prompt: /tmp/prompt.txt
    ai_system: codex
  agent:tech-lead:
    prompt: /tmp/prompt.txt
    ai_system: claude-code

review:
  enabled: true
  default: agent:reviewer
  code_review_label: needs-code-review
  code_reviewed_label: code-reviewed
  exchange:
    mode: via-mcp
    probe:
      schedule: interval
      interval_days: 2
    loop:
      max_rounds: 6
      max_no_progress: 1
      require_validation: false
  nits:
    default_policy: address
    by_agent:
      agent:coder: ignore
  run_audit:
    min_runtime_minutes: 30
    on_timeout: false
  retrospective:
    enabled: true
    trigger_label: lack-of-review-redo
    reviewed_label: lack-of-review-reviewed
    changes_requested_label: lack-of-review-needs-work
  internal:
    enabled: true
    max_rounds: 3
    instructions: .io/fast-internal-review.md
  keep_current_approach_label: reviewer-keep-current-approach
  tech_lead_review_agent: agent:tech-lead
  tech_lead_reviewed_label: tech-lead-reviewed
  tech_lead_review_threshold: 5
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.code_review_agent == "agent:reviewer"
        assert config.code_review_label == "needs-code-review"
        assert config.code_reviewed_label == "code-reviewed"
        assert config.review_exchange_mode == "via-mcp"
        assert config.review_exchange_probe_schedule == "interval"
        assert config.review_exchange_probe_interval_days == 2
        assert config.review_exchange_max_rounds == 6
        assert config.review_exchange_max_no_progress == 1
        assert config.review_exchange_require_validation is False
        assert config.review_nits_default_policy == "address"
        assert config.review_nits_by_agent == {"agent:coder": "ignore"}
        assert config.review_run_audit_min_runtime_minutes == 30
        assert config.review_run_audit_on_timeout is False
        assert config.retrospective_review_enabled is True
        assert config.retrospective_review_trigger_label == "lack-of-review-redo"
        assert config.retrospective_reviewed_label == "lack-of-review-reviewed"
        assert (
            config.retrospective_changes_requested_label == "lack-of-review-needs-work"
        )
        assert config.internal_review_enabled is True
        assert config.internal_review_max_rounds == 3
        assert config.internal_review_instructions == ".io/fast-internal-review.md"
        assert (
            config.review_keep_current_approach_label
            == "reviewer-keep-current-approach"
        )
        assert config.tech_lead_review_agent == "agent:tech-lead"
        assert config.tech_lead_reviewed_label == "tech-lead-reviewed"
        assert config.tech_lead_review_threshold == 5

    def test_retrospective_review_requires_default_reviewer(self, tmp_path):
        """Retrospective review cannot run without a reviewer agent."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:coder:
    prompt: /tmp/prompt.txt

review:
  retrospective:
    enabled: true
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        errors = config.validate()
        assert any(
            "review.retrospective.enabled requires review.default" in e for e in errors
        )

    @pytest.mark.parametrize(
        ("internal_yaml", "error_fragment"),
        [
            ('enabled: "yes"', "review.internal.enabled"),
            ("max_rounds: nope", "review.internal.max_rounds"),
            ("max_rounds: true", "review.internal.max_rounds"),
            ("max_rounds: 0", "review.internal.max_rounds"),
            ("max_rounds: 51", "review.internal.max_rounds"),
            ("instructions: 123", "review.internal.instructions"),
            ('instructions: ""', "review.internal.instructions"),
            ("instructions: ../outside.md", "review.internal.instructions"),
            ("instructions: /tmp/outside.md", "review.internal.instructions"),
        ],
    )
    def test_internal_review_rejects_invalid_bounded_configuration(
        self,
        tmp_path,
        internal_yaml,
        error_fragment,
    ):
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(
            "worktrees:\n"
            "  base: /tmp\n"
            "review:\n"
            "  internal:\n"
            f"    {internal_yaml}\n",
            encoding="utf-8",
        )

        errors = Config.load(config_file).validate()

        assert any(error_fragment in error for error in errors)

    def test_internal_review_section_must_be_a_mapping(self, tmp_path):
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text("review:\n  internal: enabled\n", encoding="utf-8")

        with pytest.raises(ConfigSectionError, match="internal"):
            Config.load(config_file)

    def test_internal_review_instruction_path_is_normalized_at_load(self, tmp_path):
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(
            'review:\n  internal:\n    instructions: " .io/internal-review.md "\n',
            encoding="utf-8",
        )

        config = Config.load(config_file)

        assert not any(
            "review.internal" in error for error in config.validate()
        )
        assert config.internal_review_instructions == ".io/internal-review.md"

    def test_retrospective_review_validates_non_empty_labels(self, tmp_path):
        """Retrospective labels are source-of-truth state and must be explicit."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:coder:
    prompt: /tmp/prompt.txt
  agent:reviewer:
    prompt: /tmp/prompt.txt

review:
  default: agent:reviewer
  retrospective:
    enabled: true
    trigger_label: ""
    reviewed_label: ""
    changes_requested_label: ""
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        errors = config.validate()
        assert any("review.retrospective.trigger_label" in e for e in errors)
        assert any("review.retrospective.reviewed_label" in e for e in errors)
        assert any("review.retrospective.changes_requested_label" in e for e in errors)

    def test_review_workflow_partial_config(self, tmp_path):
        """Test loading review workflow with partial config (code review only)."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

review:
  enabled: true
  default: agent:reviewer
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.code_review_agent == "agent:reviewer"
        assert config.code_review_label == "needs-code-review"  # default
        assert config.code_reviewed_label == "code-reviewed"  # default
        assert config.tech_lead_review_agent is None  # not configured
        assert config.tech_lead_review_threshold == 0  # default

    def test_review_threshold_zero_means_manual_only(self):
        """Test that tech_lead_review_threshold=0 means manual tech_lead review only."""
        config = Config()
        config.tech_lead_review_agent = "agent:tech-lead"
        config.tech_lead_review_threshold = 0

        # Threshold of 0 means auto-trigger is disabled
        assert config.tech_lead_review_threshold == 0

    def test_validation_coverage_guardrail_from_yaml(self, tmp_path):
        """Test loading coverage guardrail config."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
agents:
  agent:test:
    prompt: {prompt_file}
    worktree_base: {tmp_path}

validation:
  quick:
    cmd: "make validate"
    timeout_seconds: 400
  coverage_guardrail:
    enabled: true
    min_percent: 85
    scope:
      - "src/issue_orchestrator/**"
    coverage_type: "branch"
    exclude:
      - "src/issue_orchestrator/generated/**"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        guardrail = config.validation.coverage_guardrail
        assert guardrail.enabled is True
        assert guardrail.min_percent == 85
        assert guardrail.apply_to == "changed"
        assert guardrail.scope == ["src/issue_orchestrator/**"]
        assert guardrail.coverage_type == "branch"
        assert guardrail.exclude == ["src/issue_orchestrator/generated/**"]

    def test_per_agent_reviewer_field(self, tmp_path):
        """Test that per-agent reviewer field is parsed from YAML."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:frontend:
    prompt: {prompt_file}
    reviewer: agent:web-reviewer
  agent:backend:
    prompt: {prompt_file}
  agent:web-reviewer:
    prompt: {prompt_file}
  agent:reviewer:
    prompt: {prompt_file}

review:
  enabled: true
  default: agent:reviewer
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Per-agent reviewer should be set
        assert config.agents["agent:frontend"].reviewer == "agent:web-reviewer"
        # Backend has no per-agent reviewer
        assert config.agents["agent:backend"].reviewer is None
        # Default reviewer should be set
        assert config.review_enabled is True
        assert config.code_review_agent == "agent:reviewer"

    def test_get_reviewer_for_agent_with_per_agent_override(self, tmp_path):
        """Test get_reviewer_for_agent returns per-agent reviewer when set."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:frontend:
    prompt: {prompt_file}
    reviewer: agent:web-reviewer
  agent:backend:
    prompt: {prompt_file}
  agent:web-reviewer:
    prompt: {prompt_file}
  agent:reviewer:
    prompt: {prompt_file}

review:
  enabled: true
  default: agent:reviewer
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Frontend should use per-agent reviewer
        assert config.get_reviewer_for_agent("agent:frontend") == "agent:web-reviewer"
        # Backend should use default reviewer
        assert config.get_reviewer_for_agent("agent:backend") == "agent:reviewer"
        # Unknown agent should use default reviewer
        assert config.get_reviewer_for_agent("agent:unknown") == "agent:reviewer"

    def test_get_reviewer_for_agent_no_default(self, tmp_path):
        """Test get_reviewer_for_agent returns None when no default reviewer."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:frontend:
    prompt: {prompt_file}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # No default reviewer configured
        assert config.code_review_agent is None
        assert config.get_reviewer_for_agent("agent:frontend") is None

    def test_review_enabled_and_default(self, tmp_path):
        """Test that review.enabled and review.default are parsed correctly."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:test:
    prompt: {prompt_file}
  agent:new-reviewer:
    prompt: {prompt_file}

review:
  enabled: true
  default: agent:new-reviewer
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.review_enabled is True
        assert config.code_review_agent == "agent:new-reviewer"

    def test_validate_per_agent_reviewer_exists(self, tmp_path):
        """Test that validation catches non-existent per-agent reviewers."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:frontend:
    prompt: {prompt_file}
    ai_system: claude-code
    reviewer: agent:nonexistent-reviewer
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        errors = config.validate()
        assert any(
            "reviewer 'agent:nonexistent-reviewer' not found" in e for e in errors
        )

    def test_validate_default_reviewer_required_when_enabled(self, tmp_path):
        """Test that default reviewer is required when review.enabled is true."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:frontend:
    prompt: {prompt_file}
    ai_system: claude-code

review:
  enabled: true
  # No default set!
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        errors = config.validate()
        assert any("no default reviewer set" in e for e in errors)

    def test_agents_require_ai_system(self, tmp_path):
        """Test that agents require ai_system."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:reviewer:
    prompt: {prompt_file}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()
        assert any("ai_system" in e for e in errors)

    def test_ai_systems_allowlist_accepts_custom(self, tmp_path):
        """Test that ai_systems.allowed accepts custom ai_system values."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

default_agent:
  provider: claude-code

agents:
  agent:custom:
    prompt: {prompt_file}
    ai_system: custom-ai

ai_systems:
  allowed:
    - custom-ai
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()
        assert not any("ai_system" in e for e in errors)

    def test_ai_systems_allowlist_accepts_comma_string(self, tmp_path):
        """Test that ai_systems.allowed can be provided as a comma-separated string."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

default_agent:
  provider: claude-code

agents:
  agent:custom:
    prompt: {prompt_file}
    ai_system: custom-ai

ai_systems:
  allowed: custom-ai, other-ai
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()
        assert not any("ai_system" in e for e in errors)

    def test_review_exchange_defers_pair_validation(self, tmp_path):
        """Config validation should not fail on unsupported pairs (runtime handles it)."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:coder:
    prompt: {prompt_file}
    ai_system: gemini
  agent:reviewer:
    prompt: {prompt_file}
    ai_system: claude-code

review:
  enabled: true
  default: agent:reviewer
  exchange:
    mode: via-mcp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()
        assert not any("unsupported ai_system" in e for e in errors)

    def test_review_exchange_ignores_unrelated_agents(self, tmp_path):
        """Test via-mcp validation ignores unrelated agents (e.g., tech_lead)."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:coder:
    prompt: {prompt_file}
    ai_system: claude-code
  agent:reviewer:
    prompt: {prompt_file}
    ai_system: codex
  agent:tech-lead:
    prompt: {prompt_file}
    ai_system: gemini

review:
  enabled: true
  default: agent:reviewer
  tech_lead_review_agent: agent:tech-lead
  exchange:
    mode: via-mcp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()
        assert not any("unsupported ai_system" in e for e in errors)

    def test_review_exchange_probe_invalid_schedule(self, tmp_path):
        """Test invalid probe schedule fails validation."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:coder:
    prompt: {prompt_file}
    ai_system: claude-code
  agent:reviewer:
    prompt: {prompt_file}
    ai_system: codex

review:
  enabled: true
  default: agent:reviewer
  exchange:
    mode: via-mcp
    probe:
      schedule: sometimes
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()
        assert any("probe.schedule" in e for e in errors)

    def test_review_nit_policy_invalid_values(self, tmp_path):
        """Review nit policies must be explicit supported workflow values."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:coder:
    prompt: {prompt_file}
    ai_system: claude-code
  agent:reviewer:
    prompt: {prompt_file}
    ai_system: codex

review:
  enabled: true
  default: agent:reviewer
  nits:
    default_policy: maybe
    by_agent:
      agent:coder: later
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()

        assert any("review.nits.default_policy" in e for e in errors)
        assert any("review.nits.by_agent.agent:coder" in e for e in errors)

    def test_review_nits_by_agent_empty_yaml_value_means_empty_mapping(self, tmp_path):
        """`by_agent:` with nothing under it (YAML None) is the idiomatic
        empty-mapping spelling and must keep loading as {} - not become a
        validation error for configs that load fine today."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:coder:
    prompt: {prompt_file}
    ai_system: claude-code
  agent:reviewer:
    prompt: {prompt_file}
    ai_system: codex

review:
  enabled: true
  default: agent:reviewer
  nits:
    by_agent:
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.review_nits_by_agent == {}
        assert not [e for e in config.validate() if "by_agent" in e]

    def test_review_nits_by_agent_non_mapping_fails_validation(self, tmp_path):
        """A non-mapping review.nits.by_agent must fail validate() loudly.

        The loader previously coerced malformed values to {} silently,
        hiding the YAML mistake from the validator entirely.
        """
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:coder:
    prompt: {prompt_file}
    ai_system: claude-code
  agent:reviewer:
    prompt: {prompt_file}
    ai_system: codex

review:
  enabled: true
  default: agent:reviewer
  nits:
    by_agent:
      - agent:coder
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()

        assert any("review.nits.by_agent must be a mapping" in e for e in errors), (
            errors
        )

    def test_review_nits_by_agent_non_string_key_fails_validation(self, tmp_path):
        """Non-string agent labels never match real labels; fail loudly."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:coder:
    prompt: {prompt_file}
    ai_system: claude-code
  agent:reviewer:
    prompt: {prompt_file}
    ai_system: codex

review:
  enabled: true
  default: agent:reviewer
  nits:
    by_agent:
      42: surface
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()

        assert any("must be a string agent label" in e for e in errors), errors

    def test_validate_no_error_when_review_disabled(self, tmp_path):
        """Test that no error when review.enabled is false (default)."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:frontend:
    prompt: {prompt_file}
    ai_system: claude-code

# No review section - defaults to enabled: false
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        errors = config.validate()
        # Should not have any review-related errors
        assert not any("reviewer" in e.lower() for e in errors)


class TestProviderResilienceConfig:
    """Tests for provider resilience config parsing."""

    def test_defaults(self):
        config = Config()
        assert config.provider_resilience.short_retry.max_attempts == 4
        assert config.provider_resilience.short_retry.initial_backoff_seconds == 5
        assert config.provider_resilience.short_retry.max_backoff_seconds == 60
        assert config.provider_resilience.short_retry.jitter is True
        assert config.provider_resilience.circuit_breaker.cooldown_seconds == 1800
        assert config.provider_resilience.circuit_breaker.max_cooldowns == 6
        assert (
            config.provider_resilience.circuit_breaker.label
            == "blocked:provider-unavailable"
        )

    def test_parsing(self, tmp_path):
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:backend:
    prompt: {prompt}
    provider: claude-code
provider_resilience:
  short_retry:
    max_attempts: 2
    initial_backoff_seconds: 7
    max_backoff_seconds: 20
    jitter: false
  circuit_breaker:
    cooldown_seconds: 900
    max_cooldowns: 3
    label: "blocked:provider-unavailable"
""")
        config = Config.load(config_file)
        assert config.provider_resilience.short_retry.max_attempts == 2
        assert config.provider_resilience.short_retry.initial_backoff_seconds == 7
        assert config.provider_resilience.short_retry.max_backoff_seconds == 20
        assert config.provider_resilience.short_retry.jitter is False
        assert config.provider_resilience.circuit_breaker.cooldown_seconds == 900
        assert config.provider_resilience.circuit_breaker.max_cooldowns == 3


class TestInterruptedSessionRetryConfig:
    """Tests for interrupted-session retry config parsing."""

    def test_defaults(self):
        config = Config()
        assert config.retry.interrupted_sessions.enabled is True
        assert config.retry.interrupted_sessions.retry_coding is True
        assert config.retry.interrupted_sessions.retry_review is True
        assert (
            config.retry.interrupted_sessions.coding_guard_label
            == "io:auto-retried-interrupted-coding"
        )
        assert (
            config.retry.interrupted_sessions.review_guard_label
            == "io:auto-retried-interrupted-review"
        )

    def test_parsing(self, tmp_path):
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:backend:
    prompt: {prompt}
retry:
  interrupted_sessions:
    enabled: true
    retry_coding: false
    retry_review: true
    coding_guard_label: "io:custom-coding-guard"
    review_guard_label: "io:custom-review-guard"
""")
        config = Config.load(config_file)
        assert config.retry.interrupted_sessions.enabled is True
        assert config.retry.interrupted_sessions.retry_coding is False
        assert config.retry.interrupted_sessions.retry_review is True
        assert (
            config.retry.interrupted_sessions.coding_guard_label
            == "io:custom-coding-guard"
        )
        assert (
            config.retry.interrupted_sessions.review_guard_label
            == "io:custom-review-guard"
        )


class TestCleanupConfig:
    """Tests for cleanup configuration."""

    def test_cleanup_config_defaults(self):
        """Test that cleanup config has sensible defaults."""
        config = Config()

        # with_tech_lead defaults
        assert config.cleanup.with_tech_lead.close_ai_session_tabs is True
        assert config.cleanup.with_tech_lead.remove_worktrees is True

        # without_tech_lead defaults
        assert config.cleanup.without_tech_lead.wait_for_code_review is True
        assert config.cleanup.without_tech_lead.close_ai_session_tabs is True
        assert config.cleanup.without_tech_lead.remove_worktrees is True

    def test_cleanup_config_from_yaml_with_tech_lead(self, tmp_path):
        """Test loading cleanup config for CTO workflow."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

cleanup:
  with_tech_lead:
    close_ai_session_tabs: false
    remove_worktrees: true
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.cleanup.with_tech_lead.close_ai_session_tabs is False
        assert config.cleanup.with_tech_lead.remove_worktrees is True
        # without_tech_lead should have defaults
        assert config.cleanup.without_tech_lead.wait_for_code_review is True
        assert config.cleanup.without_tech_lead.close_ai_session_tabs is True

    def test_cleanup_config_from_yaml_without_tech_lead(self, tmp_path):
        """Test loading cleanup config for non-CTO workflow."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

cleanup:
  without_tech_lead:
    wait_for_code_review: false
    close_ai_session_tabs: true
    remove_worktrees: true
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.cleanup.without_tech_lead.wait_for_code_review is False
        assert config.cleanup.without_tech_lead.close_ai_session_tabs is True
        assert config.cleanup.without_tech_lead.remove_worktrees is True
        # with_tech_lead should have defaults
        assert config.cleanup.with_tech_lead.close_ai_session_tabs is True
        assert config.cleanup.with_tech_lead.remove_worktrees is True

    def test_cleanup_config_from_yaml_both_sections(self, tmp_path):
        """Test loading cleanup config with both sections specified."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

cleanup:
  with_tech_lead:
    close_ai_session_tabs: true
    remove_worktrees: true
  without_tech_lead:
    wait_for_code_review: false
    close_ai_session_tabs: false
    remove_worktrees: false
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # with_tech_lead
        assert config.cleanup.with_tech_lead.close_ai_session_tabs is True
        assert config.cleanup.with_tech_lead.remove_worktrees is True

        # without_tech_lead
        assert config.cleanup.without_tech_lead.wait_for_code_review is False
        assert config.cleanup.without_tech_lead.close_ai_session_tabs is False
        assert config.cleanup.without_tech_lead.remove_worktrees is False

    def test_cleanup_config_partial_fields_use_defaults(self, tmp_path):
        """Test that unspecified cleanup fields use defaults."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

cleanup:
  with_tech_lead:
    remove_worktrees: true
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Specified field
        assert config.cleanup.with_tech_lead.remove_worktrees is True
        # Unspecified field should use default
        assert config.cleanup.with_tech_lead.close_ai_session_tabs is True

    def test_cleanup_config_empty_section_uses_defaults(self, tmp_path):
        """Test that empty cleanup section uses all defaults."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

cleanup: {}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # All defaults
        assert config.cleanup.with_tech_lead.close_ai_session_tabs is True
        assert config.cleanup.with_tech_lead.remove_worktrees is True
        assert config.cleanup.without_tech_lead.wait_for_code_review is True
        assert config.cleanup.without_tech_lead.close_ai_session_tabs is True
        assert config.cleanup.without_tech_lead.remove_worktrees is True

    def test_cleanup_config_missing_section_uses_defaults(self, tmp_path):
        """Test that missing cleanup section uses all defaults."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # All defaults when section is missing
        assert config.cleanup.with_tech_lead.close_ai_session_tabs is True
        assert config.cleanup.with_tech_lead.remove_worktrees is True
        assert config.cleanup.without_tech_lead.wait_for_code_review is True


class TestConfigValidation:
    """Tests for config validation at startup."""

    def test_removed_convergence_required_wins_is_unknown(self, tmp_path):
        """claims.convergence_required_wins is no longer accepted."""
        config_content = """
claims:
  enabled: true
  convergence_required_wins: 2
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert (
            "Unknown config field: 'claims.convergence_required_wins'"
            in config.validate()
        )

    def test_validate_missing_prompt_file(self, tmp_path):
        """Test validation catches missing prompt files."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /nonexistent/path/prompt.md
    ai_system: claude-code
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()

        assert any("prompt file not found" in e for e in errors)

    def test_validate_worktree_base_resolved_to_absolute(self, tmp_path):
        """Test that relative worktree_base is resolved to absolute path."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("# Test prompt")

        config_content = f"""
worktrees:
  base: ./worktrees

agents:
  agent:test:
    prompt: {prompt_file}
    ai_system: claude-code
"""
        # Config must be at <repo>/.issue-orchestrator/config/<name>.yaml
        # so repo_root is correctly calculated (3 levels up)
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Relative path should be resolved to absolute (worktrees.base)
        assert config.worktree_base.is_absolute()
        assert str(config.worktree_base).startswith(str(tmp_path))

    def test_missing_worktree_base_uses_dedicated_sibling_collection(
        self,
        tmp_path,
    ):
        """A config omission must not place generated worktrees beside every repo."""
        repo_root = tmp_path / "repo"
        prompt_file = repo_root / "prompt.md"
        config_dir = repo_root / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        prompt_file.write_text("# Test prompt")
        config_file = config_dir / "default.yaml"
        config_file.write_text(
            f"""
agents:
  agent:test:
    prompt: {prompt_file}
"""
        )

        config = Config.load(config_file)

        expected = tmp_path / "worktrees" / "repo"
        assert config.worktree_base == expected
        assert expected.is_dir()
        assert config.to_dict()["worktrees"]["base"] == "../worktrees/repo"

    def test_validate_worktree_base_created_if_missing(self, tmp_path):
        """Test that worktree_base directory is created during load."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("# Test prompt")
        worktree_dir = tmp_path / "new-worktrees"

        config_content = f"""
worktrees:
  base: {worktree_dir}

agents:
  agent:test:
    prompt: {prompt_file}
"""
        # Config must be at <repo>/.issue-orchestrator/config/<name>.yaml
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
        config_file.write_text(config_content)

        # Directory doesn't exist yet
        assert not worktree_dir.exists()

        config = Config.load(config_file)

        # Directory should be created (worktrees.base)
        assert worktree_dir.exists()
        assert worktree_dir.is_dir()
        assert config.worktree_base == worktree_dir

    def test_validate_invalid_review_agent_reference(self, tmp_path):
        """Test validation catches invalid code_review_agent reference."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("# Test prompt")
        worktree_dir = tmp_path / "worktrees"
        worktree_dir.mkdir()

        config_content = f"""
worktrees:
  base: {worktree_dir}

agents:
  agent:test:
    prompt: {prompt_file}
    ai_system: claude-code

review:
  enabled: true
  default: agent:nonexistent
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()

        assert any("review.default 'agent:nonexistent' not found" in e for e in errors)

    def test_validate_valid_config_returns_empty(self, tmp_path):
        """Test that valid config returns no errors."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("# Test prompt")
        worktree_dir = tmp_path / "worktrees"
        worktree_dir.mkdir()

        config_content = f"""
worktrees:
  base: {worktree_dir}

default_agent:
  provider: claude-code

agents:
  agent:test:
    prompt: {prompt_file}
    model: haiku
    ai_system: claude-code
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()

        assert errors == []

    def test_validate_or_raise_raises_on_errors(self, tmp_path):
        """Test validate_or_raise raises ValueError with all errors."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /nonexistent/prompt.md
    ai_system: claude-code
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        import pytest

        with pytest.raises(ValueError) as exc_info:
            config.validate_or_raise()

        assert "Configuration errors" in str(exc_info.value)
        assert "prompt file not found" in str(exc_info.value)


class TestE2EPRLabelsConfig:
    """Tests for e2e_pr_labels configuration."""

    def test_e2e_pr_labels_defaults_to_empty_list(self):
        """e2e_pr_labels should default to empty list."""
        config = Config()
        assert config.e2e_pr_labels == []

    def test_e2e_pr_labels_loaded_from_yaml(self, tmp_path):
        """e2e_pr_labels should be loaded correctly from YAML."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

e2e:
  pr_labels:
    - test-data
    - e2e-test
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e_pr_labels == ["test-data", "e2e-test"]

    def test_e2e_pr_labels_inline_yaml_syntax(self, tmp_path):
        """e2e_pr_labels should work with inline YAML list syntax."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

e2e:
  pr_labels: ["test-data"]
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e_pr_labels == ["test-data"]

    def test_e2e_pr_labels_included_in_to_event_dict(self):
        """e2e_pr_labels should be included in to_event_dict output."""
        config = Config()
        config.e2e_pr_labels = ["test-data", "cleanup"]

        result = config.to_event_dict()

        assert result["e2e"]["pr_labels"] == ["test-data", "cleanup"]

    def test_e2e_pr_labels_not_specified_defaults_to_empty(self, tmp_path):
        """e2e_pr_labels should default to empty list when not in YAML."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e_pr_labels == []


class TestE2EStopOnFirstFailureConfig:
    """Tests for e2e.stop_on_first_failure configuration."""

    def test_stop_on_first_failure_defaults_to_false(self):
        """stop_on_first_failure should default to False."""
        config = Config()
        assert config.e2e.stop_on_first_failure is False

    def test_stop_on_first_failure_true_from_yaml(self, tmp_path):
        """stop_on_first_failure=true should be loaded correctly."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

e2e:
  stop_on_first_failure: true
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e.stop_on_first_failure is True

    def test_stop_on_first_failure_false_from_yaml(self, tmp_path):
        """stop_on_first_failure=false should be loaded correctly."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

e2e:
  stop_on_first_failure: false
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e.stop_on_first_failure is False

    def test_stop_on_first_failure_not_specified_defaults_false(self, tmp_path):
        """stop_on_first_failure should default to False when not in YAML."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

e2e:
  enabled: true
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e.stop_on_first_failure is False


class TestE2ERunnerKindConfig:
    """Tests for e2e.runner_kind parsing."""

    def test_invalid_runner_kind_raises(self) -> None:
        from issue_orchestrator.infra.config_sections import parse_e2e_config

        with pytest.raises(
            ValueError,
            match="e2e.runner_kind must be 'pytest' or 'command'",
        ):
            parse_e2e_config({"runner_kind": "vitest"})


class TestE2EAutoSettings:
    """Tests for e2e auto quarantine/issue settings."""

    def test_auto_settings_defaults(self):
        """Auto settings should default to enabled with backend agent label."""
        config = Config()
        assert config.e2e.auto_quarantine is True
        assert config.e2e.auto_create_issues is True
        assert config.e2e.issue_agent_label == "agent:backend"

    def test_auto_settings_from_yaml(self, tmp_path):
        """Auto settings should load from YAML."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("prompt")

        config_content = f"""
worktrees:
  base: /tmp

agents:
  agent:backend:
    prompt: {prompt_file}

e2e:
  auto_quarantine: false
  auto_create_issues: false
  issue_agent_label: agent:backend
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e.auto_quarantine is False
        assert config.e2e.auto_create_issues is False
        assert config.e2e.issue_agent_label == "agent:backend"


class TestE2EFlakeConfig:
    """Tests for e2e flake detection configuration."""

    def test_flake_threshold_defaults_to_20(self):
        """flake_threshold should default to 20 (flip rate percentage)."""
        config = Config()
        assert config.e2e.flake_threshold == 20

    def test_flake_window_runs_defaults_to_10(self):
        """flake_window_runs should default to 10."""
        config = Config()
        assert config.e2e.flake_window_runs == 10

    def test_flake_config_from_yaml(self, tmp_path):
        """Flake settings should be loaded from YAML."""
        config_content = """
worktrees:
  base: /tmp/worktrees
  repo_root: /tmp/repo

e2e:
  flake_threshold: 5
  flake_window_runs: 20
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e.flake_threshold == 5
        assert config.e2e.flake_window_runs == 20


class TestTechLeadConfig:
    """Tests for tech_lead issue configuration."""

    def test_tech_lead_config_defaults(self):
        """TechLeadConfig should have sensible defaults."""
        config = Config()

        assert config.tech_lead.enabled is None
        assert config.tech_lead_enabled is False
        assert config.tech_lead.inherit_labels == []
        assert config.tech_lead.explicit_labels == []
        assert config.tech_lead.milestone_strategy.inherit_from_issues == "latest"
        assert config.tech_lead.milestone_strategy.explicit is None
        assert config.tech_lead.priority is None
        assert config.tech_lead.dedup.enabled is True
        assert config.tech_lead.dedup.similarity_threshold == 0.72

    def test_legacy_agent_presence_enables_tech_lead_when_switch_is_omitted(self):
        config = Config()
        config.tech_lead_review_agent = "agent:tech-lead"

        assert config.tech_lead.enabled is None
        assert config.tech_lead_enabled is True

    def test_explicit_switch_overrides_legacy_agent_presence(self):
        config = Config()
        config.tech_lead_review_agent = "agent:tech-lead"

        config.tech_lead.enabled = False
        assert config.tech_lead_enabled is False

        config.tech_lead.enabled = True
        assert config.tech_lead_enabled is True

    def test_explicit_disable_survives_save_and_reload(self, tmp_path):
        config = Config()
        config.tech_lead_review_agent = "agent:tech-lead"
        config.tech_lead.enabled = False
        saved = tmp_path / "saved.yaml"

        assert config.to_dict()["tech_lead"] == {"enabled": False}
        config.save(saved)
        reloaded = Config.load(saved)

        assert reloaded.tech_lead_review_agent == "agent:tech-lead"
        assert reloaded.tech_lead.enabled is False
        assert reloaded.tech_lead_enabled is False

    def test_legacy_omission_stays_omitted_when_serialized(self):
        config = Config()
        config.tech_lead_review_agent = "agent:tech-lead"

        assert "tech_lead" not in config.to_dict()

    @pytest.mark.parametrize(("raw", "expected"), (("false", False), ("true", True)))
    def test_tech_lead_enabled_from_yaml(self, tmp_path, raw: str, expected: bool):
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(
            f"""
agents:
  agent:tech-lead:
    prompt: /tmp/tech-lead.md
review:
  tech_lead_review_agent: agent:tech-lead
  tech_lead_follow_up_agent: agent:tech-lead
tech_lead:
  enabled: {raw}
"""
        )

        config = Config.load(config_file)

        assert config.tech_lead.enabled is expected
        assert config.tech_lead_enabled is expected

    def test_tech_lead_enabled_rejects_non_boolean_yaml(self, tmp_path):
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text('tech_lead:\n  enabled: "false"\n')

        with pytest.raises(ValueError, match="tech_lead.enabled must be a boolean"):
            Config.load(config_file)

    def test_explicit_enable_without_agent_fails_validation(self):
        config = Config()
        config.tech_lead.enabled = True

        errors = config.validate()

        assert any("tech_lead.enabled is true" in error for error in errors)

    def test_tech_lead_dedup_from_yaml(self, tmp_path):
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(
            """
tech_lead:
  dedup:
    enabled: false
    similarity_threshold: 0.88
"""
        )

        config = Config.load(config_file)

        assert config.tech_lead.dedup.enabled is False
        assert config.tech_lead.dedup.similarity_threshold == 0.88

    @pytest.mark.parametrize("threshold", (0.0, -0.1, 1.01))
    def test_tech_lead_dedup_invalid_threshold_fails_validation(self, threshold: float):
        config = Config()
        config.tech_lead.dedup.similarity_threshold = threshold

        errors = config.validate()

        assert any("tech_lead.dedup.similarity_threshold" in error for error in errors)

    def test_finding_promotion_defaults(self):
        """#6957: the lane ships ON but gated, so promotion needs one approval."""
        findings = Config().tech_lead.findings

        assert findings.promote == "gated"
        assert findings.min_evidence == 2
        assert findings.max_open_promoted == 3
        assert findings.route == {"default": PromotionRouteTarget(repo="self")}
        assert findings.enabled is True
        assert findings.gated is True

    def test_finding_promotion_from_yaml(self, tmp_path):
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(
            """
tech_lead:
  findings:
    promote: auto
    min_evidence: 4
    max_open_promoted: 1
    route:
      completion-pipeline: issue-orchestrator/issue-orchestrator
      default: self
"""
        )

        findings = Config.load(config_file).tech_lead.findings

        assert findings.promote == "auto"
        assert findings.gated is False
        assert findings.min_evidence == 4
        assert findings.max_open_promoted == 1
        assert findings.route_for("completion-pipeline").repo == (
            "issue-orchestrator/issue-orchestrator"
        )
        assert findings.route_for("ui").repo == "self"
        assert findings.target_repos() == ("issue-orchestrator/issue-orchestrator",)

    def test_finding_promotion_route_defaults_to_self_when_omitted(self, tmp_path):
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(
            """
tech_lead:
  findings:
    route:
      completion-pipeline: owner/repo
"""
        )

        findings = Config.load(config_file).tech_lead.findings

        assert findings.route_for("anything-else").repo == "self"

    def test_finding_promotion_default_route_is_case_insensitive(self, tmp_path):
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(
            """
tech_lead:
  findings:
    route:
      Default: owner/repo
"""
        )

        findings = Config.load(config_file).tech_lead.findings

        assert findings.route == {"default": PromotionRouteTarget(repo="owner/repo")}
        assert findings.route_for("anything-else").repo == "owner/repo"

    def test_finding_promotion_target_repos_fold_repository_case(self):
        findings = Config().tech_lead.findings
        findings.route = {
            "one": PromotionRouteTarget(repo="Owner/Repo"),
            "two": PromotionRouteTarget(repo="owner/repo"),
        }

        assert findings.target_repos() == ("Owner/Repo",)

    def test_promote_off_disables_the_lane(self):
        config = Config()
        config.tech_lead.findings.promote = "off"

        assert config.tech_lead.findings.enabled is False
        assert config.validate() == [] or not any(
            "tech_lead.findings" in error for error in config.validate()
        )

    @pytest.mark.parametrize(
        "attr,value,fragment",
        (
            ("promote", "sometimes", "tech_lead.findings.promote"),
            ("min_evidence", 0, "tech_lead.findings.min_evidence"),
            ("max_open_promoted", 0, "tech_lead.findings.max_open_promoted"),
        ),
    )
    def test_invalid_finding_promotion_settings_fail_startup(
        self, attr: str, value, fragment: str
    ):
        """Fail-loud: a misconfigured lane must never silently never-promote."""
        config = Config()
        setattr(config.tech_lead.findings, attr, value)

        assert any(fragment in error for error in config.validate())

    def test_malformed_route_target_fails_startup(self):
        config = Config()
        config.tech_lead.findings.route = {
            "default": PromotionRouteTarget(repo="not-a-repo")
        }

        errors = config.validate()

        assert any("tech_lead.findings.route" in error for error in errors)

    @pytest.mark.parametrize(
        "route_yaml,fragment",
        (
            ("'': owner/repo", "keys must be non-empty"),
            ("completion-pipeline: ''", "must not be empty"),
            (
                "Queue: owner/one\n      queue: owner/two",
                "duplicate case-insensitive area",
            ),
            ("completion-pipeline: []", "must be 'self', 'owner/repo'"),
            ("completion-pipeline: 3", "must be 'self', 'owner/repo'"),
            ("completion-pipeline:\n        nope: x", "unknown key"),
            ("completion-pipeline:\n        scope_label: x", "requires a non-empty"),
            (
                "completion-pipeline:\n        repo: owner/repo\n"
                "        scope_label: [1]",
                "must be a label string",
            ),
        ),
    )
    def test_blank_or_ambiguous_finding_routes_fail_load(
        self, tmp_path, route_yaml: str, fragment: str
    ):
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(
            f"tech_lead:\n  findings:\n    route:\n      {route_yaml}\n"
        )

        with pytest.raises(ValueError, match=fragment):
            Config.load(config_file)

    @pytest.mark.parametrize("value", ("[]", "''", "false", "0", "3", "'gated'"))
    def test_a_non_mapping_findings_block_is_rejected_not_defaulted(
        self, tmp_path, value: str
    ):
        """#6957 round-5 review F14: the same truthiness bug, one level up.

        ``tech_lead.findings`` defaults to ``promote: gated`` with
        ``route.default: self``, so silently replacing an explicit value with
        ``{}`` ENABLES issue creation — the opposite of what an operator writing
        ``findings: false`` intends.
        """
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"tech_lead:\n  findings: {value}\n")

        with pytest.raises(ValueError, match="tech_lead.findings must be a mapping"):
            Config.load(config_file)

    @pytest.mark.parametrize(
        "body", ("tech_lead:\n  priority: P1\n", "tech_lead:\n  findings: null\n")
    )
    def test_omitted_or_null_findings_takes_the_defaults(self, tmp_path, body: str):
        """Omission and an explicit null are the only ways to accept defaults."""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(body)

        findings = Config.load(config_file).tech_lead.findings

        assert findings.promote == "gated"
        assert findings.route == {"default": PromotionRouteTarget(repo="self")}

    def test_promote_off_is_how_the_lane_is_disabled(self, tmp_path):
        """The message points here, so it had better work."""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text("tech_lead:\n  findings:\n    promote: off\n")

        assert Config.load(config_file).tech_lead.findings.enabled is False

    @pytest.mark.parametrize("value", ("[]", "''", "false", "0"))
    def test_falsy_non_mapping_route_is_rejected_not_defaulted(
        self, tmp_path, value: str
    ):
        """#6957 review F6: an explicitly supplied non-mapping must fail loudly.

        ``data.get("route") or {}`` accepted every falsy value and silently
        replaced it with ``default: self`` — quietly redirecting a cross-repo
        routing feature into the managed repo.
        """
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"tech_lead:\n  findings:\n    route: {value}\n")

        with pytest.raises(ValueError, match="route must be a mapping"):
            Config.load(config_file)

    @pytest.mark.parametrize("value", ("", "null"))
    def test_omitted_or_null_route_takes_the_default(self, tmp_path, value: str):
        """Omission and an explicit null are the only accepted 'no route table'."""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(
            "tech_lead:\n  findings:\n    min_evidence: 3\n"
            + (f"    route: {value}\n" if value else "")
        )

        findings = Config.load(config_file).tech_lead.findings

        assert findings.route == {"default": PromotionRouteTarget(repo="self")}

    def test_foreign_route_declares_the_targets_scheduling_labels(self, tmp_path):
        """#6957 review F2: a route owns the target's whole queue contract."""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(
            """
tech_lead:
  findings:
    route:
      completion-pipeline:
        repo: owner/repo
        scope_label: upstream-scope
        agent_label: agent:platform
      default: self
"""
        )

        findings = Config.load(config_file).tech_lead.findings

        target = findings.route_for("completion-pipeline")
        assert target.repo == "owner/repo"
        assert target.scope_label == "upstream-scope"
        assert target.agent_label == "agent:platform"
        assert findings.target_repos() == ("owner/repo",)

    def test_self_route_may_not_redeclare_the_managed_repos_contract(self):
        """The managed repo's scheduling labels come from its own config."""
        config = Config()
        config.tech_lead.findings.route = {
            "default": PromotionRouteTarget(repo="self", scope_label="something")
        }

        errors = config.validate()

        assert any("routes to 'self'" in error for error in errors)

    def test_foreign_route_with_a_blank_agent_label_fails_startup(self):
        config = Config()
        config.tech_lead.findings.route = {
            "default": PromotionRouteTarget(repo="owner/repo", agent_label="")
        }

        errors = config.validate()

        assert any("agent_label" in error for error in errors)

    def test_tech_lead_config_from_yaml(self, tmp_path):
        """Test loading tech_lead config from YAML."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

tech_lead:
  inherit_labels:
    - "io-e2e-test-data"
    - "team:backend"
  explicit_labels:
    - "needs-batch-review"
  milestone_strategy:
    inherit_from_issues: earliest
  priority: "P1"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.tech_lead.inherit_labels == ["io-e2e-test-data", "team:backend"]
        assert config.tech_lead.explicit_labels == ["needs-batch-review"]
        assert config.tech_lead.milestone_strategy.inherit_from_issues == "earliest"
        assert config.tech_lead.priority == "P1"

    def test_tech_lead_config_explicit_milestone(self, tmp_path):
        """Test explicit milestone overrides inherit strategy."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

tech_lead:
  milestone_strategy:
    explicit: "v2.0"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.tech_lead.milestone_strategy.explicit == "v2.0"
        # inherit_from_issues still has default but explicit takes precedence in planner
        assert config.tech_lead.milestone_strategy.inherit_from_issues == "latest"

    def test_tech_lead_config_comma_separated_labels(self, tmp_path):
        """Test that comma-separated label strings are parsed."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

tech_lead:
  inherit_labels: "label1, label2, label3"
  explicit_labels: "explicit1"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.tech_lead.inherit_labels == ["label1", "label2", "label3"]
        assert config.tech_lead.explicit_labels == ["explicit1"]

    def test_tech_lead_config_empty_section_uses_defaults(self, tmp_path):
        """Test that empty tech_lead section uses defaults."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

tech_lead: {}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.tech_lead.inherit_labels == []
        assert config.tech_lead.explicit_labels == []
        assert config.tech_lead.milestone_strategy.inherit_from_issues == "latest"
        assert config.tech_lead.priority is None

    def test_tech_lead_config_max_expedited_default(self):
        """Expedite lane cap (#6870) defaults to a small positive value."""
        assert Config().tech_lead.max_expedited == 3

    def test_tech_lead_config_max_expedited_from_yaml(self, tmp_path):
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

tech_lead:
  max_expedited: 5
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)
        assert Config.load(config_file).tech_lead.max_expedited == 5

    def test_tech_lead_config_max_expedited_zero_parses_and_validates(self, tmp_path):
        # 0 disables the lane and is a valid configuration.
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("# Test prompt")
        worktree_dir = tmp_path / "worktrees"
        worktree_dir.mkdir()
        config_content = f"""
worktrees:
  base: {worktree_dir}
default_agent:
  provider: claude-code
agents:
  agent:test:
    prompt: {prompt_file}
    model: haiku
    ai_system: claude-code
tech_lead:
  max_expedited: 0
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)
        config = Config.load(config_file)
        assert config.tech_lead.max_expedited == 0
        assert config.validate() == []

    def test_tech_lead_config_max_expedited_negative_fails_validation(self, tmp_path):
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("# Test prompt")
        worktree_dir = tmp_path / "worktrees"
        worktree_dir.mkdir()
        config_content = f"""
worktrees:
  base: {worktree_dir}
default_agent:
  provider: claude-code
agents:
  agent:test:
    prompt: {prompt_file}
    model: haiku
    ai_system: claude-code
tech_lead:
  max_expedited: -1
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)
        errors = Config.load(config_file).validate()
        assert any("max_expedited must be between 0 and 20" in e for e in errors), (
            errors
        )

    def test_tech_lead_startup_errors_unit(self):
        cls = Config().tech_lead.__class__
        assert cls().startup_errors() == []
        assert cls(max_expedited=-2).startup_errors()
        assert "between 0 and" in cls(max_expedited=-2).startup_errors()[0]

    def test_tech_lead_config_max_expedited_bound_parity_with_settings(self):
        # B3: the runtime config bound and the settings-form bound are the SAME
        # ceiling (both driven by TECH_LEAD_MAX_EXPEDITED_LIMIT), so a value that
        # startup accepts can never blow up from_config() in the settings surface.
        from issue_orchestrator.infra.config_models import (
            TECH_LEAD_MAX_EXPEDITED_LIMIT,
            TechLeadConfig,
        )

        limit = TECH_LEAD_MAX_EXPEDITED_LIMIT
        assert TechLeadConfig(max_expedited=limit).startup_errors() == []
        over = TechLeadConfig(max_expedited=limit + 1).startup_errors()
        assert over and f"between 0 and {limit}" in over[0]

        from issue_orchestrator.infra.settings_schema import ReviewSettings
        from pydantic import ValidationError

        assert (
            ReviewSettings(tech_lead_max_expedited=limit).tech_lead_max_expedited
            == limit
        )
        with pytest.raises(ValidationError):
            ReviewSettings(tech_lead_max_expedited=limit + 1)

    def test_tech_lead_config_max_expedited_over_limit_fails_validation(self, tmp_path):
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("# Test prompt")
        worktree_dir = tmp_path / "worktrees"
        worktree_dir.mkdir()
        config_content = f"""
worktrees:
  base: {worktree_dir}
default_agent:
  provider: claude-code
agents:
  agent:test:
    prompt: {prompt_file}
    model: haiku
    ai_system: claude-code
tech_lead:
  max_expedited: 21
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)
        errors = Config.load(config_file).validate()
        assert any("max_expedited must be between 0 and 20" in e for e in errors), (
            errors
        )

    def test_tech_lead_config_included_in_to_event_dict(self):
        """tech_lead config should be included in to_event_dict output."""
        config = Config()
        config.tech_lead.inherit_labels.append("test-label")
        config.tech_lead.explicit_labels.append("explicit-label")
        config.tech_lead.authority.post_comment = "propose"

        result = config.to_event_dict()

        assert "tech_lead" in result
        assert result["tech_lead"]["inherit_labels"] == ["test-label"]
        assert result["tech_lead"]["explicit_labels"] == ["explicit-label"]
        assert result["tech_lead"]["max_expedited"] == 3
        assert result["tech_lead"]["dedup"] == {
            "enabled": True,
            "similarity_threshold": 0.72,
        }
        assert result["tech_lead"]["findings"] == {
            "promote": "gated",
            "min_evidence": 2,
            "max_open_promoted": 3,
            "route": {
                "default": {
                    "repo": "self",
                    "scope_label": None,
                    "agent_label": None,
                }
            },
        }
        assert (
            result["tech_lead"]["milestone_strategy"]["inherit_from_issues"] == "latest"
        )
        # All five graduated-authority modes are operator-visible (#6761 F7).
        assert result["tech_lead"]["authority"] == {
            "post_comment": "propose",
            "create_issue": "execute",
            "flag_pattern": "execute",
            "reset_retry": "propose",
            "kill_hung_session": "propose",
        }

    def test_tech_lead_authority_defaults(self):
        """Authority defaults: GitHub-write actions execute, act-level propose."""
        config = Config()

        assert config.tech_lead.authority.post_comment == "execute"
        assert config.tech_lead.authority.create_issue == "execute"
        assert config.tech_lead.authority.flag_pattern == "execute"
        assert config.tech_lead.authority.reset_retry == "propose"
        assert config.tech_lead.authority.kill_hung_session == "propose"
        assert config.validate() == [] or not any(
            "tech_lead.authority" in e for e in config.validate()
        )

    def test_tech_lead_authority_from_yaml(self, tmp_path):
        """tech_lead.authority.<key> parses per-action-type modes."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

tech_lead:
  authority:
    post_comment: propose
    create_issue: propose
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.tech_lead.authority.post_comment == "propose"
        assert config.tech_lead.authority.create_issue == "propose"
        # Unset keys keep defaults
        assert config.tech_lead.authority.flag_pattern == "execute"
        assert config.tech_lead.authority.reset_retry == "propose"

    def test_tech_lead_max_concurrent_defaults_to_none(self):
        """Unset tech_lead.max_concurrent shares the worker budget (None)."""
        assert Config().tech_lead.max_concurrent is None

    def test_tech_lead_max_concurrent_from_yaml(self, tmp_path):
        """tech_lead.max_concurrent parses into a reserved additive budget."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

tech_lead:
  max_concurrent: 2
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.tech_lead.max_concurrent == 2
        assert config.tech_lead.to_event_dict()["max_concurrent"] == 2

    def test_tech_lead_max_concurrent_rejects_below_one(self):
        """A reserved budget < 1 is a misconfiguration; fail startup loudly."""
        config = Config()
        config.tech_lead.max_concurrent = 0
        errors = config.validate()
        assert any("tech_lead.max_concurrent" in e for e in errors), errors

    def test_tech_lead_authority_bad_value_rejected_at_load(self, tmp_path):
        """A mode outside execute|propose fails config parsing loudly."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

tech_lead:
  authority:
    post_comment: yolo
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        with pytest.raises(ValueError, match="tech_lead.authority.post_comment"):
            Config.load(config_file)

    @pytest.mark.parametrize("key", ["kill_hung_session"])
    def test_tech_lead_authority_unwired_act_level_execute_is_startup_error(self, key):
        """Unwired act-level 'execute' must fail startup validation, never no-op."""
        config = Config()
        setattr(config.tech_lead.authority, key, "execute")

        errors = config.validate()

        assert any(
            f"tech_lead.authority.{key}" in e and "#6764" in e for e in errors
        ), errors

    def test_tech_lead_authority_reset_retry_execute_is_valid_at_startup(self):
        """reset_retry is wired (#6764 first slice): execute passes validation."""
        config = Config()
        config.tech_lead.authority.reset_retry = "execute"

        errors = config.validate()

        assert not any("tech_lead.authority.reset_retry" in e for e in errors), errors

    def test_tech_lead_authority_mode_for_floor_and_unknown(self):
        """escalate_to_human always executes; unknown action types raise."""
        config = Config()
        config.tech_lead.authority.post_comment = "propose"

        assert config.tech_lead.authority.mode_for("escalate_to_human") == "execute"
        assert config.tech_lead.authority.mode_for("post_comment") == "propose"
        with pytest.raises(ValueError, match="unknown tech_lead action type"):
            config.tech_lead.authority.mode_for("push_code")

    def test_tech_lead_health_review_default_disabled(self):
        """Periodic review is off; problem-storm escalation has safe defaults."""
        config = Config()

        assert config.tech_lead.health_review.interval_minutes == 0
        assert config.tech_lead.health_review.storm_threshold == 3
        assert config.tech_lead.health_review.storm_window_minutes == 5

    def test_tech_lead_health_review_from_yaml(self, tmp_path):
        """tech_lead.health_review.interval_minutes parses as an int."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

tech_lead:
  health_review:
    interval_minutes: 240
    storm_threshold: 4
    storm_window_minutes: 10
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.tech_lead.health_review.interval_minutes == 240
        assert config.tech_lead.health_review.storm_threshold == 4
        assert config.tech_lead.health_review.storm_window_minutes == 10

    def test_tech_lead_health_review_negative_interval_is_startup_error(self):
        """A negative interval is a misconfiguration, never silently disabled.

        The documented disable value is exactly 0; ``-5`` must fail startup
        validation loudly so an operator cannot accidentally turn the trigger
        off with an out-of-range number (#6763 finding 8).
        """
        config = Config()
        config.tech_lead.health_review.interval_minutes = -5

        errors = config.validate()

        assert any(
            "tech_lead.health_review.interval_minutes" in e and "-5" in e
            for e in errors
        ), errors

    def test_tech_lead_health_review_startup_errors_reports_negative(self):
        """The owning config block reports its own invariant (single owner)."""
        config = Config()
        config.tech_lead.health_review.interval_minutes = -1

        errors = config.tech_lead.health_review.startup_errors()

        assert len(errors) == 1
        assert ">= 0" in errors[0]

    def test_tech_lead_health_review_zero_and_positive_have_no_startup_errors(self):
        """0 (disabled) and any positive interval are both valid."""
        config = Config()
        config.tech_lead.health_review.interval_minutes = 0
        assert config.tech_lead.health_review.startup_errors() == []
        config.tech_lead.health_review.interval_minutes = 240
        assert config.tech_lead.health_review.startup_errors() == []

    @pytest.mark.parametrize(
        ("attr", "value", "path"),
        [
            ("storm_threshold", -1, "storm_threshold"),
            ("storm_window_minutes", 0, "storm_window_minutes"),
        ],
    )
    def test_tech_lead_health_review_rejects_invalid_storm_settings(
        self, attr: str, value: int, path: str
    ) -> None:
        config = Config()
        setattr(config.tech_lead.health_review, attr, value)

        errors = config.validate()

        assert any(f"tech_lead.health_review.{path}" in error for error in errors)


class TestSchedulingConfig:
    """Tests for scheduling configuration."""

    def test_scheduling_defaults(self):
        """SchedulingConfig should have sensible defaults."""
        config = Config()
        assert config.scheduling.default_priority_tier == 1

    def test_scheduling_config_from_yaml(self, tmp_path):
        """Test loading scheduling config from YAML."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

scheduling:
  default_priority_tier: 2
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.scheduling.default_priority_tier == 2

    def test_scheduling_config_included_in_to_event_dict(self):
        """Scheduling config should be included in to_event_dict output."""
        config = Config()
        config.scheduling.default_priority_tier = 3

        result = config.to_event_dict()

        assert "scheduling" in result
        assert result["scheduling"]["default_priority_tier"] == 3


class TestPriorityValidation:
    """Tests for priority-related validation."""

    def test_scheduling_default_priority_tier_invalid(self, tmp_path):
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

scheduling:
  default_priority_tier: 12
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        errors = config.validate()
        assert "scheduling.default_priority_tier must be between 0 and 9" in errors

    def test_tech_lead_priority_invalid_format(self, tmp_path):
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

tech_lead:
  priority: "priority:high"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        errors = config.validate()
        assert "tech_lead.priority must be a tier like 'P0'..'P9'" in errors


class TestValidationConfig:
    """Tests for validation.* configuration."""

    @pytest.mark.parametrize(
        "unsupported_key",
        ["cmd", "timeout_seconds", "pre_push_dirty_check", "bogus"],
    )
    def test_unsupported_validation_keys_fail_fast(self, tmp_path, unsupported_key):
        unsupported_values = {
            "cmd": '"make validate"',
            "timeout_seconds": "400",
            "pre_push_dirty_check": "tracked",
            "bogus": "true",
        }
        config_content = f"""
agents:
  agent:test:
    prompt: /tmp/prompt.txt
validation:
  {unsupported_key}: {unsupported_values[unsupported_key]}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        with pytest.raises(ValueError) as exc_info:
            Config.load(config_file)
        message = str(exc_info.value)
        assert f"validation.{unsupported_key}" in message
        assert "Supported keys:" in message
        assert "Legacy" not in message
        assert "Migrate" not in message

    def test_validation_warns_when_only_quick_command_configured(
        self, tmp_path, caplog
    ):
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
validation:
  quick:
    cmd: make test
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        Config.load(config_file)

        assert "validation.publish.cmd is not" in caplog.text

    def test_validation_warns_when_only_publish_command_configured(
        self, tmp_path, caplog
    ):
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
validation:
  publish:
    cmd: make validate-pr
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        Config.load(config_file)

        assert "validation.quick.cmd is not" in caplog.text

    def test_publish_dirty_check_all_is_valid(self):
        config = Config()
        config.validation.publish.dirty_check = "all"

        errors = config.validate()
        assert not any("validation.publish.dirty_check" in err for err in errors)

    def test_publish_dirty_check_invalid_fails_validation(self):
        config = Config()
        config.validation.publish.dirty_check = "invalid-mode"

        errors = config.validate()
        assert (
            "validation.publish.dirty_check must be one of: tracked, unstaged, all, off"
            in errors
        )

    def test_junit_xml_paths_load_from_yaml(self, tmp_path):
        """validation.junit_xml_paths must round-trip through YAML — without
        this the documented opt-in for the structured issue-drawer JUnit
        view silently no-ops."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
validation:
  quick:
    cmd: make test
  junit_xml_paths:
    - "test-results.xml"
    - "build/test-results/test/*.xml"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)
        config = Config.load(config_file)
        assert config.validation.junit_xml_paths == (
            "test-results.xml",
            "build/test-results/test/*.xml",
        )

    def test_junit_xml_paths_default_empty_when_omitted(self, tmp_path):
        """Repos that don't opt in keep the empty default."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
validation:
  quick:
    cmd: make test
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)
        config = Config.load(config_file)
        assert config.validation.junit_xml_paths == ()

    def test_junit_xml_paths_round_trip_through_to_dict(self, tmp_path):
        """Config.to_dict must serialize junit_xml_paths so reload preserves it."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
validation:
  quick:
    cmd: make test
  junit_xml_paths:
    - "test-results.xml"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)
        config = Config.load(config_file)
        round_tripped = config.to_dict()
        assert round_tripped["validation"]["junit_xml_paths"] == ["test-results.xml"]


class TestConfigSectionErrors:
    """Test clear error messages for invalid config sections."""

    def test_string_section_gives_clear_error(self, tmp_path):
        """When a section is a string instead of dict, error is clear."""
        from issue_orchestrator.infra.config import ConfigSectionError

        config_content = """
repo: owner/repo-name
agents: {}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        with pytest.raises(ConfigSectionError) as exc_info:
            Config.load(config_file)

        error_msg = str(exc_info.value)
        assert "Invalid config section 'repo'" in error_msg
        assert "Got string: 'owner/repo-name'" in error_msg
        assert "Expected a mapping" in error_msg

    def test_list_section_gives_clear_error(self, tmp_path):
        """When a section is a list instead of dict, error is clear."""
        from issue_orchestrator.infra.config import ConfigSectionError

        config_content = """
agents:
  - item1
  - item2
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        with pytest.raises(ConfigSectionError) as exc_info:
            Config.load(config_file)

        error_msg = str(exc_info.value)
        assert "Invalid config section 'agents'" in error_msg
        assert "Got a list" in error_msg

    def test_none_section_treated_as_empty_dict(self, tmp_path):
        """Section with only comments (None in YAML) is treated as empty."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

repo:
  # Just a comment, creates None
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        # Should not raise - None becomes {}
        config = Config.load(config_file)
        assert config.repo is None  # No repo name set

    def test_nested_section_error_gives_context(self, tmp_path):
        """Error in nested section shows full path."""
        from issue_orchestrator.infra.config import ConfigSectionError

        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

execution:
  concurrency: "not a dict"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        with pytest.raises(ConfigSectionError) as exc_info:
            Config.load(config_file)

        error_msg = str(exc_info.value)
        assert "concurrency" in error_msg
        assert "Got string" in error_msg


class TestEnvVarSubstitution:
    """Tests for ${VAR} environment variable substitution in config."""

    def test_expands_env_var_in_string(self, tmp_path, monkeypatch):
        """${VAR} in config value is replaced with env var value."""
        monkeypatch.setenv("TEST_CLAIMANT_ID", "prod-west-1")

        config_content = """
claims:
  enabled: true
  claimant_id: "${TEST_CLAIMANT_ID}"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.claims.claimant_id == "prod-west-1"

    def test_expands_multiple_env_vars_in_string(self, tmp_path, monkeypatch):
        """Multiple ${VAR} references in one string are all expanded."""
        monkeypatch.setenv("ENV_NAME", "prod")
        monkeypatch.setenv("REGION", "west")

        config_content = """
claims:
  enabled: true
  claimant_id: "${ENV_NAME}-${REGION}-orchestrator"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.claims.claimant_id == "prod-west-orchestrator"

    def test_expands_env_var_in_nested_config(self, tmp_path, monkeypatch):
        """${VAR} works in deeply nested config values."""
        monkeypatch.setenv("GITHUB_TOKEN_VAR", "MY_GITHUB_TOKEN")

        config_content = """
repo:
  github:
    token_env: "${GITHUB_TOKEN_VAR}"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.github_token_env == "MY_GITHUB_TOKEN"

    def test_parses_repo_github_keyring_fields(self, tmp_path):
        """repo.github keyring fields load into config."""
        config_content = """
repo:
  github:
    keyring_service: "tixmeup-github"
    keyring_username: "bruce"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.github_keyring_service == "tixmeup-github"
        assert config.github_keyring_username == "bruce"

    def test_parses_repo_github_app_fields(self, tmp_path):
        """repo.github.app fields load into config."""
        config_content = """
repo:
  github:
    app:
      client_id: "Iv23example"
      app_id: "4250697"
      installation_id: "145305179"
      private_key_path: "~/.config/issue-orchestrator/github-apps/bot.pem"
      private_key_env: "ISSUE_ORCH_GITHUB_APP_PRIVATE_KEY"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.github_app_client_id == "Iv23example"
        assert config.github_app_id == "4250697"
        assert config.github_app_installation_id == "145305179"
        assert (
            config.github_app_private_key_path
            == "~/.config/issue-orchestrator/github-apps/bot.pem"
        )
        assert config.github_app_private_key_env == "ISSUE_ORCH_GITHUB_APP_PRIVATE_KEY"
        assert config.github_app_auth_configured() is True

    def test_expands_env_var_in_list(self, tmp_path, monkeypatch):
        """${VAR} works in list items."""
        monkeypatch.setenv("EXCLUDE_LABEL", "wip")

        config_content = """
filtering:
  exclude_labels:
    - "${EXCLUDE_LABEL}"
    - "draft"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert "wip" in config.filtering.exclude_labels
        assert "draft" in config.filtering.exclude_labels

    def test_parses_exclude_label_prefixes_from_string(self, tmp_path):
        """filtering.exclude_label_prefixes accepts comma-separated strings."""
        config_content = """
filtering:
  exclude_label_prefixes: "io:e2e:, tmp:"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.filtering.exclude_label_prefixes == ["io:e2e:", "tmp:"]

    def test_expands_env_var_in_exclude_label_prefixes(self, tmp_path, monkeypatch):
        """${VAR} works in filtering.exclude_label_prefixes list items."""
        monkeypatch.setenv("EXCLUDE_PREFIX", "io:e2e:")

        config_content = """
filtering:
  exclude_label_prefixes:
    - "${EXCLUDE_PREFIX}"
    - "tmp:"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.filtering.exclude_label_prefixes == ["io:e2e:", "tmp:"]

    def test_error_on_missing_env_var(self, tmp_path):
        """Missing env var raises ConfigEnvVarError with clear message."""
        from issue_orchestrator.infra.config import ConfigEnvVarError

        config_content = """
claims:
  enabled: true
  claimant_id: "${NONEXISTENT_VAR_12345}"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        with pytest.raises(ConfigEnvVarError) as exc_info:
            Config.load(config_file)

        error_msg = str(exc_info.value)
        assert "NONEXISTENT_VAR_12345" in error_msg
        assert "not set" in error_msg

    def test_error_message_includes_config_path(self, tmp_path):
        """Error message shows where in config the missing var was referenced."""
        from issue_orchestrator.infra.config import ConfigEnvVarError

        config_content = """
claims:
  claimant_id: "${MISSING_VAR}"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        with pytest.raises(ConfigEnvVarError) as exc_info:
            Config.load(config_file)

        error_msg = str(exc_info.value)
        assert "claims.claimant_id" in error_msg

    def test_literal_string_without_env_var_unchanged(self, tmp_path):
        """Strings without ${VAR} are unchanged."""
        config_content = """
claims:
  enabled: true
  claimant_id: "literal-string-value"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.claims.claimant_id == "literal-string-value"

    def test_numbers_and_booleans_unchanged(self, tmp_path):
        """Non-string values (numbers, booleans) pass through unchanged."""
        config_content = """
claims:
  enabled: true
  lease_seconds: 900
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.claims.enabled is True
        assert config.claims.lease_seconds == 900


class TestConfigSerialization:
    """Tests for Config.to_dict() and Config.save() methods."""

    def test_to_dict_basic(self, tmp_path):
        """Test to_dict returns basic config structure."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:test:
    prompt: {prompt_file}
    model: sonnet

execution:
  concurrency:
    max_concurrent_sessions: 5
    session_timeout_minutes: 30
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result = config.to_dict()

        assert result["repo"]["name"] == "owner/repo"
        assert "agents" in result
        assert "agent:test" in result["agents"]
        assert result["execution"]["concurrency"]["max_concurrent_sessions"] == 5
        assert result["execution"]["concurrency"]["session_timeout_minutes"] == 30

    def test_to_dict_e2e_settings(self, tmp_path):
        """Test to_dict includes E2E settings when non-default."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:test:
    prompt: {prompt_file}

e2e:
  enabled: true
  auto_run_interval_minutes: 60
  stop_on_first_failure: true
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result = config.to_dict()

        assert "e2e" in result
        assert result["e2e"]["enabled"] is True
        assert result["e2e"]["auto_run_interval_minutes"] == 60
        assert result["e2e"]["stop_on_first_failure"] is True

    def test_to_dict_omits_default_review_rework_cycle_limit(self, tmp_path):
        """Default review max_rework_cycles should not churn saved config."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:test:
    prompt: {prompt_file}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result = config.to_dict()

        assert "review" not in result or "max_rework_cycles" not in result["review"]

    def test_to_dict_round_trips_non_default_internal_review_settings(
        self,
        tmp_path,
    ):
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(
            "review:\n"
            "  internal:\n"
            "    enabled: true\n"
            "    max_rounds: 4\n"
            "    instructions: .io/custom-internal-review.md\n",
            encoding="utf-8",
        )

        result = Config.load(config_file).to_dict()

        assert result["review"]["internal"] == {
            "enabled": True,
            "max_rounds": 4,
            "instructions": ".io/custom-internal-review.md",
        }

    def test_to_dict_includes_retrospective_review_settings(self, tmp_path):
        """Retrospective review labels round-trip through saved config."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:coder:
    prompt: {prompt_file}
  agent:reviewer:
    prompt: {prompt_file}

review:
  default: agent:reviewer
  retrospective:
    enabled: true
    trigger_label: lack-of-review-redo
    reviewed_label: lack-of-review-reviewed
    changes_requested_label: lack-of-review-needs-work
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result = config.to_dict()

        assert result["review"]["retrospective"] == {
            "enabled": True,
            "trigger_label": "lack-of-review-redo",
            "reviewed_label": "lack-of-review-reviewed",
            "changes_requested_label": "lack-of-review-needs-work",
        }

    def test_to_dict_sqlite_backup_settings(self, tmp_path):
        """Test to_dict includes sqlite_backup settings when non-default."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:test:
    prompt: {prompt_file}

sqlite_backup:
  enabled: false
  cadence_hours: 6
  check_interval_minutes: 15
  retention_daily: 7
  retention_weekly: 4
  enforce_on_startup: false
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result = config.to_dict()

        assert "sqlite_backup" in result
        assert result["sqlite_backup"]["enabled"] is False
        assert result["sqlite_backup"]["cadence_hours"] == 6
        assert result["sqlite_backup"]["check_interval_minutes"] == 15
        assert result["sqlite_backup"]["retention_daily"] == 7
        assert result["sqlite_backup"]["retention_weekly"] == 4
        assert result["sqlite_backup"]["enforce_on_startup"] is False

    def test_to_dict_timeline_settings(self, tmp_path):
        """Test to_dict includes timeline settings when non-default."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:test:
    prompt: {prompt_file}

timeline:
  max_records: 1200
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result = config.to_dict()

        assert "timeline" in result
        assert result["timeline"]["max_records"] == 1200

    def test_to_dict_omits_defaults(self, tmp_path):
        """Test to_dict omits default values to keep output minimal."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:test:
    prompt: {prompt_file}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result = config.to_dict()

        # E2E should not be present since all defaults
        assert "e2e" not in result

        # Labels should not be present since all defaults
        assert "labels" not in result

        # Retry should not be present since interrupted-session retry defaults are minimal
        assert "retry" not in result

    def test_save_writes_yaml(self, tmp_path):
        """Test save() writes valid YAML to file."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:test:
    prompt: {prompt_file}

execution:
  concurrency:
    max_concurrent_sessions: 7
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Change a setting
        config.max_concurrent_sessions = 10

        # Save to new file
        output_file = tmp_path / "output.yaml"
        config.save(output_file)

        # Verify file exists and contains expected content
        assert output_file.exists()
        content = output_file.read_text()
        assert "max_concurrent_sessions: 10" in content
        assert "owner/repo" in content

    def test_save_uses_config_path_by_default(self, tmp_path):
        """Test save() uses config_path when no path specified."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:test:
    prompt: {prompt_file}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        config.max_concurrent_sessions = 15

        # Save without specifying path
        result_path = config.save()

        assert result_path == config.config_path
        content = config_file.read_text()
        assert "max_concurrent_sessions: 15" in content

    def test_save_raises_without_path(self):
        """Test save() raises ValueError when no path is available."""
        config = Config()

        with pytest.raises(ValueError, match="No path specified"):
            config.save()

    def test_to_dict_roundtrip(self, tmp_path):
        """Test that to_dict output can be loaded back."""
        import yaml

        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:test:
    prompt: {prompt_file}
    model: haiku
    timeout_minutes: 60

execution:
  concurrency:
    max_concurrent_sessions: 8
    session_timeout_minutes: 90

e2e:
  enabled: true
  auto_run_interval_minutes: 45
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result_dict = config.to_dict()

        # Write dict back to YAML
        output_file = tmp_path / "roundtrip.yaml"
        with open(output_file, "w") as f:
            yaml.dump(result_dict, f)

        # Load from the new file
        config2 = Config.load(output_file)

        # Key settings should match
        assert config2.repo == config.repo
        assert config2.max_concurrent_sessions == config.max_concurrent_sessions
        assert config2.session_timeout_minutes == config.session_timeout_minutes
        assert config2.e2e.enabled == config.e2e.enabled
        assert (
            config2.e2e.auto_run_interval_minutes
            == config.e2e.auto_run_interval_minutes
        )


class TestHooksConfig:
    """Tests for hooks configuration parsing."""

    def test_hooks_config_defaults(self, tmp_path):
        """Test default hooks config values."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Default values
        assert config.hooks.ai_gate.interval_days == 7
        assert config.hooks.ai_gate.dangerous_allow_failure is False

    def test_hooks_config_custom_interval(self, tmp_path):
        """Test custom AI gate test interval."""
        config_content = """
worktrees:
  base: /tmp

hooks:
  ai_gate:
    interval_days: 14

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.hooks.ai_gate.interval_days == 14
        assert config.hooks.ai_gate.dangerous_allow_failure is False

    def test_hooks_config_disabled(self, tmp_path):
        """Test AI gate test disabled with interval_days=0."""
        config_content = """
worktrees:
  base: /tmp

hooks:
  ai_gate:
    interval_days: 0

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.hooks.ai_gate.interval_days == 0

    def test_hooks_config_dangerous_allow_failure(self, tmp_path):
        """Test dangerous_allow_failure setting."""
        config_content = """
worktrees:
  base: /tmp

hooks:
  ai_gate:
    dangerous_allow_failure: true

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.hooks.ai_gate.dangerous_allow_failure is True

    def test_hooks_config_in_to_event_dict(self, tmp_path):
        """Test hooks config is included in to_event_dict()."""
        config_content = """
worktrees:
  base: /tmp

hooks:
  ai_gate:
    interval_days: 30
    dangerous_allow_failure: true

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        event_dict = config.to_event_dict()

        assert "hooks" in event_dict
        assert event_dict["hooks"]["ai_gate"]["interval_days"] == 30
        assert event_dict["hooks"]["ai_gate"]["dangerous_allow_failure"] is True

    def test_hooks_config_to_dict_non_default(self, tmp_path):
        """Test to_dict() includes hooks when non-default."""
        config_content = """
worktrees:
  base: /tmp

hooks:
  ai_gate:
    interval_days: 14
    dangerous_allow_failure: true

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result = config.to_dict()

        assert "hooks" in result
        assert result["hooks"]["ai_gate"]["interval_days"] == 14
        assert result["hooks"]["ai_gate"]["dangerous_allow_failure"] is True

    def test_hooks_config_to_dict_default_values(self, tmp_path):
        """Test to_dict() omits hooks when all values are default."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result = config.to_dict()

        # Hooks section should not be present when using defaults
        assert "hooks" not in result
