"""Tests for the repo-specific pre-push runtime budget guard."""

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "repo-specific" / "scripts" / "check_prepush_runtime_budget.py"
PUBLISH_COMMAND = "make validate-pr-raw"


def write_config(
    path: Path,
    *,
    baseline_seconds: int = 540,
    max_increase_seconds: int = 240,
    max_seconds: int = 900,
    metric: str = "validation_elapsed_seconds",
) -> None:
    path.write_text(
        f"""
pre_push_runtime:
  enabled: true
  command: "{PUBLISH_COMMAND}"
  metric: "{metric}"
  baseline_seconds: {baseline_seconds}
  max_increase_seconds: {max_increase_seconds}
  max_seconds: {max_seconds}
""",
        encoding="utf-8",
    )


def write_timings(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def successful_prepush_record(
    *,
    head_sha: str = "abc123",
    elapsed_seconds: float,
    cache_hit: bool = False,
) -> dict[str, object]:
    return {
        "kind": "prepush_gate_summary",
        "phase": "validation_gate",
        "final_exit_code": 0,
        "head_sha": head_sha,
        "command": PUBLISH_COMMAND,
        "validation_cache_hit": cache_hit,
        "validation_elapsed_seconds": elapsed_seconds,
    }


def run_guard(
    config: Path,
    timings: Path,
    head_sha: str = "abc123",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--config",
            str(config),
            "--timings-file",
            str(timings),
            "--head-sha",
            head_sha,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_passes_when_latest_uncached_success_is_within_budgets(tmp_path: Path) -> None:
    config = tmp_path / "budget.yaml"
    timings = tmp_path / "timings.jsonl"
    write_config(config)
    write_timings(timings, [successful_prepush_record(elapsed_seconds=640)])

    result = run_guard(config, timings)

    assert result.returncode == 0
    assert "Pre-push runtime budget passed" in result.stdout


def test_fails_when_runtime_exceeds_absolute_threshold(tmp_path: Path) -> None:
    config = tmp_path / "budget.yaml"
    timings = tmp_path / "timings.jsonl"
    write_config(config, baseline_seconds=540, max_increase_seconds=400, max_seconds=720)
    write_timings(timings, [successful_prepush_record(elapsed_seconds=721)])

    result = run_guard(config, timings)

    assert result.returncode == 1
    assert "exceeds max_seconds 720.0s" in result.stderr
    assert "amend or commit to create a new HEAD" in result.stderr
    assert "repo-specific/config/pre-push-runtime-budget.yaml" in result.stderr


def test_fails_when_runtime_exceeds_configured_increase(tmp_path: Path) -> None:
    config = tmp_path / "budget.yaml"
    timings = tmp_path / "timings.jsonl"
    write_config(config, baseline_seconds=540, max_increase_seconds=120, max_seconds=900)
    write_timings(timings, [successful_prepush_record(elapsed_seconds=661)])

    result = run_guard(config, timings)

    assert result.returncode == 1
    assert "baseline_seconds + max_increase_seconds" in result.stderr
    assert "540.0s + 120.0s = 660.0s" in result.stderr


def test_skips_cached_success_without_uncached_measurement(tmp_path: Path) -> None:
    config = tmp_path / "budget.yaml"
    timings = tmp_path / "timings.jsonl"
    write_config(config)
    write_timings(timings, [successful_prepush_record(elapsed_seconds=0.1, cache_hit=True)])

    result = run_guard(config, timings)

    assert result.returncode == 0
    assert "was cached" in result.stdout


def test_fails_when_no_successful_measurement_exists(tmp_path: Path) -> None:
    config = tmp_path / "budget.yaml"
    timings = tmp_path / "timings.jsonl"
    write_config(config)
    write_timings(
        timings,
        [
            {
                **successful_prepush_record(elapsed_seconds=10),
                "final_exit_code": 1,
            }
        ],
    )

    result = run_guard(config, timings)

    assert result.returncode == 1
    assert "no successful uncached pre-push timing record" in result.stderr
    assert f"matching command '{PUBLISH_COMMAND}'" in result.stderr


def test_rejects_bool_metric_value(tmp_path: Path) -> None:
    config = tmp_path / "budget.yaml"
    timings = tmp_path / "timings.jsonl"
    write_config(config)
    record = successful_prepush_record(elapsed_seconds=10)
    record["validation_elapsed_seconds"] = True
    write_timings(timings, [record])

    result = run_guard(config, timings)

    assert result.returncode == 1
    assert "missing numeric 'validation_elapsed_seconds'" in result.stderr


def test_the_budget_command_matches_the_configured_publish_command() -> None:
    """Two files that must agree, with nothing making them.

    The guard selects timing records by exact command string. When they drift,
    the failure is not "the build got slower" — it is "no timing record
    exists", and the push is refused with a message about seeding a cache that
    is already seeded. That happened: moving the publish gate to the direct
    lane backend (#7163) left this file naming the condor command, and a green,
    cached validation could not be pushed.

    Nothing tested this pairing, which is why the drift was invisible until a
    push hit it. Cheap to assert, so asserted.
    """
    from pathlib import Path

    import yaml

    from issue_orchestrator.infra.config import Config

    repo_root = Path(__file__).resolve().parents[2]
    budget = yaml.safe_load(
        (repo_root / "repo-specific" / "config" / "pre-push-runtime-budget.yaml")
        .read_text(encoding="utf-8")
    )
    config = Config.load(
        repo_root / ".issue-orchestrator" / "config" / "modes" / "default" / "main.yaml"
    )

    assert budget["pre_push_runtime"]["command"] == config.validation.publish.cmd, (
        "pre-push-runtime-budget.yaml selects timing records by this exact "
        "string; drift from validation.publish.cmd refuses every push with a "
        "misleading 'run make validate-pr to seed a timing record'"
    )
