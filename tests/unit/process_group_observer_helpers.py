"""Typed process-observation test adapters."""

from __future__ import annotations

from issue_orchestrator.domain.process_group import (
    ProcessBirthIdentity,
    ProcessGroupAbsent,
    ProcessGroupObservation,
    ProcessIdentityAbsent,
    ProcessIdentityObservation,
    ProcessIdentityPermissionDenied,
    ProcessIdentityPresent,
    ProcessSessionLeaderAbsent,
    ProcessSessionLeaderPermissionDenied,
    ProcessSessionLeaderPresent,
    ProcessSessionLeaderStale,
    ProcessSessionObservation,
)


class RecordingProcessGroupObserver:
    """Return explicit observations while recording every requested identity."""

    def __init__(
        self,
        *,
        process_observation: ProcessIdentityObservation = ProcessIdentityAbsent(),
        group_observation: ProcessGroupObservation = ProcessGroupAbsent(),
    ) -> None:
        self._process_observation = process_observation
        self._group_observation = group_observation
        self.process_ids: list[int] = []
        self.process_group_ids: list[int] = []

    def observe_process(self, process_id: int) -> ProcessIdentityObservation:
        self.process_ids.append(process_id)
        return self._process_observation

    def observe_group(self, process_group_id: int) -> ProcessGroupObservation:
        self.process_group_ids.append(process_group_id)
        return self._group_observation

    def observe_session(
        self,
        process_id: int,
        expected_birth_identity: ProcessBirthIdentity,
    ) -> ProcessSessionObservation:
        self.process_ids.append(process_id)
        if type(self._process_observation) is ProcessIdentityAbsent:
            return ProcessSessionLeaderAbsent()
        if type(self._process_observation) is ProcessIdentityPermissionDenied:
            return ProcessSessionLeaderPermissionDenied(
                self._process_observation.detail
            )
        if type(self._process_observation) is not ProcessIdentityPresent:
            raise AssertionError("process identity observation is a closed union")
        if self._process_observation.birth_identity != expected_birth_identity:
            return ProcessSessionLeaderStale(self._process_observation)
        self.process_group_ids.append(process_id)
        return ProcessSessionLeaderPresent(
            self._process_observation,
            self._group_observation,
        )
