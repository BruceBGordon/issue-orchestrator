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
    inspect_repository_orchestrator_ownership,
    is_shutdown_complete,
)

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..infra.launcher import LaunchResult
    from ..infra.repo_identity import RepoIdentity


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
        return self.status_code == 409 and self.payload.get("error") == "orphaned_running"


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
            if isinstance(supervisor_data.get("port"), (int, type(None))):
                payload["port"] = supervisor_data["port"]
            if isinstance(supervisor_data.get("instances"), list):
                payload["instances"] = supervisor_data["instances"]
        return RepositoryEngineStartResult(payload)

    def _resolve_existing_ownership(
        self,
        request: RepositoryEngineStartRequest,
        expected_config_fingerprint: str,
    ) -> RepositoryEngineStartResult | None:
        ownership = inspect_repository_orchestrator_ownership(
            request.repo_root,
            request.selection,
        )
        expected_identity = build_repo_identity(request.repo_root)
        for detected in ownership.all:
            annotate_identity_mismatch(
                detected,
                detected.get("info", {}),
                expected_identity,
            )
        matching = tuple(
            detected
            for detected in ownership.matching
            if detected.get("info", {}).get("config_fingerprint")
            == expected_config_fingerprint
        )
        conflicting = ownership.conflicting + tuple(
            detected for detected in ownership.matching if detected not in matching
        )
        if conflicting and not request.force_restart:
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
                            **detected["active_selection"],
                            "config_fingerprint": detected.get("info", {}).get(
                                "config_fingerprint", ""
                            ),
                        }
                        for detected in conflicting
                    ],
                    "ports": [detected["port"] for detected in conflicting],
                },
                409,
            )
        if request.force_restart:
            for detected in ownership.all:
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
        return self._resolve_matching_ownership(
            request,
            matching,
            expected_config_fingerprint,
        )

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
