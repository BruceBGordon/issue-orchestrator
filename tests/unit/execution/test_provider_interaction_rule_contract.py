"""The dog that didn't bark: rules that match the commands we actually build.

`builtin_session_interaction_rules` matched NOTHING for the command the codex
provider really emits, and had done since the isolated `CODEX_HOME` prefix was
introduced. Nothing failed. Nothing was logged. A rule that never fires is
indistinguishable from a rule with nothing to do, so the codex trust-worktree
prompt simply went unanswered forever and the only symptom was somebody else's
timeout.

Both sides of that boundary were tested in isolation and both were green:
the provider had tests for the argv it builds, and the matcher had tests for
the shapes it recognises — using hand-written command strings that nobody had
checked against the real ones. The gap was between them.

So these tests join the two: take the argv a provider ACTUALLY builds, hand it
to the matcher, and assert the rules come out. No hand-written command strings
here on purpose — a literal in this file could drift from the provider exactly
the way the old matcher tests did.
"""

from __future__ import annotations

import shlex
import tempfile
from pathlib import Path

import pytest

from issue_orchestrator.execution.agent_runner_providers.codex import CodexProvider
from issue_orchestrator.execution.session_interactions import (
    builtin_session_interaction_rules,
)


@pytest.fixture
def working_directory() -> Path:
    return Path(tempfile.mkdtemp(prefix="rule-contract-"))


def _rule_names(command: list[str]) -> set[str]:
    return {rule.name for rule in builtin_session_interaction_rules(shlex.join(command))}


def test_the_interactive_codex_command_matches_its_trust_rule(
    working_directory: Path,
) -> None:
    """The regression itself.

    The provider runs codex through an isolated home:

        env CODEX_HOME=<runtime> codex ...

    which defeated the matcher twice over — a leading `env` COMMAND that was
    never trimmed, and an assignment whose value is a path, rejected by a
    guard that refused any token containing a slash.
    """
    command = CodexProvider().build_command(
        "review this",
        working_directory=working_directory,
        approval_mode="full-auto",
    )

    assert "codex-trust-worktree" in _rule_names(command), (
        "the codex trust prompt has no rule for the command we actually "
        f"build, so it will never be answered: {shlex.join(command)[:200]}"
    )


def test_the_codex_command_still_starts_with_the_env_prefix(
    working_directory: Path,
) -> None:
    """Pins the shape the test above exists to defend.

    If the provider stops wrapping codex in `env`, the assertion above starts
    passing for a reason unrelated to the matcher, and the regression it
    guards could return unnoticed under a different prefix.
    """
    command = CodexProvider().build_command(
        "review this",
        working_directory=working_directory,
        approval_mode="full-auto",
    )

    assert command[0] == "env"
    assert command[1].startswith("CODEX_HOME=")
    assert "/" in command[1], "the path-valued assignment is the tricky case"


def test_a_one_shot_exec_command_is_not_treated_as_interactive(
    working_directory: Path,
) -> None:
    """The rules are for the TUI. `codex exec` has no trust prompt to answer.

    Guards the other direction: a matcher loosened until it matches the real
    command must not start matching every codex invocation.
    """
    command = CodexProvider().build_command(
        "review this",
        working_directory=working_directory,
        approval_mode="full-auto",
        execution_mode="exec",
    )

    assert "codex-trust-worktree" not in _rule_names(command)
