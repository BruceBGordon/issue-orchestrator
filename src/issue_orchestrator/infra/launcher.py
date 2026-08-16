"""Unified launcher for the issue orchestrator.

All entry points (CLI, Control Center, MCP) converge through this module
to ensure consistent pre-flight (doctor) checks before starting.

Two launch modes:
- ``launch_preflight_only``: runs doctor checks, returns result. CLI uses
  this because it builds the orchestrator in-process afterwards.
- ``launch_subprocess``: runs doctor checks then calls ``supervisor.start()``
  to launch the orchestrator as a subprocess. CC and MCP use this.

``preflight`` is an alias for ``launch_preflight_only`` kept for readability
when the caller only wants to display readiness (e.g. CC page load).
"""

import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Optional

from ..domain.repository_launch_selection import RepositoryLaunchSelection
from ..ports.repository_engine_supervisor import RepositoryEngineLock, SupervisorOps
from . import supervisor
from .config import Config
from .doctor import run_doctor
from .doctor.types import DoctorResult
from .repo_lock import AlreadyRunning, ConfigurationIdentityConflict
from ..ports.command_runner import CommandRunner

# Type alias for the doctor function, enabling DI in tests.
DoctorFn = Callable[..., DoctorResult]

logger = logging.getLogger(__name__)


def _supervisor_identity(info: RepositoryEngineLock) -> dict[str, str]:
    """Return the effective configuration identity recorded by a supervisor lock."""
    return {
        "configuration_mode": info.configuration_mode,
        "config_name": info.config_name,
        "config_fingerprint": info.config_fingerprint,
    }


def _default_command_runner() -> CommandRunner:
    from ..execution.command_runner import LocalCommandRunner

    return LocalCommandRunner()


class UnknownLaunchStatusError(ValueError):
    """A launcher status outside the shared vocabulary reached a consumer.

    Deliberately loud. Consumers classify statuses to decide whether startup
    succeeded; an unrecognised one silently taking the success branch is how a
    failed launch gets reported to operators as "started".
    """


class UnclassifiedLaunchStatusError(ValueError):
    """A :class:`LaunchStatus` member declares no success/failure disposition.

    The counterpart to :class:`UnknownLaunchStatusError`, which guards the
    *wire*. This one guards the *enum*: adding a member without also placing it
    in exactly one of the disposition sets would otherwise let a real failure be
    classified as a successful start, because "not a known failure" is not the
    same statement as "startup succeeded".
    """


class LaunchStatus(StrEnum):
    """The complete vocabulary a :class:`LaunchResult` may report.

    Shared so every consumer — CLI, Control API, MCP — classifies outcomes the
    same way instead of comparing raw strings.

    Classification is *total*, not opt-in. Every member must be declared in
    either :meth:`success_statuses` or :meth:`failure_statuses`; membership of
    neither is an error rather than an implicit success. That distinction is the
    whole point: with an opt-in failure set, a member added later inherits the
    success branch by omission, and a launch that did not happen is reported to
    every MCP client as "started".
    """

    OK = "ok"
    DOCTOR_WARNING = "doctor_warning"
    ALREADY_RUNNING = "already_running"
    DOCTOR_ERROR = "doctor_error"
    LAUNCH_ERROR = "launch_error"
    CONFIGURATION_CONFLICT = "configuration_conflict"

    @classmethod
    def success_statuses(cls) -> frozenset["LaunchStatus"]:
        """The statuses that mean the orchestrator is up.

        A warning still starts it, and ``ALREADY_RUNNING`` means it is already
        up — we merely lost the race to start it.
        """
        return _SUCCESS_STATUSES

    @classmethod
    def failure_statuses(cls) -> frozenset["LaunchStatus"]:
        """The statuses that mean startup did not happen."""
        return _FAILURE_STATUSES

    @property
    def is_failure(self) -> bool:
        """Whether startup did not happen.

        Raises:
            UnclassifiedLaunchStatusError: if this member does not appear in
                exactly one disposition set. Never defaults to ``False``.
        """
        cls = type(self)
        membership = (
            self in cls.success_statuses(),
            self in cls.failure_statuses(),
        )
        try:
            return _IS_FAILURE_BY_DISPOSITION_MEMBERSHIP[membership]
        except KeyError:
            raise UnclassifiedLaunchStatusError(
                f"launcher status {self.value!r} must be in exactly one of the "
                "success or failure sets; classify it explicitly rather than "
                "letting it read as a successful start"
            ) from None

    @classmethod
    def parse(cls, value: "LaunchStatus | str") -> "LaunchStatus":
        """Return the member for ``value``, or fail loudly.

        ``LaunchResult`` is a plain dataclass, so a raw string can still reach
        it from an older caller or a test double. Consumers should route every
        status through here rather than defaulting unknown values to success.
        """
        try:
            return cls(value)
        except ValueError:
            raise UnknownLaunchStatusError(
                f"unknown launcher status {value!r}; expected one of "
                f"{sorted(member.value for member in cls)}"
            ) from None


_SUCCESS_STATUSES = frozenset(
    {LaunchStatus.OK, LaunchStatus.DOCTOR_WARNING, LaunchStatus.ALREADY_RUNNING}
)
_FAILURE_STATUSES = frozenset(
    {
        LaunchStatus.DOCTOR_ERROR,
        LaunchStatus.LAUNCH_ERROR,
        LaunchStatus.CONFIGURATION_CONFLICT,
    }
)
_IS_FAILURE_BY_DISPOSITION_MEMBERSHIP = {
    (True, False): False,
    (False, True): True,
}


def _verify_every_status_is_classified() -> None:
    """Fail at import if the disposition sets do not partition ``LaunchStatus``.

    Import time is the right moment: a member added without a disposition then
    breaks the CLI, the Control API, and the MCP server immediately and
    identically, instead of surfacing later as a failed launch that a client
    renders as a successful one.
    """
    overlapping = _SUCCESS_STATUSES & _FAILURE_STATUSES
    if overlapping:
        raise UnclassifiedLaunchStatusError(
            "launcher statuses classified as both success and failure: "
            f"{sorted(status.value for status in overlapping)}"
        )
    unclassified = set(LaunchStatus) - _SUCCESS_STATUSES - _FAILURE_STATUSES
    if unclassified:
        raise UnclassifiedLaunchStatusError(
            "launcher statuses with no success/failure disposition: "
            f"{sorted(status.value for status in unclassified)}"
        )


_verify_every_status_is_classified()


@dataclass
class LaunchResult:
    """Result of a launcher operation."""

    doctor: DoctorResult
    launched: bool
    status: LaunchStatus
    error: Optional[str] = None
    supervisor: Optional[dict[str, Any]] = None
    conflict: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "doctor": self.doctor.to_dict(),
            "launched": self.launched,
            "status": LaunchStatus.parse(self.status).value,
        }
        if self.error is not None:
            result["error"] = self.error
        if self.supervisor is not None:
            result["supervisor"] = self.supervisor
        if self.conflict is not None:
            result["conflict"] = self.conflict
        return result


def _run_preflight(
    config: Config,
    runner: Optional[CommandRunner] = None,
    doctor_fn: Optional[DoctorFn] = None,
) -> tuple[DoctorResult, LaunchStatus]:
    """Run doctor checks and return ``(result, status)``.

    Args:
        doctor_fn: Callable to use instead of ``run_doctor``.
            Also skipped when ``ISSUE_ORCHESTRATOR_SKIP_DOCTOR=1`` is set
            (needed by integration tests that spawn subprocesses).

    Returns:
        ``(doctor_result, status)`` where status is ``OK``, ``DOCTOR_WARNING``,
        or ``DOCTOR_ERROR``.
    """
    if os.environ.get("ISSUE_ORCHESTRATOR_SKIP_DOCTOR") == "1":
        return DoctorResult(checks=[]), LaunchStatus.OK
    fn = doctor_fn or run_doctor
    doctor_start = time.time()
    doctor_result = fn(config=config, runner=runner)
    logger.info(
        "[STARTUP_TIMING] phase=preflight_doctor elapsed=%.3fs overall=%s checks=%d",
        time.time() - doctor_start,
        doctor_result.overall,
        len(doctor_result.checks),
    )
    if doctor_result.overall == "error":
        return doctor_result, LaunchStatus.DOCTOR_ERROR
    if doctor_result.overall == "warning":
        return doctor_result, LaunchStatus.DOCTOR_WARNING
    return doctor_result, LaunchStatus.OK


def preflight(
    config: Config,
    runner: Optional[CommandRunner] = None,
    doctor_fn: Optional[DoctorFn] = None,
) -> LaunchResult:
    """Run doctor checks only. Returns LaunchResult with launched=False.

    Use for "show readiness" — runs doctor without starting anything.
    """
    if runner is None:
        runner = _default_command_runner()
    doctor_result, status = _run_preflight(config, runner, doctor_fn=doctor_fn)
    return LaunchResult(
        doctor=doctor_result,
        launched=False,
        status=status,
    )


def launch_preflight_only(
    config: Config,
    runner: Optional[CommandRunner] = None,
    doctor_fn: Optional[DoctorFn] = None,
) -> LaunchResult:
    """Run doctor checks only, for CLI which builds in-process.

    CLI calls this, then proceeds to ``build_orchestrator()`` itself.
    Alias for ``preflight()`` — named differently for clarity at call sites.
    """
    return preflight(config, runner, doctor_fn=doctor_fn)


def _supervisor_launch_options(
    *,
    mode: str,
    expected_config_fingerprint: str,
    expected_identity: Optional[dict[str, Any]],
    start_paused: bool,
    log_level: Optional[str],
) -> dict[str, Any]:
    """Translate optional launch policy once for both supervisor start paths."""
    options: dict[str, Any] = {
        "expected_config_fingerprint": expected_config_fingerprint,
    }
    if mode != "default":
        options["mode"] = mode
    if expected_identity is not None:
        options["expected_identity"] = expected_identity
    if start_paused:
        options["start_paused"] = True
    if log_level is not None:
        options["log_level"] = log_level
    return options


def _start_with_supervisor(
    sv: SupervisorOps,
    *,
    repo_root: Path,
    config: Config,
    selection: RepositoryLaunchSelection,
    instance_id: Optional[str],
    port: Optional[int],
    expected_identity: Optional[dict[str, Any]],
    start_paused: bool,
    log_level: Optional[str],
) -> dict[str, Any]:
    launch_options = _supervisor_launch_options(
        mode=selection.mode.value,
        expected_config_fingerprint=config.config_fingerprint,
        expected_identity=expected_identity,
        start_paused=start_paused,
        log_level=log_level,
    )
    if config.instances > 1 and instance_id is None:
        start_instances_kwargs: dict[str, Any] = {
            "repo_root": repo_root,
            "config_name": selection.config.value,
            **launch_options,
        }
        infos = sv.start_instances(**start_instances_kwargs)
        identity = (
            _supervisor_identity(infos[0])
            if infos
            else {
                "configuration_mode": config.configuration_mode,
                "config_name": config.config_name,
                "config_fingerprint": config.config_fingerprint,
            }
        )
        return {
            **identity,
            "instances": [
                {
                    "pid": info.pid,
                    "port": info.http_port,
                    "instance_id": info.instance_id,
                    **_supervisor_identity(info),
                }
                for info in infos
            ],
        }

    start_kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        "config_name": selection.config.value,
        "instance_id": instance_id,
        "port": port,
        **launch_options,
    }
    info = sv.start(**start_kwargs)

    supervisor_data = {
        "pid": info.pid,
        "port": info.http_port,
        **_supervisor_identity(info),
    }
    if info.instance_id:
        supervisor_data["instance_id"] = info.instance_id
    return supervisor_data


def _resolve_launch_selection(
    config: Config,
    *,
    config_name: str | None,
    mode: str | None,
) -> RepositoryLaunchSelection:
    """Derive selection from Config and reject any independently supplied drift."""
    selection = config.launch_selection
    if config_name is None and mode is None:
        return selection
    explicit = RepositoryLaunchSelection.parse(
        mode=mode or selection.mode.value,
        config_name=config_name or selection.config.value,
    )
    if explicit != selection:
        raise ValueError(
            "Launch selection does not match the preflighted Config: "
            f"requested={explicit.mode.value}/{explicit.config.value}, "
            f"loaded={selection.mode.value}/{selection.config.value}"
        )
    return selection


def launch_subprocess(
    repo_root: Path,
    config: Config,
    config_name: str | None = None,
    mode: str | None = None,
    runner: Optional[CommandRunner] = None,
    instance_id: Optional[str] = None,
    port: Optional[int] = None,
    expected_identity: Optional[dict[str, Any]] = None,
    start_paused: bool = False,
    log_level: Optional[str] = None,
    supervisor_ops: Optional[SupervisorOps] = None,
    doctor_fn: Optional[DoctorFn] = None,
) -> LaunchResult:
    """Run doctor checks, then supervisor.start() if checks pass.

    Used by CC and MCP entry points.

    Args:
        repo_root: Repository root path.
        config: Loaded configuration.
        config_name: Config file name for the supervisor.
        runner: Optional command runner for guardrails checks.
        instance_id: Optional instance ID for multi-instance mode.
        port: Optional port override.
        log_level: Optional repository engine log level override.
        supervisor_ops: Optional supervisor operations (DI for tests).
        doctor_fn: Callable that runs doctor checks. Defaults to
            ``run_doctor``.  Tests can inject a no-op to skip checks.

    Returns:
        LaunchResult with doctor results and supervisor info.
    """
    selection = _resolve_launch_selection(
        config,
        config_name=config_name,
        mode=mode,
    )
    if runner is None:
        runner = _default_command_runner()
    doctor_result, status = _run_preflight(config, runner, doctor_fn=doctor_fn)

    if status == "doctor_error":
        return LaunchResult(
            doctor=doctor_result,
            launched=False,
            status=LaunchStatus.DOCTOR_ERROR,
        )

    # Doctor passed (ok or warning) — start the orchestrator subprocess
    sv = supervisor_ops or supervisor
    try:
        supervisor_data = _start_with_supervisor(
            sv,
            repo_root=repo_root,
            config=config,
            selection=selection,
            instance_id=instance_id,
            port=port,
            expected_identity=expected_identity,
            start_paused=start_paused,
            log_level=log_level,
        )

        return LaunchResult(
            doctor=doctor_result,
            launched=True,
            status=status,  # "ok" or "doctor_warning"
            supervisor=supervisor_data,
        )
    except ConfigurationIdentityConflict as exc:
        return LaunchResult(
            doctor=doctor_result,
            launched=False,
            status=LaunchStatus.CONFIGURATION_CONFLICT,
            error=str(exc),
            conflict={
                "active": {
                    "mode": exc.active[0],
                    "config_name": exc.active[1],
                    "config_fingerprint": exc.active[2],
                },
                "requested": {
                    "mode": exc.requested[0],
                    "config_name": exc.requested[1],
                    "config_fingerprint": exc.requested[2],
                },
            },
        )
    except AlreadyRunning:
        from .repo_lock import read_lock

        info = read_lock(repo_root, instance_id)
        supervisor_data = None
        if info:
            supervisor_data = {
                "pid": info.pid,
                "port": info.http_port,
                **_supervisor_identity(info),
            }
            if info.instance_id:
                supervisor_data["instance_id"] = info.instance_id
        return LaunchResult(
            doctor=doctor_result,
            launched=False,
            status=LaunchStatus.ALREADY_RUNNING,
            error="Orchestrator already running",
            supervisor=supervisor_data,
        )
    except Exception as exc:
        logger.exception("Failed to launch orchestrator subprocess")
        return LaunchResult(
            doctor=doctor_result,
            launched=False,
            status=LaunchStatus.LAUNCH_ERROR,
            error=str(exc),
        )
