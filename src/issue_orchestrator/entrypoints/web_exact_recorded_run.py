"""HTTP adapter for exact recorded-run lookup failures."""

from __future__ import annotations

from typing import TypeAlias, assert_never

from fastapi.responses import JSONResponse

from ..execution.recorded_session_runs import (
    ExactRecordedRun,
    InvalidRecordedRunReference,
    RecordedRunIssueMismatch,
    RecordedRunNotFound,
    RecordedRunUntrusted,
    RecordedRunUnreadable,
    resolve_exact_recorded_run,
)

ExactRecordedRunResponse: TypeAlias = ExactRecordedRun | JSONResponse


def exact_recorded_run_response(
    raw_run_dir: str | None,
    *,
    issue_number: int,
) -> ExactRecordedRunResponse:
    """Resolve the exact requested run and translate closed failures to HTTP."""
    if not raw_run_dir:
        return JSONResponse(
            {
                "error": "run_dir is required",
                "hint": "Open terminal recordings from a run-scoped timeline action.",
            },
            status_code=400,
        )
    result = resolve_exact_recorded_run(raw_run_dir, issue_number=issue_number)
    match result:
        case ExactRecordedRun():
            return result
        case InvalidRecordedRunReference(detail=detail):
            return JSONResponse({"error": detail}, status_code=400)
        case RecordedRunNotFound(detail=detail):
            return JSONResponse({"error": detail}, status_code=404)
        case RecordedRunIssueMismatch(expected_issue_number=expected):
            return JSONResponse(
                {"error": f"Requested run does not belong to issue #{expected}"},
                status_code=404,
            )
        case RecordedRunUntrusted(detail=detail):
            return JSONResponse({"error": detail}, status_code=400)
        case RecordedRunUnreadable(detail=detail):
            return JSONResponse({"error": detail}, status_code=500)
        case _:
            assert_never(result)
