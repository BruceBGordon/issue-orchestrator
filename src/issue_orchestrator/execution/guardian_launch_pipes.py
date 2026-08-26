# pyright: strict
"""All-or-nothing guardian launch pipe ownership."""

from __future__ import annotations

from typing import NoReturn

from ..domain.posix_pipe import PosixPipeClosed, PosixPipeCloseFailed
from ..domain.posix_process import PosixDescriptorMapping
from ..ports.guardian_launch_pipes import (
    GuardianChildPipeDescriptors,
    GuardianLaunchPipes,
    GuardianLaunchPipesClosed,
    GuardianLaunchPipesCloseFailed,
    GuardianParentPipeEndpoints,
)
from ..ports.posix_pipe import (
    PosixPipe,
    PosixPipeFactory,
    PosixPipeReader,
    PosixPipeWriter,
)
from ..domain.independent_cleanup import (
    CleanupAction,
    CleanupFailed,
    CleanupSucceeded,
    IndependentCleanupPlan,
)


def _close_pipe(pipe: PosixPipe) -> None:
    outcome = pipe.close()
    if type(outcome) is PosixPipeClosed:
        return
    if type(outcome) is not PosixPipeCloseFailed:
        raise AssertionError("POSIX pipe close result is a closed union")
    raise outcome.error


def _cleanup_error(actions: tuple[CleanupAction, ...]) -> BaseException | None:
    outcome = IndependentCleanupPlan(actions).run()
    if type(outcome) is CleanupSucceeded:
        return None
    if type(outcome) is not CleanupFailed:
        raise AssertionError("cleanup outcome is a closed union")
    errors = tuple(failure.error for failure in outcome.failures)
    if len(errors) == 1:
        return errors[0]
    return BaseExceptionGroup("guardian launch pipe cleanup failed", errors)


def _raise_with_cleanup(
    message: str,
    primary: BaseException,
    cleanup: BaseException | None,
) -> NoReturn:
    if cleanup is None:
        raise primary
    raise BaseExceptionGroup(message, (primary, cleanup))


class OwnedGuardianLaunchPipes:
    """Own activation and lifetime pipes through terminal cleanup."""

    def __init__(
        self,
        result: PosixPipe,
        start: PosixPipe,
        owner_ready: PosixPipe,
        parent_lifetime: PosixPipe,
    ) -> None:
        self._result = result
        self._start = start
        self._owner_ready = owner_ready
        self._parent_lifetime = parent_lifetime
        self._parent_readers: list[PosixPipeReader] = []
        self._parent_writers: list[PosixPipeWriter] = []
        self._closed = False

    @property
    def child_descriptors(self) -> GuardianChildPipeDescriptors:
        self._require_open()
        return GuardianChildPipeDescriptors(
            result_writer=self._result.write_descriptor,
            start_reader=self._start.read_descriptor,
            owner_ready_writer=self._owner_ready.write_descriptor,
            parent_lifetime_reader=self._parent_lifetime.read_descriptor,
        )

    def descriptor_mappings(
        self,
        inherited_descriptors: tuple[int, ...],
    ) -> tuple[PosixDescriptorMapping, ...]:
        self._require_open()
        child = self.child_descriptors
        all_descriptors = (
            *inherited_descriptors,
            child.result_writer,
            child.start_reader,
            child.owner_ready_writer,
            child.parent_lifetime_reader,
        )
        if len(all_descriptors) != len(set(all_descriptors)):
            raise ValueError("guardian inherited descriptors must be unique")
        return tuple(
            PosixDescriptorMapping(descriptor, descriptor)
            for descriptor in all_descriptors
        )

    def transfer_parent_endpoints_after_launch(
        self,
    ) -> GuardianParentPipeEndpoints:
        self._require_open()
        try:
            result_reader = self._result.transfer_reader_after_launch()
            self._parent_readers.append(result_reader)
            start_writer = self._start.transfer_writer_after_launch()
            self._parent_writers.append(start_writer)
            owner_ready_reader = self._owner_ready.transfer_reader_after_launch()
            self._parent_readers.append(owner_ready_reader)
            parent_lifetime_writer = (
                self._parent_lifetime.transfer_writer_after_launch()
            )
            self._parent_writers.append(parent_lifetime_writer)
        except BaseException as transfer_error:
            _raise_with_cleanup(
                "guardian parent endpoint transfer and cleanup both failed",
                transfer_error,
                self._cleanup_owned(),
            )
        return GuardianParentPipeEndpoints(
            result_reader,
            start_writer,
            owner_ready_reader,
            parent_lifetime_writer,
        )

    def close(
        self,
    ) -> GuardianLaunchPipesClosed | GuardianLaunchPipesCloseFailed:
        cleanup = self._cleanup_owned()
        if cleanup is None:
            return GuardianLaunchPipesClosed()
        return GuardianLaunchPipesCloseFailed(cleanup)

    def _cleanup_owned(self) -> BaseException | None:
        if self._closed:
            return None
        self._closed = True
        actions = (
            tuple(
                CleanupAction(
                    f"guardian-parent-reader-{index}-close",
                    reader.close,
                )
                for index, reader in enumerate(self._parent_readers)
            )
            + tuple(
                CleanupAction(
                    f"guardian-parent-writer-{index}-close",
                    writer.close,
                )
                for index, writer in enumerate(self._parent_writers)
            )
            + tuple(
                CleanupAction(
                    f"guardian-{name}-pipe-close",
                    lambda pipe=owned_pipe: _close_pipe(pipe),
                )
                for name, owned_pipe in (
                    ("result", self._result),
                    ("start", self._start),
                    ("owner-ready", self._owner_ready),
                    ("parent-lifetime", self._parent_lifetime),
                )
            )
        )
        self._parent_readers.clear()
        self._parent_writers.clear()
        return _cleanup_error(actions)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("guardian launch pipes are closed")


class PosixGuardianLaunchPipesFactory:
    """Incrementally acquire every guardian launch pipe."""

    def __init__(self, pipe_factory: PosixPipeFactory) -> None:
        if not callable(getattr(pipe_factory, "open", None)):
            raise ValueError(
                "PosixGuardianLaunchPipesFactory.pipe_factory must implement "
                "PosixPipeFactory"
            )
        self._pipe_factory = pipe_factory

    def create(self) -> GuardianLaunchPipes:
        acquired: list[PosixPipe] = []
        try:
            for _role in ("result", "start", "owner-ready", "parent-lifetime"):
                acquired.append(self._pipe_factory.open())
        except BaseException as acquisition_error:
            cleanup = _cleanup_error(
                tuple(
                    CleanupAction(
                        f"partial-guardian-pipe-{index}-close",
                        lambda pipe=owned_pipe: _close_pipe(pipe),
                    )
                    for index, owned_pipe in enumerate(acquired)
                )
            )
            _raise_with_cleanup(
                "guardian pipe acquisition and cleanup both failed",
                acquisition_error,
                cleanup,
            )
        result, start, owner_ready, parent_lifetime = acquired
        return OwnedGuardianLaunchPipes(
            result,
            start,
            owner_ready,
            parent_lifetime,
        )
