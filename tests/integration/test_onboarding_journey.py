"""Integration coverage for the local onboarding journey."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from issue_orchestrator.entrypoints.cli_tools.setup_wizard import run_wizard
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.doctor.checks import guardrails as guardrail_checks
from issue_orchestrator.infra.doctor.checks import hooks as hook_checks
from issue_orchestrator.infra.doctor.checks import schema as schema_checks
from issue_orchestrator.infra.doctor.checks import workspace as workspace_checks
from issue_orchestrator.ports.repository_setup import (
    RepositorySetupGitHubVerification,
)
from tests.git_push_authorization import authorized_local_fixture_git_env

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GIT_ENV_STRIP = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
)


class _QueuePrompter:
    """Simple deterministic prompter for integration wizard runs."""

    def __init__(self, answers: list[object]) -> None:
        self._answers = list(answers)
        self._index = 0
        self.printed: list[str] = []

    def _next(self, question: str) -> object:
        if self._index >= len(self._answers):
            raise AssertionError(f"No answer left for prompt: {question}")
        answer = self._answers[self._index]
        self._index += 1
        return answer

    def print(self, message: str) -> None:
        self.printed.append(message)

    def input(self, question: str, default: str = "") -> str:
        answer = self._next(question)
        return default if answer == "" else str(answer)

    def yes_no(self, question: str, default: bool = True) -> bool:
        answer = self._next(question)
        if isinstance(answer, bool):
            return answer
        return str(answer).lower() in {"y", "yes", "true"}

    def choice(
        self, question: str, choices: list[str], allow_custom: bool = False
    ) -> str:
        answer = str(self._next(question))
        if answer in choices:
            return answer
        if allow_custom:
            return answer
        raise AssertionError(f"Unexpected choice {answer!r} for {question}")


def _clean_git_env() -> dict[str, str]:
    """Return an env for fixture git commands isolated from agent sessions.

    The fixture pushes to a local bare repo it creates under ``tmp_path``, so
    it needs the shared test-only push authorization (see
    ``tests/git_push_authorization.py``).
    """
    return authorized_local_fixture_git_env(strip=_GIT_ENV_STRIP)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = _clean_git_env()
    git_bin = shutil.which("git", path=env.get("PATH"))
    if git_bin is None:
        raise AssertionError(f"git executable not found on PATH: {env.get('PATH', '')}")

    command = [git_bin, *args]
    result = subprocess.run(
        command,
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise AssertionError(
            "git command failed\n"
            f"cwd: {repo}\n"
            f"command: {command!r}\n"
            f"returncode: {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _init_repo(repo: Path, origin_repo: Path) -> None:
    repo.mkdir()
    _git(repo.parent, "init", "--bare", str(origin_repo))
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "remote", "add", "origin", str(origin_repo))
    (repo / "README.md").write_text("# onboarding smoke\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "push", "-u", "origin", "main")


def _commit_onboarding_files(repo: Path) -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add onboarding files")
    _git(repo, "push", "origin", "main")


def _local_onboarding_prompter() -> _QueuePrompter:
    return _QueuePrompter(
        [
            "New project - set up from scratch",
            "Advanced setup",
            "example/test-repo",
            "agent:dev",
            ".prompts/dev.md",
            "45",
            "claude-code",
            "sonnet",
            "default",
            False,
            "",
            "1",
            "milestone_number",
            "",
            "M0",
            "../",
            "",
            True,
            "web",
            "8080",
            "subprocess",
            "io",
            "true",
            "",
            "300",
            "300",
            "",
            False,
            False,
            "",
            True,
            True,
            False,
        ]
    )


def _verify_setup_authorization(repo_name, authorization):
    """Keep the local journey behind the GitHub verification port."""
    return RepositorySetupGitHubVerification(
        identity="onboarding-test-user",
        repository=repo_name,
        auth_kind="personal",
        source="integration test credential",
        normalized_authorization=authorization,
    )


@pytest.mark.integration
@pytest.mark.heavy_e2e
def test_local_onboarding_smoke_journey(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the real local onboarding flow in a temp git repo."""
    repo = tmp_path / "target-repo"
    origin_repo = tmp_path / "origin.git"
    _init_repo(repo, origin_repo)
    monkeypatch.chdir(repo)

    prompter = _local_onboarding_prompter()
    prereqs = {
        "git": True,
        "github_auth": True,
        "provider:claude-code": True,
        "any_ai_provider": True,
    }
    host = Mock()
    host.list_labels.return_value = []

    with patch(
        "issue_orchestrator.entrypoints.cli_tools.setup_wizard.check_prerequisites",
        return_value=prereqs,
    ), patch(
        "issue_orchestrator.entrypoints.cli_tools.setup_wizard.fetch_github_labels",
        return_value=[],
    ), patch(
        "issue_orchestrator.entrypoints.cli_tools.setup_wizard._get_repository_host",
        return_value=host,
    ), patch(
        "issue_orchestrator.entrypoints.cli_tools.setup_wizard."
        "verify_repository_setup_github_authorization",
        side_effect=_verify_setup_authorization,
    ), patch(
        # Readiness is an interactive, CLI-dependent step with its own tests;
        # this journey verifies the config/guardrail flow, so skip the offer.
        "issue_orchestrator.entrypoints.cli_tools.setup_wizard.offer_readiness_assessment",
    ):
        run_wizard(target_path=repo, prompter=prompter)

    config_path = (
        repo
        / ".issue-orchestrator"
        / "config"
        / "modes"
        / "default"
        / "default.yaml"
    )
    assert config_path.exists()
    assert (repo / ".prompts" / "dev.md").exists()
    assert (repo / ".githooks" / "pre-push").exists()
    assert (repo / "scripts" / "verify-pr.sh").exists()
    assert (repo / ".claude" / "hooks" / "block-no-verify.sh").exists()

    _commit_onboarding_files(repo)

    config = Config.load(config_path)
    config.worktree_seed_ref = "HEAD"
    config.hooks.ai_gate.interval_days = 0
    assert config.session_interactions.enabled is True
    assert config.internal_review_enabled is False
    agent = config.agents["agent:dev"]
    agent.provider = None
    agent.command = "sh -c 'echo onboarding-smoke-agent'"
    agent.meta_agent = "claude-code"
    agent.ai_system = "claude-code"

    runner = LocalCommandRunner()
    checks = [
        *workspace_checks.check_working_directory(runner),
        *workspace_checks.check_hook_dependencies(repo),
        *workspace_checks.check_agents(config),
        *schema_checks.run_schema_checks(config),
        *hook_checks.check_hook_verification(config),
        *hook_checks.check_repo_guardrails(config),
        *guardrail_checks.check_guardrails(config, runner),
    ]

    errors = [f"{check.name}: {check.detail}" for check in checks if check.status == "error"]
    assert not errors, "\n".join(errors)

    by_name = {check.name: check for check in checks}
    assert by_name["Working Directory"].status in {"ok", "warning"}
    assert by_name["Agent Scripts"].status == "ok"
    assert by_name["Agent Prompts"].status == "ok"
    assert by_name["AI Agent Hooks (Installation)"].status == "ok"
    assert by_name["AI Agent Hooks (Verification)"].status == "ok"
    assert by_name["Repo Guardrails"].status == "ok"
    assert by_name["Test Worktree"].status == "ok"
