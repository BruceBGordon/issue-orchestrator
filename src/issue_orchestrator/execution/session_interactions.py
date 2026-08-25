"""Rule-based prompt-response helpers for running PTY-backed sessions."""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass
from typing import Callable, Sequence

logger = logging.getLogger(__name__)

_MAX_BUFFER_CHARS = 12000
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_OSC_ESCAPE_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_WHITESPACE_RE = re.compile(r"\s+")
_SHELL_COMMAND_SEPARATORS = frozenset({"&&", ";", "||"})


def _normalize_terminal_text(text: str) -> str:
    """Collapse terminal control noise into a stable search buffer."""
    if not text:
        return ""
    text = _OSC_ESCAPE_RE.sub(" ", text)
    text = _ANSI_ESCAPE_RE.sub(" ", text)
    text = text.replace("\r", " ").replace("\n", " ")
    text = _WHITESPACE_RE.sub(" ", text)
    return text.casefold().strip()


@dataclass(frozen=True)
class SessionInteractionRule:
    """One deterministic prompt-response rule."""

    name: str
    required_substrings: tuple[str, ...]
    response: str
    # Reserved for future cooldown/edge-trigger semantics; current rules are one-shot only.
    fire_once: bool = True

    def __post_init__(self) -> None:
        if not self.required_substrings:
            raise ValueError("SessionInteractionRule.required_substrings cannot be empty")
        if not self.fire_once:
            raise ValueError("SessionInteractionRule only supports fire_once=True")


@dataclass(frozen=True)
class _CompiledRule:
    rule: SessionInteractionRule
    markers: tuple[str, ...]


class SessionInteractionHandler:
    """Matches terminal output against rules and sends line-based responses."""

    def __init__(
        self,
        *,
        session_name: str,
        rules: Sequence[SessionInteractionRule],
        max_buffer_chars: int = _MAX_BUFFER_CHARS,
    ) -> None:
        self._session_name = session_name
        self._max_buffer_chars = max(256, max_buffer_chars)
        self._buffer = ""
        self._sender: Callable[[str], bool] | None = None
        self._fired_rules: set[str] = set()
        self._rules = tuple(
            _CompiledRule(
                rule=rule,
                markers=tuple(
                    marker
                    for marker in (_normalize_terminal_text(item) for item in rule.required_substrings)
                    if marker
                ),
            )
            for rule in rules
        )

    def bind_sender(self, sender: Callable[[str], bool]) -> None:
        """Attach a line-oriented sender once the PTY session exists."""
        self._sender = sender

    @property
    def all_rules_fired(self) -> bool:
        """Whether every configured one-shot rule has fired."""
        return all(compiled.rule.name in self._fired_rules for compiled in self._rules)

    def on_output(self, data: bytes | str) -> None:
        """Observe PTY output and fire matching rules."""
        text = data.decode("utf-8", errors="ignore") if isinstance(data, bytes) else data
        normalized = _normalize_terminal_text(text)
        if not normalized:
            return

        combined = f"{self._buffer} {normalized}".strip() if self._buffer else normalized
        self._buffer = combined[-self._max_buffer_chars :]

        for compiled in self._rules:
            rule = compiled.rule
            if rule.fire_once and rule.name in self._fired_rules:
                continue
            if not compiled.markers or not all(marker in self._buffer for marker in compiled.markers):
                continue
            sender = self._sender
            if sender is None:
                logger.warning(
                    "[session-interactions] matched rule before sender was ready: session=%s rule=%s",
                    self._session_name,
                    rule.name,
                )
                continue
            sent = sender(rule.response)
            logger.info(
                "[session-interactions] rule fired: session=%s rule=%s sent=%s response=%s",
                self._session_name,
                rule.name,
                sent,
                "<enter>" if rule.response == "" else rule.response,
            )
            if sent and rule.fire_once:
                self._fired_rules.add(rule.name)


def builtin_session_interaction_rules(command: str) -> tuple[SessionInteractionRule, ...]:
    """Return built-in rules that apply to a specific session command.

    This intentionally targets the raw interactive Claude launch shape that the
    subprocess plugin receives from SessionLauncher, plus the interactive Codex
    launch shape used by persistent review exchange. It accepts leading shell
    environment assignments such as ``FOO=bar && claude ...``. It assumes those
    shell separators are whitespace-delimited, which matches the orchestrator's
    SessionLauncher command shape.
    """
    rules: list[SessionInteractionRule] = []
    if _looks_like_claude_command(command):
        rules.append(
            SessionInteractionRule(
                name="claude-trust-worktree",
                required_substrings=(
                    "Quick safety check: Is this a project you created or one you trust?",
                    "Yes, I trust this folder",
                    "No, exit",
                ),
                response="",
            ),
        )
    if _looks_like_interactive_codex_command(command):
        rules.append(
            SessionInteractionRule(
                name="codex-trust-worktree",
                required_substrings=(
                    "Do you trust the contents of this directory?",
                    "Yes, continue",
                    "No, quit",
                ),
                response="",
            ),
        )
    return tuple(rules)


def _looks_like_claude_command(command: str) -> bool:
    return _claude_command_tokens(command) is not None


def _looks_like_interactive_codex_command(command: str) -> bool:
    tokens = _codex_command_tokens(command)
    return tokens is not None and _is_codex_interactive_command_tokens(tokens)


def _claude_command_tokens(command: str) -> list[str] | None:
    """Extract the whitespace-delimited Claude command segment from a shell command."""
    return _matching_command_tokens(command, _is_claude_command_tokens)


def _codex_command_tokens(command: str) -> list[str] | None:
    """Extract the whitespace-delimited Codex command segment from a shell command."""
    return _matching_command_tokens(command, _is_codex_command_tokens)


def _matching_command_tokens(
    command: str,
    predicate: Callable[[Sequence[str] | None], bool],
) -> list[str] | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

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
    if predicate(command_tokens):
        return command_tokens
    return None


def _trim_command_prefix(tokens: Sequence[str]) -> list[str] | None:
    trimmed = list(tokens)
    while trimmed and (
        trimmed[0].rsplit("/", 1)[-1] in {"command", "env", "exec"}
        or _looks_like_env_assignment(trimmed[0])
    ):
        trimmed = trimmed[1:]
    return trimmed or None


def _is_claude_command_tokens(tokens: Sequence[str] | None) -> bool:
    if not tokens:
        return False
    executable = tokens[0].rsplit("/", 1)[-1]
    return executable == "claude"


def _is_codex_command_tokens(tokens: Sequence[str] | None) -> bool:
    if not tokens:
        return False
    executable = tokens[0].rsplit("/", 1)[-1]
    return executable == "codex"


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
    if not _is_codex_command_tokens(tokens):
        return False
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
    key, _, _ = token.partition("=")
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is not None
