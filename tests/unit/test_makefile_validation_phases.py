"""Tests for Makefile validation phase orchestration."""

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _gnu_make() -> str:
    make_bin = shutil.which("gmake") or shutil.which("make")
    if make_bin is None:
        pytest.fail("GNU make is required to validate Makefile targets")
    result = subprocess.run(
        [make_bin, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or "GNU Make" not in result.stdout:
        pytest.fail("GNU make is required to validate Makefile targets")
    return make_bin


def _dry_run(target: str, **overrides: str) -> list[str]:
    env = dict(os.environ)
    env.pop("MAKEFLAGS", None)
    # These may be exported by an outer validation Make invocation. A dry run
    # must derive its own defaults unless the test explicitly overrides one.
    for variable in (
        "PARALLEL",
        "UNIT_PARALLEL",
        "SIMULATED_PARALLEL",
        "SIMULATED_AGENT_PARALLEL",
        "INTEGRATION_PARALLEL",
        "INTEGRATION_AGENT_PARALLEL",
        "PROVIDER_PARALLEL",
        "CLAUDE_PROVIDER_PARALLEL",
        "CODEX_PROVIDER_PARALLEL",
        "WEB_PARALLEL",
    ):
        env.pop(variable, None)
    env.update(
        {
            "VALIDATE_LANE_JOBS": "7",
            "VALIDATE_E2E_JOBS": "1",
            **overrides,
        }
    )
    result = subprocess.run(
        [_gnu_make(), "-n", "--always-make", target],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _matching_indexes(lines: list[str], *fragments: str) -> list[int]:
    return [
        index
        for index, line in enumerate(lines)
        if all(fragment in line for fragment in fragments)
    ]


def _find_line(lines: list[str], *fragments: str) -> int:
    matches = _matching_indexes(lines, *fragments)
    if not matches:
        raise AssertionError(
            f"Missing line containing {fragments!r}. Output:\n" + "\n".join(lines)
        )
    if len(matches) > 1:
        raise AssertionError(
            f"Expected one line containing {fragments!r}, got {len(matches)}"
        )
    return matches[0]


def _assert_job_count(line: str, jobs: int) -> None:
    assert re.search(rf"(?:^|\s)-j\s*{jobs}(?:\s|$)", line), line


def _assert_no_job_count(line: str) -> None:
    assert not re.search(r"(?:^|\s)-j\s*\d+(?:\s|$)", line), line


def _makefile_variable_words(name: str) -> list[str]:
    makefile = REPO_ROOT / "Makefile"
    text = makefile.read_text(encoding="utf-8")
    variables = {
        match.group("name"): match.group("value")
        for match in re.finditer(
            r"^(?P<name>[A-Z][A-Z0-9_]*)\s*:?=\s*(?P<value>.+)$",
            text,
            re.MULTILINE,
        )
    }
    assert name in variables, f"Makefile variable {name} not found"
    value = variables[name]
    reference = re.compile(r"\$\((?P<name>[A-Z][A-Z0-9_]*)\)")
    while match := reference.search(value):
        referenced_name = match.group("name")
        assert referenced_name in variables, (
            f"Makefile variable {name} references unknown {referenced_name}"
        )
        value = value[: match.start()] + variables[referenced_name] + value[match.end() :]
    return value.split()


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is None:
            return None
        return f"{parent}.{node.attr}"
    return None


def _has_live_codex_marker(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        _dotted_name(node) == "pytest.mark.live_codex"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    )


def _files_with_pytest_marker(marker_name: str) -> set[str]:
    marker = f"pytest.mark.{marker_name}"
    files: set[str] = set()
    for root in (REPO_ROOT / "tests/integration", REPO_ROOT / "tests/simulated_scenarios"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if any(
                _dotted_name(node) == marker
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
            ):
                files.add(path.relative_to(REPO_ROOT).as_posix())
    return files


def test_validate_impl_submits_static_and_six_core_lanes_concurrently():
    lines = _dry_run("_validate-impl")
    static_lines = _dry_run("_validate-static-lane")
    granted_static_lines = _dry_run("_validate-static-granted")

    static_index = _find_line(
        static_lines,
        "executor-run",
        "--work-key io:static-v2",
        "--min-concurrency 1",
        "--max-concurrency 3",
        "_validate-static-granted",
    )
    granted_static_index = _find_line(
        granted_static_lines,
        "validate-static-phase",
        "ISSUE_ORCHESTRATOR_EXECUTOR_CONCURRENCY",
        "_validate-static-impl",
    )
    core_lanes_index = _find_line(
        lines,
        "validate-core-lanes-phase",
        "_validate-static-lane",
        "test-unit",
        "test-simulated-core",
        "test-integration-core-local",
        "test-provider-core-claude",
        "test-provider-core-codex",
        "test-web",
    )

    assert "--work-key io:static-v2" in static_lines[static_index]
    assert "-j${ISSUE_ORCHESTRATOR_EXECUTOR_CONCURRENCY:" in (
        granted_static_lines[granted_static_index]
    )
    _assert_job_count(lines[core_lanes_index], 7)


def test_default_pr_lane_ranges_are_reported_for_post_hoc_diagnosis():
    lines = _dry_run("validate-pr-raw")
    config = lines[
        _find_line(
            lines,
            "lane_jobs=7",
            "static_range=1-3",
            "unit_range=8-24",
            "simulated_range=4-8",
            "integration_range=2-4",
            "claude_range=1-2",
            "codex_range=2-3",
            "web_range=4-12",
        )
    ]

    assert "lane_jobs=7" in config


def test_validate_pr_impl_submits_static_and_provider_split_lanes_concurrently():
    lines = _dry_run("_validate-pr-impl")

    pr_index = _find_line(
        lines,
        "validate-pr-lanes-phase",
        "_validate-static-lane",
        "test-provider-pr-claude",
        "test-provider-pr-codex",
        "test-web",
    )

    _assert_job_count(lines[pr_index], 7)


def test_numeric_xdist_overrides_collapse_the_grant_range_but_use_xdist_auto():
    unit_lines = _dry_run("test-unit", UNIT_PARALLEL="3")
    simulated_lines = _dry_run("test-simulated-core", SIMULATED_PARALLEL="1")
    integration_lines = _dry_run(
        "test-integration-core-local",
        INTEGRATION_PARALLEL="3",
    )

    unit = unit_lines[
        _find_line(
            unit_lines,
            "--work-key io:unit",
            "--min-concurrency 3",
            "--max-concurrency 3",
            " -n auto ",
        )
    ]
    simulated = simulated_lines[
        _find_line(
            simulated_lines,
            "--work-key io:simulated-core",
            "--min-concurrency 1",
            "--max-concurrency 1",
            " -n auto ",
        )
    ]
    integration = integration_lines[
        _find_line(
            integration_lines,
            "--work-key io:integration-core",
            "--min-concurrency 3",
            "--max-concurrency 3",
            " -n auto ",
        )
    ]

    assert "--dist=loadgroup" in unit
    assert "--dist=loadgroup" in simulated
    assert "--dist=loadgroup" in integration


def test_validation_executor_command_is_replaceable_with_direct_adapter():
    lines = _dry_run(
        "test-unit",
        EXECUTOR_RUN="./scripts/executor-run-direct",
        UNIT_PARALLEL="3",
    )

    pytest_line = lines[
        _find_line(
            lines,
            "./scripts/executor-run-direct --work-key io:unit",
            "--min-concurrency 3",
            "--max-concurrency 3",
            " -n auto ",
        )
    ]

    assert "issue_orchestrator.entrypoints.cli executor-run" not in pytest_line


@pytest.mark.parametrize(
    ("target", "provider", "maximum_concurrency", "required_paths"),
    [
        (
            "test-provider-pr-claude",
            "claude",
            2,
            [
                "test_ai_gate_hooks.py",
                "test_sandbox_os_boundary.py",
                "test_foreign_repo_lifecycle.py",
                "test_claude_execution.py",
                "test_live_agent_chain.py",
            ],
        ),
        (
            "test-provider-pr-codex",
            "codex",
            3,
            [
                "test_sandbox_os_boundary.py",
                "test_persistent_review_exchange_integration.py",
                "test_foreign_repo_lifecycle.py",
                "test_codex_execution.py",
            ],
        ),
    ],
)
def test_pr_provider_lanes_are_bounded_and_exclusive(
    target: str,
    provider: str,
    maximum_concurrency: int,
    required_paths: list[str],
):
    lines = _dry_run(target)
    pytest_line = lines[
        _find_line(
            lines,
            f"--work-key io:provider-{provider}",
            f"--min-concurrency {1 if provider == 'claude' else 2}",
            f"--max-concurrency {maximum_concurrency}",
            f"--exclusive {provider}",
            " -n auto ",
            "--dist=loadgroup",
            f'-m "provider_{provider} and not requires_infra"',
        )
    ]

    for path in required_paths:
        assert path in pytest_line


def test_provider_lane_can_be_forced_serial_for_diagnosis():
    lines = _dry_run("test-provider-pr-claude", PROVIDER_PARALLEL="0")
    pytest_line = lines[
        _find_line(
            lines,
            "--work-key io:provider-claude",
            "--min-concurrency 1",
            "--max-concurrency 1",
            "--exclusive claude",
            'provider_claude and not requires_infra',
        )
    ]

    assert " -n " not in f" {pytest_line} "


def test_provider_specific_override_does_not_change_other_provider():
    claude_lines = _dry_run(
        "test-provider-pr-claude",
        CLAUDE_PROVIDER_PARALLEL="1",
    )
    codex_lines = _dry_run(
        "test-provider-pr-codex",
        CLAUDE_PROVIDER_PARALLEL="1",
    )

    _find_line(
        claude_lines,
        "--work-key io:provider-claude",
        "--min-concurrency 1",
        "--max-concurrency 1",
        "--exclusive claude",
    )
    _find_line(
        codex_lines,
        "--work-key io:provider-codex",
        "--min-concurrency 2",
        "--max-concurrency 3",
        "--exclusive codex",
        " -n auto ",
    )


@pytest.mark.parametrize(
    "target",
    (
        "test-unit",
        "test-simulated-core",
        "test-simulated-agent",
        "test-integration-core-local",
        "test-provider-pr-claude",
        "test-provider-pr-codex",
        "test-web",
    ),
)
def test_global_parallel_zero_disables_xdist_in_every_validation_lane(
    target: str,
) -> None:
    lines = _dry_run(target, PARALLEL="0")
    executor_line = lines[
        _find_line(lines, "executor-run", "--min-concurrency 1", "--max-concurrency 1")
    ]

    assert " -n " not in f" {executor_line} "


def test_provider_range_does_not_expose_machine_capacity_to_client_command():
    lines = _dry_run("test-provider-pr-claude")
    pytest_line = lines[
        _find_line(
            lines,
            "--work-key io:provider-claude",
            "--min-concurrency 1",
            "--max-concurrency 2",
            "--exclusive claude",
            'provider_claude and not requires_infra',
        )
    ]

    assert " -n auto " in f" {pytest_line} "
    assert "ISSUE_ORCHESTRATOR_EXECUTOR_CAPACITY" not in pytest_line


def test_browser_lane_declares_measured_range_and_exclusive_browser_resource():
    lines = _dry_run("test-web")

    _find_line(
        lines,
        "--work-key io:web",
        "--min-concurrency 4",
        "--max-concurrency 12",
        "--exclusive browser",
        "tests/e2e_web",
        " -n auto ",
        "--dist=loadgroup",
    )


@pytest.mark.parametrize(
    ("provider", "pr_variable", "core_variable", "agent_variable"),
    [
        (
            "claude",
            "PROVIDER_PR_CLAUDE_PATHS",
            "PROVIDER_CORE_CLAUDE_PATHS",
            "INTEGRATION_CLAUDE_AGENT_FILES",
        ),
        (
            "codex",
            "PROVIDER_PR_CODEX_PATHS",
            "PROVIDER_CORE_CODEX_PATHS",
            "INTEGRATION_CODEX_AGENT_FILES",
        ),
    ],
)
def test_every_provider_marked_file_is_owned_by_the_declared_lanes(
    provider: str,
    pr_variable: str,
    core_variable: str,
    agent_variable: str,
):
    marked = _files_with_pytest_marker(f"provider_{provider}")
    pr_paths = set(_makefile_variable_words(pr_variable))
    core_paths = set(_makefile_variable_words(core_variable))
    pr_only_paths = {
        *_makefile_variable_words(agent_variable),
        *_makefile_variable_words("SIMULATED_PROVIDER_PATH"),
    }

    assert pr_paths == marked
    assert core_paths == marked - pr_only_paths


def test_codex_lane_does_not_collapse_every_provider_test_to_one_worker():
    interactive = (
        REPO_ROOT / "tests/integration/test_persistent_review_exchange_integration.py"
    ).read_text(encoding="utf-8")
    execution = (REPO_ROOT / "tests/integration/test_codex_execution.py").read_text(
        encoding="utf-8"
    )
    foreign = (
        REPO_ROOT / "tests/simulated_scenarios/test_foreign_repo_lifecycle.py"
    ).read_text(encoding="utf-8")

    assert 'xdist_group("codex-interactive")' in interactive
    assert 'xdist_group("codex-exec")' in execution
    assert 'xdist_group("codex-exec")' in foreign
    assert 'xdist_group("codex")' not in "\n".join(
        (interactive, execution, foreign)
    )


def test_validate_full_impl_runs_e2e_after_pr_phase():
    lines = _dry_run("_validate-full-impl")

    pr_index = _find_line(lines, "_validate-pr-impl")
    e2e_index = _find_line(lines, "test-e2e")

    _assert_job_count(lines[e2e_index], 1)

    assert pr_index < e2e_index


def test_validate_pr_raw_does_not_schedule_entire_graph_at_validate_jobs():
    lines = _dry_run("validate-pr-raw")
    raw_pr_index = _find_line(lines, "_validate-pr-impl")

    _assert_no_job_count(lines[raw_pr_index])


def test_validate_pr_raw_does_not_reenter_cache_aware_verify_script():
    # validation.publish.cmd points at `make validate-pr-raw`, which is what the
    # cache-aware wrapper (scripts/verify-pr.sh) ultimately runs. If the raw
    # target invoked verify-pr.sh again the pre-push gate would recurse.
    lines = _dry_run("validate-pr-raw")

    assert all("verify-pr.sh" not in line for line in lines)


def test_validate_pr_uses_cache_aware_verify_script():
    lines = _dry_run("validate-pr")

    verify_index = _find_line(lines, "./scripts/verify-pr.sh")

    assert all("validate_runner" not in line for line in lines[: verify_index + 1])


def test_agent_validation_targets_emit_timing_markers():
    simulated_lines = _dry_run("test-simulated-agent", SIMULATED_PARALLEL="0")
    integration_lines = _dry_run("test-integration-agent", INTEGRATION_AGENT_PARALLEL="0")

    _find_line(simulated_lines, "[validate-timing] START target=$target")
    _find_line(simulated_lines, "[validate-timing] END target=$target")
    _find_line(simulated_lines, 'target="test-simulated-agent"')

    starts = _matching_indexes(integration_lines, "[validate-timing] START target=$target")
    ends = _matching_indexes(integration_lines, "[validate-timing] END target=$target")
    assert len(starts) == 1
    assert len(ends) == 1

    agent_index = _find_line(integration_lines, 'target="test-integration-agent"')
    assert starts == [agent_index]
    assert all(
        'target="test-integration-agent-live-codex"' not in line
        for line in integration_lines
    )
    assert all("live_codex" not in line for line in integration_lines)


def test_core_validation_runs_live_codex_marker_serially():
    lines = _dry_run("test-integration-core", INTEGRATION_PARALLEL="0")

    starts = _matching_indexes(lines, "[validate-timing] START target=$target")
    ends = _matching_indexes(lines, "[validate-timing] END target=$target")
    assert len(starts) == 2
    assert len(ends) == 2

    core_index = _find_line(lines, 'target="test-integration-core"')
    live_codex_index = _find_line(lines, 'target="test-integration-core-live-codex"')
    non_live_marker_index = _find_line(
        lines,
        '-m "not requires_infra and not live_codex and not provider_claude and not provider_codex"',
    )
    live_marker_index = _find_line(lines, '-m "live_codex and not requires_infra"')

    assert core_index < live_codex_index
    assert non_live_marker_index == core_index
    assert live_marker_index == live_codex_index
    assert all(
        "::test_real_interactive_codex_reviewer_round_trips_through_exchange" not in line
        for line in lines
    )


def test_agent_backed_integration_runs_serial_by_default():
    lines = _dry_run("test-integration-agent")
    pytest_line = lines[
        _find_line(
            lines,
            "tests/integration/test_claude_execution.py",
            "tests/integration/test_codex_execution.py",
            "tests/integration/test_live_agent_chain.py",
        )
    ]

    assert " -n " not in f" {pytest_line} "
    assert ' -m "' not in pytest_line
    assert all("test-integration-agent-live-codex" not in line for line in lines)


def test_agent_backed_integration_allows_explicit_parallel_override():
    lines = _dry_run("test-integration-agent", INTEGRATION_AGENT_PARALLEL="2")
    pytest_line = lines[
        _find_line(
            lines,
            "tests/integration/test_claude_execution.py",
            "tests/integration/test_codex_execution.py",
            "tests/integration/test_live_agent_chain.py",
        )
    ]

    assert " -n auto " in f" {pytest_line} "
    assert ' -m "' not in pytest_line
    assert all("test-integration-agent-live-codex" not in line for line in lines)


def test_agent_backed_integration_files_do_not_reintroduce_live_codex_marker():
    agent_files = _makefile_variable_words("INTEGRATION_AGENT_FILES")

    offenders = [
        path
        for path in agent_files
        if _has_live_codex_marker(REPO_ROOT / path)
    ]

    assert offenders == [], (
        "live_codex tests in INTEGRATION_AGENT_FILES would run in the main "
        f"agent phase instead of a serial live-provider lane: {offenders}"
    )


def test_live_agent_transport_is_scheduled_by_e2e_not_agent_integration():
    integration_lines = _dry_run("test-integration-agent")
    e2e_lines = _dry_run("test-e2e")

    assert all(
        "tests/e2e/test_live_agent_transport.py" not in line
        for line in integration_lines
    )
    # The e2e lane must actually collect the transport test: pin that the
    # pytest invocation targets the whole tests/e2e dir with no --ignore and
    # no -m deselection.
    e2e_pytest_line = e2e_lines[_find_line(e2e_lines, "tests/e2e")]
    assert "--ignore" not in e2e_pytest_line
    assert " -m " not in f" {e2e_pytest_line} "
