"""Invocation-scoped Codex hook wiring for orchestrated sessions."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import tomllib
from typing import Iterator

from ...domain.sandbox_scope import SandboxUnsupportedError
from . import block_no_verify

_RUNTIME_ROOT_ENV = "ISSUE_ORCHESTRATOR_CODEX_RUNTIME_ROOT"
_RUNTIME_LAYOUT_VERSION = "v2"


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
    if not source_auth.is_file():
        raise SandboxUnsupportedError(
            f"Codex authentication not found at {source_auth}; run `codex login` first"
        )
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
    else:
        runtime_auth.symlink_to(source_auth)
    verify_codex_runtime_home()
    projects = tuple(Path(path).resolve() for path in untrusted_projects)
    if projects:
        _record_untrusted_projects(runtime_home, projects)
        verify_codex_runtime_home()
    return runtime_home


def _managed_untrusted_projects(config_path: Path) -> set[str]:
    if not config_path.exists():
        return set()
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SandboxUnsupportedError(
            f"Codex automation config is unreadable or invalid: {config_path}: {exc}"
        ) from exc
    if set(document) != {"projects"} or not isinstance(
        projects := document["projects"], dict
    ):
        raise SandboxUnsupportedError(
            f"Codex automation config contains unmanaged settings: {config_path}"
        )
    for project, settings in projects.items():
        if not isinstance(project, str) or settings != {"trust_level": "untrusted"}:
            raise SandboxUnsupportedError(
                f"Codex automation config contains managed-project drift: {config_path}"
            )
    return set(projects)


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
        configured = _managed_untrusted_projects(config_path)
        configured.update(str(project) for project in projects)
        lines = ["# Managed by issue-orchestrator; all project layers stay untrusted."]
        for project in sorted(configured):
            lines.extend(
                [
                    "",
                    f"[projects.{json.dumps(project)}]",
                    'trust_level = "untrusted"',
                ]
            )
        temp_path = runtime_home / f".config.toml.{os.getpid()}.tmp"
        try:
            with temp_path.open("x", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, config_path)
        finally:
            temp_path.unlink(missing_ok=True)


def verify_codex_runtime_home() -> Path:
    """Reject hook sources and unmanaged settings in the automation home."""
    runtime_home = codex_runtime_home()
    runtime_auth = runtime_home / "auth.json"
    source_auth = _source_codex_home() / "auth.json"
    if not runtime_auth.is_symlink() or runtime_auth.resolve() != source_auth.resolve():
        raise SandboxUnsupportedError(
            f"Codex automation home is not initialized; run setup-hooks: {runtime_home}"
        )
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
