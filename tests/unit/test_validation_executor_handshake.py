"""Boundary tests for the inherited nested-executor handshake."""

from __future__ import annotations

import os

import pytest

from issue_orchestrator.infra.validation_executor_handshake import (
    VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT,
    validate_executor_handshake_payload,
)


def test_handshake_uses_the_public_environment_prefix_exactly_once() -> None:
    assert (
        VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT.descriptor_variable
        == "ISSUE_ORCHESTRATOR_VALIDATION_EXECUTOR_HANDSHAKE_FD"
    )


def test_absent_handshake_is_an_explicit_non_validation_invocation() -> None:
    environment = {"BASE": "preserved"}

    VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT.acknowledge_if_requested(environment)

    assert environment == {"BASE": "preserved"}


def test_requested_handshake_is_written_and_consumed() -> None:
    read_descriptor, write_descriptor = os.pipe()
    environment = {
        VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT.descriptor_variable: str(
            write_descriptor
        )
    }
    try:
        VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT.acknowledge_if_requested(environment)

        acknowledgements = validate_executor_handshake_payload(
            os.read(read_descriptor, 64)
        )
        assert len(acknowledgements) == 1
        assert acknowledgements[0].acknowledged_at_monotonic > 0
        assert environment == {}
        with pytest.raises(OSError):
            os.write(write_descriptor, b"\x01")
    finally:
        os.close(read_descriptor)


@pytest.mark.parametrize("raw_descriptor", ["", "2", "not-a-descriptor", "3.5"])
def test_malformed_requested_handshake_fails_fast(raw_descriptor: str) -> None:
    environment = {
        VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT.descriptor_variable: raw_descriptor
    }

    with pytest.raises(ValueError, match="handshake descriptor"):
        VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT.acknowledge_if_requested(environment)


def test_malformed_handshake_payload_fails_fast() -> None:
    with pytest.raises(RuntimeError, match="payload is malformed"):
        validate_executor_handshake_payload(b"\x01\x02")
