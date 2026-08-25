"""Typed terminal launch contract preserving shell and interaction intent."""

from __future__ import annotations

import shlex
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .path_guards import require_absolute_path, require_path_under


_SHELL_COMMAND_SEPARATORS = frozenset({"&&", ";", "||"})


class TerminalShell(StrEnum):
    """Shell whose language a configured terminal command uses."""

    BASH = "/bin/bash"


class TerminalInteractionIntent(StrEnum):
    """Explicit deterministic interaction behavior for one terminal launch."""

    NONE = "none"
    CLAUDE_TRUST_WORKTREE = "claude-trust-worktree"
    CODEX_TRUST_WORKTREE = "codex-trust-worktree"

    @classmethod
    def classify(cls, shell_command: str) -> TerminalInteractionIntent:
        """Classify the original command before any executor wrapping occurs."""
        if type(shell_command) is not str or not shell_command:
            raise ValueError(
                "TerminalInteractionIntent.classify requires a non-empty command"
            )
        if _matching_command_tokens(shell_command, _is_claude_command_tokens):
            return cls.CLAUDE_TRUST_WORKTREE
        codex = _matching_command_tokens(shell_command, _is_codex_command_tokens)
        if codex is not None and _is_codex_interactive_command_tokens(codex):
            return cls.CODEX_TRUST_WORKTREE
        return cls.NONE


@dataclass(frozen=True, slots=True)
class TerminalRunDestination:
    """Run-owned terminal recording destination used for launch and cancellation."""

    run_dir: Path
    recording_path: Path

    def __post_init__(self) -> None:
        require_absolute_path(self.run_dir, "TerminalRunDestination.run_dir")
        require_absolute_path(
            self.recording_path,
            "TerminalRunDestination.recording_path",
        )
        require_path_under(
            self.recording_path,
            self.run_dir,
            "TerminalRunDestination.recording_path",
        )


@dataclass(frozen=True, slots=True)
class TerminalLaunch:
    """Executable shell text plus semantics that serialization cannot preserve."""

    shell_command: str
    shell: TerminalShell
    interaction_intent: TerminalInteractionIntent
    destination: TerminalRunDestination

    def __post_init__(self) -> None:
        if type(self.shell_command) is not str or not self.shell_command:
            raise ValueError("TerminalLaunch.shell_command must not be empty")
        if type(self.shell) is not TerminalShell:
            raise ValueError("TerminalLaunch.shell must be TerminalShell")
        if type(self.interaction_intent) is not TerminalInteractionIntent:
            raise ValueError(
                "TerminalLaunch.interaction_intent must be TerminalInteractionIntent"
            )
        if type(self.destination) is not TerminalRunDestination:
            raise ValueError(
                "TerminalLaunch.destination must be TerminalRunDestination"
            )

    @classmethod
    def classified(
        cls,
        shell_command: str,
        shell: TerminalShell,
        destination: TerminalRunDestination,
    ) -> TerminalLaunch:
        """Construct from an unwrapped command at the terminal boundary."""
        return cls(
            shell_command=shell_command,
            shell=shell,
            interaction_intent=TerminalInteractionIntent.classify(shell_command),
            destination=destination,
        )


def _matching_command_tokens(
    command: str,
    predicate: Callable[[Sequence[str] | None], bool],
) -> list[str] | None:
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise ValueError(
            "terminal interaction intent requires valid shell quoting"
        ) from exc

    current: list[str] = []
    for token in tokens:
        if token in _SHELL_COMMAND_SEPARATORS:
            command_tokens = _trim_command_prefix(current)
            if predicate(command_tokens):
                return command_tokens
            current = []
            continue
        current.append(token)

    command_tokens = _trim_command_prefix(current)
    return command_tokens if predicate(command_tokens) else None


def _trim_command_prefix(tokens: Sequence[str]) -> list[str] | None:
    trimmed = list(tokens)
    while trimmed and (
        trimmed[0].rsplit("/", 1)[-1] in {"command", "env", "exec"}
        or _looks_like_env_assignment(trimmed[0])
    ):
        trimmed = trimmed[1:]
    return trimmed or None


def _is_claude_command_tokens(tokens: Sequence[str] | None) -> bool:
    return bool(tokens) and tokens[0].rsplit("/", 1)[-1] == "claude"


def _is_codex_command_tokens(tokens: Sequence[str] | None) -> bool:
    return bool(tokens) and tokens[0].rsplit("/", 1)[-1] == "codex"


_CODEX_SUBCOMMANDS = frozenset(
    {
        "exec",
        "e",
        "review",
        "login",
        "logout",
        "mcp",
        "plugin",
        "mcp-server",
        "app-server",
        "remote-control",
        "app",
        "completion",
        "update",
        "doctor",
        "sandbox",
        "debug",
        "apply",
        "a",
        "resume",
        "archive",
        "delete",
        "unarchive",
        "fork",
        "cloud",
        "exec-server",
        "features",
        "help",
    }
)
_CODEX_OPTIONS_WITH_VALUES = frozenset(
    {
        "-a",
        "--add-dir",
        "--ask-for-approval",
        "-c",
        "--cd",
        "-C",
        "-i",
        "--image",
        "-m",
        "--model",
        "-p",
        "--profile",
        "--remote",
        "--remote-auth-token-env",
        "-s",
        "--sandbox",
        "--local-provider",
    }
)


def _is_codex_interactive_command_tokens(tokens: Sequence[str]) -> bool:
    """Return whether Codex arguments select the interactive root command."""
    skip_next = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            return True
        if token in _CODEX_SUBCOMMANDS:
            return False
        if token.startswith("--") and "=" in token:
            continue
        if token in _CODEX_OPTIONS_WITH_VALUES:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return True
    return True


def _looks_like_env_assignment(token: str) -> bool:
    if "=" not in token or token.startswith("-"):
        return False
    name, _, _value = token.partition("=")
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is not None
