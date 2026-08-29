"""Shared validation timing artifact helpers."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..ports.machine_state import MachineStateSampler
from .machine_state import default_machine_state_sampler, stamp_machine_state

_CONFIG_KEY_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"
_CONFIG_FIELD_PATTERN = rf"{_CONFIG_KEY_PATTERN}=\S+"
_CONFIG_RE = re.compile(
    rf"\[validate-timing\] CONFIG (?P<fields>{_CONFIG_FIELD_PATTERN}(?:\s+{_CONFIG_FIELD_PATTERN})*)\s*$"
)
_CONFIG_FIELD_RE = re.compile(rf"(?P<key>{_CONFIG_KEY_PATTERN})=(?P<value>\S+)")
_START_RE = re.compile(
    r"\[validate-timing\] START target=(?P<target>\S+) at=(?P<at>\S+)"
)
_END_RE = re.compile(
    r"\[validate-timing\] END target=(?P<target>\S+) "
    r"status=(?P<status>-?\d+) elapsed=(?P<elapsed>\d+)s at=(?P<at>\S+)"
)


def resolve_git_dir(worktree: Path) -> Path | None:
    """Resolve the git dir for the current worktree without shelling out."""
    dot_git = worktree / ".git"
    if dot_git.is_dir():
        return dot_git
    if not dot_git.is_file():
        return None
    content = dot_git.read_text(encoding="utf-8").strip()
    prefix = "gitdir: "
    if not content.startswith(prefix):
        return None
    git_dir = Path(content[len(prefix) :].strip())
    if not git_dir.is_absolute():
        git_dir = (worktree / git_dir).resolve()
    return git_dir


def resolve_git_common_dir(worktree: Path) -> Path | None:
    """Resolve the repository's shared git dir for cross-worktree artifacts."""
    git_dir = resolve_git_dir(worktree)
    if git_dir is None:
        return None
    commondir_file = git_dir / "commondir"
    if not commondir_file.exists():
        return git_dir
    common_dir = Path(commondir_file.read_text(encoding="utf-8").strip())
    if not common_dir.is_absolute():
        common_dir = (git_dir / common_dir).resolve()
    return common_dir


def read_head_ref_name(git_dir: Path) -> str | None:
    """Read the current branch name from HEAD when it points at a ref."""
    head_file = git_dir / "HEAD"
    if not head_file.exists():
        return None
    head = head_file.read_text(encoding="utf-8").strip()
    prefix = "ref: refs/heads/"
    if not head.startswith(prefix):
        return None
    return head[len(prefix) :]


def read_branch_name(worktree: Path) -> str | None:
    """Best-effort branch name for diagnostics records without git subprocesses."""
    git_dir = resolve_git_dir(worktree)
    if git_dir is None:
        return None
    branch = read_head_ref_name(git_dir)
    if branch:
        return branch
    common_dir = resolve_git_common_dir(worktree)
    if common_dir is not None:
        return read_head_ref_name(common_dir)
    return None


def current_branch_name(worktree: Path) -> str | None:
    """Best-effort branch name for diagnostics records."""
    return read_branch_name(worktree)


def read_head_sha(worktree: Path) -> str | None:
    """The worktree's HEAD commit SHA via file reads — NO subprocess (#6824 R9).

    Reads ``.git/HEAD``; a detached HEAD holds the SHA directly, otherwise the
    named ref is resolved from the loose ref file or ``packed-refs`` in the
    shared git dir. Best-effort: None when it cannot be determined.
    """
    git_dir = resolve_git_dir(worktree)
    if git_dir is None:
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head or None
    ref = head[len("ref:") :].strip()
    common = resolve_git_common_dir(worktree) or git_dir
    try:
        sha = (common / ref).read_text(encoding="utf-8").strip()
        if sha:
            return sha
    except OSError:
        pass
    try:
        for line in (common / "packed-refs").read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "^")):
                continue
            sha, _, name = stripped.partition(" ")
            if name == ref:
                return sha
    except OSError:
        pass
    return None


def get_shared_timings_file(worktree: Path) -> Path | None:
    """Return the shared JSONL timing file path for this repository."""
    common_dir = resolve_git_common_dir(worktree)
    if common_dir is None:
        return None
    return common_dir / "issue-orchestrator" / "validate-timings.jsonl"


def append_jsonl(path: Path | None, record: dict[str, object]) -> None:
    """Append one JSON object to a JSONL file, creating parents as needed."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        # Use one O_APPEND write so concurrent validation gates cannot interleave
        # JSONL fragments on local POSIX filesystems such as macOS APFS.
        written = os.write(fd, line)
        if written != len(line):
            raise OSError(f"short JSONL write to {path}: {written} of {len(line)}")
    finally:
        os.close(fd)


def build_timing_envelope(
    *,
    wall_started_at: datetime,
    monotonic_started_at: float,
    wall_ended_at: datetime | None = None,
    monotonic_ended_at: float | None = None,
) -> dict[str, object]:
    """Return common elapsed-time fields for validation timing records."""
    if wall_ended_at is None:
        wall_ended_at = datetime.now(timezone.utc)
    if monotonic_ended_at is None:
        monotonic_ended_at = time.monotonic()
    return {
        "monotonic_elapsed_seconds": round(
            monotonic_ended_at - monotonic_started_at, 3
        ),
        "wall_started_at": wall_started_at.isoformat(),
        "wall_ended_at": wall_ended_at.isoformat(),
        "wall_elapsed_seconds": round(
            (wall_ended_at - wall_started_at).total_seconds(), 3
        ),
    }


def append_validation_timing(
    worktree: Path,
    record: dict[str, object],
    machine_state: MachineStateSampler | None = None,
) -> None:
    """Append one validation timing record with shared worktree context.

    ``machine_state`` defaults to the process-wide host sampler; tests
    (and any caller wanting a different probe) inject their own.
    """
    payload: dict[str, object] = {
        "worktree": str(worktree),
        "branch": current_branch_name(worktree),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(record)
    # Stamped last so a caller's own keys can never displace the
    # envelope: "how long" is worthless without "under what".
    payload.update(
        stamp_machine_state(machine_state or default_machine_state_sampler())
    )
    append_jsonl(get_shared_timings_file(worktree), payload)


def record_gate_timings(
    suite: str,
    worktree: Path,
    command: str,
    stdout: str,
    stderr: str,
) -> None:
    """Record target timings from captured publish-gate output."""
    if suite != "publish_gate":
        return
    recorder = ValidateTimingRecorder(worktree=worktree, command=command)
    recorder.process_output(stdout)
    recorder.process_output(stderr)


@dataclass
class ValidateTimingRecorder:
    """Collect and persist per-target validate timings from marker lines."""

    worktree: Path
    command: str
    run_id: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    # Injectable so tests can fake the probe; defaults to the shared
    # per-process host sampler so one gate probes the host on a bounded
    # cadence no matter how many records it writes.
    machine_state: MachineStateSampler = field(
        default_factory=default_machine_state_sampler
    )
    branch: str | None = field(init=False)
    output_path: Path | None = field(init=False)
    config: dict[str, str] = field(default_factory=dict)
    starts: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.branch = current_branch_name(self.worktree)
        self.output_path = get_shared_timings_file(self.worktree)

    def process_output(self, output: str) -> None:
        """Process captured command output containing validate timing markers."""
        for line in output.splitlines():
            self.process_line(line)

    def process_line(self, line: str) -> None:
        config_match = _CONFIG_RE.search(line)
        if config_match:
            self.config = {
                field.group("key"): field.group("value")
                for field in _CONFIG_FIELD_RE.finditer(config_match.group("fields"))
            }
            return

        start_match = _START_RE.search(line)
        if start_match:
            self.starts[start_match.group("target")] = start_match.group("at")
            return

        end_match = _END_RE.search(line)
        if not end_match:
            return

        target = end_match.group("target")
        record: dict[str, object] = {
            "kind": "target_timing",
            "run_id": self.run_id,
            "command": self.command,
            "worktree": str(self.worktree),
            "branch": self.branch,
            "target": target,
            "status": int(end_match.group("status")),
            "elapsed_seconds": int(end_match.group("elapsed")),
            "started_at": self.starts.pop(target, None),
            "ended_at": end_match.group("at"),
        }
        self._append(record)

    def finalize(
        self,
        *,
        exit_code: int,
        total_elapsed_seconds: float,
        wall_started_at: datetime | None = None,
        monotonic_started_at: float | None = None,
        wall_ended_at: datetime | None = None,
        monotonic_ended_at: float | None = None,
    ) -> None:
        record: dict[str, object] = {
            "kind": "run_summary",
            "run_id": self.run_id,
            "command": self.command,
            "worktree": str(self.worktree),
            "branch": self.branch,
            "exit_code": exit_code,
            "total_elapsed_seconds": round(total_elapsed_seconds, 3),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        if wall_started_at is not None and monotonic_started_at is not None:
            record.update(
                build_timing_envelope(
                    wall_started_at=wall_started_at,
                    monotonic_started_at=monotonic_started_at,
                    wall_ended_at=wall_ended_at,
                    monotonic_ended_at=monotonic_ended_at,
                )
            )
        self._append(record)

    def append_resource_sample(self, sample: dict[str, object]) -> None:
        """Persist one periodic host resource sample."""
        record: dict[str, object] = {
            "kind": "resource_sample",
            "run_id": self.run_id,
            "command": self.command,
            "worktree": str(self.worktree),
            "branch": self.branch,
            **sample,
        }
        self._append(record)

    def _append(self, record: dict[str, object]) -> None:
        """The one exit every record this recorder writes goes through.

        Config context first, then the machine-state envelope — stamped
        last so neither a record's own fields nor a config key can
        displace the covariate that explains the timing (#7127).
        """
        for key, value in self.config.items():
            record[key] = value
        record.update(stamp_machine_state(self.machine_state))
        append_jsonl(self.output_path, record)
