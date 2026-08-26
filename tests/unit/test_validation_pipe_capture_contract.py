"""Contract tests for the typed validation pipe-capture boundary."""

from __future__ import annotations

from typing import cast

import pytest

from issue_orchestrator.domain.validation_execution import ValidationCommandOutput
from issue_orchestrator.ports.validation_pipe_capture import (
    ValidationPipeCaptureResult,
)


def test_capture_result_accepts_absent_or_exception_failure() -> None:
    output = ValidationCommandOutput("stdout", "stderr")
    failure = RuntimeError("capture failure")

    assert ValidationPipeCaptureResult(output, None).failure is None
    assert ValidationPipeCaptureResult(output, failure).failure is failure


def test_capture_result_rejects_non_exception_failure() -> None:
    with pytest.raises(ValueError, match="must be None or BaseException"):
        ValidationPipeCaptureResult(
            ValidationCommandOutput("", ""),
            cast(BaseException, object()),
        )
