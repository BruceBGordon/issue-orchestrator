"""Unit tests for the setup wizard."""

import shutil

import pytest
import issue_orchestrator.entrypoints.cli_tools.setup_wizard as setup_wizard_module
import issue_orchestrator.entrypoints.cli_tools.readiness_launch as readiness_launch_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from issue_orchestrator.entrypoints.cli_tools.setup_wizard import (
    DetectedState,
    FileCollector,
    PlannedWrite,
    Prompter,
    ConsolePrompter,
    check_prerequisites,
    create_starter_prompt,
    create_tech_lead_review_prompt,
    detect_repo,
    fetch_github_labels,
    find_existing_config,
    run_wizard,
    scan_existing_repo,
    wizard_existing_project,
    wizard_new_project,
    write_config,
)
from issue_orchestrator.entrypoints.setup_wizard_common import (
    build_agent_checks,
    find_existing_default_config,
    plan_setup_labels,
    write_missing_setup_prompts,
)
from issue_orchestrator.execution.repository_setup_artifacts import (
    plan_missing_setup_prompts,
)
from issue_orchestrator.ports.repository_setup import (
    RepositorySetupGitHubVerification,
)


@pytest.mark.parametrize(
    ("review", "section", "wait_for_review"),
    [
        ({"tech_lead_review_agent": "agent:lead"}, "with_tech_lead", None),
        ({"enabled": True}, "without_tech_lead", True),
        ({}, "without_tech_lead", False),
    ],
)
def test_generated_configs_default_to_review_gated_worktree_cleanup(
    review: dict, section: str, wait_for_review: bool | None,
) -> None:
    cleanup = setup_wizard_module.default_cleanup_config({"review": review})

    assert cleanup[section]["remove_worktrees"] is True
    if wait_for_review is not None:
        assert cleanup[section]["wait_for_code_review"] is wait_for_review


@pytest.fixture(autouse=True)
def _verified_setup_github_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _verify(repo_name, authorization):
        return RepositorySetupGitHubVerification(
            identity="setup-test-user",
            repository=repo_name,
            auth_kind=(
                "github_app" if authorization.kind == "github_app" else "personal"
            ),
            source="test credential",
            normalized_authorization=authorization,
        )

    monkeypatch.setattr(
        setup_wizard_module,
        "verify_repository_setup_github_authorization",
        _verify,
    )


class MockPrompter:
    """Mock prompter for testing wizard flows."""

    def __init__(self, answers: list):
        """Initialize with a list of answers to return in order.

        Args:
            answers: List of values to return for input/yes_no/choice calls.
                    For yes_no, use True/False.
                    For choice, use the choice string.
                    For input, use the string value.
        """
        self.answers = list(answers)
        self.answer_index = 0
        self.printed: list[str] = []
        self.questions_asked: list[str] = []

    def _get_answer(self, question: str):
        """Get the next answer from the queue."""
        self.questions_asked.append(question)
        if self.answer_index >= len(self.answers):
            raise IndexError(f"No more answers available for question: {question}")
        answer = self.answers[self.answer_index]
        self.answer_index += 1
        return answer

    def print(self, message: str) -> None:
        self.printed.append(message)

    def input(self, question: str, default: str = "") -> str:
        answer = self._get_answer(question)
        return answer if answer != "" else default

    def yes_no(self, question: str, default: bool = True) -> bool:
        if question == "Enable coder-owned internal reviewer loop?" and (
            self.answer_index >= len(self.answers)
            or not isinstance(self.answers[self.answer_index], bool)
        ):
            return default
        answer = self._get_answer(question)
        if isinstance(answer, bool):
            return answer
        # Allow string answers
        return answer.lower() in ("y", "yes", "true")

    def choice(
        self, question: str, choices: list[str], allow_custom: bool = False
    ) -> str:
        return self._get_answer(question)


def test_prompt_int_retries_on_invalid_input() -> None:
    """Numeric wizard prompts should recover instead of crashing."""
    prompter = MockPrompter(["oops", "8080"])

    value = setup_wizard_module._prompt_int(  # noqa: SLF001
        prompter,
        "Web Dashboard Port",
        8080,
        min_value=0,
        max_value=65535,
    )

    assert value == 8080
    assert any("Invalid number" in msg for msg in prompter.printed)


def test_wizard_collects_internal_reviewer_configuration() -> None:
    prompter = MockPrompter([True, "3", ".io/fast-review.md"])
    config: dict[str, object] = {
        "review": {"enabled": True},
    }

    setup_wizard_module._collect_internal_reviewer(prompter, config)  # noqa: SLF001

    assert config["review"]["internal"] == {
        "enabled": True,
        "max_rounds": 3,
        "instructions": ".io/fast-review.md",
    }


def test_wizard_preserves_existing_internal_reviewer_configuration(
    tmp_path: Path,
) -> None:
    config: dict[str, object] = {
        "review": {
            "enabled": True,
            "internal": {
                "enabled": True,
                "max_rounds": 7,
                "instructions": ".io/custom-internal-review.md",
            },
        },
    }
    prompter = MockPrompter([True, "", ""])

    setup_wizard_module._collect_internal_reviewer(prompter, config)  # noqa: SLF001

    assert config["review"]["internal"] == {
        "enabled": True,
        "max_rounds": 7,
        "instructions": ".io/custom-internal-review.md",
    }
    planned = plan_missing_setup_prompts(config, tmp_path)
    assert [(item.agent, item.path) for item in planned] == [
        ("internal-review", tmp_path / ".io/custom-internal-review.md"),
    ]


def test_wizard_explicitly_disables_existing_internal_reviewer(
    tmp_path: Path,
) -> None:
    config: dict[str, object] = {
        "review": {
            "enabled": True,
            "internal": {
                "enabled": True,
                "max_rounds": 7,
                "instructions": ".io/custom-internal-review.md",
            },
        },
    }
    prompter = MockPrompter([False])

    setup_wizard_module._collect_internal_reviewer(prompter, config)  # noqa: SLF001

    assert config["review"]["internal"] == {
        "enabled": False,
        "max_rounds": 7,
        "instructions": ".io/custom-internal-review.md",
    }
    assert plan_missing_setup_prompts(config, tmp_path) == ()


def test_prompt_claude_session_interactions_enables_rule() -> None:
    """Claude-backed onboarding should offer startup interactions once."""
    config = {
        "agents": {
            "agent:backend": {
                "provider": "claude-code",
            }
        },
        "execution": {"concurrency": {"max_concurrent_sessions": 3}},
    }
    prompter = MockPrompter([True])

    setup_wizard_module._prompt_claude_session_interactions(  # noqa: SLF001
        config, prompter
    )

    assert config["execution"]["session_interactions"] == {"enabled": True}
    printed = "\n".join(prompter.printed)
    assert "auto-accept this trusted startup prompt" in printed


def test_prompt_claude_session_interactions_can_be_declined() -> None:
    """Declining startup interactions should leave the default disabled state."""
    config = {
        "agents": {
            "agent:backend": {
                "provider": "claude-code",
            }
        },
        "execution": {"concurrency": {"max_concurrent_sessions": 3}},
    }
    prompter = MockPrompter([False])

    setup_wizard_module._prompt_claude_session_interactions(  # noqa: SLF001
        config, prompter
    )

    assert "session_interactions" not in config["execution"]


class TestCreateStarterPrompt:
    """Test the create_starter_prompt function."""

    def test_creates_prompt_file(self, tmp_path):
        """Test that starter prompt is created with correct content."""
        prompt_path = tmp_path / "prompts" / "backend.md"

        create_starter_prompt("agent:backend", prompt_path)

        assert prompt_path.exists()
        content = prompt_path.read_text()
        assert "Backend Agent Prompt" in content
        assert "{issue_number}" in content
        assert "{issue_title}" in content
        assert "coding-done" in content

    def test_creates_parent_directories(self, tmp_path):
        """Test that parent directories are created."""
        prompt_path = tmp_path / "deep" / "nested" / "prompt.md"

        create_starter_prompt("agent:test", prompt_path)

        assert prompt_path.exists()
        assert prompt_path.parent.exists()

    def test_extracts_agent_short_name(self, tmp_path):
        """Test that agent short name is extracted correctly."""
        prompt_path = tmp_path / "prompt.md"

        create_starter_prompt("agent:frontend-ui", prompt_path)

        content = prompt_path.read_text()
        assert "Frontend-Ui Agent Prompt" in content


def test_setup_wizard_ui_mode_copy_is_client_neutral() -> None:
    """UI mode copy should not imply localhost-only browser access."""
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "issue_orchestrator"
        / "entrypoints"
        / "cli_tools"
        / "setup_wizard.py"
    )
    text = source.read_text(encoding="utf-8")

    assert "Browser dashboard (recommended)" in text
    assert "forwarded client URL" in text
    assert "Browser dashboard at localhost (recommended)" not in text


class TestCreateTechLeadReviewPrompt:
    """Test the create_tech_lead_review_prompt function."""

    def test_creates_tech_lead_prompt_with_labels(self, tmp_path):
        """Test that tech_lead prompt is created with label values substituted."""
        prompt_path = tmp_path / "tech-lead.md"

        create_tech_lead_review_prompt(
            prompt_path, "needs-tech-lead-review", "tech-lead-reviewed"
        )

        assert prompt_path.exists()
        content = prompt_path.read_text()

        # Check labels are substituted (not placeholders)
        assert "needs-tech-lead-review" in content
        assert "tech-lead-reviewed" in content
        assert "{review_label}" not in content
        assert "{reviewed_label}" not in content

    def test_forbids_gh_and_promises_manifest_labeling(self, tmp_path):
        """Test the manifest contract: no gh usage, orchestrator labels manifest PRs."""
        prompt_path = tmp_path / "tech-lead.md"

        create_tech_lead_review_prompt(
            prompt_path, "my-review-label", "my-reviewed-label"
        )

        content = prompt_path.read_text()

        # The agent must never call gh (reads or writes)
        assert 'gh pr list --label "my-review-label"' not in content
        assert "gh pr comment" not in content
        assert "gh issue create" not in content
        # Labels still appear: the selection label and the promised outcome label
        assert "my-review-label" in content
        assert "my-reviewed-label" in content

    def test_uses_injected_run_dir_not_run_scans(self, tmp_path):
        """Guardrail: prompts must use the active run contract, never scan runs.

        Every managed session gets ISSUE_ORCHESTRATOR_RUN_DIR; wildcard
        scans over sessions/* with head -1 can pick a stale run whenever the
        worktree holds more than one (#6768 B2). Covers the generated prompt
        and the packaged example prompts.
        """
        import re

        prompt_path = tmp_path / "tech-lead.md"
        create_tech_lead_review_prompt(prompt_path, "review", "reviewed")
        sources = {"generated prompt": prompt_path.read_text()}

        repo_root = Path(__file__).resolve().parents[2]
        for example in ("tech-lead-review.md", "tech-lead-data-sources.md"):
            sources[example] = (
                repo_root / "examples" / "prompts" / example
            ).read_text()

        forbidden = re.compile(r"sessions/\*|head -1|ls -d")
        for name, text in sources.items():
            assert "ISSUE_ORCHESTRATOR_RUN_DIR" in text, (
                f"{name} must use the run contract"
            )
            match = forbidden.search(text)
            assert match is None, (
                f"{name} contains run-scan discovery: {match.group(0)!r}"
            )

    def test_includes_flavor_assignment_contract(self, tmp_path):
        """Prompts must lead with the assignment contract (ADR-0031).

        Both tech_lead flavors share one launch path; the prompt must direct the
        agent to read tech-lead-assignment.json and behave per flavor, including
        never auditing PRs during a failure investigation. Covers the
        generated prompt and the packaged/dogfood prompt variants.
        """
        prompt_path = tmp_path / "tech-lead.md"
        create_tech_lead_review_prompt(prompt_path, "review", "reviewed")
        sources = {"generated prompt": prompt_path.read_text()}

        repo_root = Path(__file__).resolve().parents[2]
        for variant in (
            repo_root / "examples" / "prompts" / "tech-lead-review.md",
            repo_root / "repo-specific" / "prompts" / "tech-lead.md",
        ):
            sources[str(variant.relative_to(repo_root))] = variant.read_text()

        for name, text in sources.items():
            assert "tech-lead-assignment.json" in text, (
                f"{name} missing assignment file"
            )
            assert "Your Assignment" in text, f"{name} missing assignment section"
            assert "batch_review" in text, f"{name} missing batch flavor"
            assert "failure_investigation" in text, f"{name} missing failure flavor"
            assert "focus_issue_number" in text, f"{name} missing focus contract"
            assert "health_review" in text, f"{name} missing health flavor"

    def test_includes_board_snapshot_contract(self, tmp_path):
        """All tech_lead prompt sources must document the board snapshot file.

        The ADR-0031 §3 observation surface only pays off if agents are told
        it exists: the generated prompt, both packaged/dogfood prompt
        variants, and the data-sources contract must all name
        board-snapshot.json (the data-sources doc lists it alongside the
        manifest as a primary local source).
        """
        prompt_path = tmp_path / "tech-lead.md"
        create_tech_lead_review_prompt(prompt_path, "review", "reviewed")
        sources = {"generated prompt": prompt_path.read_text()}

        repo_root = Path(__file__).resolve().parents[2]
        for variant in (
            repo_root / "examples" / "prompts" / "tech-lead-review.md",
            repo_root / "examples" / "prompts" / "tech-lead-data-sources.md",
            repo_root / "repo-specific" / "prompts" / "tech-lead.md",
        ):
            sources[str(variant.relative_to(repo_root))] = variant.read_text()

        for name, text in sources.items():
            assert "board-snapshot.json" in text, f"{name} missing board snapshot"

    def test_substitutes_label_variables(self, tmp_path):
        """Test that label placeholders are substituted with actual values."""
        prompt_path = tmp_path / "tech-lead.md"

        create_tech_lead_review_prompt(prompt_path, "review", "reviewed")

        content = prompt_path.read_text()

        # Label placeholders should be substituted
        assert "{review_label}" not in content
        assert "{reviewed_label}" not in content
        # Actual labels should be present
        assert "review" in content
        assert "reviewed" in content

    def test_includes_review_workflow(self, tmp_path):
        """Test that review workflow is included."""
        prompt_path = tmp_path / "tech-lead.md"

        create_tech_lead_review_prompt(prompt_path, "review", "reviewed")

        content = prompt_path.read_text()

        assert "Batch Review Flow" in content
        assert "Document Your Findings" in content
        assert "Patterns observed" in content
        assert "Audit Principles" in content

    def test_includes_manifest_investigation_steps(self, tmp_path):
        """Test that manifest-based PR investigation steps are included."""
        prompt_path = tmp_path / "cto.md"

        create_tech_lead_review_prompt(prompt_path, "review", "reviewed")

        content = prompt_path.read_text()

        assert "tech-lead-data" in content
        assert "manifest.json" in content
        assert "pr-<number>-diff.txt" in content
        assert "pr-<number>-meta.json" in content
        assert "For Each PR" in content

    def test_includes_completion_instructions(self, tmp_path):
        """Test that coding-done completion instructions are included."""
        prompt_path = tmp_path / "cto.md"

        create_tech_lead_review_prompt(prompt_path, "review", "reviewed")

        content = prompt_path.read_text()

        assert "coding-done completed" in content
        assert "coding-done blocked" in content
        assert "--implementation" in content
        assert "--problems" in content
        assert "reviewer-done" not in content

    def test_creates_parent_directories(self, tmp_path):
        """Test that parent directories are created."""
        prompt_path = tmp_path / "deep" / "nested" / "cto.md"

        create_tech_lead_review_prompt(prompt_path, "review", "reviewed")

        assert prompt_path.exists()


class TestDetectRepo:
    """Test the detect_repo function."""

    @patch("issue_orchestrator.entrypoints.setup_wizard_common.run_git")
    def test_detect_https_url(self, mock_run_git):
        """Test detecting repo from HTTPS URL."""
        mock_run_git.return_value = (True, "https://github.com/owner/repo.git")

        repo = detect_repo()

        assert repo == "owner/repo"

    @patch("issue_orchestrator.entrypoints.setup_wizard_common.run_git")
    def test_detect_ssh_url(self, mock_run_git):
        """Test detecting repo from SSH URL."""
        mock_run_git.return_value = (True, "git@github.com:owner/repo.git")

        repo = detect_repo()

        assert repo == "owner/repo"

    @patch("issue_orchestrator.entrypoints.setup_wizard_common.run_git")
    def test_detect_ssh_url_with_scheme(self, mock_run_git):
        """Test detecting repo from SSH URL with explicit scheme."""
        mock_run_git.return_value = (True, "ssh://git@github.com/owner/repo.git")

        repo = detect_repo()

        assert repo == "owner/repo"

    @patch("issue_orchestrator.entrypoints.setup_wizard_common.run_git")
    def test_detect_https_without_git_suffix(self, mock_run_git):
        """Test detecting repo from HTTPS URL without .git suffix."""
        mock_run_git.return_value = (True, "https://github.com/owner/repo")

        repo = detect_repo()

        assert repo == "owner/repo"

    @patch("issue_orchestrator.entrypoints.setup_wizard_common.run_git")
    def test_returns_none_on_failure(self, mock_run_git):
        """Test that None is returned when git command fails."""
        mock_run_git.return_value = (False, "")

        repo = detect_repo()

        assert repo is None

    @patch("issue_orchestrator.entrypoints.setup_wizard_common.run_git")
    def test_returns_none_for_non_github(self, mock_run_git):
        """Test that None is returned for non-GitHub remotes."""
        mock_run_git.return_value = (True, "https://gitlab.com/owner/repo.git")

        repo = detect_repo()

        assert repo is None


class TestCheckPrerequisites:
    """Test the check_prerequisites function."""

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.run_git")
    @patch("subprocess.run")
    @patch("issue_orchestrator.execution.providers.resolve_github_token")
    @patch("shutil.which")
    def test_all_prerequisites_met(
        self, mock_which, mock_token, mock_subprocess, mock_git
    ):
        """Test when all prerequisites are met."""
        mock_git.return_value = (True, "git version 2.40.0")
        mock_token.return_value = "token"
        mock_subprocess.return_value = Mock(returncode=0)
        # Mock shutil.which to make providers appear available
        mock_which.return_value = "/usr/bin/claude"

        checks = check_prerequisites()

        assert checks["git"] is True
        assert checks["github_auth"] is True
        # Check that at least one provider is available (provider registry checks)
        assert checks["any_ai_provider"] is True

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.run_git")
    @patch("subprocess.run")
    @patch("issue_orchestrator.execution.providers.resolve_github_token")
    def test_missing_git(self, mock_token, mock_subprocess, mock_git):
        """Test when git is missing."""
        mock_git.return_value = (False, "")
        mock_token.return_value = "token"
        mock_subprocess.return_value = Mock(returncode=0)

        checks = check_prerequisites()

        assert checks["git"] is False
        assert checks["github_auth"] is True

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.run_git")
    @patch("subprocess.run")
    @patch("issue_orchestrator.execution.providers.resolve_github_token")
    def test_github_not_authenticated(self, mock_token, mock_subprocess, mock_git):
        """Test when GitHub token is missing."""
        mock_git.return_value = (True, "git version 2.40.0")
        mock_token.side_effect = RuntimeError("missing token")
        mock_subprocess.return_value = Mock(returncode=0)

        checks = check_prerequisites()

        assert checks["github_auth"] is False


class TestFetchGithubLabels:
    """Test the fetch_github_labels function."""

    @patch("issue_orchestrator.entrypoints.setup_wizard_common.get_repository_host")
    def test_fetches_labels(self, mock_client_factory):
        """Test successful label fetching."""
        mock_client = Mock()
        mock_client.list_labels.return_value = [{"name": "bug"}, {"name": "agent:web"}]
        mock_client_factory.return_value = mock_client

        labels = fetch_github_labels("owner/repo")

        assert labels == ["bug", "agent:web"]

    @patch("issue_orchestrator.entrypoints.setup_wizard_common.get_repository_host")
    def test_returns_empty_on_failure(self, mock_client_factory):
        """Test that empty list is returned on failure."""
        mock_client_factory.side_effect = RuntimeError("boom")

        labels = fetch_github_labels("owner/repo")

        assert labels == []

    @patch("issue_orchestrator.entrypoints.setup_wizard_common.get_repository_host")
    def test_returns_empty_on_invalid_payload(self, mock_client_factory):
        """Test that empty list is returned for invalid payload."""
        mock_client = Mock()
        mock_client.list_labels.return_value = ["not-a-dict"]
        mock_client_factory.return_value = mock_client

        labels = fetch_github_labels("owner/repo")

        assert labels == []


class TestFindExistingConfig:
    """Test the find_existing_config function."""

    def test_finds_config_in_current_dir(self, tmp_path):
        """Test finding config in current directory."""
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
        config_file.write_text("repo:\n  name: owner/repo\nagents: {}")

        path, config = find_existing_config(tmp_path)

        assert path == config_file
        assert config["repo"]["name"] == "owner/repo"

    def test_finds_config_in_hidden_dir(self, tmp_path):
        """Test finding config in .issue-orchestrator/config directory."""
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "custom.yaml"
        config_file.write_text("repo:\n  name: owner/repo")

        path, config = find_existing_config(tmp_path)

        assert path == config_file

    def test_returns_none_when_not_found(self, tmp_path):
        """Test that None is returned when config not found."""
        path, config = find_existing_config(tmp_path)

        assert path is None
        assert config is None

    def test_prefers_root_over_hidden(self, tmp_path):
        """Test that default.yaml is preferred over other yaml files."""
        # Create default config
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        default_config = config_dir / "default.yaml"
        default_config.write_text("repo:\n  name: default/repo")

        # Create another yaml file
        other_config = config_dir / "other.yaml"
        other_config.write_text("repo:\n  name: other/repo")

        path, config = find_existing_config(tmp_path)

        assert path == default_config
        assert config["repo"]["name"] == "default/repo"


class TestFindExistingDefaultConfig:
    """Test legacy Control Center config discovery."""

    def test_only_finds_default_yaml(self, tmp_path):
        """Control Center detection should ignore non-default config files."""
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "custom.yaml").write_text("repo:\n  name: owner/repo")

        path, config = find_existing_default_config(tmp_path)

        assert path is None
        assert config is None

    def test_returns_path_and_none_when_config_cannot_be_read(self, tmp_path):
        """Control Center detection should preserve the path on read failure."""
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
        config_file.write_text("repo:\n  name: owner/repo")

        with patch("builtins.open", side_effect=PermissionError("denied")):
            path, config = find_existing_default_config(tmp_path)

        assert path == config_file
        assert config is None


class TestSetupWizardSharedHelpers:
    """Test shared setup-wizard extraction helpers."""

    def test_write_missing_setup_prompts_chooses_prompt_type_by_agent_role(
        self, tmp_path
    ):
        """Prompt generation should stay role-aware after extraction."""
        collector = FileCollector()
        config = {
            "agents": {
                "agent:backend": {"prompt": ".prompts/backend.md"},
                "agent:reviewer": {"prompt": ".prompts/reviewer.md"},
                "agent:tech-lead": {"prompt": ".prompts/tech-lead.md"},
            },
            "review": {
                "default": "agent:reviewer",
                "code_review_label": "needs-review",
                "code_reviewed_label": "reviewed",
                "tech_lead_review_agent": "agent:tech-lead",
                "tech_lead_reviewed_label": "tech-lead-reviewed",
            },
        }

        created_paths = write_missing_setup_prompts(
            config,
            tmp_path,
            file_collector=collector,
        )

        assert created_paths == [
            tmp_path / ".prompts" / "backend.md",
            tmp_path / ".prompts" / "reviewer.md",
            tmp_path / ".prompts" / "tech-lead.md",
        ]
        writes_by_agent = {write.agent: write for write in collector.writes}
        assert "coding-done" in writes_by_agent["agent:backend"].content
        assert "needs-review" in writes_by_agent["agent:reviewer"].content
        assert "reviewer-done approved" in writes_by_agent["agent:reviewer"].content
        tech_lead_content = writes_by_agent["agent:tech-lead"].content
        assert "tech-lead-data" in tech_lead_content
        assert "coding-done completed" in tech_lead_content
        assert "reviewer-done" not in tech_lead_content

    def test_write_missing_setup_prompts_preserves_first_shared_target(self, tmp_path):
        """Compatibility writes should apply each resolved prompt target once."""
        config = {
            "agents": {
                "agent:frontend": {"prompt": ".io/shared.md"},
                "agent:backend": {"prompt": ".io/../.io/shared.md"},
            },
        }

        created_paths = write_missing_setup_prompts(config, tmp_path)

        shared_path = (tmp_path / ".io" / "shared.md").resolve()
        assert created_paths == [shared_path]
        assert "# Frontend Agent Prompt" in shared_path.read_text()

    def test_plan_setup_labels_matches_cli_defaults(self):
        """CLI setup should keep priority labels and default-agent review gating."""
        labels = plan_setup_labels(
            {
                "agents": {
                    "agent:backend": {},
                    "agent:reviewer": {},
                },
                "labels": {"prefix": "io"},
                "review": {
                    "default": "agent:reviewer",
                    "tech_lead_review_agent": "agent:tech-lead",
                },
            }
        )

        ordered_label_names = [name for name, _, _ in labels]
        label_names = set(ordered_label_names)
        assert len(ordered_label_names) == len(label_names)
        assert "agent:backend" in label_names
        assert "agent:reviewer" in label_names
        assert "priority:high" in label_names
        assert "io:in-progress" in label_names
        assert "io:tech-lead-needs-human" in label_names
        assert "needs-code-review" in label_names
        assert "code-reviewed" in label_names
        assert "tech-lead-reviewed" in label_names
        assert "tech-lead-failed" in label_names
        # R3: a tech-lead-enabled config provisions the act-level proposal gate
        # (raw, never prefixed), health-review marker, and the static blocking
        # observation marker. Dynamic area:* labels are provisioned at apply.
        assert "proposed-tech-lead" in label_names
        assert "tech_lead:health-review" in label_names
        assert "tech-lead-observation" in label_names
        assert next(label for label in labels if label[0] == "code-reviewed") == (
            "code-reviewed",
            "0E8A16",
            "PR has been code reviewed",
        )

    def test_plan_setup_labels_omits_gate_without_tech_lead(self):
        """No tech lead agent -> no gate label to provision."""
        labels = plan_setup_labels(
            {
                "agents": {"agent:backend": {}},
                "review": {"default": "agent:reviewer"},
            }
        )
        label_names = {name for name, _, _ in labels}
        assert "proposed-tech-lead" not in label_names
        assert "tech-lead-observation" not in label_names

    def test_required_repo_labels_includes_tech_lead_gate(self):
        """The CLI `init` label set (single owner) provisions the R3 gate."""
        from unittest.mock import Mock

        from issue_orchestrator.entrypoints.setup_wizard_common import (
            required_repo_labels,
        )
        from issue_orchestrator.infra.config import Config

        config = Config()
        config.agents = {"agent:backend": Mock()}
        config.tech_lead_review_agent = "agent:tech-lead"
        config.tech_lead_review_label = "needs-tech-lead-review"
        config.tech_lead_reviewed_label = "tech-lead-reviewed"
        config.tech_lead_failed_label = "tech-lead-failed"

        labels = required_repo_labels(config)

        assert "proposed-tech-lead" in labels
        assert "agent:tech-lead" in labels
        assert "needs-tech-lead-review" in labels
        assert "tech-lead-reviewed" in labels
        assert "tech-lead-failed" in labels
        assert "tech_lead:health-review" in labels
        assert "tech-lead-observation" in labels
        assert "agent:backend" in labels
        # De-duped: no label appears twice.
        assert len(labels) == len(set(labels))

    def test_required_repo_labels_omits_tech_lead_when_unconfigured(self):
        from unittest.mock import Mock

        from issue_orchestrator.entrypoints.setup_wizard_common import (
            required_repo_labels,
        )
        from issue_orchestrator.infra.config import Config

        config = Config()
        config.agents = {"agent:backend": Mock()}

        labels = required_repo_labels(config)

        assert "proposed-tech-lead" not in labels
        assert "tech-lead-observation" not in labels
        assert "agent:backend" in labels

    def test_plan_setup_labels_can_preserve_control_api_behavior(self):
        """Control Center setup should keep its legacy label surface."""
        labels = plan_setup_labels(
            {
                "agents": {"agent:backend": {}},
                "review": {"enabled": True},
            },
            include_priority_labels=False,
            include_review_labels_without_default=True,
        )

        label_names = {name for name, _, _ in labels}
        assert "priority:high" not in label_names
        assert "needs-code-review" in label_names
        assert "code-reviewed" in label_names

    @patch(
        "issue_orchestrator.entrypoints.setup_wizard_common._probe_cli_version",
        return_value="claude 1.2.3",
    )
    @patch("issue_orchestrator.entrypoints.setup_wizard_common.shutil.which")
    def test_build_agent_checks_prefers_version_output(
        self,
        mock_which,
        mock_probe_cli_version,
    ):
        """Prereq checks should surface version strings, not raw paths."""
        mock_which.return_value = "/usr/local/bin/claude"
        config = SimpleNamespace(
            agents={
                "agent:backend": SimpleNamespace(command="claude --model sonnet"),
            }
        )

        checks = build_agent_checks(config)

        assert checks == [
            {
                "name": "claude CLI",
                "ok": True,
                "detail": "claude 1.2.3",
            }
        ]
        mock_probe_cli_version.assert_called_once_with(
            "/usr/local/bin/claude",
            fallback="/usr/local/bin/claude",
        )

    def test_build_agent_checks_uses_provider_executable(self, monkeypatch):
        """Provider-backed agents should check the provider CLI, not legacy command."""

        class Provider:
            name = "claude-code"
            executable = "claude"

            def is_available(self):
                return True

            def check_version(self):
                return "2.1.112 (Claude Code)"

        monkeypatch.setattr(
            "issue_orchestrator.agent_runner.get_provider",
            lambda name: Provider(),
        )
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/local/bin/{name}")
        config = SimpleNamespace(
            agents={
                "agent:backend": SimpleNamespace(
                    provider="claude-code",
                    command="legacy-missing-claude-command",
                ),
            },
            default_agent=None,
        )

        checks = build_agent_checks(config)

        assert checks == [
            {
                "name": "claude-code CLI",
                "ok": True,
                "detail": "2.1.112 (Claude Code) (executable: claude)",
            }
        ]

    def test_build_agent_checks_reports_unknown_provider(self, monkeypatch):
        """Invalid provider names should be surfaced as prereq failures."""
        monkeypatch.setattr(
            "issue_orchestrator.agent_runner.get_provider",
            Mock(side_effect=ValueError("Unknown provider")),
        )
        config = SimpleNamespace(
            agents={
                "agent:backend": SimpleNamespace(
                    provider="mystery-ai",
                    command="legacy-missing-command",
                ),
            },
            default_agent=None,
        )

        checks = build_agent_checks(config)

        assert checks == [
            {
                "name": "mystery-ai CLI",
                "ok": False,
                "detail": "Unknown provider configured for agent:backend: mystery-ai",
            }
        ]

    def test_build_agent_checks_reports_provider_path_context(self, monkeypatch):
        """Missing provider CLIs should include enough PATH/NVM context to debug."""

        class Provider:
            name = "claude-code"
            executable = "claude"

            def is_available(self):
                return False

        monkeypatch.setattr(
            "issue_orchestrator.agent_runner.get_provider",
            lambda name: Provider(),
        )
        monkeypatch.setenv("NVM_BIN", "/Users/test/.nvm/versions/node/v24.11.1/bin")
        monkeypatch.setenv(
            "PATH", "/Users/test/.nvm/versions/node/v24.11.1/bin:/usr/bin:/bin"
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.provider_cli_diagnostics._find_executable_outside_path",
            lambda executable: [
                Path("/Users/test/.nvm/versions/node/v24.14.1/bin/claude")
            ],
        )
        config = SimpleNamespace(
            agents={
                "agent:backend": SimpleNamespace(
                    provider="claude-code",
                    command="legacy-missing-claude-command",
                ),
            },
            default_agent=None,
        )

        checks = build_agent_checks(config)

        assert checks == [
            {
                "name": "claude-code CLI",
                "ok": False,
                "detail": (
                    "claude-code (expected executable: claude); "
                    "executable 'claude' not found on PATH; "
                    "NVM_BIN=/Users/test/.nvm/versions/node/v24.11.1/bin; "
                    "found outside PATH: /Users/test/.nvm/versions/node/v24.14.1/bin/claude"
                ),
            }
        ]


class TestScanExistingRepo:
    """Test the scan_existing_repo function."""

    @patch(
        "issue_orchestrator.entrypoints.cli_tools.setup_wizard.find_prompt_candidates"
    )
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.find_existing_config")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.fetch_github_labels")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    def test_scans_repo(
        self, mock_repo, mock_labels, mock_config, mock_prompts, tmp_path
    ):
        """Test scanning an existing repo."""
        mock_repo.return_value = "owner/repo"
        mock_labels.return_value = ["bug", "agent:web", "agent:backend"]
        mock_config.return_value = (None, None)
        mock_prompts.return_value = []

        state = scan_existing_repo(tmp_path)

        assert state.repo == "owner/repo"
        assert state.agent_labels == ["agent:web", "agent:backend"]
        assert len(state.github_labels) == 3

    @patch(
        "issue_orchestrator.entrypoints.cli_tools.setup_wizard.find_prompt_candidates"
    )
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.find_existing_config")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.fetch_github_labels")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    def test_handles_no_repo(
        self, mock_repo, mock_labels, mock_config, mock_prompts, tmp_path
    ):
        """Test scanning when repo detection fails."""
        mock_repo.return_value = None
        mock_config.return_value = (None, None)
        mock_prompts.return_value = []

        state = scan_existing_repo(tmp_path)

        assert state.repo is None
        assert state.github_labels == []
        assert state.agent_labels == []


class TestWizardNewProject:
    """Test the wizard_new_project function."""

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.get_provider")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.list_providers")
    def test_codex_blank_model_uses_cli_default(
        self,
        mock_list_providers,
        mock_get_provider,
    ):
        """Leaving the Codex model blank should omit model pinning."""
        mock_list_providers.return_value = ["codex"]
        mock_get_provider.return_value = SimpleNamespace(
            description="OpenAI Codex CLI",
            is_available=lambda: True,
        )

        prompter = MockPrompter(
            [
                "45",  # timeout
                "codex",  # provider
                "",  # model blank => use CLI default
                False,  # not a review agent
            ]
        )

        config = setup_wizard_module._prompt_agent_config(  # noqa: SLF001
            prompter,
            agent_name="agent:backend",
            prompt_path=".prompts/backend.md",
        )

        assert config["provider"] == "codex"
        assert config["ai_system"] == "codex"
        assert "model" not in config

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host")
    def test_creates_basic_config(self, mock_client_factory, mock_detect_repo):
        """Test creating a basic config with one agent."""
        mock_detect_repo.return_value = "owner/repo"
        mock_client_factory.return_value = Mock()

        prompter = MockPrompter(
            [
                "Advanced setup",  # setup depth
                "owner/repo",  # repo (accept detected)
                "agent:backend",  # first agent label
                ".prompts/backend.md",  # prompt path (accept default)
                "45",  # timeout
                "claude-code",  # agent provider
                "sonnet",  # model choice
                "default",  # permission mode
                False,  # is this a review agent?
                "",  # empty to finish agents
                "3",  # max concurrent sessions
                "due_date",  # milestone sort strategy
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                "../",  # worktree base
                "",  # setup commands (empty to skip)
                True,  # enable Claude startup interactions
                "web",  # ui mode
                "8080",  # web port
                "tmux",  # terminal backend
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                "",  # filtering label
                False,  # disable review
            ]
        )

        config = wizard_new_project(prompter)

        assert config["repo"]["name"] == "owner/repo"
        assert "agent:backend" in config["agents"]
        assert config["agents"]["agent:backend"]["prompt"] == ".prompts/backend.md"
        assert config["agents"]["agent:backend"]["model"] == "sonnet"
        assert config["agents"]["agent:backend"]["timeout_minutes"] == 45
        assert config["agents"]["agent:backend"]["provider"] == "claude-code"
        assert config["agents"]["agent:backend"]["ai_system"] == "claude-code"
        # Non-review agent gets work-oriented initial_prompt
        assert "initial_prompt" in config["agents"]["agent:backend"]
        assert "Work on issue" in config["agents"]["agent:backend"]["initial_prompt"]
        assert "{pr_number}" not in config["agents"]["agent:backend"]["initial_prompt"]
        assert config["execution"]["concurrency"]["max_concurrent_sessions"] == 3
        assert config["execution"]["session_interactions"] == {"enabled": True}
        assert config["ui"]["mode"] == "web"
        printed = "\n".join(prompter.printed)
        assert "Claude Code note: trust is stored per worktree path." in printed

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host")
    def test_review_agent_gets_pr_number_in_prompt(
        self, mock_client_factory, mock_detect_repo
    ):
        """Test that review agents get initial_prompt with {pr_number}."""
        mock_detect_repo.return_value = "owner/repo"
        mock_client_factory.return_value = Mock()

        prompter = MockPrompter(
            [
                "Advanced setup",  # setup depth
                "owner/repo",
                "agent:reviewer",
                ".prompts/reviewer.md",
                "30",  # timeout
                "claude-code",  # agent provider
                "sonnet",  # model
                "default",  # permission mode
                True,  # YES, this IS a review agent
                "",  # finish agents
                "3",
                "due_date",
                "",  # milestone order (optional)
                "M0",
                "../",
                "",  # setup commands (empty to skip)
                True,  # enable Claude startup interactions
                "web",
                "8080",
                "tmux",
                "io",
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                "",  # filtering label
                False,  # disable review
            ]
        )

        config = wizard_new_project(prompter)

        # Review agent gets review-oriented initial_prompt with pr_number
        assert "initial_prompt" in config["agents"]["agent:reviewer"]
        assert "Review PR" in config["agents"]["agent:reviewer"]["initial_prompt"]
        assert "{pr_number}" in config["agents"]["agent:reviewer"]["initial_prompt"]

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host")
    def test_adds_agent_prefix_when_missing(
        self, mock_client_factory, mock_detect_repo
    ):
        """Test that agent: prefix is added when user confirms."""
        mock_detect_repo.return_value = None
        mock_client_factory.return_value = Mock()

        prompter = MockPrompter(
            [
                "Advanced setup",  # setup depth
                "owner/repo",  # repo (no detection)
                "backend",  # agent label without prefix
                True,  # yes to add prefix
                ".prompts/backend.md",
                "60",  # timeout
                "claude-code",  # agent provider
                "opus",  # model
                "default",  # permission mode
                False,  # is this a review agent?
                "",  # finish agents
                "2",  # max concurrent
                "due_date",  # milestone sort strategy
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                "../",
                "",  # setup commands (empty to skip)
                True,  # enable Claude startup interactions
                "tmux",  # ui mode (tmux doesn't need port)
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                "",  # filtering label
                False,  # disable review
            ]
        )

        config = wizard_new_project(prompter)

        assert "agent:backend" in config["agents"]
        assert "backend" not in config["agents"]

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host")
    def test_multiple_agents(self, mock_client_factory, mock_detect_repo):
        """Test creating config with multiple agents."""
        mock_detect_repo.return_value = "owner/repo"
        mock_client_factory.return_value = Mock()

        prompter = MockPrompter(
            [
                "Advanced setup",  # setup depth
                "owner/repo",
                # First agent
                "agent:frontend",
                ".prompts/frontend.md",
                "30",  # timeout
                "claude-code",  # agent provider
                "sonnet",  # model
                "default",  # permission mode
                False,  # is this a review agent?
                # Second agent
                "agent:backend",
                ".prompts/backend.md",
                "60",  # timeout
                "claude-code",  # agent provider
                "opus",  # model
                "bypassPermissions",  # permission mode (different for variety)
                True,  # confirm bypassPermissions
                False,  # is this a review agent?
                # Finish
                "",
                "5",  # max concurrent
                "due_date",  # milestone sort strategy
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                "../",
                "",  # setup commands (empty to skip)
                True,  # enable Claude startup interactions
                "web",
                "9000",  # custom port
                "tmux",  # terminal backend
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                "",  # filtering label
                False,  # disable review
            ]
        )

        config = wizard_new_project(prompter)

        assert len(config["agents"]) == 2
        assert "agent:frontend" in config["agents"]
        assert "agent:backend" in config["agents"]
        assert config["agents"]["agent:frontend"]["model"] == "sonnet"
        assert config["agents"]["agent:backend"]["model"] == "opus"

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host")
    def test_custom_agent_command(self, mock_client_factory, mock_detect_repo):
        """Test creating config with a custom agent command."""
        mock_detect_repo.return_value = "owner/repo"
        mock_client_factory.return_value = Mock()

        prompter = MockPrompter(
            [
                "Advanced setup",  # setup depth
                "owner/repo",
                "agent:custom",
                ".prompts/custom.md",
                "30",  # timeout
                "custom",  # agent type (custom command)
                "codex exec {prompt}",  # custom command
                False,  # is this a review agent?
                "",  # finish agents
                "3",
                "due_date",  # milestone sort strategy
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                "../",
                "",  # setup commands (empty to skip)
                "web",
                "8080",
                "tmux",  # terminal backend
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                "",  # filtering label
                False,  # disable review
            ]
        )

        config = wizard_new_project(prompter)

        assert "agent:custom" in config["agents"]
        agent_cfg = config["agents"]["agent:custom"]
        assert agent_cfg["command"] == "codex exec {prompt}"
        assert agent_cfg["ai_system"] == "codex"
        # Custom agents don't get permission_mode since it's Claude-specific
        assert "permission_mode" not in agent_cfg

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host")
    def test_review_workflow_enabled(self, mock_client_factory, mock_detect_repo):
        """Test enabling two-stage review workflow."""
        mock_detect_repo.return_value = "owner/repo"
        mock_client_factory.return_value = Mock()

        prompter = MockPrompter(
            [
                "Advanced setup",  # setup depth
                "owner/repo",
                "agent:backend",
                ".prompts/backend.md",
                "45",  # timeout
                "claude-code",  # agent provider
                "sonnet",  # model
                "default",  # permission mode
                False,  # is this a review agent?
                "",  # finish agents
                "3",
                "due_date",  # milestone sort strategy
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                "../",
                "",  # setup commands (empty to skip)
                True,  # enable Claude startup interactions
                "web",
                "8080",
                "tmux",  # terminal backend
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                "",  # filtering label
                True,  # enable code review
                "agent:reviewer",  # code review agent
                "needs-code-review",  # code review label
                "code-reviewed",  # code reviewed label
                True,  # enable Stage 2: tech_lead batch review
                "agent:tech-lead",  # tech_lead review agent
                "tech-lead-reviewed",  # tech_lead reviewed label
                "5",  # threshold
                "agent:backend",  # tech_lead follow-up worker agent (#6779 R14)
            ]
        )

        config = wizard_new_project(prompter)

        # Stage 1: Code Review (new structure)
        assert config["review"]["enabled"] is True
        assert config["review"]["default"] == "agent:reviewer"
        assert config["review"]["code_review_label"] == "needs-code-review"
        assert config["review"]["code_reviewed_label"] == "code-reviewed"

        # Stage 2: Tech Lead Batch Review
        assert config["review"]["tech_lead_review_agent"] == "agent:tech-lead"
        # R14: a configured tech lead agent must also route create_issue follow-ups
        # to a worker agent, so the wizard collects it up front.
        assert config["review"]["tech_lead_follow_up_agent"] == "agent:backend"
        assert config["review"]["tech_lead_reviewed_label"] == "tech-lead-reviewed"
        assert config["review"]["tech_lead_review_threshold"] == 5
        assert config["tech_lead"]["enabled"] is True

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host")
    def test_requires_at_least_one_agent(self, mock_client_factory, mock_detect_repo):
        """Test that wizard requires at least one agent."""
        mock_detect_repo.return_value = "owner/repo"
        mock_client_factory.return_value = Mock()

        prompter = MockPrompter(
            [
                "Advanced setup",  # setup depth
                "owner/repo",
                "",  # try to finish with no agents
                "agent:backend",  # now add one
                ".prompts/backend.md",
                "45",  # timeout
                "claude-code",  # agent provider
                "sonnet",  # model
                "default",  # permission mode
                False,  # is this a review agent?
                "",  # finish
                "3",
                "due_date",  # milestone sort strategy
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                "../",
                "",  # setup commands (empty to skip)
                True,  # enable Claude startup interactions
                "web",
                "8080",
                "tmux",  # terminal backend
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                "",  # filtering label
                False,  # disable review
            ]
        )

        config = wizard_new_project(prompter)

        # Should have exactly one agent (after forcing user to add one)
        assert len(config["agents"]) == 1
        # Check that "You need at least one agent!" was printed
        assert any("at least one agent" in msg for msg in prompter.printed)


class TestWizardExistingProject:
    """Test the wizard_existing_project function."""

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host")
    def test_basic_existing_project(self, mock_client_factory):
        """Test onboarding an existing project."""
        mock_client_factory.return_value = Mock()

        state = DetectedState(
            repo="owner/repo",
            github_labels=["bug", "agent:web"],
            agent_labels=["agent:web"],
            existing_config=None,
            config_path=None,
            prompt_candidates=[],
        )

        prompter = MockPrompter(
            [
                True,  # add agent:web to config
                ".prompts/web.md",  # prompt path
                "45",  # timeout
                "claude-code",  # agent provider
                "sonnet",  # model
                "default",  # permission mode
                False,  # is this a review agent?
                "3",  # max concurrent
                "due_date",  # milestone sort strategy
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                "../",  # worktree base
                "",  # setup commands (empty to skip)
                True,  # enable Claude startup interactions
                "web",  # ui mode
                "8080",  # port
                "tmux",  # terminal backend
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                False,  # disable Stage 1 review
            ]
        )

        config, _ = wizard_existing_project(state, prompter)

        assert config["repo"]["name"] == "owner/repo"
        assert "agent:web" in config["agents"]
        assert config["agents"]["agent:web"]["provider"] == "claude-code"
        assert config["agents"]["agent:web"]["ai_system"] == "claude-code"
        assert config["execution"]["concurrency"]["max_concurrent_sessions"] == 3
        assert config["execution"]["session_interactions"] == {"enabled": True}
        assert config["validation"]["quick"]["cmd"] == "make test"
        assert config["validation"]["quick"]["timeout_seconds"] == 300
        assert config["validation"]["publish"]["cmd"] == "make test"
        assert config["validation"]["publish"]["timeout_seconds"] == 1800

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host")
    def test_preserves_existing_config(self, mock_client_factory):
        """Test that existing config is preserved when updating."""
        mock_client_factory.return_value = Mock()

        state = DetectedState(
            repo="owner/repo",
            github_labels=["agent:web", "agent:backend"],
            agent_labels=["agent:web", "agent:backend"],
            existing_config={
                "repo": {"name": "owner/repo"},
                "agents": {
                    "agent:web": {
                        "prompt": ".prompts/web.md",
                        "model": "sonnet",
                        "timeout_minutes": 45,
                    }
                },
                "execution": {"concurrency": {"max_concurrent_sessions": 3}},
                "ui": {"mode": "tmux"},
            },
            config_path=Path(".issue-orchestrator.yaml"),
            prompt_candidates=[],
        )

        prompter = MockPrompter(
            [
                True,  # update existing config
                # agent:backend is not in config, so wizard asks about it
                True,  # add agent:backend
                ".prompts/backend.md",  # prompt path
                "60",  # timeout
                "claude-code",  # agent provider
                "opus",  # model
                "default",  # permission mode
                False,  # is this a review agent?
                # No more missing agents
                # agent:web is in config but let's say it's in github_labels too (no missing labels)
                # Concurrency already configured - won't ask
                # Milestone sort not configured - will ask
                "due_date",  # milestone sort strategy
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                # Worktrees needed for backend
                "../",
                "",  # setup commands (empty to skip)
                True,  # enable Claude startup interactions
                # UI mode already configured - won't ask
                # Label prefix not configured
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                # Review not configured
                False,  # no review
            ]
        )

        config, _ = wizard_existing_project(state, prompter)

        # Original config preserved
        assert config["agents"]["agent:web"]["prompt"] == ".prompts/web.md"
        assert config["agents"]["agent:web"]["model"] == "sonnet"
        # New agent added
        assert "agent:backend" in config["agents"]
        assert config["agents"]["agent:backend"]["model"] == "opus"
        # Existing settings preserved
        assert config["ui"]["mode"] == "tmux"

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host")
    def test_preserves_agent_config_for_label_creation(self, mock_client_factory):
        """Test that agent config is preserved for later label creation."""
        mock_client = Mock()
        mock_client_factory.return_value = mock_client

        state = DetectedState(
            repo="owner/repo",
            github_labels=[],  # No labels on GitHub
            agent_labels=[],
            existing_config={
                "repo": {"name": "owner/repo"},
                "agents": {
                    "agent:web": {
                        "prompt": ".prompts/web.md",
                        "model": "sonnet",
                        "timeout_minutes": 45,
                    },
                },
                "execution": {"concurrency": {"max_concurrent_sessions": 3}},
            },
            config_path=Path(".issue-orchestrator.yaml"),
            prompt_candidates=[],
        )

        prompter = MockPrompter(
            [
                True,  # update existing config
                # No unconfigured agents
                # Milestone sort missing
                "due_date",  # milestone sort strategy
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                # Worktree missing
                "../",
                "",  # setup commands (empty to skip)
                # UI mode missing
                "web",
                "8080",
                "tmux",
                # Label prefix
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                # Review
                False,  # disable Stage 1 review
            ]
        )

        config, _ = wizard_existing_project(state, prompter)

        # Agent config is preserved - labels will be created by run_wizard
        assert "agent:web" in config["agents"]
        assert config["repo"]["name"] == "owner/repo"

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host")
    def test_fresh_config_when_declined(self, mock_client_factory):
        """Test starting fresh when declining to update existing config."""
        mock_client_factory.return_value = Mock()

        state = DetectedState(
            repo="owner/repo",
            github_labels=["agent:web"],
            agent_labels=["agent:web"],
            existing_config={
                "repo": {"name": "owner/repo"},
                "agents": {
                    "agent:old": {
                        "prompt": ".prompts/old.md",
                        "model": "haiku",
                        "timeout_minutes": 30,
                    }
                },
            },
            config_path=Path(".issue-orchestrator.yaml"),
            prompt_candidates=[],
        )

        prompter = MockPrompter(
            [
                False,  # DON'T update existing config - start fresh
                # Now asks about agent:web since we started fresh
                True,  # add agent:web
                ".prompts/web.md",
                "45",  # timeout
                "claude-code",  # agent provider
                "sonnet",  # model
                "default",  # permission mode
                False,  # is this a review agent?
                # Concurrency (fresh config needs this)
                "2",
                # Milestone sort (fresh config needs this)
                "due_date",  # milestone sort strategy
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                # Worktree
                "../",
                "",  # setup commands (empty to skip)
                True,  # enable Claude startup interactions
                # UI mode (fresh)
                "tmux",
                # Label prefix
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                # Review
                False,
            ]
        )

        config, _ = wizard_existing_project(state, prompter)

        # Old agent should NOT be in config
        assert "agent:old" not in config["agents"]
        # New agent should be
        assert "agent:web" in config["agents"]

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host")
    def test_prompts_for_repo_when_not_detected(self, mock_client_factory):
        """Test that repo is prompted when not in state or config."""
        mock_client_factory.return_value = Mock()

        state = DetectedState(
            repo=None,  # Not detected
            github_labels=[],
            agent_labels=[],
            existing_config=None,
            config_path=None,
            prompt_candidates=[],
        )

        prompter = MockPrompter(
            [
                "manual/repo",  # manual repo entry
                "agent:dev",  # manual agent label
                ".prompts/dev.md",  # prompt path
                "45",  # timeout
                "claude-code",  # agent provider
                "sonnet",  # model
                "default",  # permission mode
                False,  # is this a review agent?
                "3",  # concurrency
                "due_date",  # milestone sort strategy
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                "../",  # worktree base (now top-level config)
                "",  # setup commands (empty to skip)
                True,  # enable Claude startup interactions
                "web",  # ui mode
                "8080",  # port (since web mode)
                "tmux",  # terminal backend
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                False,  # disable Stage 1 review
            ]
        )

        config, _ = wizard_existing_project(state, prompter)

        assert config["repo"]["name"] == "manual/repo"
        assert "agent:dev" in config["agents"]
        assert config["agents"]["agent:dev"]["ai_system"] == "claude-code"


class TestRunWizard:
    """Test the run_wizard function."""

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.check_prerequisites")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.scan_existing_repo")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.fetch_github_labels")
    @patch("os.chdir")
    def test_new_project_flow(
        self, mock_chdir, mock_labels, mock_scan, mock_prereqs, tmp_path
    ):
        """Test the full wizard flow for a new project."""
        mock_prereqs.return_value = {
            "git": True,
            "github_auth": True,
            "any_ai_provider": True,
        }
        mock_scan.return_value = DetectedState(repo="owner/repo")
        mock_labels.return_value = []  # No existing labels

        # Create target directory
        target = tmp_path / "myproject"
        target.mkdir()

        prompter = MockPrompter(
            [
                # Mode choice (no directory prompt since we pass target_path)
                "New project - set up from scratch",
                "Advanced setup",  # setup depth
                # wizard_new_project answers
                "owner/repo",
                "agent:backend",
                ".prompts/backend.md",
                "45",  # timeout
                "claude-code",  # agent provider
                "sonnet",  # model
                "default",  # permission mode
                False,  # is this a review agent?
                "",  # finish agents
                "3",
                "due_date",  # milestone sort strategy
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                "../",
                "",  # setup commands (empty to skip)
                True,  # enable Claude startup interactions
                "web",
                "8080",
                "tmux",
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                "",  # filtering label
                False,  # disable review
                # Post-wizard (new flow)
                ".issue-orchestrator.yaml",  # config filename
                True,  # Apply these changes?
                False,  # Install repo-local guardrails and AI agent hooks now?
                False,  # Set up AI provider API keys now?
            ]
        )

        host = Mock()
        host.list_labels.return_value = []
        with patch(
            "issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo",
            return_value="owner/repo",
        ):
            with patch(
                "issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host",
                return_value=host,
            ):
                with patch(
                    "issue_orchestrator.entrypoints.cli_tools.setup_wizard.offer_readiness_assessment"
                ):
                    run_wizard(target_path=target, prompter=prompter)

        # Verify files were created
        assert (target / ".issue-orchestrator.yaml").exists() or any(
            "apply" in msg.lower() for msg in prompter.printed
        )
        printed = "\n".join(prompter.printed)
        assert (
            "Install repo guardrails + AI hooks (recommended): issue-orchestrator setup-guardrails"
            in printed
        )
        assert "Run: issue-orchestrator doctor" in printed
        assert printed.index("issue-orchestrator setup-guardrails") < printed.index(
            "issue-orchestrator doctor"
        )
        assert printed.index("issue-orchestrator doctor") < printed.index(
            "issue-orchestrator start"
        )
        assert "Trusted session interactions are enabled." in printed
        assert "auto-accept Claude's initial trust prompt" in printed
        assert (
            "pre-approving the parent directory does not auto-trust child worktrees."
            in printed
        )

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.check_prerequisites")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.fetch_github_labels")
    @patch("os.chdir")
    def test_aborts_when_apply_declined(
        self, mock_chdir, mock_labels, mock_prereqs, tmp_path
    ):
        """Test that wizard aborts when user declines to apply changes."""
        mock_prereqs.return_value = {
            "git": True,
            "github_auth": True,
            "any_ai_provider": True,
        }
        mock_labels.return_value = []  # No existing labels

        target = tmp_path / "myproject"
        target.mkdir()

        prompter = MockPrompter(
            [
                # No directory prompt since we pass target_path
                "New project - set up from scratch",
                "Advanced setup",  # setup depth
                "owner/repo",
                "agent:backend",
                ".prompts/backend.md",
                "45",  # timeout
                "claude-code",  # agent provider
                "sonnet",  # model
                "default",  # permission mode
                False,  # is this a review agent?
                "",
                "3",
                "due_date",  # milestone sort strategy
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                "../",
                "",  # setup commands (empty to skip)
                True,  # enable Claude startup interactions
                "web",
                "8080",
                "tmux",
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                "",  # filtering label
                False,  # disable review
                # Post-wizard (new flow)
                ".issue-orchestrator.yaml",  # config filename
                False,  # DON'T apply changes (exits here)
            ]
        )

        with patch(
            "issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo",
            return_value="owner/repo",
        ):
            with patch(
                "issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host",
                return_value=Mock(),
            ):
                with patch(
                    "issue_orchestrator.entrypoints.cli_tools.setup_wizard.offer_readiness_assessment"
                ):
                    with pytest.raises(SystemExit):
                        run_wizard(target_path=target, prompter=prompter)

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.check_prerequisites")
    @patch("os.chdir")
    def test_warns_on_missing_prerequisites(self, mock_chdir, mock_prereqs, tmp_path):
        """Test that wizard warns when prerequisites are missing."""
        mock_prereqs.return_value = {
            "git": True,
            "github_auth": False,  # Not authenticated
            "any_ai_provider": False,  # No providers installed
        }

        target = tmp_path / "myproject"
        target.mkdir()

        prompter = MockPrompter(
            [
                # No directory prompt since we pass target_path
                False,  # Don't continue without prereqs
            ]
        )

        with pytest.raises(SystemExit):
            run_wizard(target_path=target, prompter=prompter)

        # Check that warning was printed
        assert any(
            "prerequisites" in msg.lower() or "missing" in msg.lower()
            for msg in prompter.printed
        )
        assert any("ISSUE_ORCH_GITHUB_TOKEN" in msg for msg in prompter.printed)

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.check_prerequisites")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.fetch_github_labels")
    @patch("os.chdir")
    def test_continues_despite_missing_prerequisites(
        self, mock_chdir, mock_labels, mock_prereqs, tmp_path
    ):
        """Test that wizard can continue despite missing prerequisites."""
        mock_prereqs.return_value = {
            "git": True,
            "github_auth": False,
            "any_ai_provider": False,
        }
        mock_labels.return_value = []

        target = tmp_path / "myproject"
        target.mkdir()

        prompter = MockPrompter(
            [
                # No directory prompt since we pass target_path
                True,  # Continue anyway
                "New project - set up from scratch",
                "Advanced setup",  # setup depth
                "owner/repo",
                "agent:backend",
                ".prompts/backend.md",
                "45",  # timeout
                "claude-code",  # agent provider
                "sonnet",  # model
                "default",  # permission mode
                False,  # is this a review agent?
                "",
                "3",
                "due_date",  # milestone sort strategy
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                "../",
                "",  # setup commands (empty to skip)
                True,  # enable Claude startup interactions
                "web",
                "8080",
                "tmux",
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                "",  # filtering label
                False,  # disable review
                # Post-wizard (new flow)
                ".issue-orchestrator.yaml",  # config filename
                False,  # Don't apply - exits here
            ]
        )

        with patch(
            "issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo",
            return_value=None,
        ):
            with patch(
                "issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host",
                return_value=Mock(),
            ):
                with patch(
                    "issue_orchestrator.entrypoints.cli_tools.setup_wizard.offer_readiness_assessment"
                ):
                    with pytest.raises(SystemExit):
                        run_wizard(target_path=target, prompter=prompter)


class TestFileCollector:
    """Test the FileCollector class for dry-run mode."""

    def test_add_write(self):
        """Test adding a write to the collector."""
        collector = FileCollector()
        collector.add_write(Path("/tmp/test.yaml"), "content: here", "create")

        assert len(collector.writes) == 1
        assert collector.writes[0].path == Path("/tmp/test.yaml")
        assert collector.writes[0].content == "content: here"
        assert collector.writes[0].action == "create"

    def test_add_multiple_writes(self):
        """Test adding multiple writes."""
        collector = FileCollector()
        collector.add_write(Path("/tmp/a.yaml"), "a", "create")
        collector.add_write(Path("/tmp/b.md"), "b", "overwrite")

        assert len(collector.writes) == 2
        assert collector.writes[0].action == "create"
        assert collector.writes[1].action == "overwrite"

    def test_add_label(self):
        """Test adding a label to the collector."""
        collector = FileCollector()
        collector.add_label("agent:test", "FF0000", "Test agent label")

        assert len(collector.labels) == 1
        assert collector.labels[0] == ("agent:test", "FF0000", "Test agent label")


class TestPlannedWrite:
    """Test the PlannedWrite dataclass."""

    def test_size_display_bytes(self):
        """Test size display for small content."""
        write = PlannedWrite(Path("/tmp/test.txt"), "hello", "create")
        assert write.size_display() == "5 B"

    def test_size_display_kilobytes(self):
        """Test size display for larger content."""
        content = "x" * 2048
        write = PlannedWrite(Path("/tmp/test.txt"), content, "create")
        assert write.size_display() == "2.0 KB"

    def test_size_display_unicode(self):
        """Test size display handles unicode correctly."""
        # Unicode characters take more bytes than characters
        write = PlannedWrite(Path("/tmp/test.txt"), "hello 世界", "create")
        # 'hello ' is 6 bytes, '世界' is 6 bytes (2 chars * 3 bytes each in UTF-8)
        assert write.size_display() == "12 B"


class TestDryRunMode:
    """Test dry-run mode for setup wizard functions."""

    def test_create_starter_prompt_dry_run(self, tmp_path):
        """Test that dry-run collects write without creating file."""
        prompt_path = tmp_path / "prompts" / "test.md"
        collector = FileCollector()

        create_starter_prompt("agent:test", prompt_path, file_collector=collector)

        # File should NOT exist
        assert not prompt_path.exists()
        # But write should be collected
        assert len(collector.writes) == 1
        assert collector.writes[0].path == prompt_path
        assert "Test Agent Prompt" in collector.writes[0].content
        assert collector.writes[0].action == "create"

    def test_write_config_dry_run(self, tmp_path):
        """Test that dry-run collects config write without creating file."""
        config_path = tmp_path / ".issue-orchestrator.yaml"
        collector = FileCollector()
        config = {"repo": {"name": "owner/repo"}, "agents": {}}

        write_config(config, config_path, file_collector=collector)

        # File should NOT exist
        assert not config_path.exists()
        # But write should be collected
        assert len(collector.writes) == 1
        assert collector.writes[0].path == config_path
        assert "name: owner/repo" in collector.writes[0].content
        assert collector.writes[0].action == "create"

    def test_write_config_dry_run_overwrite(self, tmp_path):
        """Test that dry-run detects overwrite action."""
        config_path = tmp_path / ".issue-orchestrator.yaml"
        config_path.write_text("old content")
        collector = FileCollector()
        config = {"repo": {"name": "owner/repo"}}

        write_config(config, config_path, file_collector=collector)

        # Old file content should be unchanged
        assert config_path.read_text() == "old content"
        # Write should be collected with overwrite action
        assert len(collector.writes) == 1
        assert collector.writes[0].action == "overwrite"

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.check_prerequisites")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.fetch_github_labels")
    @patch("os.chdir")
    def test_run_wizard_dry_run_no_files_written(
        self, mock_chdir, mock_labels, mock_prereqs, tmp_path
    ):
        """Test that dry-run mode doesn't write any files."""
        mock_prereqs.return_value = {
            "git": True,
            "github_auth": True,
            "any_ai_provider": True,
        }
        mock_labels.return_value = []

        target = tmp_path / "myproject"
        target.mkdir()

        prompter = MockPrompter(
            [
                "New project - set up from scratch",
                "Advanced setup",  # setup depth
                "owner/repo",
                "agent:backend",
                ".prompts/backend.md",
                "45",
                "claude-code",
                "sonnet",
                "default",
                False,  # is this a review agent?
                "",  # finish agents
                "3",
                "due_date",
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                "../",
                "",  # setup commands (empty to skip)
                True,  # enable Claude startup interactions
                "web",
                "8080",
                "tmux",
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                "",  # filtering label
                False,  # disable review
            ]
        )

        with patch(
            "issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo",
            return_value="owner/repo",
        ):
            with patch(
                "issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host",
                return_value=Mock(),
            ):
                run_wizard(target_path=target, prompter=prompter, dry_run=True)

        # No config file should be created
        assert not (target / ".issue-orchestrator.yaml").exists()
        # No prompts directory should be created
        assert not (target / ".prompts").exists()

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.check_prerequisites")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.fetch_github_labels")
    @patch("os.chdir")
    def test_run_wizard_dry_run_shows_summary(
        self, mock_chdir, mock_labels, mock_prereqs, tmp_path
    ):
        """Test that dry-run mode shows summary output."""
        mock_prereqs.return_value = {
            "git": True,
            "github_auth": True,
            "any_ai_provider": True,
        }
        mock_labels.return_value = []

        target = tmp_path / "myproject"
        target.mkdir()

        prompter = MockPrompter(
            [
                "New project - set up from scratch",
                "Advanced setup",  # setup depth
                "owner/repo",
                "agent:backend",
                ".prompts/backend.md",
                "45",
                "claude-code",
                "sonnet",
                "default",
                False,  # is this a review agent?
                "",  # finish agents
                "3",
                "due_date",
                "",  # milestone order (optional)
                "M0",  # foundation milestone
                "../",
                "",  # setup commands (empty to skip)
                True,  # enable Claude startup interactions
                "web",
                "8080",
                "tmux",
                "io",  # label prefix
                "make test",  # quick validation command
                "make test",  # publish validation command
                "300",  # quick validation timeout
                "1800",  # publish validation timeout
                "",  # filtering label
                False,  # disable review
            ]
        )

        with patch(
            "issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo",
            return_value="owner/repo",
        ):
            with patch(
                "issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host",
                return_value=Mock(),
            ):
                run_wizard(target_path=target, prompter=prompter, dry_run=True)

        # Should show dry run header
        assert any("DRY RUN" in msg for msg in prompter.printed)
        # Should show summary (now titled "CHANGES THAT WOULD BE APPLIED")
        assert any("CHANGES" in msg for msg in prompter.printed)
        # Should suggest running without --dry-run
        assert any("without --dry-run" in msg for msg in prompter.printed)


class TestOfferReadinessAssessment:
    """Handler-side tests for the optional readiness step (soft, never blocks)."""

    def test_runs_when_accepted_single_cli(self, tmp_path) -> None:
        prompter = MockPrompter([True])  # yes_no -> run
        launcher = Mock(return_value=0)

        readiness_launch_module.offer_readiness_assessment(
            prompter,
            tmp_path,
            available_clis=lambda: ["claude"],
            launcher=launcher,
        )

        launcher.assert_called_once_with("claude", tmp_path)
        assert any("Launching claude" in m for m in prompter.printed)
        assert any("Continuing setup" in m for m in prompter.printed)

    def test_skipped_when_declined(self, tmp_path) -> None:
        prompter = MockPrompter([False])
        launcher = Mock()

        readiness_launch_module.offer_readiness_assessment(
            prompter,
            tmp_path,
            available_clis=lambda: ["claude"],
            launcher=launcher,
        )

        launcher.assert_not_called()
        assert any("Skipped" in m for m in prompter.printed)
        assert any("SKILL.md" in m for m in prompter.printed)

    def test_prompts_choice_with_multiple_clis(self, tmp_path) -> None:
        prompter = MockPrompter([True, "codex"])  # yes_no, then choice
        launcher = Mock(return_value=0)

        readiness_launch_module.offer_readiness_assessment(
            prompter,
            tmp_path,
            available_clis=lambda: ["claude", "codex"],
            launcher=launcher,
        )

        launcher.assert_called_once_with("codex", tmp_path)
        assert "Which agent?" in prompter.questions_asked

    def test_no_cli_prints_pointer(self, tmp_path) -> None:
        prompter = MockPrompter([])
        launcher = Mock()

        readiness_launch_module.offer_readiness_assessment(
            prompter,
            tmp_path,
            available_clis=lambda: [],
            launcher=launcher,
        )

        launcher.assert_not_called()
        assert any("No agent CLI" in m for m in prompter.printed)
        assert any("SKILL.md" in m for m in prompter.printed)

    def test_dry_run_is_noop(self, tmp_path) -> None:
        prompter = MockPrompter([])
        launcher = Mock()

        readiness_launch_module.offer_readiness_assessment(
            prompter,
            tmp_path,
            dry_run=True,
            available_clis=lambda: ["claude"],
            launcher=launcher,
        )

        launcher.assert_not_called()
        assert prompter.printed == []

    def test_launch_failure_never_blocks(self, tmp_path) -> None:
        prompter = MockPrompter([True])

        def boom(*_args, **_kwargs):
            # ANY exception from the launch chain — OSError (missing binary, bad
            # interpreter), or an unexpected one (e.g. a bad runner contract) —
            # must drop to "could not launch", never propagate.
            raise RuntimeError("unexpected launcher failure")

        # Must not raise — readiness is advisory and cannot block setup.
        readiness_launch_module.offer_readiness_assessment(
            prompter,
            tmp_path,
            available_clis=lambda: ["claude"],
            launcher=boom,
        )

        assert any("Could not launch" in m for m in prompter.printed)
