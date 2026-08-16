"""Repository lock management.

Ensures only one orchestrator runs per repository (or per instance in
multi-instance mode) by maintaining lock files with PID and process liveness
checks.

Single-instance mode: .issue-orchestrator/lock.json
Multi-instance mode:  .issue-orchestrator/locks/{instance_id}.json
"""

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .repo_identity import lock_file, locks_dir, normalize_repo_root, state_dir

# Process-local registry of held gate file descriptors, keyed by the metadata
# lock path. ``flock`` is bound to the open file DESCRIPTION (the fd), so the
# descriptor MUST stay open for the whole lock lifetime — closing it releases
# the flock. release_lock (and the held_repo_lock finally) close them here.
_HELD_GATE_FDS: dict[str, list[int]] = {}


class AlreadyRunning(Exception):
    """Raised when another orchestrator is already running for this repo/instance."""

    def __init__(
        self,
        pid: int,
        repo_root: Path,
        port: int | None,
        instance_id: str | None = None,
    ):
        self.pid = pid
        self.repo_root = repo_root
        self.port = port
        self.instance_id = instance_id
        instance_str = f" instance={instance_id}" if instance_id else ""
        super().__init__(
            f"Orchestrator already running for {repo_root}{instance_str} (pid={pid}, port={port})"
        )


class ConfigurationIdentityConflict(Exception):
    """Raised when a repository already has a different live configuration owner."""

    def __init__(
        self,
        repo_root: Path,
        *,
        requested: tuple[str, str, str],
        active: tuple[str, str, str],
    ) -> None:
        self.repo_root = repo_root
        self.requested = requested
        self.active = active
        super().__init__(
            "Repository already has a live engine with configuration identity "
            f"{active[0]!r}/{active[1]!r} ({active[2][:12] or 'no fingerprint'}); "
            "stop every repository engine before starting "
            f"{requested[0]!r}/{requested[1]!r} "
            f"({requested[2][:12] or 'no fingerprint'})"
        )


class RepositoryLifecycleBusy(Exception):
    """Raised when a live repository lifecycle owner holds the shared gate."""


@dataclass
class LockInfo:
    """Information stored in the lock file."""

    repo_root: str
    pid: int
    started_at: str
    http_port: int | None
    state_dir: str
    recovered: bool = False
    instance_id: str | None = None  # For multi-instance deployments
    last_heartbeat_at: str | None = None
    configuration_mode: str = "default"
    config_name: str = "default.yaml"
    config_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        result = {
            "repo_root": self.repo_root,
            "pid": self.pid,
            "started_at": self.started_at,
            "http_port": self.http_port,
            "state_dir": self.state_dir,
            "recovered": self.recovered,
            "last_heartbeat_at": self.last_heartbeat_at,
            "configuration_mode": self.configuration_mode,
            "config_name": self.config_name,
            "config_fingerprint": self.config_fingerprint,
        }
        if self.instance_id is not None:
            result["instance_id"] = self.instance_id
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LockInfo":
        """Create from dict (JSON deserialization)."""
        return cls(
            repo_root=data["repo_root"],
            pid=data["pid"],
            started_at=data["started_at"],
            http_port=data.get("http_port"),
            state_dir=data["state_dir"],
            recovered=data.get("recovered", False),
            instance_id=data.get("instance_id"),
            last_heartbeat_at=data.get("last_heartbeat_at"),
            configuration_mode=data.get("configuration_mode", "default"),
            config_name=data.get("config_name", "default.yaml"),
            config_fingerprint=data.get("config_fingerprint", ""),
        )


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive.

    ``kill(pid, 0)`` sends no signal; the errno distinguishes the cases and they
    must NOT be collapsed to "dead" (#6824 R2): ``ESRCH`` (ProcessLookupError)
    means the process is absent, but ``EPERM`` (PermissionError) means it EXISTS
    and is owned by ANOTHER user — treating that as dead would let a user-owned
    invocation overwrite a live service-owned legacy engine (split-brain). Any
    other error fails conservatively as alive rather than risk a double-run.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False  # ESRCH: no such process
    except PermissionError:
        return True  # EPERM: process exists, owned by another user
    except OSError:
        return True  # unknown error: assume alive (fail-safe, never double-run)


def _read_lock(lock_path: Path) -> LockInfo | None:
    """Read and parse lock file.

    Args:
        lock_path: Path to lock.json

    Returns:
        LockInfo if file exists and is valid, None otherwise
    """
    if not lock_path.exists():
        return None

    try:
        with open(lock_path) as f:
            data = json.load(f)
        return LockInfo.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _write_lock(lock_path: Path, info: LockInfo) -> None:
    """Write lock file atomically.

    Args:
        lock_path: Path to lock.json
        info: Lock information to write
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Write to temp file first, then rename for atomicity
    tmp_path = lock_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(info.to_dict(), f, indent=2)
    tmp_path.rename(lock_path)


def _repo_gate_path(repo_root: Path) -> Path:
    """The ONE repo-wide flock gate that serializes every startup mode.

    Single-instance and one-shot commands take it ``LOCK_EX`` (excluding
    everything); a named multi-instance engine takes it ``LOCK_SH`` (so N named
    instances coexist, yet a one-shot's ``LOCK_EX`` still excludes them all).
    This is the single atomic owner the review's A1 requires — it replaces the
    old read-check-rename race AND the TOCTOU ``list_instance_locks`` scan.
    """
    return repo_root / ".issue-orchestrator" / "repo.lock"


def _instance_gate_path(repo_root: Path, instance_id: str) -> Path:
    """Per-instance-id flock gate: rejects a duplicate SAME instance_id."""
    return locks_dir(repo_root) / f"{instance_id}.lock"


def _configuration_owner_gate_path(repo_root: Path) -> Path:
    """Serialize configuration-owner checks performed by named instances."""
    return locks_dir(repo_root) / "configuration-owner.lock"


def _lifecycle_mutation_gate_path(repo_root: Path) -> Path:
    """Serialize parent startup transactions with launch-selection changes."""
    return locks_dir(repo_root) / "lifecycle-mutation.lock"


def _acquire_gate(path: Path, *, exclusive: bool) -> int:
    """Open ``path`` and take a non-blocking flock; raise BlockingIOError on conflict.

    Returns the held fd (caller keeps it open for the lock lifetime). ``flock`` is
    bound to the open file description, so a second ``os.open`` of the same file —
    even in the same process — conflicts, which is exactly the mutual exclusion
    the old code lacked.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    flag = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
    try:
        fcntl.flock(fd, flag)
    except OSError:
        os.close(fd)
        raise
    return fd


def _acquire_serialization_gate(path: Path) -> int:
    """Take a short-lived blocking exclusive flock used only for publication."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        os.close(fd)
        raise
    return fd


def acquire_lock(
    repo_root: Path | str,
    port: int | None = None,
    instance_id: str | None = None,
    *,
    configuration_mode: str = "default",
    config_name: str = "default.yaml",
    config_fingerprint: str = "",
) -> LockInfo:
    """Acquire the repository lock atomically (or instance-specific lock).

    Exclusion is a repo-wide ``flock`` gate (see :func:`_repo_gate_path`), NOT the
    old read-check-then-write on the metadata file — two callers can no longer
    both "win". The metadata ``lock.json`` / ``locks/{id}.json`` remains the
    on-disk advertisement (pid/port/heartbeat) that the supervisor and status
    endpoints read; it is no longer the exclusion primitive.

    Args:
        repo_root: Repository root path
        port: HTTP port the orchestrator will listen on
        instance_id: Optional instance ID for multi-instance deployments

    Returns:
        LockInfo for the acquired lock

    Raises:
        AlreadyRunning: If another orchestrator is running for this repo/instance
    """
    repo_root = normalize_repo_root(repo_root)
    lock_path = lock_file(repo_root, instance_id)
    exclusive = instance_id is None

    # Named instances share the repo gate, so serialize their identity check and
    # metadata publication with a short-lived exclusive owner gate. Without this
    # gate, two first instances using different modes could both observe no
    # metadata and publish conflicting identities.
    owner_gate_fd: int | None = None
    if instance_id is not None:
        owner_gate_fd = _acquire_serialization_gate(
            _configuration_owner_gate_path(repo_root)
        )

    held: list[int] = []
    try:
        # 1. Repo-wide lifecycle gate.
        try:
            held.append(
                _acquire_gate(_repo_gate_path(repo_root), exclusive=exclusive)
            )
        except OSError as exc:
            raise _already_running(repo_root, lock_path, instance_id) from exc

        # 2. Per-instance gate — reject a duplicate SAME instance_id (multi only;
        #    single-instance/one-shot are already excluded by the LOCK_EX gate).
        if instance_id is not None:
            try:
                held.append(
                    _acquire_gate(
                        _instance_gate_path(repo_root, instance_id), exclusive=True
                    )
                )
            except OSError as exc:
                raise _already_running(repo_root, lock_path, instance_id) from exc

        # A live pre-flock process has metadata but no gate. Check every legacy
        # advertisement that conflicts with this lifecycle before publishing.
        legacy = _conflicting_legacy_holder(repo_root, lock_path, instance_id)
        if legacy is not None:
            raise AlreadyRunning(
                pid=legacy.pid,
                repo_root=repo_root,
                port=legacy.http_port,
                instance_id=legacy.instance_id,
            )
        if instance_id is not None:
            assert_repository_configuration_identity(
                repo_root,
                configuration_mode=configuration_mode,
                config_name=config_name,
                config_fingerprint=config_fingerprint,
            )
        recovered = _read_lock(lock_path) is not None
        info = LockInfo(
            repo_root=str(repo_root),
            pid=os.getpid(),
            started_at=datetime.now(timezone.utc).isoformat(),
            http_port=port,
            state_dir=str(state_dir(repo_root)),
            recovered=recovered,
            instance_id=instance_id,
            last_heartbeat_at=datetime.now(timezone.utc).isoformat(),
            configuration_mode=configuration_mode,
            config_name=config_name,
            config_fingerprint=config_fingerprint,
        )
        _write_lock(lock_path, info)
        _HELD_GATE_FDS.setdefault(str(lock_path), []).extend(held)
        held = []
        return info
    finally:
        # Any failure before successful publication releases every lifecycle
        # gate. The short-lived owner gate is never retained by a live engine.
        _release_fds(held)
        if owner_gate_fd is not None:
            _release_fds([owner_gate_fd])


def assert_repository_configuration_identity(
    repo_root: Path | str,
    *,
    configuration_mode: str,
    config_name: str,
    config_fingerprint: str,
) -> None:
    """Fail when any live named instance owns a different configuration.

    ``acquire_lock`` invokes this while holding the configuration-owner gate,
    which makes the check atomic with publishing a new named lock. Supervisors
    may also call it before spawning processes to fail before a partial launch;
    the lock-level invocation remains the authoritative race-free guard.
    """
    repo_root = normalize_repo_root(repo_root)
    requested = (configuration_mode, config_name, config_fingerprint)
    active_identities = sorted(
        {
            (
                info.configuration_mode,
                info.config_name,
                info.config_fingerprint,
            )
            for info in list_instance_locks(repo_root)
        }
    )
    for active in active_identities:
        if active != requested:
            raise ConfigurationIdentityConflict(
                repo_root,
                requested=requested,
                active=active,
            )


def _already_running(
    repo_root: Path, lock_path: Path, instance_id: str | None
) -> AlreadyRunning:
    """Build AlreadyRunning from the metadata advertisement (best-effort)."""
    existing = _read_lock(lock_path)
    return AlreadyRunning(
        pid=existing.pid if existing else -1,
        repo_root=repo_root,
        port=existing.http_port if existing else None,
        instance_id=instance_id,
    )


def _live_holder(lock_path: Path) -> LockInfo | None:
    """A LIVE, different-pid holder advertised at ``lock_path`` (else None)."""
    info = _read_lock(lock_path)
    if info is not None and info.pid != os.getpid() and _is_process_alive(info.pid):
        return info
    return None


def _conflicting_legacy_holder(
    repo_root: Path, lock_path: Path, instance_id: str | None
) -> LockInfo | None:
    """A live pre-flock holder (metadata, no gate) that conflicts with this acquire.

    Cross-mode matrix (#6824 R2): winning the flock gate cannot exclude a legacy
    process that holds no gate, so EVERY conflicting advertisement is checked —
    not just this mode's own file. An EXCLUSIVE acquire (single-instance /
    one-shot) conflicts with a live ``lock.json`` AND every live ``locks/*.json``;
    a NAMED acquire conflicts with its own same-id ``locks/{id}.json`` AND a live
    ``lock.json`` (a single-instance engine excludes all names).
    """
    own = _live_holder(lock_path)
    if own is not None:
        return own
    single = _live_holder(lock_file(repo_root, None))
    if single is not None:
        return single
    if instance_id is None:
        for info in list_instance_locks(repo_root):
            if info.pid != os.getpid():
                return info
    return None


def _release_fds(fds: list[int]) -> None:
    """Release + close held gate fds (releasing their flocks)."""
    for fd in fds:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError:
            pass


@contextmanager
def repository_lifecycle_mutation(repo_root: Path | str) -> Iterator[None]:
    """Serialize short-lived lifecycle decisions before a child owns the repo."""
    normalized = normalize_repo_root(repo_root)
    try:
        gate_fd = _acquire_gate(
            _lifecycle_mutation_gate_path(normalized),
            exclusive=True,
        )
    except OSError as exc:
        raise RepositoryLifecycleBusy from exc
    try:
        yield
    finally:
        _release_fds([gate_fd])


@contextmanager
def exclusive_repository_lifecycle(repo_root: Path | str) -> Iterator[None]:
    """Hold the shared repository gate while mutating lifecycle-owned state.

    Repository engines retain this gate for their entire lifetime. Selection
    changes use the same gate so the running check and registry write are one
    atomic lifecycle operation rather than a read-check-write race.
    """
    normalized = normalize_repo_root(repo_root)
    with repository_lifecycle_mutation(normalized):
        try:
            gate_fd = _acquire_gate(_repo_gate_path(normalized), exclusive=True)
        except OSError as exc:
            raise RepositoryLifecycleBusy from exc
        try:
            yield
        finally:
            _release_fds([gate_fd])


def release_lock(
    repo_root: Path | str,
    pid: int | None = None,
    instance_id: str | None = None,
) -> bool:
    """Release the repository lock (or instance-specific lock).

    Only releases if the lock belongs to the specified PID (or current process).

    Args:
        repo_root: Repository root path
        pid: PID that should own the lock (defaults to current process)
        instance_id: Optional instance ID for multi-instance deployments

    Returns:
        True if lock was released, False if lock didn't exist or belonged to another process
    """
    repo_root = normalize_repo_root(repo_root)
    lock_path = lock_file(repo_root, instance_id)
    pid = pid or os.getpid()

    existing = _read_lock(lock_path)
    # Validate ownership BEFORE releasing anything (#6824 R2): a wrong-pid
    # release must NOT silently drop the flock (leaving a split-brain where
    # another process can acquire while the metadata still names this one). Only
    # the true owner releases its gate fds and unlinks its metadata.
    if existing is not None and existing.pid != pid:
        return False

    held = _HELD_GATE_FDS.pop(str(lock_path), [])
    try:
        if existing is None:
            return bool(held)
        try:
            # Metadata deletion is part of the owned publication lifecycle:
            # keep the flock until the old advertisement is gone.
            lock_path.unlink()
            return True
        except OSError:
            return False
    finally:
        _release_fds(held)


@contextmanager
def held_repo_lock(
    repo_root: Path | str,
    port: int | None = None,
    instance_id: str | None = None,
) -> Iterator[LockInfo]:
    """Acquire and hold the repo lock across a whole in-process lifecycle.

    The supervised engine (:mod:`..entrypoints.run_orchestrator`) already
    acquires + releases the lock around its run; a one-shot in-process command
    must do the SAME rather than a read-only :func:`is_locked` peek. A peek is
    check-then-act: two one-shots (or a one-shot and the engine) can both pass
    the check and then run concurrently against one repo.

    :func:`acquire_lock` now takes the repo-wide ``LOCK_EX`` gate (a one-shot
    passes ``instance_id=None``), which atomically excludes the single-instance
    engine, every multi-instance engine (they hold ``LOCK_SH`` on the same
    gate), and any other one-shot — so no post-acquire ``list_instance_locks``
    scan is needed (that scan was itself a TOCTOU). Releases on every exit path.
    """
    repo_root = normalize_repo_root(repo_root)
    info = acquire_lock(repo_root, port, instance_id)
    try:
        yield info
    finally:
        release_lock(repo_root, pid=info.pid, instance_id=instance_id)


def read_lock(repo_root: Path | str, instance_id: str | None = None) -> LockInfo | None:
    """Read the current lock file for a repository (or specific instance).

    Args:
        repo_root: Repository root path
        instance_id: Optional instance ID for multi-instance deployments

    Returns:
        LockInfo if lock exists, None otherwise
    """
    repo_root = normalize_repo_root(repo_root)
    return _read_lock(lock_file(repo_root, instance_id))


def is_locked(repo_root: Path | str, instance_id: str | None = None) -> bool:
    """Check if a repository (or specific instance) has an active lock.

    Args:
        repo_root: Repository root path
        instance_id: Optional instance ID for multi-instance deployments

    Returns:
        True if there's an active lock with a live process
    """
    info = read_lock(repo_root, instance_id)
    if info is None:
        return False
    return _is_process_alive(info.pid)


def list_instance_locks(repo_root: Path | str) -> list[LockInfo]:
    """List all active instance locks for a repository.

    Args:
        repo_root: Repository root path

    Returns:
        List of LockInfo for all active instances
    """
    repo_root = normalize_repo_root(repo_root)
    locks_directory = locks_dir(repo_root)

    if not locks_directory.exists():
        return []

    active_locks = []
    for lock_path in locks_directory.glob("*.json"):
        info = _read_lock(lock_path)
        if info is not None and _is_process_alive(info.pid):
            active_locks.append(info)

    return active_locks


def touch_lock(
    repo_root: Path | str,
    pid: int | None = None,
    instance_id: str | None = None,
) -> bool:
    """Update lock heartbeat timestamp for the owning process.

    Returns False when the lock does not exist or belongs to another process.
    """
    repo_root = normalize_repo_root(repo_root)
    lock_path = lock_file(repo_root, instance_id)
    expected_pid = pid or os.getpid()
    existing = _read_lock(lock_path)
    if existing is None or existing.pid != expected_pid:
        return False
    existing.last_heartbeat_at = datetime.now(timezone.utc).isoformat()
    _write_lock(lock_path, existing)
    return True


def set_lock_http_port(
    repo_root: Path | str,
    port: int,
    pid: int | None = None,
    instance_id: str | None = None,
) -> bool:
    """Update the lock's HTTP port for the owning process."""
    repo_root = normalize_repo_root(repo_root)
    lock_path = lock_file(repo_root, instance_id)
    expected_pid = pid or os.getpid()
    existing = _read_lock(lock_path)
    if existing is None or existing.pid != expected_pid:
        return False
    existing.http_port = port
    existing.last_heartbeat_at = datetime.now(timezone.utc).isoformat()
    _write_lock(lock_path, existing)
    return True
