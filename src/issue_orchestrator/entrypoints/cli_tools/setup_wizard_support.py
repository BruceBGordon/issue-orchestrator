"""Support types and helpers for the interactive setup wizard."""

from __future__ import annotations

import getpass
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..setup_wizard_common import (
    FileCollector,
    detect_repo as detect_repo,
    fetch_github_labels as fetch_github_labels,
)


class Prompter(Protocol):
    """Protocol for user interaction - enables testing via dependency injection."""

    def print(self, message: str) -> None:
        """Print a message to the user."""
        ...

    def input(self, question: str, default: str = "") -> str:
        """Prompt for text input with optional default."""
        ...

    def yes_no(self, question: str, default: bool = True) -> bool:
        """Prompt for yes/no answer."""
        ...

    def choice(
        self, question: str, choices: list[str], allow_custom: bool = False
    ) -> str:
        """Prompt user to choose from a list."""
        ...


class ConsolePrompter:
    """Real console-based prompter for interactive use."""

    def print(self, message: str) -> None:
        print(message)

    def input(self, question: str, default: str = "") -> str:
        if default:
            result = input(f"{question} [{default}]: ").strip()
            return result if result else default
        return input(f"{question}: ").strip()

    def yes_no(self, question: str, default: bool = True) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        result = input(f"{question} {suffix}: ").strip().lower()
        if not result:
            return default
        return result in ("y", "yes")

    def choice(
        self, question: str, choices: list[str], allow_custom: bool = False
    ) -> str:
        print(f"\n{question}")
        for i, choice in enumerate(choices, 1):
            print(f"  {i}. {choice}")
        if allow_custom:
            print(f"  {len(choices) + 1}. Other (enter custom)")

        while True:
            result = input("Choice: ").strip()
            try:
                idx = int(result) - 1
                if 0 <= idx < len(choices):
                    return choices[idx]
                if allow_custom and idx == len(choices):
                    return input("Enter custom value: ").strip()
            except ValueError:
                pass
            print("Invalid choice, try again.")


@dataclass
class DetectedState:
    """What we found in an existing repo."""

    repo: str | None = None
    github_labels: list[str] = field(default_factory=list)
    agent_labels: list[str] = field(default_factory=list)
    existing_config: dict | None = None
    config_path: Path | None = None
    prompt_candidates: list[Path] = field(default_factory=list)


RunGit = Callable[[list[str]], tuple[bool, str]]
ProviderLister = Callable[[], list[str]]
ProviderGetter = Callable[[str], "SetupProvider"]
class SetupProvider(Protocol):
    def is_available(self) -> bool:
        """Return whether this provider can run on the current machine."""
        ...


class DetectRepoFn(Protocol):
    def __call__(self, *, cwd: Path | None = None) -> str | None:
        """Detect the repository slug for a working directory."""
        ...


def check_prerequisites(
    run_git_func: RunGit,
    list_providers_func: ProviderLister,
    get_provider_func: ProviderGetter,
) -> dict[str, bool]:
    """Check if required tools are installed."""
    checks = {}

    # git
    ok, _ = run_git_func(["--version"])
    checks["git"] = ok

    # GitHub token
    try:
        from ...execution.providers import resolve_github_token

        resolve_github_token(configured_token=None, configured_env=None)
        checks["github_auth"] = True
    except Exception:
        checks["github_auth"] = False

    # Check AI providers from registry
    providers = list_providers_func()
    any_provider_available = False
    for name in providers:
        provider = get_provider_func(name)
        is_available = provider.is_available()
        checks[f"provider:{name}"] = is_available
        if is_available:
            any_provider_available = True

    # At least one provider should be available
    checks["any_ai_provider"] = any_provider_available

    return checks


def scan_existing_repo(
    path: Path | None,
    detect_repo_func: DetectRepoFn,
    fetch_github_labels_func: Callable[[str], list[str]],
    find_existing_config_func: Callable[[Path], tuple[Path | None, dict | None]],
    find_prompt_candidates_func: Callable[[Path], list[Path]],
) -> DetectedState:
    """Scan an existing repo and detect its state."""
    if path is None:
        path = Path.cwd()
    state = DetectedState()

    # Detect repo from the target path.
    state.repo = detect_repo_func(cwd=path)

    # Find existing config.
    state.config_path, state.existing_config = find_existing_config_func(path)

    # Fetch GitHub labels if we have a repo.
    if state.repo:
        state.github_labels = fetch_github_labels_func(state.repo)
        state.agent_labels = [l for l in state.github_labels if l.startswith("agent:")]

    # Find prompt candidates.
    state.prompt_candidates = find_prompt_candidates_func(path)

    return state


def print_changes_summary(
    collector: FileCollector, prompter: Prompter, dry_run: bool = False
) -> None:
    """Print summary of changes to be applied."""
    prompter.print("\n" + "=" * 50)
    if dry_run:
        prompter.print("CHANGES THAT WOULD BE APPLIED")
    else:
        prompter.print("CHANGES TO APPLY")
    prompter.print("=" * 50)

    if collector.writes:
        prompter.print("\nFiles to create/modify:")
        for write in collector.writes:
            prompter.print(f"  [{write.action}] {write.path} ({write.size_display()})")
    else:
        prompter.print("\nFiles: (none)")

    if collector.labels:
        prompter.print("\nGitHub labels to ensure:")
        for name, _color, desc in collector.labels:
            prompter.print(f"  • {name} - {desc}")
    else:
        prompter.print("\nGitHub labels: (none planned)")

    prompter.print("")


def setup_ai_providers(prompter: Prompter) -> None:
    """Ask about AI providers and help store keys in keyring."""
    from ...infra.ai_keys import get_ai_providers, read_ai_key, store_ai_key

    prompter.print("\n" + "=" * 50)
    prompter.print("AI PROVIDER SETUP")
    prompter.print("=" * 50)
    prompter.print("\nSome agent providers can authenticate with API keys.")
    prompter.print("Keys are stored securely in your system keyring.\n")

    for key_name, info in get_ai_providers().items():
        existing = read_ai_key(key_name)
        if existing:
            status = "[configured]"
            prompter.print(f"  {info['name']}: {status}")
            if not prompter.yes_no(f"  Update {info['name']} key?", default=False):
                continue
        else:
            if not prompter.yes_no(f"Configure {info['name']}?", default=False):
                continue

        # Show setup instructions.
        prompter.print(f"\n  --- {info['name']} Setup ---")
        setup_cmd = info.get("setup_cmd")
        if setup_cmd:
            prompter.print(f"  Run in another terminal: {setup_cmd}")
            prompter.print("  Then paste the key here.")
        else:
            prompter.print(f"  {info.get('setup_help', '')}")
        prompter.print(f"  URL: {info.get('url', '')}\n")

        # Prompt for key.
        value = getpass.getpass(f"  Paste your {key_name}: ")
        if value.strip():
            try:
                store_ai_key(key_name, value.strip())
                prompter.print(f"  ✓ Stored {key_name} in keyring\n")
            except Exception as e:
                prompter.print(f"  ✗ Failed to store key: {e}")
                prompter.print(
                    "    You can set it as an environment variable instead.\n"
                )
        else:
            prompter.print("  Skipped (no key provided)\n")


def prompt_worktree_setup_commands(prompter: Prompter) -> list[str]:
    """Prompt for per-worktree setup commands; returns [] if none provided.

    Shared by the new-project and existing-project wizard flows.
    """
    prompter.print("\n--- Worktree Setup Commands ---")
    prompter.print(
        "Commands to run in each new worktree after creation (e.g., install deps)."
    )
    prompter.print("Examples:")
    prompter.print("  npm install")
    prompter.print("  pip install -e '.[dev]'")
    prompter.print("  make setup")
    setup_input = prompter.input(
        "Setup commands (comma-separated, or empty to skip)", ""
    )
    if not setup_input.strip():
        return []
    return [cmd.strip() for cmd in setup_input.split(",") if cmd.strip()]
