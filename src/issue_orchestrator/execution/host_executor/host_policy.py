# pyright: strict
"""Machine-local executor discovery and aggressiveness persistence."""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_state_path
from pydantic import ValidationError

from ...domain.executor import (
    ExecutorAggressiveness,
    ExecutorPolicy,
    ExecutorPolicyChange,
    ExecutorPolicySource,
)
from ._contracts import PersistedPolicyRecord
from ..atomic_record_store import AtomicRecordStore
from ..posix_file_lock import (
    PosixFileLockAcquisition,
    PosixFileLockFilePresence,
    PosixFileLockMode,
    PosixFileLockOwner,
    PosixFileLockSpecification,
)


EXECUTOR_POOL_DIR_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR"
EXECUTOR_AGGRESSIVENESS_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_AGGRESSIVENESS_PERCENT"

_DEFAULT_AGGRESSIVENESS_PERCENT = 100


def _parse_integer_environment(name: str, raw: str) -> int:
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if str(parsed) != raw:
        raise ValueError(f"{name} must be a base-ten integer without padding")
    return parsed


def detected_executor_cpu_count() -> int:
    """Return the host CPU count or fail when the OS cannot provide it."""
    detected = os.cpu_count()
    if detected is None:
        raise RuntimeError("cannot determine host CPU count for executor")
    if detected < 1:
        raise RuntimeError("host CPU count must be positive")
    return detected


def default_executor_pool_dir() -> Path:
    """Return the per-user machine-local pool shared by every managed repo."""
    override = os.environ.get(EXECUTOR_POOL_DIR_ENV)
    if override is not None:
        if not override:
            raise ValueError(f"{EXECUTOR_POOL_DIR_ENV} must not be empty")
        return Path(override).expanduser().resolve()
    return user_state_path("issue-orchestrator") / "executor-pools" / "host-v2"


class ExecutorPolicyStore:
    """Own the persisted machine policy and its environment override."""

    def __init__(
        self,
        pool_dir: Path,
        atomic_records: AtomicRecordStore,
    ) -> None:
        self._pool_dir = pool_dir
        self._path = pool_dir / "policy.json"
        self._atomic_records = atomic_records
        self._file_locks = PosixFileLockOwner()
        self._policy_lock = PosixFileLockSpecification(
            (pool_dir / "policy.lock").resolve(),
            PosixFileLockMode.EXCLUSIVE,
            PosixFileLockAcquisition.BLOCKING,
            PosixFileLockFilePresence.CREATE_IF_MISSING,
        )

    def effective(self) -> ExecutorPolicy:
        self._atomic_records.prune_crash_remnants()
        environment = os.environ.get(EXECUTOR_AGGRESSIVENESS_ENV)
        if environment is not None:
            percent = _parse_integer_environment(
                EXECUTOR_AGGRESSIVENESS_ENV,
                environment,
            )
            return ExecutorPolicy(
                ExecutorAggressiveness(percent),
                ExecutorPolicySource.ENVIRONMENT,
            )
        if not self._path.exists():
            return ExecutorPolicy(
                ExecutorAggressiveness(_DEFAULT_AGGRESSIVENESS_PERCENT),
                ExecutorPolicySource.DEFAULT,
            )
        return self._read_persisted().to_domain(ExecutorPolicySource.PERSISTED)

    def configure(
        self,
        aggressiveness: ExecutorAggressiveness,
    ) -> ExecutorPolicyChange:
        self._pool_dir.mkdir(parents=True, exist_ok=True)
        record = PersistedPolicyRecord(aggressiveness_percent=aggressiveness.percent)
        with self._file_locks.hold(self._policy_lock):
            self._atomic_records.write(self._path, record)
        saved = ExecutorPolicy(aggressiveness, ExecutorPolicySource.PERSISTED)
        return ExecutorPolicyChange(saved=saved, effective=self.effective())

    def _read_persisted(self) -> PersistedPolicyRecord:
        try:
            return PersistedPolicyRecord.model_validate_json(
                self._path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            raise RuntimeError(f"invalid host executor policy: {self._path}") from exc
