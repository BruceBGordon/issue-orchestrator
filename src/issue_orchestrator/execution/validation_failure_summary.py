"""Helpers for summarizing failed validation runs for operator-facing UI."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain.artifact_contracts import ValidationFailed, ValidationPassed
from ..domain.run_manifest import RunManifest
from ..infra.e2e_reports import (
    JUnitCaseResult,
    discover_report_artifacts,
    parse_junit_report,
)
from ..infra.validation_junit_paths import (
    configured_validation_junit_xml_paths,
    validation_record_junit_modified_after,
)
from ..ports.session_output import ValidationRecord

logger = logging.getLogger(__name__)

_FAILED_TEST_RE = re.compile(r"^FAILED\s+(\S+)")
_MAX_FAILED_TESTS = 10
_MAX_STDOUT_EXCERPT_LINES = 40
_MAX_STDERR_EXCERPT_LINES = 20
_FAILURES_MARKER = "=================================== FAILURES ==================================="
_FAILURES_END_MARKERS = (
    "============================= slowest",
    "=========================== short test summary info ============================",
)


@dataclass(frozen=True)
class ValidationFailureSummary:
    # ``status`` is "passed" or "failed". The dataclass name predates passed-run
    # support; the structure now represents either outcome so the same dialog
    # can render both. ``failed_tests`` / ``stdout_excerpt`` may legitimately
    # be empty for passed runs.
    status: str
    reason: str
    suite: str
    command: str
    exit_code: int
    started_at: str
    ended_at: str
    failed_tests: tuple[str, ...]
    stdout_excerpt: tuple[str, ...]
    stderr_excerpt: tuple[str, ...]
    validation_record_path: str | None
    validation_stdout_path: str | None
    validation_stderr_path: str | None
    junit_cases: tuple[JUnitCaseResult, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "suite": self.suite,
            "command": self.command,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "failed_tests": list(self.failed_tests),
            "stdout_excerpt": list(self.stdout_excerpt),
            "stderr_excerpt": list(self.stderr_excerpt),
            "validation_record_path": self.validation_record_path,
            "validation_stdout_path": self.validation_stdout_path,
            "validation_stderr_path": self.validation_stderr_path,
            "junit_cases": [_junit_case_to_dict(case) for case in self.junit_cases],
        }


def _junit_case_to_dict(case: JUnitCaseResult) -> dict[str, Any]:
    """Serialize a `JUnitCaseResult` to the dict shape consumed by the
    `ValidationFailureDialogPayload.junit_cases` field. Mirrors the
    `JUnitCasePayload` schema in `docs/api/ui-openapi.json` exactly.
    """
    return {
        "case_id": case.case_id,
        "display_name": case.display_name,
        "suite_name": case.suite_name,
        "outcome": case.outcome,
        "duration_seconds": case.duration_seconds,
        "failure_details": case.failure_details,
        "system_out": case.system_out,
        "system_err": case.system_err,
    }


def load_validation_failure_summary(
    run_dir: Path,
    *,
    junit_xml_paths: tuple[str, ...] | list[str] = (),
    junit_search_root: Path | None = None,
    include_passed: bool = False,
) -> ValidationFailureSummary | None:
    """Return a concise summary of a run's validation outcome.

    By default returns a summary only when validation failed — preserves the
    existing contract for callers like ``_current_run_validation_diagnostic``
    that only flag failures on the issue list. The dialog endpoint passes
    ``include_passed=True`` to also surface passed runs (so users can spot-
    check JUnit / stdout for green runs), in which case the returned summary
    has ``status="passed"`` and likely empty ``failed_tests`` / ``stdout``.

    Recorded JUnit XML artifacts in the run manifest are authoritative. If a
    run predates recorded evidence, ``junit_xml_paths`` is used as a legacy
    discovery source rooted at ``junit_search_root`` (defaults to the manifest's
    worktree, or run_dir as a last resort).
    """
    try:
        manifest = RunManifest.load(run_dir)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None

    # Read the typed outcome via the manifest property — that protects
    # us from inconsistent legacy triples (e.g. status="passed" with a
    # stale failure reason) by trusting the typed status. ``ValidationRetry``
    # is intentionally NOT a summary surface: this dialog renders
    # terminal outcomes only. A retry-state run gets None so the
    # caller can decide what to do (e.g. wait for the retry to land).
    outcome = manifest.validation_outcome
    if isinstance(outcome, ValidationPassed):
        if not include_passed:
            return None
        status = "passed"
    elif isinstance(outcome, ValidationFailed):
        status = "failed"
    else:
        return None

    worktree = manifest.worktree if manifest.worktree else None
    record_path = _resolve_run_artifact(
        run_dir,
        manifest.validation_record_path,
        fallback_name="validation-record.json",
        worktree=worktree,
    )
    stdout_path = _resolve_run_artifact(
        run_dir,
        manifest.validation_stdout,
        fallback_name="validation-stdout.log",
        worktree=worktree,
    )
    stderr_path = _resolve_run_artifact(
        run_dir,
        manifest.validation_stderr,
        fallback_name="validation-stderr.log",
        worktree=worktree,
    )

    record = _load_validation_record(record_path) if record_path else None
    stdout_lines = _read_lines(stdout_path)
    stderr_lines = _read_lines(stderr_path)
    failed_tests = _extract_failed_tests(stdout_lines)

    recorded_junit_paths = manifest.junit_xml_paths(
        key_prefix="validation_junit_xml_"
    )
    if recorded_junit_paths:
        junit_cases = _load_recorded_junit_cases(recorded_junit_paths)
    else:
        junit_cases = _load_junit_cases(
            junit_xml_paths,
            junit_search_root or (Path(worktree) if worktree else run_dir),
            modified_after=validation_record_junit_modified_after(record),
        )

    # ValidationFailed always carries a non-empty reason by construction;
    # ValidationPassed has no reason. Project the union to the legacy
    # string field this dataclass exposes — preserving the rendering
    # contract while reading from the typed source of truth.
    if isinstance(outcome, ValidationFailed):
        reason = outcome.reason
    else:
        reason = "Validation passed"
    return ValidationFailureSummary(
        status=status,
        reason=reason,
        suite=record.suite if record else "",
        command=record.command if record else "",
        exit_code=record.exit_code if record else 0,
        started_at=record.started_at if record else "",
        ended_at=record.ended_at if record else "",
        failed_tests=failed_tests,
        stdout_excerpt=_extract_stdout_excerpt(stdout_lines),
        stderr_excerpt=_extract_stderr_excerpt(stderr_lines),
        validation_record_path=str(record_path) if record_path else None,
        validation_stdout_path=str(stdout_path) if stdout_path else None,
        validation_stderr_path=str(stderr_path) if stderr_path else None,
        junit_cases=junit_cases,
    )


def load_validation_failure_summary_with_config(
    run_dir: Path,
    *,
    config: Any,
    include_passed: bool = False,
) -> ValidationFailureSummary | None:
    """Config-aware wrapper that threads configured JUnit paths.

    Both the dashboard's ``/api/dialog/validation-failure/`` route and
    the issue-detail diagnostic path call this helper so they cannot
    disagree on whether structured JUnit cases reach the user. If
    ``config`` is None or has no ``validation`` block, JUnit parsing is
    skipped unless the run manifest already recorded validation JUnit
    evidence. Pass ``include_passed=True`` to also return passed runs.
    """
    return load_validation_failure_summary(
        run_dir,
        junit_xml_paths=configured_validation_junit_xml_paths(config),
        include_passed=include_passed,
    )


def _load_recorded_junit_cases(
    junit_xml_paths: tuple[str, ...],
) -> tuple[JUnitCaseResult, ...]:
    """Parse recorded JUnit XMLs, tolerating files that no longer exist.

    Manifests record paths captured at validation time. Gradle's ``test``
    task wipes ``build/test-results/<task>/`` before each run and rewrites
    it, so a run that crashed, timed out, or was killed mid-suite leaves
    the manifest pointing at paths the next run has since deleted. The
    issue-detail route is read-only and must not 500 on this partial
    state; missing files become "no case data" instead.
    """
    cases: list[JUnitCaseResult] = []
    for path in junit_xml_paths:
        candidate = Path(path)
        if not candidate.exists():
            logger.debug(
                "Recorded JUnit XML no longer on disk; skipping: %s", candidate
            )
            continue
        try:
            cases.extend(parse_junit_report(candidate))
        except ValueError as exc:
            # Malformed/empty XML (interrupted writer) is the same failure
            # class as missing — surface to logs, don't 500 the UI.
            logger.debug("Could not parse recorded JUnit XML %s: %s", candidate, exc)
    return tuple(cases)


def _load_junit_cases(
    junit_xml_paths: tuple[str, ...] | list[str],
    search_root: Path,
    *,
    modified_after: float | None = None,
) -> tuple[JUnitCaseResult, ...]:
    paths = tuple(p for p in junit_xml_paths if p)
    if not paths or not search_root.exists():
        return ()
    try:
        cases, _ = discover_report_artifacts(
            search_root,
            junit_xml_paths=paths,
            artifact_paths=(),
            modified_after=modified_after,
        )
    except ValueError as exc:
        # Validation may legitimately fail before producing JUnit XML
        # (e.g., a typecheck step exits before the test step writes its
        # report). Treat that as "no structured results", not a fatal error.
        logger.debug(
            "JUnit XML not available for validation summary at %s: %s",
            search_root, exc,
        )
        return ()
    return tuple(cases)


def _resolve_run_artifact(
    run_dir: Path,
    manifest_path: str | None,
    *,
    fallback_name: str,
    worktree: str | None,
) -> Path | None:
    local_path = run_dir / fallback_name
    if local_path.exists():
        return local_path
    if not manifest_path:
        return None
    candidate = Path(manifest_path)
    if candidate.is_absolute():
        return candidate if candidate.exists() else None
    if worktree:
        worktree_candidate = Path(worktree) / candidate
        if worktree_candidate.exists():
            return worktree_candidate
    run_candidate = run_dir / candidate
    return run_candidate if run_candidate.exists() else None


def _load_validation_record(path: Path) -> ValidationRecord | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return ValidationRecord.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


def _read_lines(path: Path | None) -> list[str]:
    if path is None:
        return []
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def _extract_failed_tests(lines: list[str]) -> tuple[str, ...]:
    failed_tests: list[str] = []
    seen: set[str] = set()
    for line in lines:
        match = _FAILED_TEST_RE.match(line.strip())
        if not match:
            continue
        nodeid = match.group(1)
        if nodeid in seen:
            continue
        seen.add(nodeid)
        failed_tests.append(nodeid)
        if len(failed_tests) >= _MAX_FAILED_TESTS:
            break
    return tuple(failed_tests)


def _extract_stdout_excerpt(lines: list[str]) -> tuple[str, ...]:
    if not lines:
        return ()
    start_index = next((idx for idx, line in enumerate(lines) if _FAILURES_MARKER in line), -1)
    if start_index >= 0:
        excerpt: list[str] = []
        for line in lines[start_index:]:
            if excerpt and any(marker in line for marker in _FAILURES_END_MARKERS):
                break
            excerpt.append(line)
            if len(excerpt) >= _MAX_STDOUT_EXCERPT_LINES:
                break
        return tuple(excerpt)
    tail = [line for line in lines if line.strip()][-_MAX_STDOUT_EXCERPT_LINES:]
    return tuple(tail)


def _extract_stderr_excerpt(lines: list[str]) -> tuple[str, ...]:
    if not lines:
        return ()
    tail = [line for line in lines if line.strip()][-_MAX_STDERR_EXCERPT_LINES:]
    return tuple(tail)
