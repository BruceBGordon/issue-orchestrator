"""Inherited handshake from nested executor clients to validation ownership."""

from __future__ import annotations

import os
import math
import struct
import time
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass

from .env import ENV_PREFIX


_HANDSHAKE_VERSION = 1
_HANDSHAKE_RECORD = struct.Struct("!Bd")


@dataclass(frozen=True, slots=True)
class ValidationExecutorAcknowledgement:
    """The exact shared monotonic instant when a nested executor assumed work."""

    acknowledged_at_monotonic: float

    def __post_init__(self) -> None:
        if (
            type(self.acknowledged_at_monotonic) is not float
            or not math.isfinite(self.acknowledged_at_monotonic)
            or self.acknowledged_at_monotonic <= 0
        ):
            raise ValueError(
                "ValidationExecutorAcknowledgement.acknowledged_at_monotonic "
                "must be a positive finite float"
            )


class ValidationExecutorHandshakeDecoder:
    """Decode a byte stream of fixed, atomic nested-executor acknowledgements."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def consume(self, payload: bytes) -> tuple[ValidationExecutorAcknowledgement, ...]:
        if type(payload) is not bytes or not payload:
            raise ValueError("validation executor handshake payload must not be empty")
        self._buffer.extend(payload)
        acknowledgements: list[ValidationExecutorAcknowledgement] = []
        while len(self._buffer) >= _HANDSHAKE_RECORD.size:
            record = bytes(self._buffer[: _HANDSHAKE_RECORD.size])
            del self._buffer[: _HANDSHAKE_RECORD.size]
            version, acknowledged_at_monotonic = _HANDSHAKE_RECORD.unpack(record)
            if version != _HANDSHAKE_VERSION:
                raise RuntimeError(
                    "validation executor handshake payload has an unsupported version"
                )
            acknowledgements.append(
                ValidationExecutorAcknowledgement(acknowledged_at_monotonic)
            )
        return tuple(acknowledgements)

    def finish(self) -> None:
        """Reject a descriptor that closed part-way through one record."""
        if self._buffer:
            raise RuntimeError("validation executor handshake payload is malformed")


@dataclass(frozen=True, slots=True)
class ValidationExecutorHandshakeEnvironment:
    """Own the optional inherited descriptor used by nested executor clients."""

    descriptor_variable: str

    def __post_init__(self) -> None:
        if type(self.descriptor_variable) is not str or not self.descriptor_variable:
            raise ValueError(
                "ValidationExecutorHandshakeEnvironment.descriptor_variable "
                "must not be empty"
            )

    def child_environment(
        self,
        environment: Mapping[str, str],
        write_descriptor: int,
    ) -> dict[str, str]:
        """Return child environment carrying one exact inherited descriptor."""
        if type(write_descriptor) is not int or write_descriptor <= 2:
            raise ValueError("validation executor handshake descriptor must exceed 2")
        child = dict(environment)
        child[self.descriptor_variable] = str(write_descriptor)
        return child

    def acknowledge_if_requested(
        self,
        environment: MutableMapping[str, str],
    ) -> None:
        """Acknowledge once, consuming the descriptor before opaque work starts."""
        raw_descriptor = environment.pop(self.descriptor_variable, None)
        if raw_descriptor is None:
            return
        if not raw_descriptor.isascii() or not raw_descriptor.isdecimal():
            raise ValueError(
                "validation executor handshake descriptor must be a decimal integer"
            )
        descriptor = int(raw_descriptor)
        if descriptor <= 2:
            raise ValueError("validation executor handshake descriptor must exceed 2")
        try:
            payload = _HANDSHAKE_RECORD.pack(
                _HANDSHAKE_VERSION,
                time.monotonic(),
            )
            written = os.write(descriptor, payload)
        except OSError as exc:
            try:
                os.close(descriptor)
            except OSError as close_exc:
                raise BaseExceptionGroup(
                    "validation executor handshake write and descriptor close failed",
                    (
                        RuntimeError(
                            "validation executor handshake descriptor is not writable"
                        ),
                        close_exc,
                    ),
                ) from exc
            raise RuntimeError(
                "validation executor handshake descriptor is not writable"
            ) from exc
        try:
            os.close(descriptor)
        except OSError as exc:
            raise RuntimeError(
                "validation executor handshake descriptor did not close"
            ) from exc
        if written != len(payload):
            raise RuntimeError("validation executor handshake write was incomplete")


VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT = ValidationExecutorHandshakeEnvironment(
    descriptor_variable=f"{ENV_PREFIX}VALIDATION_EXECUTOR_HANDSHAKE_FD"
)


def validate_executor_handshake_payload(
    payload: bytes,
) -> tuple[ValidationExecutorAcknowledgement, ...]:
    """Decode one complete payload for tests and non-streaming boundaries."""
    decoder = ValidationExecutorHandshakeDecoder()
    acknowledgements = decoder.consume(payload)
    decoder.finish()
    return acknowledgements
