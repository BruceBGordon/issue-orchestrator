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
    """Every test runs inside an isolated 'worktree' with the gate
    environment set and HEAD agreeing with it (the seam replaces the
    git read; one integration test in test_lane_verdict_gate.py uses
    a real repository)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(SHA_ENVIRONMENT_VARIABLE, SHA)
    monkeypatch.setenv(
        LANES_ENVIRONMENT_VARIABLE, "test-unit typecheck test-web"
    )
    monkeypatch.setattr(
        lane_verdict_module, "_current_head", lambda worktree: SHA
    )
    return tmp_path


def test_check_miss_says_run(gate_environment: Path) -> None:
    assert main(["check", "--target", "test-unit"]) == 3


def test_record_green_then_check_skips(
    gate_environment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["record", "--target", "test-unit", "--exit-status", "0"]) == 0
    assert main(["check", "--target", "test-unit"]) == 0
    out = capsys.readouterr().out
    assert f"recorded-green-at-{SHA[:12]}" in out
    assert f"cached-green-at-{SHA[:12]}" in out
    assert "skipping" in out


def test_failure_is_never_recorded(gate_environment: Path) -> None:
    assert main(["record", "--target", "test-unit", "--exit-status", "1"]) == 0
    assert main(["check", "--target", "test-unit"]) == 3


def test_non_member_targets_pass_through_uncached(
    gate_environment: Path,
) -> None:
    """Phase aggregates and non-gate targets are outside the lane set:
    check always says run, record is a no-op."""
    assert main(["check", "--target", "validate-pr-flat-phase"]) == 3
    assert (
        main(
            ["record", "--target", "validate-pr-flat-phase", "--exit-status", "0"]
        )
        == 0
    )
    assert main(["check", "--target", "validate-pr-flat-phase"]) == 3


def test_missing_environment_is_a_configuration_error(
    gate_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SHA_ENVIRONMENT_VARIABLE)
    assert main(["check", "--target", "test-unit"]) == 78
    monkeypatch.setenv(SHA_ENVIRONMENT_VARIABLE, SHA)
    monkeypatch.setenv(LANES_ENVIRONMENT_VARIABLE, "   ")
    assert main(["check", "--target", "test-unit"]) == 78


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
    assert main(["check", "--target", "test-unit"]) == 70
    assert main(["record", "--target", "test-unit", "--exit-status", "0"]) == 70
    assert "tree moved mid-gate" in capsys.readouterr().err


def test_corrupt_store_fails_the_lane_never_green(
    gate_environment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record_green(gate_environment, SHA, "test-unit")
    path = gate_environment / LANE_VERDICTS_RELATIVE / SHA / "test-unit.json"
    path.write_text("garbage", encoding="utf-8")
    assert main(["check", "--target", "test-unit"]) == 70
    assert "delete it" in capsys.readouterr().err


def test_verdict_at_other_sha_does_not_skip(gate_environment: Path) -> None:
    record_green(gate_environment, OTHER_SHA, "test-unit")
    assert main(["check", "--target", "test-unit"]) == 3


def test_renamed_lane_has_no_verdict_and_runs(gate_environment: Path) -> None:
    record_green(gate_environment, SHA, "test-unit")
    # The lane set names the new target; the old verdict is unreachable
    # under the new name — a rename can never be vacuously covered.
    assert main(["check", "--target", "typecheck"]) == 3
