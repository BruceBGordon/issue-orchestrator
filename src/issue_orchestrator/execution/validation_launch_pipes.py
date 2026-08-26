# pyright: strict
"""All-or-nothing owner for validation process launch pipes."""

from __future__ import annotations

from collections.abc import Mapping

from ..domain.posix_pipe import PosixPipeClosed, PosixPipeCloseFailed
from ..domain.posix_process import (
    PosixDescriptorMapping,
    PosixProcessEnvironment,
)
from ..infra.validation_executor_handshake import (
    VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT,
)
from ..ports.posix_pipe import PosixPipe, PosixPipeFactory, PosixPipeReader
from ..ports.validation_launch_pipes import (
    ValidationLaunchPipes,
    ValidationLaunchPipesClosed,
    ValidationLaunchPipesCloseFailed,
    ValidationLaunchReaders,
)
from ..domain.independent_cleanup import (
    CleanupAction,
    CleanupFailed,
    CleanupSucceeded,
    IndependentCleanupPlan,
)


_HANDSHAKE_CHILD_DESCRIPTOR = 3


def _close_pipe(pipe: PosixPipe) -> None:
    outcome = pipe.close()
    if type(outcome) is PosixPipeClosed:
        return
    if type(outcome) is not PosixPipeCloseFailed:
        raise AssertionError("POSIX pipe close result is a closed union")
    raise outcome.error


def _close_reader(reader: PosixPipeReader) -> None:
    reader.close()


def _cleanup_error(actions: tuple[CleanupAction, ...]) -> BaseException | None:
    outcome = IndependentCleanupPlan(actions).run()
    if type(outcome) is CleanupSucceeded:
        return None
    if type(outcome) is not CleanupFailed:
        raise AssertionError("cleanup outcome is a closed union")
    errors = tuple(failure.error for failure in outcome.failures)
    if len(errors) == 1:
        return errors[0]
    return BaseExceptionGroup("validation launch pipe cleanup failed", errors)


def _error_with_cleanup(
    message: str,
    primary: BaseException,
    cleanup: BaseException | None,
) -> BaseException:
    if cleanup is None:
        return primary
    return BaseExceptionGroup(message, (primary, cleanup))


class OwnedValidationLaunchPipes:
    """Own stdout, stderr, and handshake pipes through total transfer."""

    def __init__(
        self, stdout: PosixPipe, stderr: PosixPipe, handshake: PosixPipe
    ) -> None:
        for field_name, pipe in (
            ("stdout", stdout),
            ("stderr", stderr),
            ("handshake", handshake),
        ):
            if not callable(getattr(pipe, "close", None)):
                raise ValueError(
                    f"OwnedValidationLaunchPipes.{field_name} must implement PosixPipe"
                )
        self._stdout = stdout
        self._stderr = stderr
        self._handshake = handshake
        self._transferred_readers: list[PosixPipeReader] = []
        self._released = False

    @property
    def descriptor_mappings(self) -> tuple[PosixDescriptorMapping, ...]:
        self._require_owned()
        return (
            PosixDescriptorMapping(self._stdout.write_descriptor, 1),
            PosixDescriptorMapping(self._stderr.write_descriptor, 2),
            PosixDescriptorMapping(
                self._handshake.write_descriptor,
                _HANDSHAKE_CHILD_DESCRIPTOR,
            ),
        )

    def child_environment(
        self,
        base_environment: Mapping[str, str],
    ) -> PosixProcessEnvironment:
        self._require_owned()
        return PosixProcessEnvironment.from_mapping(
            VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT.child_environment(
                base_environment,
                _HANDSHAKE_CHILD_DESCRIPTOR,
            )
        )

    def transfer_readers_after_launch(self) -> ValidationLaunchReaders:
        self._require_owned()
        try:
            for pipe in (self._stdout, self._stderr, self._handshake):
                self._transferred_readers.append(pipe.transfer_reader_after_launch())
        except BaseException as transfer_error:
            raise _error_with_cleanup(
                "validation launch reader transfer and cleanup both failed",
                transfer_error,
                self._cleanup_owned(),
            )
        stdout, stderr, handshake = self._transferred_readers
        readers = ValidationLaunchReaders(stdout, stderr, handshake)
        self._transferred_readers.clear()
        self._released = True
        return readers

    def close(
        self,
    ) -> ValidationLaunchPipesClosed | ValidationLaunchPipesCloseFailed:
        cleanup = self._cleanup_owned()
        if cleanup is None:
            return ValidationLaunchPipesClosed()
        return ValidationLaunchPipesCloseFailed(cleanup)

    def _cleanup_owned(self) -> BaseException | None:
        if self._released:
            return None
        actions = tuple(
            (
                CleanupAction(
                    f"validation-transferred-reader-{index}-close",
                    lambda stream=reader: _close_reader(stream),
                )
                for index, reader in enumerate(self._transferred_readers)
            )
        ) + tuple(
            CleanupAction(
                f"validation-launch-{name}-pipe-close",
                lambda owned_pipe=pipe: _close_pipe(owned_pipe),
            )
            for name, pipe in (
                ("stdout", self._stdout),
                ("stderr", self._stderr),
                ("handshake", self._handshake),
            )
        )
        self._transferred_readers.clear()
        self._released = True
        return _cleanup_error(actions)

    def _require_owned(self) -> None:
        if self._released:
            raise RuntimeError("validation launch pipes were already released")


class PosixValidationLaunchPipesFactory:
    """Incrementally acquire three pipes and roll back every partial state."""

    def __init__(self, pipe_factory: PosixPipeFactory) -> None:
        if not callable(getattr(pipe_factory, "open", None)):
            raise ValueError(
                "PosixValidationLaunchPipesFactory.pipe_factory must implement "
                "PosixPipeFactory"
            )
        self._pipe_factory = pipe_factory

    def create(self) -> ValidationLaunchPipes:
        acquired: list[PosixPipe] = []
        try:
            for _role in ("stdout", "stderr", "handshake"):
                acquired.append(self._pipe_factory.open())
        except BaseException as acquisition_error:
            cleanup = _cleanup_error(
                tuple(
                    CleanupAction(
                        f"partial-validation-launch-pipe-{index}-close",
                        lambda pipe=owned_pipe: _close_pipe(pipe),
                    )
                    for index, owned_pipe in enumerate(acquired)
                )
            )
            raise _error_with_cleanup(
                "validation launch pipe acquisition and cleanup both failed",
                acquisition_error,
                cleanup,
            )
        stdout, stderr, handshake = acquired
        return OwnedValidationLaunchPipes(stdout, stderr, handshake)
