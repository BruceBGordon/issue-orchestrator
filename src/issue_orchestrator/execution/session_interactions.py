"""Rule-based prompt-response helpers for running PTY-backed sessions."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, Sequence

from ..domain.terminal_launch import TerminalInteractionIntent

logger = logging.getLogger(__name__)

_MAX_BUFFER_CHARS = 12000
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_OSC_ESCAPE_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_WHITESPACE_RE = re.compile(r"\s+")


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


def builtin_session_interaction_rules(
    intent: TerminalInteractionIntent,
) -> tuple[SessionInteractionRule, ...]:
    """Return built-in rules for one already-classified terminal launch."""
    if type(intent) is not TerminalInteractionIntent:
        raise ValueError(
            "builtin_session_interaction_rules requires TerminalInteractionIntent"
        )
    rules: list[SessionInteractionRule] = []
    if intent is TerminalInteractionIntent.CLAUDE_TRUST_WORKTREE:
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
    if intent is TerminalInteractionIntent.CODEX_TRUST_WORKTREE:
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
