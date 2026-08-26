# pyright: strict
"""Typed owner for host-executor request identity and queue ordering."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re

from ...domain.executor_monitoring import ExecutorRequestId


_REQUEST_NONCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class ExecutorInvocationIdentity:
    """Unique request identity paired with monotonic queue order."""

    request_id: ExecutorRequestId
    queue_sequence: int

    def __post_init__(self) -> None:
        if type(self.request_id) is not ExecutorRequestId:
            raise ValueError(
                "ExecutorInvocationIdentity.request_id must be ExecutorRequestId"
            )
        if type(self.queue_sequence) is not int or self.queue_sequence < 1:
            raise ValueError(
                "ExecutorInvocationIdentity.queue_sequence must be positive"
            )


class ExecutorRequestIdentityFactory:
    """Create trace identities without using wall time for queue ordering."""

    def __init__(
        self,
        *,
        wall_time_nanoseconds: Callable[[], int],
        monotonic_nanoseconds: Callable[[], int],
        process_id: Callable[[], int],
        request_nonce: Callable[[], str],
    ) -> None:
        for name, value in (
            ("wall_time_nanoseconds", wall_time_nanoseconds),
            ("monotonic_nanoseconds", monotonic_nanoseconds),
            ("process_id", process_id),
            ("request_nonce", request_nonce),
        ):
            if not callable(value):
                raise ValueError(f"ExecutorRequestIdentityFactory.{name} must be callable")
        self._wall_time_nanoseconds = wall_time_nanoseconds
        self._monotonic_nanoseconds = monotonic_nanoseconds
        self._process_id = process_id
        self._request_nonce = request_nonce

    def create(self) -> ExecutorInvocationIdentity:
        """Create one identity, validating every injected system observation."""
        wall_time = self._wall_time_nanoseconds()
        queue_sequence = self._monotonic_nanoseconds()
        process_id = self._process_id()
        request_nonce = self._request_nonce()
        if type(wall_time) is not int or wall_time < 1:
            raise RuntimeError("wall-clock nanoseconds must be positive")
        if type(queue_sequence) is not int or queue_sequence < 1:
            raise RuntimeError("monotonic nanoseconds must be positive")
        if type(process_id) is not int or process_id < 1:
            raise RuntimeError("executor process id must be positive")
        if (
            type(request_nonce) is not str
            or not _REQUEST_NONCE_PATTERN.fullmatch(request_nonce)
        ):
            raise RuntimeError(
                "executor request nonce must be 32 lowercase hexadecimal characters"
            )
        return ExecutorInvocationIdentity(
            request_id=ExecutorRequestId(
                f"{wall_time:020d}-{process_id}-{request_nonce}"
            ),
            queue_sequence=queue_sequence,
        )
