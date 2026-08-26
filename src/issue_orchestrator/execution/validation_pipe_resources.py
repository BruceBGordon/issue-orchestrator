"""All-or-nothing owner for validation pipe selector resources."""

from __future__ import annotations

import selectors
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, cast

from ..ports.posix_pipe import PosixPipeReader


class ValidationPipeRole(StrEnum):
    """Stable role of one descriptor in the validation capture protocol."""

    STDOUT = "stdout"
    STDERR = "stderr"
    EXECUTOR_HANDSHAKE = "executor-handshake"


class ValidationPipeSelector(Protocol):
    """Selector behavior needed by the validation resource owner."""

    def register(
        self,
        fileobj: int,
        events: int,
        data: object | None = None,
    ) -> selectors.SelectorKey: ...

    def unregister(self, fileobj: int) -> selectors.SelectorKey: ...

    def select(
        self, timeout: float | None = None
    ) -> list[tuple[selectors.SelectorKey, int]]: ...

    def close(self) -> None: ...


ValidationPipeSelectorFactory = Callable[[], ValidationPipeSelector]


def default_validation_pipe_selector() -> ValidationPipeSelector:
    """Create the platform's preferred selector behind the typed seam."""
    return cast(ValidationPipeSelector, selectors.DefaultSelector())


def _combine_resource_failures(
    primary: BaseException,
    secondary: BaseException | None,
    message: str,
) -> BaseException:
    if secondary is None:
        return primary
    return BaseExceptionGroup(message, (primary, secondary))


@dataclass(slots=True)
class ValidationPipeResourceOwner:
    """Own registration, selection, and total idempotent closure for three pipes."""

    stdout: PosixPipeReader
    stderr: PosixPipeReader
    handshake_reader: PosixPipeReader
    selector_factory: ValidationPipeSelectorFactory
    _selector: ValidationPipeSelector = field(init=False)
    _descriptors: dict[ValidationPipeRole, int] = field(
        default_factory=dict,
        init=False,
    )
    _registered: set[ValidationPipeRole] = field(default_factory=set, init=False)
    _open_streams: set[ValidationPipeRole] = field(default_factory=set, init=False)
    _selector_open: bool = field(default=False, init=False)
    _closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not callable(self.selector_factory):
            raise ValueError(
                "ValidationPipeResourceOwner.selector_factory must be callable"
            )
        streams = (
            (ValidationPipeRole.STDOUT, self.stdout),
            (ValidationPipeRole.STDERR, self.stderr),
            (ValidationPipeRole.EXECUTOR_HANDSHAKE, self.handshake_reader),
        )
        self._open_streams.update(role for role, _stream in streams)
        try:
            self._selector = self.selector_factory()
        except BaseException as setup_error:
            failures: list[BaseException] = []
            self._close_streams(failures)
            raise _combine_resource_failures(
                setup_error,
                self._failure_from(failures),
                "validation selector setup and pipe cleanup both failed",
            )
        self._selector_open = True
        try:
            for role, stream in streams:
                descriptor = stream.fileno()
                if type(descriptor) is not int or descriptor < 0:
                    raise ValueError(f"validation {role.value} descriptor is invalid")
                self._selector.register(descriptor, selectors.EVENT_READ)
                self._descriptors[role] = descriptor
                self._registered.add(role)
        except BaseException as setup_error:
            cleanup_error = self.close()
            raise _combine_resource_failures(
                setup_error,
                cleanup_error,
                "validation pipe setup and cleanup both failed",
            )

    def descriptor(self, role: ValidationPipeRole) -> int:
        """Return the registered descriptor for one required pipe role."""
        if type(role) is not ValidationPipeRole:
            raise ValueError("validation pipe descriptor requires ValidationPipeRole")
        try:
            return self._descriptors[role]
        except KeyError as error:
            raise RuntimeError(
                f"validation {role.value} descriptor was not registered"
            ) from error

    def select(self, timeout_seconds: float) -> tuple[int, ...]:
        """Return ready descriptors while this owner remains open."""
        if self._closed:
            raise RuntimeError("validation pipe resources are already closed")
        return tuple(
            int(key.fd) for key, _mask in self._selector.select(timeout_seconds)
        )

    def unregister(self, role: ValidationPipeRole) -> None:
        """Stop selecting one role; a failure remains visible to final cleanup."""
        if type(role) is not ValidationPipeRole:
            raise ValueError("validation pipe unregister requires ValidationPipeRole")
        if role not in self._registered:
            return
        self._selector.unregister(self.descriptor(role))
        self._registered.remove(role)

    def is_registered(self, role: ValidationPipeRole) -> bool:
        """Report whether one protocol role still requires an EOF observation."""
        if type(role) is not ValidationPipeRole:
            raise ValueError(
                "validation pipe registration query requires ValidationPipeRole"
            )
        return role in self._registered

    def close(self) -> BaseException | None:
        """Attempt every independent cleanup action exactly once without raising."""
        if self._closed:
            return None
        failures: list[BaseException] = []
        self._close_registrations(failures)
        self._close_streams(failures)
        self._close_selector(failures)
        self._closed = True
        return self._failure_from(failures)

    def _close_registrations(self, failures: list[BaseException]) -> None:
        for role in ValidationPipeRole:
            if role not in self._registered:
                continue
            try:
                self._selector.unregister(self.descriptor(role))
            except BaseException as error:
                error.add_note(f"while unregistering validation {role.value}")
                failures.append(error)
            finally:
                self._registered.discard(role)

    def _close_streams(self, failures: list[BaseException]) -> None:
        streams = (
            (ValidationPipeRole.STDOUT, self.stdout),
            (ValidationPipeRole.STDERR, self.stderr),
            (ValidationPipeRole.EXECUTOR_HANDSHAKE, self.handshake_reader),
        )
        for role, stream in streams:
            if role not in self._open_streams:
                continue
            try:
                stream.close()
            except BaseException as error:
                error.add_note(f"while closing validation {role.value}")
                failures.append(error)
            finally:
                self._open_streams.remove(role)

    def _close_selector(self, failures: list[BaseException]) -> None:
        if self._selector_open:
            try:
                self._selector.close()
            except BaseException as error:
                error.add_note("while closing the validation pipe selector")
                failures.append(error)
            finally:
                self._selector_open = False

    @staticmethod
    def _failure_from(failures: list[BaseException]) -> BaseException | None:
        if not failures:
            return None
        if len(failures) == 1:
            return failures[0]
        return BaseExceptionGroup("validation pipe resources did not close", failures)
