"""Unit tests for workspace doctor checks."""

import subprocess
from pathlib import Path

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.infra.config import Config, DefaultAgentConfig
from issue_orchestrator.ports.command_runner import CommandResult, OutputNewlines
from issue_orchestrator.infra.doctor.checks.workspace import check_agents


def _agent_scripts_check(config: Config):
    return next(check for check in check_agents(config) if check.name == "Agent Scripts")


class _FakeProvider:
    def __init__(self, name: str, executable: str, available: bool = True):
        self.name = name
        self.executable = executable
        self._available = available

    def is_available(self) -> bool:
        return self._available


def _patch_providers(monkeypatch, providers: dict[str, _FakeProvider]) -> None:
    def get_provider(name: str) -> _FakeProvider:
        try:
            return providers[name]
        except KeyError as exc:
            raise ValueError(f"Unknown provider: {name!r}") from exc

    monkeypatch.setattr("issue_orchestrator.agent_runner.get_provider", get_provider)


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )


def _agent_prompts_check(config: Config, runner=None):
    return next(
        check for check in check_agents(config, runner=runner) if check.name == "Agent Prompts"
    )


def test_agent_scripts_use_provider_registry_for_provider_agents(
    monkeypatch,
    tmp_path: Path,
):
    config = Config()
    config.agents = {
        "agent:backend": AgentConfig(
            prompt_path=tmp_path / "backend.md",
            provider="claude-code",
        ),
        "agent:reviewer": AgentConfig(
            prompt_path=tmp_path / "reviewer.md",
            provider="codex",
        ),
    }
    _patch_providers(
        monkeypatch,
        {
            "claude-code": _FakeProvider("claude-code", "claude"),
            "codex": _FakeProvider("codex", "codex"),
        },
    )
    monkeypatch.setattr(
        "issue_orchestrator.infra.doctor.checks.workspace.shutil.which",
        lambda _: None,
    )

    check = _agent_scripts_check(config)

    assert check.status == "ok"
    assert check.detail == "All found"


def test_agent_scripts_report_missing_configured_provider_cli(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("NVM_BIN", "/Users/test/.nvm/versions/node/v24.11.1/bin")
    monkeypatch.setenv("PATH", "/Users/test/.nvm/versions/node/v24.11.1/bin:/usr/bin:/bin")
    monkeypatch.setattr(
        "issue_orchestrator.infra.provider_cli_diagnostics._find_executable_outside_path",
        lambda executable: [Path("/Users/test/.nvm/versions/node/v24.14.1/bin/claude")],
    )
    config = Config()
    config.agents = {
        "agent:backend": AgentConfig(
            prompt_path=tmp_path / "backend.md",
            provider="claude-code",
        ),
    }
    _patch_providers(
        monkeypatch,
        {"claude-code": _FakeProvider("claude-code", "claude", available=False)},
    )

    check = _agent_scripts_check(config)

    assert check.status == "error"
    assert check.detail == (
        "Missing: agent:backend: claude-code (expected executable: claude); "
        "executable 'claude' not found on PATH; "
        "NVM_BIN=/Users/test/.nvm/versions/node/v24.11.1/bin; "
        "found outside PATH: /Users/test/.nvm/versions/node/v24.14.1/bin/claude"
    )


def test_agent_scripts_use_default_agent_provider(monkeypatch, tmp_path: Path):
    config = Config()
    config.default_agent = DefaultAgentConfig(provider="codex")
    config.agents = {
        "agent:reviewer": AgentConfig(prompt_path=tmp_path / "reviewer.md"),
    }
    _patch_providers(monkeypatch, {"codex": _FakeProvider("codex", "codex")})
    monkeypatch.setattr(
        "issue_orchestrator.infra.doctor.checks.workspace.shutil.which",
        lambda _: None,
    )

    check = _agent_scripts_check(config)

    assert check.status == "ok"
    assert check.detail == "All found"


def test_agent_scripts_still_validate_legacy_commands(monkeypatch, tmp_path: Path):
    config = Config()
    config.agents = {
        "agent:legacy": AgentConfig(
            prompt_path=tmp_path / "legacy.md",
            command="missing-agent --do-work",
        ),
    }
    monkeypatch.setattr(
        "issue_orchestrator.infra.doctor.checks.workspace.shutil.which",
        lambda _: None,
    )

    check = _agent_scripts_check(config)

    assert check.status == "error"
    assert check.detail == "Missing: agent:legacy: missing-agent"


def test_agent_prompts_error_when_prompt_not_committed_to_head(
    monkeypatch,
    tmp_path: Path,
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")
    (repo_root / "README.md").write_text("hello\n")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "init")

    prompt_path = repo_root / ".prompts" / "dev.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text("Prompt\n")

    config = Config()
    config.repo_root = repo_root
    config.worktree_seed_ref = "HEAD"
    config.agents = {
        "agent:dev": AgentConfig(prompt_path=prompt_path, provider="codex"),
    }

    check = _agent_prompts_check(config)

    assert check.status == "error"
    assert "Not available from worktree seed ref HEAD" in check.detail
    assert ".prompts/dev.md" in check.detail
    assert "set worktrees.seed_ref for local iteration" in check.detail


def test_agent_prompts_warn_when_prompt_only_modified_locally(
    monkeypatch,
    tmp_path: Path,
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")
    prompt_path = repo_root / ".prompts" / "dev.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text("Prompt\n")
    _git(repo_root, "add", ".prompts/dev.md")
    _git(repo_root, "commit", "-m", "add prompt")
    prompt_path.write_text("Prompt updated\n")

    config = Config()
    config.repo_root = repo_root
    config.worktree_seed_ref = "HEAD"
    config.agents = {
        "agent:dev": AgentConfig(prompt_path=prompt_path, provider="codex"),
    }

    check = _agent_prompts_check(config)

    assert check.status == "warning"
    assert ".prompts/dev.md" in check.detail
    assert "seed ref version (HEAD)" in check.detail


def test_agent_prompts_use_injected_runner(monkeypatch, tmp_path: Path):
    class _RecordingRunner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def run(
            self,
            command: str | list[str],
            *,
            cwd: Path | None = None,
            env: dict[str, str] | None = None,
            timeout_seconds: int | None = None,
            shell: bool = False,
            newlines: OutputNewlines = OutputNewlines.TRANSLATED,
        ) -> CommandResult:
            assert isinstance(command, list)
            self.commands.append(command)
            if "cat-file" in command:
                return CommandResult(returncode=0, stdout="", stderr="")
            if "status" in command:
                return CommandResult(returncode=0, stdout="", stderr="")
            raise AssertionError(f"Unexpected git command: {command}")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    prompt_path = repo_root / ".prompts" / "dev.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text("Prompt\n")

    config = Config()
    config.repo_root = repo_root
    config.worktree_seed_ref = "HEAD"
    config.agents = {
        "agent:dev": AgentConfig(prompt_path=prompt_path, provider="codex"),
    }

    runner = _RecordingRunner()
    check = _agent_prompts_check(config, runner=runner)

    assert check.status == "ok"
    assert runner.commands == [
        ["git", "-C", str(repo_root), "cat-file", "-e", "HEAD:.prompts/dev.md"],
        ["git", "-C", str(repo_root), "status", "--porcelain", "--", ".prompts/dev.md"],
    ]
