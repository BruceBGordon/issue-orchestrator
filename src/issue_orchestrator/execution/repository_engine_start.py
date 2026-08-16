"""Single owner for starting a repository engine across command surfaces."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..contracts.repository_engine import RepositoryEngineStartPayload
from ..domain.repository_launch_selection import RepositoryLaunchSelection
from ..ports.repository_engine_supervisor import (
    RUNNING_SUPERVISOR_STATE,
    SupervisorOps,
)
from .control_center_runtime import (
    annotate_identity_mismatch,
    build_repo_identity,
    inspect_orchestrator_at_port,
    inspect_repository_orchestrator_ownership,
    is_shutdown_complete,
    live_repository_engine_statuses,
)

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..infra.launcher import LaunchResult
    from ..infra.repo_identity import RepoIdentity
    from ..ports.repository_engine_supervisor import SupervisorStatus


@dataclass(frozen=True, slots=True)
class RepositoryEngineStartRequest:
    repo_root: Path
    selection: RepositoryLaunchSelection
    config_path: Path | None = None
    instance_id: str | None = None
    port: int | None = None
    force_restart: bool = False
    start_paused: bool = False
    actor: str = "control-center"


@dataclass(frozen=True, slots=True)
class RepositoryEngineStartResult:
    payload: RepositoryEngineStartPayload
    status_code: int = 200

    @property
    def succeeded(self) -> bool:
        return self.status_code == 200

    @property
    def orphaned_running(self) -> bool:
        return (
            self.status_code == 409 and self.payload.get("error") == "orphaned_running"
        )


@dataclass(frozen=True, slots=True)
class ExistingRepositoryEngineOwnership:
    """One coherent snapshot of tracked and port-probed runtime ownership."""

    tracked: tuple[SupervisorStatus, ...]
    tracked_ports: frozenset[int]
    tracked_details: dict[int, dict[str, Any] | None]
    orphan_matching: tuple[dict[str, Any], ...]
    orphan_conflicting: tuple[dict[str, Any], ...]
    matching: tuple[dict[str, Any], ...]
    conflicting_orphans: tuple[dict[str, Any], ...]
    tracked_matching: tuple[SupervisorStatus, ...]
    tracked_conflicting: tuple[SupervisorStatus, ...]


def record_repository_engine_launch(
    repo_root: Path,
    selection: RepositoryLaunchSelection,
) -> None:
    """Persist the selection only after runtime ownership is published."""
    from ..infra.repo_registry import record_launched_selection

    record_launched_selection(repo_root, selection)


def _summarize_doctor_failures(doctor: Any) -> str:
    failed = [check for check in doctor.checks if check.status == "error"]
    if not failed:
        return "Pre-flight checks failed"
    parts = []
    for check in failed[:2]:
        name = (getattr(check, "name", "check") or "check").strip()
        detail = (getattr(check, "detail", "") or "").strip()
        parts.append(f"{name}: {detail}" if detail else name)
    if len(failed) > 2:
        parts.append(f"+{len(failed) - 2} more")
    return "Pre-flight checks failed: " + "; ".join(parts)


class StartRepositoryEngineCommand:
    """Enforce ownership, preflight, launch, and registry attribution once."""

    def __init__(self, supervisor: SupervisorOps) -> None:
        self._supervisor = supervisor

    def execute(
        self,
        request: RepositoryEngineStartRequest,
    ) -> RepositoryEngineStartResult:
        from ..infra.repo_lock import (
            RepositoryLifecycleBusy,
            repository_lifecycle_mutation,
        )

        try:
            with repository_lifecycle_mutation(request.repo_root):
                return self._execute_guarded(request)
        except RepositoryLifecycleBusy:
            return RepositoryEngineStartResult(
                {
                    "error": "lifecycle_busy",
                    "detail": "Another repository lifecycle change is in progress.",
                },
                409,
            )

    def _execute_guarded(
        self,
        request: RepositoryEngineStartRequest,
    ) -> RepositoryEngineStartResult:
        config, load_failure = self._load_config(request)
        if load_failure is not None:
            return load_failure
        assert config is not None
        ownership_result = self._resolve_existing_ownership(
            request,
            config.config_fingerprint,
        )
        if ownership_result is not None:
            return ownership_result

        from ..infra.launcher import launch_subprocess

        expected_identity = build_repo_identity(request.repo_root)
        launch_result = launch_subprocess(
            repo_root=request.repo_root,
            config=config,
            config_name=request.selection.config.value,
            mode=request.selection.mode.value,
            instance_id=request.instance_id,
            port=request.port,
            supervisor_ops=self._supervisor,
            expected_identity=expected_identity.to_dict(),
            start_paused=request.start_paused,
        )
        return self._complete_launch(
            request,
            config,
            expected_identity,
            launch_result,
        )

    @staticmethod
    def _load_config(
        request: RepositoryEngineStartRequest,
    ) -> tuple[Config | None, RepositoryEngineStartResult | None]:
        from ..infra.config import Config, get_config_path
        from ..infra.config_paths import require_engine_launch_config_path

        selected_config_path = get_config_path(
            request.repo_root,
            request.selection.config.value,
            request.selection.mode,
        )
        config_path = request.config_path or selected_config_path
        try:
            resolved_config_path = require_engine_launch_config_path(config_path)
            resolved_selected_path = require_engine_launch_config_path(
                selected_config_path
            )
        except ValueError as exc:
            return None, RepositoryEngineStartResult(
                {"error": "invalid_config_path", "detail": str(exc)}, 400
            )
        if resolved_config_path != resolved_selected_path:
            return None, RepositoryEngineStartResult(
                {
                    "error": "configuration_repository_mismatch",
                    "detail": (
                        "Explicit config path is not the selected mode/config "
                        "inside the requested repository."
                    ),
                },
                400,
            )
        try:
            config = Config.load(resolved_config_path)
            if (
                request.config_path is not None
                and config.launch_selection != request.selection
            ):
                return None, RepositoryEngineStartResult(
                    {
                        "error": "configuration_selection_mismatch",
                        "detail": "Explicit config path does not match the requested mode/config.",
                    },
                    400,
                )
        except FileNotFoundError as exc:
            return None, RepositoryEngineStartResult(
                {"error": "config_not_found", "detail": str(exc)},
                404,
            )
        return config, None

    def _complete_launch(
        self,
        request: RepositoryEngineStartRequest,
        config: Config,
        expected_identity: RepoIdentity,
        launch_result: LaunchResult,
    ) -> RepositoryEngineStartResult:
        from ..infra.launcher import launch_subprocess

        restarted = False
        outcome = launch_result.status
        if (
            outcome == "already_running"
            and launch_result.supervisor
            and is_shutdown_complete(launch_result.supervisor.get("port"))
        ):
            self._supervisor.stop(
                request.repo_root,
                reason="restart after shutdown-complete repository engine",
                actor=request.actor,
            )
            time.sleep(0.5)
            launch_result = launch_subprocess(
                repo_root=request.repo_root,
                config=config,
                config_name=request.selection.config.value,
                mode=request.selection.mode.value,
                instance_id=request.instance_id,
                port=request.port,
                supervisor_ops=self._supervisor,
                expected_identity=expected_identity.to_dict(),
                start_paused=request.start_paused,
            )
            restarted = True

        failure = self._launch_failure(launch_result)
        if failure is not None:
            return failure

        record_repository_engine_launch(request.repo_root, request.selection)
        from ..infra.launcher import LaunchStatus

        payload: RepositoryEngineStartPayload = {
            "status": "restarted" if restarted else "started",
            "launch_status": LaunchStatus.parse(launch_result.status).value,
            "repo_root": str(request.repo_root),
            "mode": request.selection.mode.value,
            "config_name": request.selection.config.value,
            "config_fingerprint": config.config_fingerprint,
            "repo_identity": expected_identity.to_dict(),
            "doctor": launch_result.doctor.to_dict(),
        }
        if launch_result.supervisor:
            supervisor_data = launch_result.supervisor
            if isinstance(supervisor_data.get("pid"), int):
                payload["pid"] = supervisor_data["pid"]
            if "port" in supervisor_data and isinstance(
                supervisor_data["port"], (int, type(None))
            ):
                payload["port"] = supervisor_data["port"]
            if isinstance(supervisor_data.get("instances"), list):
                payload["instances"] = supervisor_data["instances"]
        return RepositoryEngineStartResult(payload)

    def _resolve_existing_ownership(
        self,
        request: RepositoryEngineStartRequest,
        expected_config_fingerprint: str,
    ) -> RepositoryEngineStartResult | None:
        ownership = self._observe_existing_ownership(
            request,
            expected_config_fingerprint,
        )
        conflict = self._configuration_conflict_result(
            request,
            expected_config_fingerprint,
            ownership,
        )
        if conflict is not None:
            return conflict
        if request.force_restart:
            return self._force_restart_existing(request, ownership)
        (
            tracked_matching,
            mismatch_result,
            stopped_mismatch,
        ) = self._remove_tracked_identity_mismatches(
            request,
            ownership.tracked_matching,
            ownership.tracked_details,
        )
        if mismatch_result is not None:
            return mismatch_result
        orphan_result = self._resolve_matching_ownership(
            request,
            ownership.matching,
            expected_config_fingerprint,
        )
        if orphan_result is not None:
            return orphan_result
        if stopped_mismatch:
            return None
        return self._resolve_tracked_ownership(
            request,
            tracked_matching,
            expected_config_fingerprint,
        )

    def _observe_existing_ownership(
        self,
        request: RepositoryEngineStartRequest,
        expected_config_fingerprint: str,
    ) -> ExistingRepositoryEngineOwnership:
        tracked = self._tracked_live_statuses(request)
        tracked_ports = frozenset(
            status.port for status in tracked if isinstance(status.port, int)
        )
        expected_identity = build_repo_identity(request.repo_root)
        probed = inspect_repository_orchestrator_ownership(
            request.repo_root,
            request.selection,
        )
        for detected in probed.all:
            annotate_identity_mismatch(
                detected,
                detected.get("info", {}),
                expected_identity,
            )
        probed_by_port = {detected["port"]: detected for detected in probed.all}
        tracked_details: dict[int, dict[str, Any] | None] = {}
        for status in tracked:
            port = status.port
            if not isinstance(port, int):
                continue
            tracked_details[port] = probed_by_port.get(port)
            if tracked_details[port] is None:
                tracked_details[port] = inspect_orchestrator_at_port(
                    request.repo_root,
                    port,
                    expected_identity=expected_identity,
                )
        orphan_matching = tuple(
            item for item in probed.matching if item.get("port") not in tracked_ports
        )
        orphan_conflicting = tuple(
            item for item in probed.conflicting if item.get("port") not in tracked_ports
        )
        matching = tuple(
            detected
            for detected in orphan_matching
            if detected.get("info", {}).get("config_fingerprint")
            == expected_config_fingerprint
        )
        conflicting_orphans = orphan_conflicting + tuple(
            detected for detected in orphan_matching if detected not in matching
        )
        tracked_matching = tuple(
            status
            for status in tracked
            if self._status_matches(
                status,
                request.selection,
                expected_config_fingerprint,
            )
        )
        tracked_conflicting = tuple(
            status for status in tracked if status not in tracked_matching
        )
        return ExistingRepositoryEngineOwnership(
            tracked=tracked,
            tracked_ports=tracked_ports,
            tracked_details=tracked_details,
            orphan_matching=orphan_matching,
            orphan_conflicting=orphan_conflicting,
            matching=matching,
            conflicting_orphans=conflicting_orphans,
            tracked_matching=tracked_matching,
            tracked_conflicting=tracked_conflicting,
        )

    @staticmethod
    def _configuration_conflict_result(
        request: RepositoryEngineStartRequest,
        expected_config_fingerprint: str,
        ownership: ExistingRepositoryEngineOwnership,
    ) -> RepositoryEngineStartResult | None:
        if request.force_restart or not (
            ownership.tracked_conflicting or ownership.conflicting_orphans
        ):
            return None
        return RepositoryEngineStartResult(
            {
                "error": "configuration_conflict",
                "detail": "A live Repository Engine owns a different configuration identity.",
                "requested": {
                    **request.selection.to_dict(),
                    "config_fingerprint": expected_config_fingerprint,
                },
                "active": [
                    {
                        "mode": status.configuration_mode,
                        "config_name": status.config_name,
                        "config_fingerprint": status.config_fingerprint,
                    }
                    for status in ownership.tracked_conflicting
                ]
                + [
                    {
                        **detected["active_selection"],
                        "config_fingerprint": detected.get("info", {}).get(
                            "config_fingerprint", ""
                        ),
                    }
                    for detected in ownership.conflicting_orphans
                ],
                "ports": [
                    status.port
                    for status in ownership.tracked_conflicting
                    if isinstance(status.port, int)
                ]
                + [detected["port"] for detected in ownership.conflicting_orphans],
            },
            409,
        )

    def _force_restart_existing(
        self,
        request: RepositoryEngineStartRequest,
        ownership: ExistingRepositoryEngineOwnership,
    ) -> RepositoryEngineStartResult | None:
        for tracked in ownership.tracked:
            stopped = self._supervisor.stop_tracked_instance(
                request.repo_root,
                tracked,
                reason="force_restart=true on repository engine start",
                actor=request.actor,
            )
            if not stopped:
                return RepositoryEngineStartResult(
                    {
                        "error": "stop_failed",
                        "detail": "Unable to stop a tracked orchestrator process.",
                        "ports": sorted(ownership.tracked_ports),
                    },
                    500,
                )
        for detected in ownership.orphan_matching + ownership.orphan_conflicting:
            stopped = self._supervisor.stop_by_port(
                detected["port"],
                force=True,
                reason="force_restart=true on repository engine start",
                actor=request.actor,
            )
            if not stopped:
                return RepositoryEngineStartResult(
                    {
                        "error": "stop_failed",
                        "detail": "Unable to stop existing orchestrator process.",
                        "port": detected["port"],
                    },
                    500,
                )
        return None

    def _remove_tracked_identity_mismatches(
        self,
        request: RepositoryEngineStartRequest,
        matching: tuple[SupervisorStatus, ...],
        details_by_port: dict[int, dict[str, Any] | None],
    ) -> tuple[
        tuple[SupervisorStatus, ...],
        RepositoryEngineStartResult | None,
        bool,
    ]:
        healthy: list[SupervisorStatus] = []
        stopped_mismatch = False
        for status in matching:
            port = status.port
            details = details_by_port.get(port) if isinstance(port, int) else None
            if details is None or not details.get("identity_mismatch"):
                healthy.append(status)
                continue
            stopped = self._supervisor.stop(
                request.repo_root,
                force=True,
                instance_id=status.instance_id,
                reason="engine identity mismatch detected on repository start",
                actor=request.actor,
            )
            if not stopped:
                return (
                    (),
                    RepositoryEngineStartResult(
                        {
                            "error": "engine_identity_mismatch",
                            "detail": "Mismatched engine detected and could not be stopped",
                            "port": port,
                        },
                        409,
                    ),
                    False,
                )
            stopped_mismatch = True
        return tuple(healthy), None, stopped_mismatch

    def _tracked_live_statuses(
        self,
        request: RepositoryEngineStartRequest,
    ) -> tuple[SupervisorStatus, ...]:
        return live_repository_engine_statuses(
            request.repo_root,
            self._supervisor,
            request.selection,
        )

    @staticmethod
    def _status_matches(
        status: SupervisorStatus,
        selection: RepositoryLaunchSelection,
        fingerprint: str,
    ) -> bool:
        return (
            status.configuration_mode == selection.mode.value
            and status.config_name == selection.config.value
            and status.config_fingerprint == fingerprint
        )

    def _resolve_tracked_ownership(
        self,
        request: RepositoryEngineStartRequest,
        matching: tuple[SupervisorStatus, ...],
        expected_config_fingerprint: str,
    ) -> RepositoryEngineStartResult | None:
        if request.instance_id is not None:
            matching = tuple(
                status
                for status in matching
                if status.instance_id == request.instance_id
            )
        if not matching:
            return None
        ports = sorted(
            status.port for status in matching if isinstance(status.port, int)
        )
        payload: RepositoryEngineStartPayload = {
            "error": "already_running",
            "status": RUNNING_SUPERVISOR_STATE,
            "repo_root": str(request.repo_root),
            "mode": request.selection.mode.value,
            "config_name": request.selection.config.value,
            "config_fingerprint": expected_config_fingerprint,
            "ports": ports,
            "instances": [status.to_dict() for status in matching],
        }
        if len(ports) == 1:
            payload["port"] = ports[0]
        return RepositoryEngineStartResult(payload, 409)

    def _resolve_matching_ownership(
        self,
        request: RepositoryEngineStartRequest,
        matching: tuple[dict[str, Any], ...],
        expected_config_fingerprint: str,
    ) -> RepositoryEngineStartResult | None:
        healthy: list[dict[str, Any]] = []
        for detected in matching:
            if not detected.get("identity_mismatch"):
                healthy.append(detected)
                continue
            stopped = self._supervisor.stop_by_port(
                detected["port"],
                force=True,
                reason="engine identity mismatch detected on repository start",
                actor=request.actor,
            )
            if not stopped:
                return RepositoryEngineStartResult(
                    {
                        "error": "engine_identity_mismatch",
                        "detail": "Mismatched engine detected and could not be stopped",
                        "port": detected["port"],
                    },
                    409,
                )
        if not healthy:
            return None
        if request.instance_id is not None:
            target = self._supervisor.status(
                request.repo_root,
                instance_id=request.instance_id,
            )
            if target.state != "running":
                return None
        detected = healthy[0]
        return RepositoryEngineStartResult(
            {
                "error": "orphaned_running",
                "status": RUNNING_SUPERVISOR_STATE,
                "port": detected["port"],
                "repo_root": str(request.repo_root),
                "mode": request.selection.mode.value,
                "config_name": request.selection.config.value,
                "config_fingerprint": expected_config_fingerprint,
                "health": detected.get("health", "unknown"),
                "tick_age_seconds": detected.get("tick_age_seconds", 0.0),
            },
            409,
        )

    @staticmethod
    def _launch_failure(launch_result: Any) -> RepositoryEngineStartResult | None:
        from ..infra.launcher import LaunchStatus

        outcome = LaunchStatus.parse(launch_result.status)
        if outcome is LaunchStatus.DOCTOR_ERROR:
            return RepositoryEngineStartResult(
                {
                    "error": "doctor_failed",
                    "detail": _summarize_doctor_failures(launch_result.doctor),
                    "doctor": launch_result.doctor.to_dict(),
                },
                422,
            )
        if outcome in {
            LaunchStatus.ALREADY_RUNNING,
            LaunchStatus.CONFIGURATION_CONFLICT,
        }:
            return RepositoryEngineStartResult(
                {
                    "error": outcome.value,
                    "detail": launch_result.error or outcome.value,
                    "doctor": launch_result.doctor.to_dict(),
                    "supervisor": launch_result.supervisor,
                    "conflict": launch_result.conflict,
                },
                409,
            )
        if not outcome.is_failure and launch_result.launched:
            return None
        return RepositoryEngineStartResult(
            {
                "error": "launch_failed",
                "detail": launch_result.error or "Unknown launch error",
                "doctor": launch_result.doctor.to_dict(),
            },
            500,
        )


__all__ = [
    "record_repository_engine_launch",
    "RepositoryEngineStartRequest",
    "RepositoryEngineStartResult",
    "StartRepositoryEngineCommand",
]
