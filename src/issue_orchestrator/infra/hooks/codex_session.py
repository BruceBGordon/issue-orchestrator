"""Invocation-scoped Codex hook wiring for orchestrated sessions."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import shlex
import sys
import tomllib
from typing import Iterator, NamedTuple

from ...domain.sandbox_scope import SandboxUnsupportedError
from . import block_no_verify

_RUNTIME_ROOT_ENV = "ISSUE_ORCHESTRATOR_CODEX_RUNTIME_ROOT"
_RUNTIME_LAYOUT_VERSION = "v2"

logger = logging.getLogger(__name__)


def _source_codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser().resolve() if raw else Path.home() / ".codex"


def codex_runtime_home() -> Path:
    """Return the isolated Codex home used by orchestrated sessions."""
    source = _source_codex_home()
    digest = hashlib.sha256(f"{_RUNTIME_LAYOUT_VERSION}:{source}".encode()).hexdigest()[
        :12
    ]
    raw_root = os.environ.get(_RUNTIME_ROOT_ENV)
    root = (
        Path(raw_root).expanduser()
        if raw_root
        else Path.home() / ".issue-orchestrator" / "codex-runtime"
    )
    return root / digest


def prepare_codex_runtime_home(*, untrusted_projects: Iterable[Path] = ()) -> Path:
    """Create the isolated Codex home and record managed untrusted projects."""
    source_auth = _source_codex_home() / "auth.json"
    runtime_home = codex_runtime_home()
    runtime_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_home.chmod(0o700)
    runtime_auth = runtime_home / "auth.json"
    if runtime_auth.is_symlink():
        if runtime_auth.resolve() != source_auth.resolve():
            raise SandboxUnsupportedError(
                f"Codex runtime auth link points to an unexpected file: {runtime_auth}"
            )
    elif runtime_auth.exists():
        raise SandboxUnsupportedError(
            f"Codex runtime auth path must be a managed symlink: {runtime_auth}"
        )
    elif source_auth.is_file():
        runtime_auth.symlink_to(source_auth)
    verify_codex_runtime_home(require_auth=False)
    projects = tuple(Path(path).resolve() for path in untrusted_projects)
    # Strip anything Codex wrote into its own home. Done here, in the mutating
    # entry point every session launch goes through, rather than in verify:
    # this is where a write belongs, and it is the path the review exchange
    # actually takes (persistent_session_exchange -> get_command ->
    # build_command -> prepare_codex_runtime_home).
    repairable = _managed_untrusted_projects(
        runtime_home / "config.toml"
    ).repairable_keys
    if repairable:
        logger.warning(
            "Codex wrote %s into the managed automation config; rewriting %s "
            "from the managed project layers",
            ", ".join(repairable),
            runtime_home / "config.toml",
        )
    if projects or repairable:
        _record_untrusted_projects(runtime_home, projects)
        verify_codex_runtime_home(require_auth=False)
    return runtime_home


#: Top-level keys Codex writes into its own home during ordinary use. The
#: runtime home IS a Codex home, so Codex persists state there: a released
#: version recorded ``[tui] model_availability_nux`` after showing a new-model
#: notice. These carry no sandbox authority, so stripping them is repair.
#:
#: Everything not listed here stays fatal, deliberately. ``mcp_servers`` and
#: ``notify`` launch programs, ``model_providers`` redirects egress via
#: ``base_url``, and ``shell_environment_policy`` reshapes the child
#: environment — none may be silently rewritten away, because a guard that
#: quietly repairs an escalation is worse than one that refuses. Add a key here
#: only after establishing that Codex writes it itself and that it cannot
#: affect the sandbox.
_CODEX_SELF_WRITTEN_KEYS = frozenset({"tui"})


class _ManagedConfig(NamedTuple):
    """What the managed Codex config holds, and what Codex added to it."""

    projects: frozenset[str]
    #: Present keys from :data:`_CODEX_SELF_WRITTEN_KEYS`. Repairable by
    #: rewriting; never dangerous. Any *other* unmanaged key raises instead of
    #: reaching this field.
    repairable_keys: tuple[str, ...]


def _managed_untrusted_projects(config_path: Path) -> _ManagedConfig:
    """Read the managed project layers, tolerating keys Codex wrote itself.

    The runtime home is an isolated Codex home, so Codex treats it as its own
    and persists ordinary state there — a released version wrote
    ``[tui] model_availability_nux`` after showing a new-model notice. Treating
    any unmanaged key as tampering turned that into a hard
    ``SandboxUnsupportedError`` on every session that used the home, which
    halted the review exchange for four issues in under an hour and marked each
    ``blocked-failed`` after 15-82 minutes of agent work.

    Only keys in :data:`_CODEX_SELF_WRITTEN_KEYS` are tolerated, and they are
    reported for the caller to strip rather than accepted in place. Every other
    unmanaged key still raises: this is a sandbox boundary, and repairing an
    escalation silently would be worse than refusing.

    Drift *inside* the managed data stays fatal too. A project layer whose
    settings are anything but ``trust_level = "untrusted"`` is an escalation of
    the boundary itself, not incidental CLI state, and must never be rewritten
    away.
    """
    if not config_path.exists():
        return _ManagedConfig(frozenset(), ())
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SandboxUnsupportedError(
            f"Codex automation config is unreadable or invalid: {config_path}: {exc}"
        ) from exc
    unmanaged = tuple(sorted(key for key in document if key != "projects"))
    if unexpected := tuple(
        key for key in unmanaged if key not in _CODEX_SELF_WRITTEN_KEYS
    ):
        raise SandboxUnsupportedError(
            f"Codex automation config contains unmanaged settings: {config_path} "
            f"({', '.join(unexpected)})"
        )
    projects = document.get("projects", {})
    if not isinstance(projects, dict):
        raise SandboxUnsupportedError(
            f"Codex automation config contains managed-project drift: {config_path}"
        )
    for project, settings in projects.items():
        if not isinstance(project, str) or settings != {"trust_level": "untrusted"}:
            raise SandboxUnsupportedError(
                f"Codex automation config contains managed-project drift: {config_path}"
            )
    return _ManagedConfig(frozenset(projects), unmanaged)


@contextmanager
def _runtime_config_lock(runtime_home: Path) -> Iterator[None]:
    lock_path = runtime_home / ".managed-config.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _record_untrusted_projects(runtime_home: Path, projects: Iterable[Path]) -> None:
    config_path = runtime_home / "config.toml"
    with _runtime_config_lock(runtime_home):
        configured = set(_managed_untrusted_projects(config_path).projects)
        configured.update(str(project) for project in projects)
        lines = ["# Managed by issue-orchestrator; all project layers stay untrusted."]
        for project in sorted(configured):
            lines.extend(
                [
                    "",
                    f"[projects.{json.dumps(project, ensure_ascii=False)}]",
                    'trust_level = "untrusted"',
                ]
            )
        temp_path = runtime_home / f".config.toml.{os.getpid()}.tmp"
        try:
            with temp_path.open("x", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, config_path)
            # Re-read while the lock is still held. Outside it, a Codex write
            # landing between the rename and the check would raise on a file we
            # had just written correctly — and that write is *correlated* with
            # this one, since the drift that triggers a repair is Codex
            # persisting state. Measured at 16.3% spurious failures on the
            # production-sized config when this check sat outside the lock.
            residual = _managed_untrusted_projects(config_path)
            if residual.repairable_keys:
                raise SandboxUnsupportedError(
                    "Codex automation config still contains unmanaged settings "
                    f"after rewrite: {config_path} "
                    f"({', '.join(residual.repairable_keys)})"
                )
        finally:
            temp_path.unlink(missing_ok=True)


def verify_codex_runtime_home(*, require_auth: bool = True) -> Path:
    """Reject hook sources and unmanaged settings in the automation home."""
    runtime_home = codex_runtime_home()
    runtime_auth = runtime_home / "auth.json"
    source_auth = _source_codex_home() / "auth.json"
    if runtime_auth.is_symlink() and runtime_auth.resolve() != source_auth.resolve():
        raise SandboxUnsupportedError(
            f"Codex runtime auth link points to an unexpected file: {runtime_auth}"
        )
    if runtime_auth.exists() and not runtime_auth.is_symlink():
        raise SandboxUnsupportedError(
            f"Codex runtime auth path must be a managed symlink: {runtime_auth}"
        )
    if require_auth and not source_auth.is_file():
        raise SandboxUnsupportedError(
            f"Codex authentication not found at {source_auth}; run `codex login` first"
        )
    if require_auth and not runtime_auth.is_symlink():
        raise SandboxUnsupportedError(
            f"Codex automation home is not initialized; run setup-hooks: {runtime_home}"
        )
    # Read-only: this raises on anything unmanaged that is not Codex's own
    # state. Repair belongs to prepare_codex_runtime_home, the mutating entry
    # point — a verify that writes would mutate the very home it is about to
    # judge compromised, and doctor/reporting paths call this too.
    _managed_untrusted_projects(runtime_home / "config.toml")
    unexpected = [path for path in (runtime_home / "hooks.json",) if path.exists()]
    if unexpected:
        raise SandboxUnsupportedError(
            "Codex automation home contains unvetted hook/config sources: "
            + ", ".join(str(path) for path in unexpected)
        )
    return runtime_home


def _outside_write_roots(path: Path, write_roots: Iterable[Path]) -> Path:
    resolved = path.resolve(strict=True)
    for root in write_roots:
        resolved_root = root.resolve()
        if resolved == resolved_root or resolved.is_relative_to(resolved_root):
            raise SandboxUnsupportedError(
                "Codex guardrail runtime must be outside agent write roots: "
                f"{resolved} is under {resolved_root}"
            )
    return resolved


def build_codex_session_hook_argv(*, write_roots: Iterable[Path] = ()) -> list[str]:
    """Return global CLI flags for the immutable orchestrator PreToolUse hook."""
    roots = tuple(write_roots)
    python = _outside_write_roots(Path(sys.executable), roots)
    policy = _outside_write_roots(Path(block_no_verify.__file__), roots)
    command = shlex.join([str(python), str(policy), "--mode", "codex"])
    hook = (
        'hooks.PreToolUse=[{matcher="Bash",hooks=['
        f'{{type="command",command={json.dumps(command)}}}'
        "]}]"
    )
    return [
        "-c",
        "features.hooks=true",
        "-c",
        "features.plugins=false",
        "-c",
        "features.plugin_sharing=false",
        "-c",
        "features.remote_plugin=false",
        "-c",
        'shell_environment_policy.exclude=["CODEX_HOME"]',
        "-c",
        hook,
        "--dangerously-bypass-hook-trust",
    ]


__all__ = [
    "build_codex_session_hook_argv",
    "codex_runtime_home",
    "prepare_codex_runtime_home",
    "verify_codex_runtime_home",
]
