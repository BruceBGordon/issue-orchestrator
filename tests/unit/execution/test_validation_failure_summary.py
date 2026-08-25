from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from issue_orchestrator.execution.validation_failure_summary import (
    load_validation_failure_summary,
    load_validation_failure_summary_with_config,
)


def test_load_validation_failure_summary_extracts_failed_tests_and_excerpts(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)

    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_name": "coding-1",
                "run_dir": str(run_dir),
                "worktree": str(worktree),
                "validation_status": "failed",
                "validation_reason": "Validation failed for deadbeef (exit_code=2)",
                "validation_record_path": ".issue-orchestrator/sessions/run-1/validation-record.json",
                "validation_stdout": ".issue-orchestrator/sessions/run-1/validation-stdout.log",
                "validation_stderr": ".issue-orchestrator/sessions/run-1/validation-stderr.log",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "validation-record.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite": "publish_gate",
                "head_sha": "deadbeef",
                "passed": False,
                "exit_code": 2,
                "command": "make validate",
                "started_at": "2026-03-22T04:53:14Z",
                "ended_at": "2026-03-22T04:53:58Z",
                "timed_out": False,
                "stdout_path": (
                    ".issue-orchestrator/sessions/run-1/validation-stdout.log"
                ),
                "stderr_path": (
                    ".issue-orchestrator/sessions/run-1/validation-stderr.log"
                ),
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "validation-stdout.log").write_text(
        "\n".join(
            [
                "[gw1] [ 99%] PASSED tests/unit/test_ok.py::test_ok",
                "=================================== FAILURES ===================================",
                "_______ TestProviderCircuitsEndpoint.test_get_provider_circuits_open _________",
                "tests/unit/test_web.py:6936: in test_get_provider_circuits_open",
                "    assert len(data[\"circuits\"]) == 1",
                "E   assert 0 == 1",
                "FAILED tests/unit/test_web.py::TestProviderCircuitsEndpoint::test_get_provider_circuits_open",
                "FAILED tests/unit/test_web.py::TestProviderCircuitsEndpoint::test_get_provider_circuits_expired",
                "============================= slowest 10 durations =============================",
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "validation-stderr.log").write_text(
        "make: *** [validate] Error 2\nerror: failed to push some refs",
        encoding="utf-8",
    )

    summary = load_validation_failure_summary(run_dir)

    assert summary is not None
    assert summary.status == "failed"
    assert summary.reason == "Validation failed for deadbeef (exit_code=2)"
    assert summary.suite == "publish_gate"
    assert summary.command == "make validate"
    assert summary.exit_code == 2
    assert summary.failed_tests == (
        "tests/unit/test_web.py::TestProviderCircuitsEndpoint::test_get_provider_circuits_open",
        "tests/unit/test_web.py::TestProviderCircuitsEndpoint::test_get_provider_circuits_expired",
    )
    assert any("FAILURES" in line for line in summary.stdout_excerpt)
    assert summary.stderr_excerpt[-1] == "error: failed to push some refs"


def test_load_validation_failure_summary_returns_none_for_passed_runs_by_default(tmp_path: Path) -> None:
    """Default (failure-only) gate preserves existing callers' contracts —
    things like the issue-detail run diagnostic should not flag passed runs.
    """
    worktree = tmp_path / "wt"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-2"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_name": "coding-2",
                "run_dir": str(run_dir),
                "worktree": str(worktree),
                "validation_status": "passed",
            }
        ),
        encoding="utf-8",
    )

    assert load_validation_failure_summary(run_dir) is None


def test_load_validation_failure_summary_returns_passed_run_when_opted_in(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-pass"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_name": "coding-pass",
                "run_dir": str(run_dir),
                "worktree": str(worktree),
                "validation_status": "passed",
                "validation_record_path": ".issue-orchestrator/sessions/run-pass/validation-record.json",
                "validation_stdout": ".issue-orchestrator/sessions/run-pass/validation-stdout.log",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "validation-record.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite": "publish_gate",
                "head_sha": "abc123",
                "passed": True,
                "exit_code": 0,
                "command": "make validate",
                "started_at": "2026-05-07T12:00:00Z",
                "ended_at": "2026-05-07T12:04:30Z",
                "timed_out": False,
                "stdout_path": (
                    ".issue-orchestrator/sessions/run-pass/validation-stdout.log"
                ),
                "stderr_path": (
                    ".issue-orchestrator/sessions/run-pass/validation-stderr.log"
                ),
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "validation-stdout.log").write_text(
        "============= 142 passed in 41.21s =============\n",
        encoding="utf-8",
    )

    summary = load_validation_failure_summary(run_dir, include_passed=True)

    assert summary is not None
    assert summary.status == "passed"
    assert summary.reason == "Validation passed"
    assert summary.exit_code == 0
    assert summary.failed_tests == ()


def _seed_failed_validation(tmp_path: Path) -> tuple[Path, Path]:
    worktree = tmp_path / "wt"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-junit"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_name": "coding-junit",
                "run_dir": str(run_dir),
                "worktree": str(worktree),
                "validation_status": "failed",
                "validation_reason": "tests failed",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "validation-record.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite": "publish_gate",
                "head_sha": "cafebabe",
                "passed": False,
                "exit_code": 1,
                "command": "make test",
                "started_at": "2026-04-28T10:00:00Z",
                "ended_at": "2026-04-28T10:01:30Z",
                "timed_out": False,
                "stdout_path": (
                    ".issue-orchestrator/sessions/run-junit/validation-stdout.log"
                ),
                "stderr_path": (
                    ".issue-orchestrator/sessions/run-junit/validation-stderr.log"
                ),
            }
        ),
        encoding="utf-8",
    )
    return worktree, run_dir


def _write_junit_xml(path: Path, *, case_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="pytest" tests="1" failures="0" errors="0" skipped="0">
  <testcase classname="tests.unit.test_recorded" name="{case_name}" time="0.01"/>
</testsuite>
""",
        encoding="utf-8",
    )


def test_load_validation_failure_summary_attaches_junit_cases_when_configured(
    tmp_path: Path,
) -> None:
    """JUnit XML emitted by validation is parsed into structured cases."""
    worktree, run_dir = _seed_failed_validation(tmp_path)
    junit_path = worktree / "test-results.xml"
    junit_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="pytest" tests="3" failures="1" errors="0" skipped="0">
  <testcase classname="tests.unit.test_a" name="test_passes" time="0.12"/>
  <testcase classname="tests.unit.test_b" name="test_fails" time="0.05">
    <failure message="AssertionError: expected 2 got 1">tests/unit/test_b.py:7
    assert 1 == 2</failure>
  </testcase>
  <testcase classname="tests.unit.test_c" name="test_skipped" time="0.0">
    <skipped message="not on this platform"/>
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )

    summary = load_validation_failure_summary(
        run_dir, junit_xml_paths=("test-results.xml",)
    )

    assert summary is not None
    assert len(summary.junit_cases) == 3
    by_outcome = {case.display_name: case.outcome for case in summary.junit_cases}
    assert by_outcome == {
        "test_passes": "passed",
        "test_fails": "failed",
        "test_skipped": "skipped",
    }
    failing = next(c for c in summary.junit_cases if c.outcome == "failed")
    assert failing.failure_details is not None
    assert "AssertionError" in failing.failure_details


def test_load_validation_failure_summary_returns_empty_junit_when_unconfigured(
    tmp_path: Path,
) -> None:
    """No junit_xml_paths configured → empty junit_cases (existing behavior preserved)."""
    _, run_dir = _seed_failed_validation(tmp_path)
    summary = load_validation_failure_summary(run_dir)
    assert summary is not None
    assert summary.junit_cases == ()


def test_load_validation_failure_summary_prefers_recorded_junit_paths(
    tmp_path: Path,
) -> None:
    """Recorded manifest evidence is authoritative over config discovery."""
    worktree, run_dir = _seed_failed_validation(tmp_path)
    recorded_path = worktree / "recorded" / "junit.xml"
    configured_path = worktree / "configured" / "junit.xml"
    _write_junit_xml(recorded_path, case_name="test_from_manifest")
    _write_junit_xml(configured_path, case_name="test_from_config")
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["artifacts"] = {
        "validation_junit_xml_recorded": {
            "kind": "junit_xml",
            "path": str(recorded_path),
            "content_type": "application/xml",
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    summary = load_validation_failure_summary(
        run_dir,
        junit_xml_paths=("configured/*.xml",),
    )

    assert summary is not None
    assert [case.display_name for case in summary.junit_cases] == [
        "test_from_manifest"
    ]


def test_load_validation_failure_summary_with_config_ignores_e2e_junit_paths(
    tmp_path: Path,
) -> None:
    worktree, run_dir = _seed_failed_validation(tmp_path)
    _write_junit_xml(worktree / "e2e-results" / "junit.xml", case_name="test_e2e")
    config = SimpleNamespace(
        validation=SimpleNamespace(junit_xml_paths=()),
        e2e=SimpleNamespace(junit_xml_paths=("e2e-results/*.xml",)),
    )

    summary = load_validation_failure_summary_with_config(run_dir, config=config)

    assert summary is not None
    # Assert the deprecated compatibility path no longer leaks E2E reports into
    # validation diagnostics.
    assert summary.junit_cases == ()


def test_load_validation_failure_summary_ignores_stale_configured_junit(
    tmp_path: Path,
) -> None:
    worktree, run_dir = _seed_failed_validation(tmp_path)
    junit_path = worktree / "reports" / "stale.xml"
    _write_junit_xml(junit_path, case_name="test_stale")
    stale_ns = 1_767_744_000_000_000_000  # 2026-01-07T00:00:00Z
    os.utime(junit_path, ns=(stale_ns, stale_ns))

    summary = load_validation_failure_summary(
        run_dir,
        junit_xml_paths=("reports/*.xml",),
    )

    assert summary is not None
    assert summary.junit_cases == ()


def test_load_validation_failure_summary_tolerates_missing_junit_file(
    tmp_path: Path,
) -> None:
    """JUnit path configured but file not produced → empty junit_cases, no error.

    Validation can fail before reaching the test step (e.g., typecheck exits
    early). The dashboard should still render the basic failure summary.
    """
    _, run_dir = _seed_failed_validation(tmp_path)
    summary = load_validation_failure_summary(
        run_dir, junit_xml_paths=("test-results.xml",)
    )
    assert summary is not None
    assert summary.junit_cases == ()


def test_load_validation_failure_summary_tolerates_missing_recorded_junit(
    tmp_path: Path,
) -> None:
    """Recorded manifest paths that no longer exist on disk must not 500.

    Repro: Gradle's ``test`` task wipes ``build/test-results/<task>/``
    before each run, so a session that crashes/times out mid-validation
    leaves the run manifest pointing at JUnit files the next run has
    since deleted. The read-only issue-detail UI route called
    ``parse_junit_report`` on those paths and surfaced ``ValueError:
    JUnit XML does not exist`` as a 500. The summary loader now skips
    missing files (and unparseable ones) so the dashboard renders the
    basic failure card with empty ``junit_cases`` instead.
    """
    worktree, run_dir = _seed_failed_validation(tmp_path)
    present_path = worktree / "recorded" / "junit-present.xml"
    missing_path = worktree / "recorded" / "junit-missing.xml"
    _write_junit_xml(present_path, case_name="test_kept")
    # missing_path is intentionally never created
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["artifacts"] = {
        "validation_junit_xml_present": {
            "kind": "junit_xml",
            "path": str(present_path),
            "content_type": "application/xml",
        },
        "validation_junit_xml_missing": {
            "kind": "junit_xml",
            "path": str(missing_path),
            "content_type": "application/xml",
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    summary = load_validation_failure_summary(run_dir)

    assert summary is not None
    assert [case.display_name for case in summary.junit_cases] == ["test_kept"]
