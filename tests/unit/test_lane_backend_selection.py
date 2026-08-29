"""Which backend is in play, and what established it.

The defect this guards (finding 1, #7138): a status tool defaulted to
`direct` whenever the environment variable was unset, so on a repository
whose gate command selects the scheduler it confidently reported the
wrong pool — and the dispatch journal one screen below said otherwise.
A wrong answer that looks like an answer is worse than no answer, so
there is deliberately no final default here.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.execution.lane_backends import (
    BACKEND_ENVIRONMENT_VARIABLE,
    BackendSource,
    SelectedBackend,
    UnknownBackend,
    backend_in_command,
    select_backend,
)

_CONFIGURED = ("LANE_EXECUTOR=condor make validate-quick",)


def test_the_repository_gate_command_establishes_the_backend() -> None:
    """The reviewer's reproduction: environment unset, repo says condor."""
    selection = select_backend(
        explicit=None, environment={}, validation_commands=_CONFIGURED
    )

    assert selection == SelectedBackend(
        name="condor", source=BackendSource.VALIDATION_COMMAND
    )


def test_nothing_establishing_a_backend_is_unknown_not_direct() -> None:
    selection = select_backend(
        explicit=None, environment={}, validation_commands=()
    )

    assert type(selection) is UnknownBackend
    assert "--backend" in selection.reason
    assert "direct" not in selection.reason


def test_an_explicit_request_beats_everything() -> None:
    selection = select_backend(
        explicit="direct",
        environment={BACKEND_ENVIRONMENT_VARIABLE: "condor"},
        validation_commands=_CONFIGURED,
    )

    assert selection == SelectedBackend(name="direct", source=BackendSource.FLAG)


def test_the_environment_beats_the_repository_command() -> None:
    """Matches what actually decides: make reads the environment."""
    selection = select_backend(
        explicit=None,
        environment={BACKEND_ENVIRONMENT_VARIABLE: "direct"},
        validation_commands=_CONFIGURED,
    )

    assert selection == SelectedBackend(
        name="direct", source=BackendSource.ENVIRONMENT
    )


def test_an_empty_environment_value_establishes_nothing() -> None:
    selection = select_backend(
        explicit=None,
        environment={BACKEND_ENVIRONMENT_VARIABLE: ""},
        validation_commands=_CONFIGURED,
    )

    assert selection == SelectedBackend(
        name="condor", source=BackendSource.VALIDATION_COMMAND
    )


def test_gates_that_disagree_are_reported_rather_than_resolved() -> None:
    """Picking one would hide a real defect in the repository config."""
    selection = select_backend(
        explicit=None,
        environment={},
        validation_commands=(
            "LANE_EXECUTOR=condor make validate-quick",
            "LANE_EXECUTOR=direct make validate-pr",
        ),
    )

    assert type(selection) is UnknownBackend
    assert "disagree" in selection.reason
    assert "condor" in selection.reason and "direct" in selection.reason


def test_gates_that_agree_are_one_answer() -> None:
    selection = select_backend(
        explicit=None,
        environment={},
        validation_commands=(
            "LANE_EXECUTOR=condor make validate-quick",
            "LANE_EXECUTOR=condor make validate-pr-raw",
        ),
    )

    assert selection == SelectedBackend(
        name="condor", source=BackendSource.VALIDATION_COMMAND
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("LANE_EXECUTOR=condor make validate-quick", "condor"),
        ("ISSUE_ORCHESTRATOR_LANE_EXECUTOR=condor make x", "condor"),
        ("make validate-quick LANE_EXECUTOR=condor", "condor"),
        ("  LANE_EXECUTOR=direct   make  x  ", "direct"),
        ("make validate-quick", None),
        ("", None),
        ("LANE_EXECUTOR= make x", None),
        ("OTHER=condor make x", None),
        # Unparseable quoting tells us nothing, and nothing is the
        # honest answer — not a guess.
        ('make "unterminated', None),
    ],
)
def test_the_backend_is_read_out_of_a_gate_command_exactly_once(
    command: str, expected: str | None
) -> None:
    assert backend_in_command(command) == expected


def test_a_later_assignment_wins_like_a_shell_reading_the_line() -> None:
    assert (
        backend_in_command("LANE_EXECUTOR=direct LANE_EXECUTOR=condor make x")
        == "condor"
    )


def test_a_selection_must_name_a_backend_and_a_source() -> None:
    with pytest.raises(ValueError, match="name"):
        SelectedBackend(name="", source=BackendSource.FLAG)
    with pytest.raises(ValueError, match="BackendSource"):
        SelectedBackend(name="condor", source="flag")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reason"):
        UnknownBackend(reason="")
