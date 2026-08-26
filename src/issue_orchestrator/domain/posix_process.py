"""Strong contracts for one retained POSIX process activation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from pathlib import Path
from typing import Mapping, cast


@dataclass(frozen=True, slots=True)
class PosixProcessProgram:
    """One exact executable and argument vector."""

    arguments: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.arguments) is not tuple or not self.arguments:
            raise ValueError("PosixProcessProgram.arguments must be a non-empty tuple")
        if any(
            type(argument) is not str or not argument or "\0" in argument
            for argument in self.arguments
        ):
            raise ValueError(
                "PosixProcessProgram.arguments must contain non-empty, NUL-free strings"
            )
        if not Path(self.arguments[0]).is_absolute():
            raise ValueError("PosixProcessProgram executable must be absolute")


@dataclass(frozen=True, slots=True)
class PosixEnvironmentVariable:
    """One exact child environment entry."""

    name: str
    value: str

    def __post_init__(self) -> None:
        if (
            type(self.name) is not str
            or not self.name
            or "=" in self.name
            or "\0" in self.name
        ):
            raise ValueError(
                "PosixEnvironmentVariable.name must be non-empty and exclude =/NUL"
            )
        if type(self.value) is not str or "\0" in self.value:
            raise ValueError("PosixEnvironmentVariable.value must be a NUL-free string")


@dataclass(frozen=True, slots=True)
class PosixProcessEnvironment:
    """An explicit complete child environment with unique names."""

    variables: tuple[PosixEnvironmentVariable, ...]

    def __post_init__(self) -> None:
        if type(self.variables) is not tuple or any(
            type(variable) is not PosixEnvironmentVariable
            for variable in self.variables
        ):
            raise ValueError(
                "PosixProcessEnvironment.variables must contain typed variables"
            )
        names = tuple(variable.name for variable in self.variables)
        if len(names) != len(set(names)):
            raise ValueError("PosixProcessEnvironment variable names must be unique")

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> PosixProcessEnvironment:
        """Copy a string mapping into stable, sorted typed entries."""
        if not isinstance(values, Mapping):
            raise ValueError("PosixProcessEnvironment requires a mapping")
        return cls(
            tuple(
                PosixEnvironmentVariable(name, value)
                for name, value in sorted(values.items())
            )
        )

    def as_mapping(self) -> dict[str, str]:
        """Materialize the OS adapter's required string mapping."""
        return {variable.name: variable.value for variable in self.variables}


class PosixProcessGroupMode(StrEnum):
    """Kernel grouping established atomically by ``posix_spawn``."""

    NEW_SESSION = "new-session"
    NEW_PROCESS_GROUP = "new-process-group"


@dataclass(frozen=True, slots=True)
class PosixProcessJoinGroup:
    """Join one existing group whose containment authority is external."""

    process_group_id: int

    def __post_init__(self) -> None:
        if type(self.process_group_id) is not int or self.process_group_id <= 1:
            raise ValueError("PosixProcessJoinGroup.process_group_id must be above 1")


PosixProcessGroup = PosixProcessGroupMode | PosixProcessJoinGroup


@dataclass(frozen=True, slots=True)
class PosixProcessActivationPolicy:
    """Independent bound for wrapper startup and the close-on-exec handshake."""

    exec_handshake_timeout_seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.exec_handshake_timeout_seconds) is not float
            or not math.isfinite(self.exec_handshake_timeout_seconds)
            or self.exec_handshake_timeout_seconds <= 0
        ):
            raise ValueError(
                "PosixProcessActivationPolicy.exec_handshake_timeout_seconds "
                "must be finite and positive"
            )


@dataclass(frozen=True, slots=True)
class PosixProcessConfiguredActivationDeadline:
    """Use the process module's configured activation safety bound."""


class PosixProcessActivationDeadlineExceededError(TimeoutError):
    """The caller's absolute activation deadline expired."""


class PosixProcessJoinedGroupContainmentRequiredError(RuntimeError):
    """Post-gate activation requires the external group owner to contain."""


@dataclass(frozen=True, slots=True)
class PosixProcessActivationDeadlineAbsent:
    """An activation error does not contain caller-deadline evidence."""


@dataclass(frozen=True, slots=True)
class PosixProcessActivationDeadlinePresent:
    """Deadline evidence plus every independent recovery failure beside it."""

    recovery_failures: tuple[BaseException, ...]

    def __post_init__(self) -> None:
        if type(self.recovery_failures) is not tuple or any(
            not isinstance(failure, BaseException) for failure in self.recovery_failures
        ):
            raise ValueError(
                "PosixProcessActivationDeadlinePresent.recovery_failures "
                "must contain exceptions"
            )


PosixProcessActivationDeadlineEvidence = (
    PosixProcessActivationDeadlineAbsent | PosixProcessActivationDeadlinePresent
)


def classify_posix_process_activation_deadline(
    error: BaseException,
) -> PosixProcessActivationDeadlineEvidence:
    """Separate an exact deadline cause from sibling recovery failures."""
    if isinstance(error, PosixProcessActivationDeadlineExceededError):
        return PosixProcessActivationDeadlinePresent(())
    if not isinstance(error, BaseExceptionGroup):
        return PosixProcessActivationDeadlineAbsent()
    group = cast(BaseExceptionGroup[BaseException], error)
    found = False
    recovery_failures: list[BaseException] = []
    for child in group.exceptions:
        classified = classify_posix_process_activation_deadline(child)
        if type(classified) is PosixProcessActivationDeadlinePresent:
            found = True
            recovery_failures.extend(classified.recovery_failures)
        elif type(classified) is PosixProcessActivationDeadlineAbsent:
            recovery_failures.append(child)
        else:
            raise AssertionError("activation deadline evidence is a closed union")
    if found:
        return PosixProcessActivationDeadlinePresent(tuple(recovery_failures))
    return PosixProcessActivationDeadlineAbsent()


@dataclass(frozen=True, slots=True)
class PosixProcessAbsoluteActivationDeadline:
    """One caller-owned monotonic expiry spanning the whole activation."""

    expires_at_monotonic: float

    def __post_init__(self) -> None:
        if (
            type(self.expires_at_monotonic) is not float
            or not math.isfinite(self.expires_at_monotonic)
            or self.expires_at_monotonic < 0.0
        ):
            raise ValueError(
                "PosixProcessAbsoluteActivationDeadline.expires_at_monotonic "
                "must be finite and non-negative"
            )


PosixProcessActivationDeadline = (
    PosixProcessConfiguredActivationDeadline | PosixProcessAbsoluteActivationDeadline
)


@dataclass(frozen=True, slots=True)
class PosixDescriptorMapping:
    """Duplicate one owned parent descriptor onto one exact child descriptor."""

    source_file_descriptor: int
    child_file_descriptor: int

    def __post_init__(self) -> None:
        for field_name, value in (
            ("source_file_descriptor", self.source_file_descriptor),
            ("child_file_descriptor", self.child_file_descriptor),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"PosixDescriptorMapping.{field_name} must be non-negative"
                )


@dataclass(frozen=True, slots=True)
class PosixProcessWithoutTerminal:
    """The child must not acquire a controlling terminal."""


@dataclass(frozen=True, slots=True)
class PosixProcessControllingTerminal:
    """The child session acquires this already-mapped descriptor as its TTY."""

    child_file_descriptor: int

    def __post_init__(self) -> None:
        if (
            type(self.child_file_descriptor) is not int
            or self.child_file_descriptor < 0
        ):
            raise ValueError(
                "PosixProcessControllingTerminal.child_file_descriptor must be "
                "non-negative"
            )


PosixProcessTerminal = PosixProcessWithoutTerminal | PosixProcessControllingTerminal


@dataclass(frozen=True, slots=True)
class PosixProcessLaunchSpec:
    """Complete caller-facing process activation specification."""

    program: PosixProcessProgram
    working_directory: Path
    environment: PosixProcessEnvironment
    group_mode: PosixProcessGroup
    descriptor_mappings: tuple[PosixDescriptorMapping, ...]
    terminal: PosixProcessTerminal
    activation_deadline: PosixProcessActivationDeadline

    def __post_init__(self) -> None:
        if type(self.program) is not PosixProcessProgram:
            raise ValueError("PosixProcessLaunchSpec.program must be typed")
        if not isinstance(self.working_directory, Path) or not (
            self.working_directory.is_absolute()
        ):
            raise ValueError(
                "PosixProcessLaunchSpec.working_directory must be an absolute Path"
            )
        if type(self.environment) is not PosixProcessEnvironment:
            raise ValueError("PosixProcessLaunchSpec.environment must be typed")
        if type(self.group_mode) not in (
            PosixProcessGroupMode,
            PosixProcessJoinGroup,
        ):
            raise ValueError("PosixProcessLaunchSpec.group_mode must be typed")
        if type(self.activation_deadline) not in (
            PosixProcessConfiguredActivationDeadline,
            PosixProcessAbsoluteActivationDeadline,
        ):
            raise ValueError("PosixProcessLaunchSpec.activation_deadline must be typed")
        targets = self._validate_descriptor_mappings()
        self._validate_terminal(targets)

    def _validate_descriptor_mappings(self) -> tuple[int, ...]:
        if type(self.descriptor_mappings) is not tuple or any(
            type(mapping) is not PosixDescriptorMapping
            for mapping in self.descriptor_mappings
        ):
            raise ValueError("PosixProcessLaunchSpec.descriptor_mappings must be typed")
        targets = tuple(
            mapping.child_file_descriptor for mapping in self.descriptor_mappings
        )
        if len(targets) != len(set(targets)):
            raise ValueError("child descriptor mapping targets must be unique")
        return targets

    def _validate_terminal(self, targets: tuple[int, ...]) -> None:
        if type(self.terminal) is PosixProcessControllingTerminal:
            if self.group_mode is not PosixProcessGroupMode.NEW_SESSION:
                raise ValueError("a controlling terminal requires a new session")
            if self.terminal.child_file_descriptor not in targets:
                raise ValueError(
                    "controlling terminal descriptor must be explicitly mapped"
                )
        elif type(self.terminal) is not PosixProcessWithoutTerminal:
            raise ValueError("PosixProcessLaunchSpec.terminal must be typed")
