# pyright: strict
"""Bounded resource history adapter for repository-scoped work."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import ValidationError

from ...control.executor_admission import ExecutorResourceObservation
from ...control.executor_admission import ExecutorWorkDemandEstimator
from ...domain.executor import ExecutorHistoryRetentionPolicy, ExecutorWorkKey
from ...domain.executor_monitoring import (
    ExecutorAllRepositories,
    ExecutorExcludedLearningHistory,
    ExecutorLearnedWork,
    ExecutorLearningSnapshot,
    ExecutorRepositoryLabelFilter,
    ExecutorRepositoryReference,
    ExecutorStatusQuery,
)
from ...ports.executor_history_lock import ExecutorHistoryRetentionLock
from ._contracts import ResourceObservationRecord, WorkHistoryRecord
from ._types import ExecutorWorkIdentity, RecordedExecutorObservation
from ..atomic_record_store import AtomicRecordStore


class ExecutorWorkHistoryStore:
    """Own strict, bounded observations for each repository work identity."""

    def __init__(
        self,
        history_dir: Path,
        retention_policy: ExecutorHistoryRetentionPolicy,
        retention_lock: ExecutorHistoryRetentionLock,
        atomic_records: AtomicRecordStore,
    ) -> None:
        if type(retention_policy) is not ExecutorHistoryRetentionPolicy:
            raise ValueError(
                "ExecutorWorkHistoryStore.retention_policy must be an "
                "ExecutorHistoryRetentionPolicy"
            )
        self._history_dir = history_dir
        self._retention_policy = retention_policy
        self._retention_lock = retention_lock
        self._atomic_records = atomic_records

    def successful_resources(
        self,
        identity: ExecutorWorkIdentity,
    ) -> tuple[ExecutorResourceObservation, ...]:
        """Return successful resource observations in recording order."""
        self._history_dir.mkdir(parents=True, exist_ok=True)
        with self._retention_lock.shared():
            self._atomic_records.prune_crash_remnants()
            return tuple(
                observation.resources
                for observation in self._observations_unlocked(identity)
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
        with self._retention_lock.exclusive():
            existing = tuple(
                item
                for item in self._observations_unlocked(identity)
                if item.exit_code == 0
            )
            bounded = (*existing, observation)[
                -self._retention_policy.maximum_observations_per_profile :
            ]
            record = WorkHistoryRecord(
                repository_key=identity.repository.key,
                repository_label=identity.repository.label,
                work_key=identity.work_key.value,
                observations=tuple(
                    ResourceObservationRecord.from_domain(item) for item in bounded
                ),
            )
            self._atomic_records.write(path, record)
            self._prune_profiles()

    def snapshot(
        self,
        demand_estimator: ExecutorWorkDemandEstimator,
        query: ExecutorStatusQuery,
    ) -> ExecutorLearningSnapshot:
        """Return a canonical bounded page plus global aggregate evidence."""
        if type(query) is not ExecutorStatusQuery:
            raise ValueError(
                "ExecutorWorkHistoryStore.snapshot requires ExecutorStatusQuery"
            )
        self._history_dir.mkdir(parents=True, exist_ok=True)
        with self._retention_lock.shared():
            self._atomic_records.prune_crash_remnants()
            records = tuple(
                self._read_record(path)
                for path in sorted(self._history_dir.glob("*.json"))
            )
        sorted_records = tuple(
            sorted(records, key=lambda item: (item.repository_key, item.work_key))
        )
        selected_records = self._select_records(sorted_records, query)
        page_records = selected_records[query.offset : query.offset + query.limit]
        learned_work = tuple(
            self._learned_work(record, demand_estimator)
            for record in page_records
            if any(observation.exit_code == 0 for observation in record.observations)
        )
        excluded_failure_history = tuple(
            self._excluded_failure_history(record)
            for record in page_records
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
                sum(observation.exit_code == 0 for observation in record.observations)
                for record in sorted_records
            ),
            failed_observation_count=sum(
                sum(observation.exit_code != 0 for observation in record.observations)
                for record in sorted_records
            ),
            total_profile_count=len(sorted_records),
            matching_profile_count=len(selected_records),
            page_offset=query.offset,
            learned_work=learned_work,
            excluded_failure_history=excluded_failure_history,
        )

    @staticmethod
    def _select_records(
        records: tuple[WorkHistoryRecord, ...],
        query: ExecutorStatusQuery,
    ) -> tuple[WorkHistoryRecord, ...]:
        selection = query.repository_selection
        if type(selection) is ExecutorAllRepositories:
            return records
        if type(selection) is ExecutorRepositoryLabelFilter:
            return tuple(
                record
                for record in records
                if record.repository_label == selection.repository_label
            )
        raise AssertionError("ExecutorStatusQuery repository selection was validated")

    def _prune_profiles(self) -> None:
        profiles = tuple(self._history_dir.glob("*.json"))
        excess = len(profiles) - self._retention_policy.maximum_profiles
        if excess <= 0:
            return
        oldest_first = sorted(
            profiles,
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        for path in oldest_first[:excess]:
            self._atomic_records.delete(path)

    def _observations_unlocked(
        self,
        identity: ExecutorWorkIdentity,
    ) -> tuple[RecordedExecutorObservation, ...]:
        """Read one profile while the caller holds ``retention.lock``."""
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
