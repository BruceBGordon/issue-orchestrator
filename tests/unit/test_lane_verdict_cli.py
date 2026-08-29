"""The lane-verdict CLI: all caching policy in one place.

TIMED_RUN contributes two calls; everything decidable — membership,
SHA integrity, corruption handling, the only-green rule — is decided
here and proven here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.cli_tools import (
    lane_verdict as lane_verdict_module,
)
from issue_orchestrator.entrypoints.cli_tools.lane_verdict import (
    LANES_ENVIRONMENT_VARIABLE,
    SHA_ENVIRONMENT_VARIABLE,
    main,
)
from issue_orchestrator.infra.lane_verdicts import (
    LANE_VERDICTS_RELATIVE,
    record_green,
)

SHA = "c" * 40
OTHER_SHA = "d" * 40


@pytest.fixture(autouse=True)
def gate_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Every test targets an isolated worktree via the explicit
    --worktree argument (cwd is deliberately irrelevant: the record
    call runs after recipes that cd away - the first live gate proved
    it), with the gate environment set and HEAD agreeing (the seam
    replaces the git read; test_lane_verdict_gate.py uses a real
    repository)."""
    monkeypatch.setenv(SHA_ENVIRONMENT_VARIABLE, SHA)
    monkeypatch.setenv(
        LANES_ENVIRONMENT_VARIABLE, "test-unit typecheck test-web"
    )
    monkeypatch.setattr(
        lane_verdict_module, "_current_head", lambda worktree: SHA
    )
    return tmp_path


def _check(worktree: Path, target: str) -> int:
    return main(["check", "--worktree", str(worktree), "--target", target])


def _record(worktree: Path, target: str, exit_status: int) -> int:
    return main(
        [
            "record",
            "--worktree",
            str(worktree),
            "--target",
            target,
            "--exit-status",
            str(exit_status),
        ]
    )


def test_check_miss_says_run(gate_environment: Path) -> None:
    assert _check(gate_environment, "test-unit") == 3


def test_record_green_then_check_skips(
    gate_environment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _record(gate_environment, "test-unit", 0) == 0
    assert _check(gate_environment, "test-unit") == 0
    out = capsys.readouterr().out
    assert f"recorded-green-at-{SHA[:12]}" in out
    assert f"cached-green-at-{SHA[:12]}" in out
    assert "skipping" in out


def test_failure_is_never_recorded(gate_environment: Path) -> None:
    assert _record(gate_environment, "test-unit", 1) == 0
    assert _check(gate_environment, "test-unit") == 3


def test_non_member_targets_pass_through_uncached(
    gate_environment: Path,
) -> None:
    """Phase aggregates and non-gate targets are outside the lane set:
    check always says run, record is a no-op."""
    assert _check(gate_environment, "validate-pr-flat-phase") == 3
    assert _record(gate_environment, "validate-pr-flat-phase", 0) == 0
    assert _check(gate_environment, "validate-pr-flat-phase") == 3


def test_missing_environment_is_a_configuration_error(
    gate_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SHA_ENVIRONMENT_VARIABLE)
    assert _check(gate_environment, "test-unit") == 78
    monkeypatch.setenv(SHA_ENVIRONMENT_VARIABLE, SHA)
    monkeypatch.setenv(LANES_ENVIRONMENT_VARIABLE, "   ")
    assert _check(gate_environment, "test-unit") == 78


def test_tree_moved_mid_gate_is_loud_on_check_and_record(
    gate_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The gate read its SHA once at start; a HEAD that moved since
    means any verdict trusted or written now describes a tree the gate
    is not validating. That incident has happened — it must be loud."""
    record_green(gate_environment, SHA, "test-unit")
    monkeypatch.setattr(
        lane_verdict_module, "_current_head", lambda worktree: OTHER_SHA
    )
    assert _check(gate_environment, "test-unit") == 70
    assert _record(gate_environment, "test-unit", 0) == 70
    assert "tree moved mid-gate" in capsys.readouterr().err


def test_corrupt_store_fails_the_lane_never_green(
    gate_environment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record_green(gate_environment, SHA, "test-unit")
    path = gate_environment / LANE_VERDICTS_RELATIVE / SHA / "test-unit.json"
    path.write_text("garbage", encoding="utf-8")
    assert _check(gate_environment, "test-unit") == 70
    assert "delete it" in capsys.readouterr().err


def test_verdict_at_other_sha_does_not_skip(gate_environment: Path) -> None:
    record_green(gate_environment, OTHER_SHA, "test-unit")
    assert _check(gate_environment, "test-unit") == 3


def test_renamed_lane_has_no_verdict_and_runs(gate_environment: Path) -> None:
    record_green(gate_environment, SHA, "test-unit")
    # The lane set names the new target; the old verdict is unreachable
    # under the new name — a rename can never be vacuously covered.
    assert _check(gate_environment, "typecheck") == 3


def test_bad_worktree_argument_is_a_configuration_error(
    gate_environment: Path,
) -> None:
    """cwd never substitutes for the worktree: a relative or missing
    path is refused before any store or git access."""
    assert main(["check", "--worktree", "relative/dir", "--target", "test-unit"]) == 78
    assert (
        main(
            [
                "check",
                "--worktree",
                str(gate_environment / "missing"),
                "--target",
                "test-unit",
            ]
        )
        == 78
    )
