"""The verdict layer's gate wiring: enabled where promised, nowhere else,
and the fail-one → re-run-one → all-cached lifecycle proven end to end.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _scrubbed_environment() -> dict[str, str]:
    # Hermetic against the hosting gate: when this test runs INSIDE a
    # verdict-enabled gate lane, LANE_VERDICT_* are exported into the
    # lane's environment and would leak into the sub-make under test —
    # the same hosting-gate-poisons-the-subprocess class as MAKEFLAGS
    # and the worker-width family.
    blocked = {
        "MAKEFLAGS",
        "MFLAGS",
        "MAKELEVEL",
        "LANE_EXECUTOR",
        "ISSUE_ORCHESTRATOR_LANE_EXECUTOR",
        "LANE_VERDICT_SHA",
        "LANE_VERDICT_LANES",
        "PARALLEL",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key not in blocked
        and not key.endswith("_PARALLEL")
        and not key.startswith("LANE_WORKERS_")
    }


def _make(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    make = shutil.which("gmake") or "make"
    return subprocess.run(
        [make, "-f", str(REPO_ROOT / "Makefile"), *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=_scrubbed_environment(),
    )


def _dry_run(target: str, *variables: str, cwd: Path) -> str:
    return _make("-n", target, *variables, cwd=cwd).stdout


def test_gate_lane_set_is_the_fan_itself(tmp_path: Path) -> None:
    """Drift guard: LANE_VERDICT_LANES is derived from the SAME
    variable the fan executes, so a lane added to the fan is cacheable
    by construction and can never be vacuously covered. Both sides come
    from dry-runs, never a hand-maintained list."""
    phase = _dry_run("_validate-pr-impl", "LANE_EXECUTOR=condor", cwd=tmp_path)
    exported = re.search(r'LANE_VERDICT_LANES="([^"]+)"', phase)
    assert exported, "gate phase does not export the lane set"
    fan = _dry_run(
        "_validate-pr-flat-impl", "LANE_EXECUTOR=condor", cwd=tmp_path
    )
    fan_targets = set(re.findall(r'target="([^"]+)"', fan))
    assert fan_targets, "flat fan dry-run produced no targets - probe broken"
    assert set(exported.group(1).split()) == fan_targets


def test_gate_reads_the_tree_sha_exactly_once(tmp_path: Path) -> None:
    phase = _dry_run("_validate-pr-impl", "LANE_EXECUTOR=condor", cwd=tmp_path)
    assert phase.count("git rev-parse HEAD") == 1


def test_direct_mode_gate_is_not_verdict_enabled(tmp_path: Path) -> None:
    """Deliberate exclusion (rationale in the Makefile): the direct
    phased topology has different leaves, and the re-run waste this
    layer removes lives in the condor publish-gate path."""
    phase = _dry_run("_validate-pr-impl", "LANE_EXECUTOR=direct", cwd=tmp_path)
    # The inert guard string appears in every TIMED_RUN body; what must
    # be absent is the ENABLEMENT — the export assignment.
    assert "LANE_VERDICT_SHA=$(git" not in phase
    assert 'LANE_VERDICT_LANES="' not in phase


def test_unenabled_timed_run_never_invokes_the_cli(tmp_path: Path) -> None:
    """Without the gate's exports the layer is inert: the guard is a
    plain env test, so validate-quick and friends pay nothing."""
    body = _dry_run("lint-complexity", "LANE_EXECUTOR=direct", cwd=tmp_path)
    assert 'if [ -z "$LANE_VERDICT_SHA" ]' in body


def _fake_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    environment = {
        **_scrubbed_environment(),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=environment)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "x"],
        cwd=repo,
        check=True,
        env=environment,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return repo, sha


def test_fail_one_rerun_one_then_all_cached(tmp_path: Path) -> None:
    """The whole prize, end to end with real make and the real CLI:
    a gate re-run after one lane's transient failure re-runs ONLY that
    lane, and a third run re-runs nothing."""
    repo, sha = _fake_repo(tmp_path)
    marker = repo / "flaky-should-fail"
    marker.write_text("")
    extra = repo / "extra.mk"
    extra.write_text(
        "ok-lane:\n"
        "\t$(call TIMED_RUN,ok-lane,echo RAN-OK)\n"
        "flaky-lane:\n"
        "\t$(call TIMED_RUN,flaky-lane,"
        "test ! -f flaky-should-fail && echo RAN-FLAKY)\n"
    )
    python = str(REPO_ROOT / ".venv" / "bin" / "python")

    def gate_run() -> subprocess.CompletedProcess[str]:
        make = shutil.which("gmake") or "make"
        return subprocess.run(
            [
                make,
                "-f",
                str(REPO_ROOT / "Makefile"),
                "-f",
                "extra.mk",
                f"PYTHON={python}",
                "ok-lane",
                "flaky-lane",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            env={
                **_scrubbed_environment(),
                "LANE_VERDICT_SHA": sha,
                "LANE_VERDICT_LANES": "ok-lane flaky-lane",
                "PYTHONPATH": str(REPO_ROOT / "src"),
            },
        )

    first = gate_run()
    assert first.returncode != 0
    assert "RAN-OK" in first.stdout
    assert "recorded-green" in first.stdout and "ok-lane" in first.stdout
    assert "RAN-FLAKY" not in first.stdout

    marker.unlink()
    second = gate_run()
    assert second.returncode == 0, second.stdout + second.stderr
    assert "RAN-OK" not in second.stdout, "cached lane re-executed"
    assert "cached-green" in second.stdout
    assert "RAN-FLAKY" in second.stdout

    third = gate_run()
    assert third.returncode == 0
    assert "RAN-OK" not in third.stdout and "RAN-FLAKY" not in third.stdout
    assert third.stdout.count("cached-green") == 2
