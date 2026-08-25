"""Strongly typed records for validation timing evidence."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeAlias, runtime_checkable


ValidationTimingScalar: TypeAlias = str | int | float | bool | None
SerializedValidationTiming: TypeAlias = dict[str, ValidationTimingScalar]
_CONFIG_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@runtime_checkable
class ValidationTimingPayload(Protocol):
    """Typed timing entity serializable at the private JSONL boundary."""

    def timing_fields(self) -> Mapping[str, ValidationTimingScalar]:
        """Return this entity's fields for the private JSON adapter."""
        ...


def _require_exact(owner: str, field: str, value: object, expected: type) -> None:
    if type(value) is not expected:
        raise ValueError(f"{owner}.{field} must have exact type {expected.__name__}")


def _require_non_empty(owner: str, field: str, value: str) -> None:
    _require_exact(owner, field, value, str)
    if not value:
        raise ValueError(f"{owner}.{field} must not be empty")


def _require_optional_non_empty(
    owner: str,
    field: str,
    value: str | None,
) -> None:
    if value is not None:
        _require_non_empty(owner, field, value)


def _require_integer(
    owner: str,
    field: str,
    value: int,
    *,
    minimum: int,
) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{owner}.{field} must be an integer >= {minimum}")


def _require_optional_integer(
    owner: str,
    field: str,
    value: int | None,
    *,
    minimum: int,
) -> None:
    if value is not None:
        _require_integer(owner, field, value, minimum=minimum)


def _require_float(
    owner: str,
    field: str,
    value: float,
    *,
    minimum: float,
) -> None:
    if type(value) is not float or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{owner}.{field} must be finite and >= {minimum}")


def _require_finite_float(owner: str, field: str, value: float) -> None:
    if type(value) is not float or not math.isfinite(value):
        raise ValueError(f"{owner}.{field} must be finite")


def _require_optional_float(
    owner: str,
    field: str,
    value: float | None,
    *,
    minimum: float,
) -> None:
    if value is not None:
        _require_float(owner, field, value, minimum=minimum)


def _require_optional_boolean(owner: str, field: str, value: bool | None) -> None:
    if value is not None:
        _require_exact(owner, field, value, bool)


def _require_path(owner: str, field: str, value: object) -> None:
    if not isinstance(value, Path):
        raise ValueError(f"{owner}.{field} must be a Path")


def merge_validation_timing_fields(
    *payloads: ValidationTimingPayload | Mapping[str, ValidationTimingScalar],
) -> SerializedValidationTiming:
    """Combine typed timing fields and reject schema collisions."""
    merged: SerializedValidationTiming = {}
    for payload in payloads:
        fields = payload if isinstance(payload, Mapping) else payload.timing_fields()
        duplicate_keys = merged.keys() & fields.keys()
        if duplicate_keys:
            duplicates = ", ".join(sorted(duplicate_keys))
            raise ValueError(f"duplicate validation timing fields: {duplicates}")
        merged.update(fields)
    return merged


@dataclass(frozen=True, slots=True)
class ValidationTimingEnvelope:
    """Two-clock elapsed observation for one completed operation."""

    monotonic_elapsed_seconds: float
    wall_started_at: str
    wall_ended_at: str
    wall_elapsed_seconds: float

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_float(
            owner,
            "monotonic_elapsed_seconds",
            self.monotonic_elapsed_seconds,
            minimum=0.0,
        )
        _require_non_empty(owner, "wall_started_at", self.wall_started_at)
        _require_non_empty(owner, "wall_ended_at", self.wall_ended_at)
        # Wall-clock adjustments are evidence, not elapsed-time authority. A
        # negative value is valid and exposes a clock rollback rather than
        # preventing the monotonic result from being recorded.
        _require_finite_float(owner, "wall_elapsed_seconds", self.wall_elapsed_seconds)

    def timing_fields(self) -> Mapping[str, ValidationTimingScalar]:
        return {
            "monotonic_elapsed_seconds": self.monotonic_elapsed_seconds,
            "wall_started_at": self.wall_started_at,
            "wall_ended_at": self.wall_ended_at,
            "wall_elapsed_seconds": self.wall_elapsed_seconds,
        }


@dataclass(frozen=True, slots=True)
class ValidationHostContext:
    """Typed hardware identity attached to one machine timing record."""

    name: str | None
    system: str | None
    release: str | None
    machine: str | None
    cpu_count: int | None
    memory_bytes: int | None

    def __post_init__(self) -> None:
        owner = type(self).__name__
        for field_name, value in (
            ("name", self.name),
            ("system", self.system),
            ("release", self.release),
            ("machine", self.machine),
        ):
            _require_optional_non_empty(owner, field_name, value)
        _require_optional_integer(owner, "cpu_count", self.cpu_count, minimum=1)
        _require_optional_integer(
            owner,
            "memory_bytes",
            self.memory_bytes,
            minimum=1,
        )

    def timing_fields(self) -> Mapping[str, ValidationTimingScalar]:
        return {
            "host_name": self.name,
            "host_system": self.system,
            "host_release": self.release,
            "host_machine": self.machine,
            "host_cpu_count": self.cpu_count,
            "host_memory_bytes": self.memory_bytes,
        }


@dataclass(frozen=True, slots=True)
class ValidationConfigurationEntry:
    """One parsed, opaque make configuration field."""

    name: str
    value: str

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_non_empty(owner, "name", self.name)
        _require_non_empty(owner, "value", self.value)
        if not _CONFIG_KEY_RE.fullmatch(self.name):
            raise ValueError(f"{owner}.name must be a configuration key")
        if any(character.isspace() for character in self.value):
            raise ValueError(f"{owner}.value must contain no whitespace")


@dataclass(frozen=True, slots=True)
class ValidationConfiguration:
    """Ordered, uniquely named configuration captured for one validation run."""

    entries: tuple[ValidationConfigurationEntry, ...]

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_exact(owner, "entries", self.entries, tuple)
        if any(
            type(entry) is not ValidationConfigurationEntry for entry in self.entries
        ):
            raise ValueError(
                f"{owner}.entries must contain ValidationConfigurationEntry values"
            )
        names = tuple(entry.name for entry in self.entries)
        if len(names) != len(set(names)):
            raise ValueError(f"{owner} entry names must be unique")

    @classmethod
    def parse(cls, fields: str) -> ValidationConfiguration:
        _require_non_empty(cls.__name__, "fields", fields)
        entries: list[ValidationConfigurationEntry] = []
        for token in fields.split():
            name, separator, value = token.partition("=")
            if separator != "=":
                raise ValueError(f"invalid validation configuration field: {token}")
            entries.append(ValidationConfigurationEntry(name, value))
        return cls(tuple(entries))

    @classmethod
    def empty(cls) -> ValidationConfiguration:
        return cls(())

    def timing_fields(self) -> Mapping[str, ValidationTimingScalar]:
        return {entry.name: entry.value for entry in self.entries}


@dataclass(frozen=True, slots=True)
class ValidationRunTimingContext:
    """Shared identity and machine facts for records from one validate run."""

    run_id: str
    command: str
    worktree: Path
    branch: str | None
    host: ValidationHostContext

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_non_empty(owner, "run_id", self.run_id)
        _require_non_empty(owner, "command", self.command)
        _require_path(owner, "worktree", self.worktree)
        _require_optional_non_empty(owner, "branch", self.branch)
        _require_exact(owner, "host", self.host, ValidationHostContext)

    def timing_fields(self) -> Mapping[str, ValidationTimingScalar]:
        return merge_validation_timing_fields(
            {
                "run_id": self.run_id,
                "command": self.command,
                "worktree": str(self.worktree),
                "branch": self.branch,
            },
            self.host,
        )


@dataclass(frozen=True, slots=True)
class ValidationTargetTiming:
    """Completed timing for one named make target."""

    context: ValidationRunTimingContext
    configuration: ValidationConfiguration
    target: str
    status: int
    elapsed_seconds: int
    started_at: str
    ended_at: str

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_exact(owner, "context", self.context, ValidationRunTimingContext)
        _require_exact(
            owner,
            "configuration",
            self.configuration,
            ValidationConfiguration,
        )
        _require_non_empty(owner, "target", self.target)
        _require_integer(owner, "status", self.status, minimum=-(2**63))
        _require_integer(owner, "elapsed_seconds", self.elapsed_seconds, minimum=0)
        _require_non_empty(owner, "started_at", self.started_at)
        _require_non_empty(owner, "ended_at", self.ended_at)

    def timing_fields(self) -> Mapping[str, ValidationTimingScalar]:
        return merge_validation_timing_fields(
            {"kind": "target_timing"},
            self.context,
            {
                "target": self.target,
                "status": self.status,
                "elapsed_seconds": self.elapsed_seconds,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
            },
            self.configuration,
        )


class ValidationRunLifecycle(StrEnum):
    """Terminal meaning of one validate-runner timing summary."""

    COMPLETED = "completed"
    CAPTURE_FAILED = "capture-failed"


@dataclass(frozen=True, slots=True)
class ValidationRunTimingSummary:
    """Completed total timing for one validate-runner invocation."""

    context: ValidationRunTimingContext
    configuration: ValidationConfiguration
    lifecycle: ValidationRunLifecycle
    exit_code: int
    child_exit_code: int
    total_elapsed_seconds: float
    recorded_at: str
    envelope: ValidationTimingEnvelope

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_exact(owner, "context", self.context, ValidationRunTimingContext)
        _require_exact(
            owner,
            "configuration",
            self.configuration,
            ValidationConfiguration,
        )
        _require_exact(owner, "lifecycle", self.lifecycle, ValidationRunLifecycle)
        _require_integer(owner, "exit_code", self.exit_code, minimum=-(2**63))
        _require_integer(
            owner,
            "child_exit_code",
            self.child_exit_code,
            minimum=-(2**63),
        )
        if (
            self.lifecycle is ValidationRunLifecycle.COMPLETED
            and self.exit_code != self.child_exit_code
        ):
            raise ValueError(
                "completed validation lifecycle must preserve the child exit code"
            )
        if (
            self.lifecycle is ValidationRunLifecycle.CAPTURE_FAILED
            and self.exit_code == 0
        ):
            raise ValueError("failed validation capture cannot record exit code zero")
        _require_float(
            owner,
            "total_elapsed_seconds",
            self.total_elapsed_seconds,
            minimum=0.0,
        )
        _require_non_empty(owner, "recorded_at", self.recorded_at)
        _require_exact(owner, "envelope", self.envelope, ValidationTimingEnvelope)

    def timing_fields(self) -> Mapping[str, ValidationTimingScalar]:
        return merge_validation_timing_fields(
            {"kind": "run_summary"},
            self.context,
            {
                "exit_code": self.exit_code,
                "child_exit_code": self.child_exit_code,
                "lifecycle": self.lifecycle.value,
                "total_elapsed_seconds": self.total_elapsed_seconds,
                "recorded_at": self.recorded_at,
            },
            self.envelope,
            self.configuration,
        )


@dataclass(frozen=True, slots=True)
class ValidationSwapUsage:
    """One host swap observation in MiB."""

    total_mb: float
    used_mb: float
    free_mb: float

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_float(owner, "total_mb", self.total_mb, minimum=0.0)
        _require_float(owner, "used_mb", self.used_mb, minimum=0.0)
        _require_float(owner, "free_mb", self.free_mb, minimum=0.0)
        if self.used_mb + self.free_mb > self.total_mb + 0.01:
            raise ValueError(f"{owner} used plus free swap exceeds total swap")

    def timing_fields(self) -> Mapping[str, ValidationTimingScalar]:
        return {
            "swap_total_mb": self.total_mb,
            "swap_used_mb": self.used_mb,
            "swap_free_mb": self.free_mb,
        }


@dataclass(frozen=True, slots=True)
class ValidationDiskObservation:
    """Cumulative and optional interval disk-I/O observation."""

    transfers_total: float
    megabytes_total: float
    transfers_delta: float | None
    megabytes_delta: float | None

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_float(owner, "transfers_total", self.transfers_total, minimum=0.0)
        _require_float(owner, "megabytes_total", self.megabytes_total, minimum=0.0)
        _require_optional_float(
            owner,
            "transfers_delta",
            self.transfers_delta,
            minimum=0.0,
        )
        _require_optional_float(
            owner,
            "megabytes_delta",
            self.megabytes_delta,
            minimum=0.0,
        )

    def timing_fields(self) -> Mapping[str, ValidationTimingScalar]:
        return {
            "disk_xfrs_total": self.transfers_total,
            "disk_mb_total": self.megabytes_total,
            "disk_xfrs_delta": self.transfers_delta,
            "disk_mb_delta": self.megabytes_delta,
        }


@dataclass(frozen=True, slots=True)
class ValidationResourceSample:
    """One typed host-pressure observation during a validation run."""

    recorded_at: str
    loadavg_1m: float | None
    loadavg_5m: float | None
    loadavg_15m: float | None
    memory_free_percent: int | None
    swap: ValidationSwapUsage | None
    disk: ValidationDiskObservation | None

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_non_empty(owner, "recorded_at", self.recorded_at)
        for field_name, value in (
            ("loadavg_1m", self.loadavg_1m),
            ("loadavg_5m", self.loadavg_5m),
            ("loadavg_15m", self.loadavg_15m),
        ):
            _require_optional_float(owner, field_name, value, minimum=0.0)
        _require_optional_integer(
            owner,
            "memory_free_percent",
            self.memory_free_percent,
            minimum=0,
        )
        if self.memory_free_percent is not None and self.memory_free_percent > 100:
            raise ValueError(f"{owner}.memory_free_percent must be <= 100")
        if self.swap is not None:
            _require_exact(owner, "swap", self.swap, ValidationSwapUsage)
        if self.disk is not None:
            _require_exact(owner, "disk", self.disk, ValidationDiskObservation)

    def timing_fields(self) -> Mapping[str, ValidationTimingScalar]:
        base: SerializedValidationTiming = {
            "recorded_at": self.recorded_at,
            "loadavg_1m": self.loadavg_1m,
            "loadavg_5m": self.loadavg_5m,
            "loadavg_15m": self.loadavg_15m,
            "memory_free_percent": self.memory_free_percent,
        }
        payloads: list[
            ValidationTimingPayload | Mapping[str, ValidationTimingScalar]
        ] = [base]
        if self.swap is not None:
            payloads.append(self.swap)
        if self.disk is not None:
            payloads.append(self.disk)
        return merge_validation_timing_fields(*payloads)


@dataclass(frozen=True, slots=True)
class ValidationResourceTiming:
    """A resource sample associated with one validate-runner invocation."""

    context: ValidationRunTimingContext
    configuration: ValidationConfiguration
    sample: ValidationResourceSample

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_exact(owner, "context", self.context, ValidationRunTimingContext)
        _require_exact(
            owner,
            "configuration",
            self.configuration,
            ValidationConfiguration,
        )
        _require_exact(owner, "sample", self.sample, ValidationResourceSample)

    def timing_fields(self) -> Mapping[str, ValidationTimingScalar]:
        return merge_validation_timing_fields(
            {"kind": "resource_sample"},
            self.context,
            self.sample,
            self.configuration,
        )


@dataclass(frozen=True, slots=True)
class PublishGateTimingSummary:
    """Typed outer timing outcome for the publish validation gate."""

    gate: str
    command: str | None
    timeout_seconds: int
    head_sha: str | None
    cache_lookup: str
    cache_hit: bool
    allowed: bool
    reason: str
    record_passed: bool | None
    record_exit_code: int | None
    record_timed_out: bool | None
    envelope: ValidationTimingEnvelope

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_non_empty(owner, "gate", self.gate)
        _require_optional_non_empty(owner, "command", self.command)
        _require_integer(owner, "timeout_seconds", self.timeout_seconds, minimum=0)
        _require_optional_non_empty(owner, "head_sha", self.head_sha)
        _require_non_empty(owner, "cache_lookup", self.cache_lookup)
        _require_exact(owner, "cache_hit", self.cache_hit, bool)
        _require_exact(owner, "allowed", self.allowed, bool)
        _require_non_empty(owner, "reason", self.reason)
        _require_optional_boolean(owner, "record_passed", self.record_passed)
        _require_optional_integer(
            owner,
            "record_exit_code",
            self.record_exit_code,
            minimum=-(2**63),
        )
        _require_optional_boolean(owner, "record_timed_out", self.record_timed_out)
        _require_exact(owner, "envelope", self.envelope, ValidationTimingEnvelope)

    def timing_fields(self) -> Mapping[str, ValidationTimingScalar]:
        return merge_validation_timing_fields(
            {
                "kind": "validation_gate_summary",
                "gate": self.gate,
                "command": self.command,
                "timeout_seconds": self.timeout_seconds,
                "head_sha": self.head_sha,
                "cache_lookup": self.cache_lookup,
                "cache_hit": self.cache_hit,
                "allowed": self.allowed,
                "reason": self.reason,
                "record_passed": self.record_passed,
                "record_exit_code": self.record_exit_code,
                "record_timed_out": self.record_timed_out,
            },
            self.envelope,
        )


@dataclass(frozen=True, slots=True)
class PrepushGateTimingSummary:
    """Typed outer timing outcome for one pre-push check."""

    head_sha: str | None
    command: str | None
    timeout_seconds: int
    dirty_check: str
    dirty_only: bool
    dirty_elapsed_seconds: float | None
    dirty_exit_code: int | None
    validation_elapsed_seconds: float | None
    validation_cache_hit: bool | None
    validation_allowed: bool | None
    validation_reason: str | None
    validation_record_exit_code: int | None
    validation_record_timed_out: bool | None
    final_exit_code: int | None
    phase: str
    error_type: str | None
    envelope: ValidationTimingEnvelope

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_optional_non_empty(owner, "head_sha", self.head_sha)
        _require_optional_non_empty(owner, "command", self.command)
        _require_integer(owner, "timeout_seconds", self.timeout_seconds, minimum=0)
        _require_non_empty(owner, "dirty_check", self.dirty_check)
        _require_exact(owner, "dirty_only", self.dirty_only, bool)
        _require_optional_float(
            owner,
            "dirty_elapsed_seconds",
            self.dirty_elapsed_seconds,
            minimum=0.0,
        )
        _require_optional_integer(
            owner,
            "dirty_exit_code",
            self.dirty_exit_code,
            minimum=-(2**63),
        )
        _require_optional_float(
            owner,
            "validation_elapsed_seconds",
            self.validation_elapsed_seconds,
            minimum=0.0,
        )
        _require_optional_boolean(
            owner,
            "validation_cache_hit",
            self.validation_cache_hit,
        )
        _require_optional_boolean(owner, "validation_allowed", self.validation_allowed)
        _require_optional_non_empty(owner, "validation_reason", self.validation_reason)
        _require_optional_integer(
            owner,
            "validation_record_exit_code",
            self.validation_record_exit_code,
            minimum=-(2**63),
        )
        _require_optional_boolean(
            owner,
            "validation_record_timed_out",
            self.validation_record_timed_out,
        )
        _require_optional_integer(
            owner,
            "final_exit_code",
            self.final_exit_code,
            minimum=-(2**63),
        )
        _require_non_empty(owner, "phase", self.phase)
        _require_optional_non_empty(owner, "error_type", self.error_type)
        _require_exact(owner, "envelope", self.envelope, ValidationTimingEnvelope)

    def timing_fields(self) -> Mapping[str, ValidationTimingScalar]:
        return merge_validation_timing_fields(
            {
                "kind": "prepush_gate_summary",
                "head_sha": self.head_sha,
                "command": self.command,
                "timeout_seconds": self.timeout_seconds,
                "dirty_check": self.dirty_check,
                "dirty_only": self.dirty_only,
                "dirty_elapsed_seconds": self.dirty_elapsed_seconds,
                "dirty_exit_code": self.dirty_exit_code,
                "validation_elapsed_seconds": self.validation_elapsed_seconds,
                "validation_cache_hit": self.validation_cache_hit,
                "validation_allowed": self.validation_allowed,
                "validation_reason": self.validation_reason,
                "validation_record_exit_code": self.validation_record_exit_code,
                "validation_record_timed_out": self.validation_record_timed_out,
                "final_exit_code": self.final_exit_code,
                "phase": self.phase,
                "error_type": self.error_type,
            },
            self.envelope,
        )
