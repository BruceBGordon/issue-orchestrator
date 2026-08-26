"""Public fault proofs for process-cancellation endpoint acquisition."""

from __future__ import annotations

import os
import socket

import pytest

from issue_orchestrator.execution.process_cancellation_endpoint import (
    ProcessCancellationAcceptedConnection,
)


class _SetBlockingFailureSocket(socket.socket):
    """Real descriptor whose nonblocking transition fails deterministically."""

    def setblocking(self, flag: bool) -> None:
        del flag
        raise RuntimeError("injected accepted-socket setblocking failure")


class _SetBlockingAndCloseFailureSocket(_SetBlockingFailureSocket):
    def __init__(
        self,
        *,
        family: socket.AddressFamily | int,
        type: socket.SocketKind | int,
        proto: int,
        fileno: int,
    ) -> None:
        super().__init__(family, type, proto, fileno)
        self.close_error = RuntimeError("injected accepted-socket close failure")

    def close(self) -> None:
        super().close()
        raise self.close_error


def test_accepted_socket_setup_failure_closes_the_descriptor() -> None:
    observed_socket, peer = socket.socketpair()
    accepted_socket = _SetBlockingFailureSocket(
        family=observed_socket.family,
        type=observed_socket.type,
        proto=observed_socket.proto,
        fileno=os.dup(observed_socket.fileno()),
    )
    observed_socket.close()
    try:
        with pytest.raises(
            RuntimeError,
            match="injected accepted-socket setblocking failure",
        ):
            ProcessCancellationAcceptedConnection(accepted_socket)

        assert accepted_socket.fileno() == -1
    finally:
        accepted_socket.close()
        peer.close()


def test_accepted_socket_setup_preserves_primary_and_close_failures() -> None:
    observed_socket, peer = socket.socketpair()
    accepted_socket = _SetBlockingAndCloseFailureSocket(
        family=observed_socket.family,
        type=observed_socket.type,
        proto=observed_socket.proto,
        fileno=os.dup(observed_socket.fileno()),
    )
    observed_socket.close()
    try:
        with pytest.raises(BaseExceptionGroup) as raised:
            ProcessCancellationAcceptedConnection(accepted_socket)

        primary_error, cleanup_error = raised.value.exceptions
        assert str(primary_error) == "injected accepted-socket setblocking failure"
        assert cleanup_error is accepted_socket.close_error
        assert accepted_socket.fileno() == -1
    finally:
        socket.socket.close(accepted_socket)
        peer.close()
