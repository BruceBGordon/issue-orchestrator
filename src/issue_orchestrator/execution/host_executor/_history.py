# pyright: strict
"""Bounded resource history adapter for repository-scoped work."""

from __future__ import annotations

import fcntl
import hashlib
import os
import uuid
from pathlib import Path

from pydantic import ValidationError

from ...control.executor_admission import ExecutorResourceObservation
from ...control.executor_admission import ExecutorWorkDemandEstimator
from ...domain.executor import ExecutorWorkKey
from ...domain.executor_monitoring import (
    ExecutorExcludedLearningHistory,
    ExecutorLearnedWork,
    ExecutorLearningSnapshot,
    ExecutorRepositoryReference,
)
from ._contracts import ResourceObservationRecord, WorkHistoryRecord
from ._types import ExecutorWorkIdentity, RecordedExecutorObservation


_MAX_OBSERVATIONS = 24


class ExecutorWorkHistoryStore:
    """Own strict, bounded observations for each repository work identity."""

    def __init__(self, history_dir: Path) -> None:
        self._history_dir = history_dir

    def successful_resources(
        self,
        identity: ExecutorWorkIdentity,
    ) -> tuple[ExecutorResourceObservation, ...]:
        """Return successful resource observations in recording order."""
        return tuple(
            observation.resources
            for observation in self._observations(identity)
            if observation.exit_code == 0
        )

    def record_successful(
        self,
        identity: ExecutorWorkIdentity,
        observation: RecordedExecutorObservation,
    ) -> None:
        """Retain one successful sample without mixing in failure evidence."""
        if observation.exit_code != 0:
            raise ValueError(
                "ExecutorWorkHistoryStore records only successful observations"
            )
        self._history_dir.mkdir(parents=True, exist_ok=True)
        path = self._profile_path(identity)
        lock_path = path.with_suffix(".lock")
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            existing = tuple(
                item for item in self._observations(identity) if item.exit_code == 0
            )
            bounded = (*existing, observation)[-_MAX_OBSERVATIONS:]
            record = WorkHistoryRecord(
                repository_key=identity.repository.key,
                repository_label=identity.repository.label,
                work_key=identity.work_key.value,
                observations=tuple(
                    ResourceObservationRecord.from_domain(item) for item in bounded
                ),
            )
            self._write_record(path, record)

    def snapshot(
        self,
        demand_estimator: ExecutorWorkDemandEstimator,
    ) -> ExecutorLearningSnapshot:
        """Return a canonical, fail-fast view of every retained profile."""
        records = tuple(
            self._read_record(path) for path in sorted(self._history_dir.glob("*.json"))
        )
        sorted_records = tuple(
            sorted(records, key=lambda item: (item.repository_key, item.work_key))
        )
        learned_work = tuple(
            self._learned_work(record, demand_estimator)
            for record in sorted_records
            if any(observation.exit_code == 0 for observation in record.observations)
        )
        excluded_failure_history = tuple(
            self._excluded_failure_history(record)
            for record in sorted_records
            if any(observation.exit_code != 0 for observation in record.observations)
        )
        fingerprint_input = "\n".join(
            record.model_dump_json() for record in sorted_records
        )
        return ExecutorLearningSnapshot(
            fingerprint_sha256=hashlib.sha256(
                fingerprint_input.encode("utf-8")
            ).hexdigest(),
            successful_observation_count=sum(
                item.successful_observation_count for item in learned_work
            ),
            learned_work=learned_work,
            excluded_failure_history=excluded_failure_history,
        )

    def _observations(
        self,
        identity: ExecutorWorkIdentity,
    ) -> tuple[RecordedExecutorObservation, ...]:
        path = self._profile_path(identity)
        if not path.exists():
            return ()
        record = self._read_record(path)
        if (
            record.repository_key != identity.repository.key
            or record.work_key != identity.work_key.value
        ):
            raise RuntimeError(f"executor work history identity mismatch: {path}")
        return tuple(observation.to_domain() for observation in record.observations)

    def _profile_path(self, identity: ExecutorWorkIdentity) -> Path:
        digest = hashlib.sha256(
            f"{identity.repository.key}\0{identity.work_key.value}".encode()
        ).hexdigest()
        return self._history_dir / f"{digest}.json"

    @staticmethod
    def _learned_work(
        record: WorkHistoryRecord,
        demand_estimator: ExecutorWorkDemandEstimator,
    ) -> ExecutorLearnedWork:
        observations = tuple(
            observation.to_domain().resources
            for observation in record.observations
            if observation.exit_code == 0
        )
        return ExecutorLearnedWork(
            repository=ExecutorRepositoryReference(
                key=record.repository_key,
                label=record.repository_label,
            ),
            work_key=ExecutorWorkKey(record.work_key),
            successful_observation_count=len(observations),
            estimated_cores_per_concurrency=(
                demand_estimator.estimate(observations).cores_per_concurrency
            ),
        )

    @staticmethod
    def _excluded_failure_history(
        record: WorkHistoryRecord,
    ) -> ExecutorExcludedLearningHistory:
        return ExecutorExcludedLearningHistory(
            repository=ExecutorRepositoryReference(
                key=record.repository_key,
                label=record.repository_label,
            ),
            work_key=ExecutorWorkKey(record.work_key),
            failed_observation_count=sum(
                observation.exit_code != 0 for observation in record.observations
            ),
        )

    @staticmethod
    def _read_record(path: Path) -> WorkHistoryRecord:
        try:
            return WorkHistoryRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            raise RuntimeError(f"invalid executor work history: {path}") from exc

    @staticmethod
    def _write_record(path: Path, record: WorkHistoryRecord) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}")
        temporary.write_text(record.model_dump_json() + "\n", encoding="utf-8")
        os.replace(temporary, path)
