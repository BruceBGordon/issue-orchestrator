from __future__ import annotations

import pytest
from unittest.mock import Mock

from issue_orchestrator.domain.terminal_launch import TerminalInteractionIntent
from issue_orchestrator.execution.session_interactions import (
    SessionInteractionHandler,
    SessionInteractionRule,
    builtin_session_interaction_rules,
)


def test_session_interaction_handler_matches_split_chunks_once() -> None:
    sender = Mock(return_value=True)
    handler = SessionInteractionHandler(
        session_name="issue-1",
        rules=[
            SessionInteractionRule(
                name="trust",
                required_substrings=(
                    "Quick safety check: Is this a project you created or one you trust?",
                    "Yes, I trust this folder",
                    "No, exit",
                ),
                response="",
            )
        ],
    )
    handler.bind_sender(sender)

    handler.on_output(b"Quick safety check: Is this a project you created ")
    handler.on_output(b"or one you trust?\r\n")
    handler.on_output(b"\xe2\x9d\xaf 1. Yes, I trust this folder\r\n  2. No, exit\r\n")
    handler.on_output(b"Enter to confirm\r\n")
    handler.on_output(
        b"Quick safety check: Is this a project you created or one you trust?\r\n"
    )

    sender.assert_called_once_with("")


def test_session_interaction_handler_ignores_ansi_noise() -> None:
    sender = Mock(return_value=True)
    handler = SessionInteractionHandler(
        session_name="issue-2",
        rules=[
            SessionInteractionRule(
                name="trust",
                required_substrings=("Yes, I trust this folder", "No, exit"),
                response="",
            )
        ],
    )
    handler.bind_sender(sender)

    handler.on_output("\x1b[32mYes, I trust this folder\x1b[0m\r\n")
    handler.on_output("\x1b[1mNo, exit\x1b[0m\r\n")

    sender.assert_called_once_with("")


def test_builtin_session_interaction_rules_are_scoped_to_claude() -> None:
    claude = TerminalInteractionIntent.CLAUDE_TRUST_WORKTREE
    assert builtin_session_interaction_rules(claude)
    assert (
        TerminalInteractionIntent.classify("claude --model sonnet 'fix it'") is claude
    )
    assert (
        TerminalInteractionIntent.classify(
            "FOO=1 BAR=2 && claude --model sonnet 'fix it'"
        )
        is claude
    )
    assert (
        TerminalInteractionIntent.classify("exec CLAUDE --model sonnet 'fix it'")
        is TerminalInteractionIntent.NONE
    )
    assert (
        TerminalInteractionIntent.classify("FOO=1 claude --model sonnet 'fix it'")
        is claude
    )
    assert (
        TerminalInteractionIntent.classify("cat prompt.md | claude --print")
        is TerminalInteractionIntent.NONE
    )
    assert (
        TerminalInteractionIntent.classify(
            "python -m provider_runner --command 'claude foo'"
        )
        is TerminalInteractionIntent.NONE
    )


def test_builtin_session_interaction_rules_include_interactive_codex_only() -> None:
    codex = TerminalInteractionIntent.CODEX_TRUST_WORKTREE
    assert builtin_session_interaction_rules(codex)
    assert (
        TerminalInteractionIntent.classify(
            "codex --ask-for-approval never --model gpt-5-codex "
            '--sandbox workspace-write "review this"'
        )
        is codex
    )
    assert (
        TerminalInteractionIntent.classify(
            "FOO=1 BAR=2 && codex -m gpt-5-codex -c model_reasoning_effort='xhigh' "
            "'review this'"
        )
        is codex
    )
    assert (
        TerminalInteractionIntent.classify(
            "env CODEX_HOME=/tmp/runtime codex --ask-for-approval never 'review this'"
        )
        is codex
    )
    assert (
        TerminalInteractionIntent.classify("codex exec --full-auto")
        is TerminalInteractionIntent.NONE
    )
    assert (
        TerminalInteractionIntent.classify("codex --model gpt-5-codex exec")
        is TerminalInteractionIntent.NONE
    )


def test_session_interaction_rules_only_support_one_shot_rules() -> None:
    with pytest.raises(ValueError, match="fire_once=True"):
        SessionInteractionRule(
            name="repeat",
            required_substrings=("prompt",),
            response="y",
            fire_once=False,
        )
