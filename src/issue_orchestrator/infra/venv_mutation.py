"""Behavior-level authority over Python-environment mutation.

The owner receives the target AND the operation, and returns or executes one
validated decision. Asking only "who owns this venv?" left each caller to
decide whether ``shared`` permitted a dependency sync, a project install, or a
full recreate -- and they answered differently. That drift is what let a shared
environment be rebuilt by ``uv venv``, let two project syncs be redirected by
an ambient ``UV_PROJECT_ENVIRONMENT`` after authorizing a different venv, and
let contradictory authority records be accepted as authorization.

Everything a caller could get wrong is therefore decided here once:

* which operations an outcome permits;
* that the record is internally consistent -- not timed out, exit code matching
  the outcome, the returned target equal to the canonical requested target, and
  all required fields present;
* that execution is bound to the authorized environment.

The decision engine is the shell script in ``resources/`` because Control
Center must consult it before this package is importable; this class is the
single Python entry point to it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..ports.command_runner import CommandRunner

GUARD_RESOURCE = Path(__file__).resolve().parent.parent / "resources" / "venv_guard.sh"


class VenvMutationRefused(RuntimeError):
    """The authority did not authorize the requested mutation."""


class VenvOutcome(str, Enum):
    OWNED = "owned"
    SHARED = "shared"
    BROKEN = "broken"
    UNCLAIMED = "unclaimed"


class VenvOperation(str, Enum):
    """What the caller intends to do, not merely which venv it touches."""

    SYNC_DEPENDENCIES = "sync-dependencies"
    INSTALL_PROJECT = "install-project"
    RECREATE = "recreate"


# The exit code each outcome must arrive with. A record whose text and status
# disagree is not a weaker authorization -- it is not an authorization.
_EXPECTED_EXIT = {
    VenvOutcome.OWNED: 0,
    VenvOutcome.SHARED: 1,
    VenvOutcome.BROKEN: 2,
    VenvOutcome.UNCLAIMED: 3,
}

_REQUIRED_FIELDS = ("outcome", "venv", "operation", "allowed")


@dataclass(frozen=True, slots=True)
class VenvMutationDecision:
    outcome: VenvOutcome
    operation: VenvOperation
    venv: Path
    sync_args: tuple[str, ...]
    reason: str
    remedy: str = ""

    @property
    def may_install_project(self) -> bool:
        return self.outcome is VenvOutcome.OWNED


class VenvMutationAuthority:
    """Resolve, validate, and execute environment mutation for one target."""

    def __init__(self, runner: CommandRunner, *, guard_path: Path | None = None) -> None:
        self._runner = runner
        # Resolved from THIS installation, never from the target checkout.
        self._guard = guard_path or GUARD_RESOURCE

    # ---------------------------------------------------------------- decide

    def authorize(
        self,
        *,
        checkout: Path,
        operation: VenvOperation,
        venv: Path | None = None,
    ) -> VenvMutationDecision:
        """Return the decision for ``operation`` on ``venv``, or refuse."""
        target = (venv or (checkout / ".venv")).expanduser()
        if not target.is_absolute():
            target = (checkout / target).resolve()
        command = [
            str(self._guard),
            "decide",
            "--quiet",
            "--checkout",
            str(checkout),
            "--venv",
            str(target),
            "--operation",
            operation.value,
        ]
        try:
            result = self._runner.run(command, cwd=checkout, timeout_seconds=30)
        except OSError as exc:
            raise VenvMutationRefused(
                f"Cannot consult the venv mutation authority at {self._guard}: {exc}"
            ) from exc

        return self._validated(result, target=target, operation=operation)

    def _validated(self, result, *, target: Path, operation: VenvOperation):
        if getattr(result, "timed_out", False):
            raise VenvMutationRefused(
                f"The venv mutation authority timed out deciding {target}; "
                f"refusing rather than assuming an outcome"
            )

        record = _parse_decision(result.stdout)
        missing = [field for field in _REQUIRED_FIELDS if field not in record]
        if missing:
            raise VenvMutationRefused(
                f"The venv mutation authority returned an incomplete decision for "
                f"{target} (missing {', '.join(missing)}; exit={result.returncode})"
            )

        try:
            outcome = VenvOutcome(record["outcome"])
        except ValueError:
            raise VenvMutationRefused(
                f"The venv mutation authority returned an unknown outcome "
                f"{record['outcome']!r} for {target}"
            ) from None

        if result.returncode != _EXPECTED_EXIT[outcome]:
            raise VenvMutationRefused(
                f"The venv mutation authority contradicted itself for {target}: "
                f"outcome={outcome.value} but exit={result.returncode} "
                f"(expected {_EXPECTED_EXIT[outcome]}); refusing"
            )

        if record.get("operation") != operation.value:
            raise VenvMutationRefused(
                f"The venv mutation authority answered about "
                f"{record.get('operation')!r}, not the requested {operation.value!r}"
            )

        returned = Path(record["venv"])
        if returned != target:
            raise VenvMutationRefused(
                f"The venv mutation authority answered about {returned}, not the "
                f"requested {target}; refusing to act on a decision about a "
                f"different environment"
            )

        if record.get("allowed") != "yes":
            raise VenvMutationRefused(
                _refusal_message(target, operation, record.get("reason", outcome.value),
                                 record.get("remedy", ""))
            )

        sync_args = tuple(record.get("sync_args", "").split())
        if operation is not VenvOperation.RECREATE and not sync_args:
            raise VenvMutationRefused(
                f"The venv mutation authority authorized {operation.value} on "
                f"{target} but supplied no arguments; refusing rather than guessing"
            )
        return VenvMutationDecision(
            outcome=outcome,
            operation=operation,
            venv=returned,
            sync_args=sync_args,
            reason=record.get("reason", ""),
            remedy=record.get("remedy", ""),
        )

    # --------------------------------------------------------------- execute

    def run_uv(
        self,
        decision: VenvMutationDecision,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: int = 600,
    ):
        """Run one uv command bound to the authorized environment.

        Pinning lives here so no caller can authorize one venv and mutate
        another through an inherited ``UV_PROJECT_ENVIRONMENT``.
        """
        result = self._runner.run(
            argv, cwd=cwd, env=self.pinned_env(decision), timeout_seconds=timeout_seconds
        )
        if result.returncode != 0:
            raise VenvMutationRefused(
                f"{' '.join(argv[:2])} failed for {decision.venv}: "
                f"{(result.stderr or '').strip()[:400]}"
            )
        return result

    def sync(
        self,
        *,
        checkout: Path,
        venv: Path | None = None,
        uv: str = "uv",
        extra_args: tuple[str, ...] = (),
        operation: VenvOperation = VenvOperation.SYNC_DEPENDENCIES,
    ) -> VenvMutationDecision:
        """Authorize and run ``uv sync`` bound to the authorized environment."""
        decision = self.authorize(checkout=checkout, operation=operation, venv=venv)
        self.run_uv(decision, [uv, "sync", *decision.sync_args, *extra_args], cwd=checkout)
        return decision

    @staticmethod
    def pinned_env(decision: VenvMutationDecision) -> dict[str, str]:
        """Environment binding uv to the authorized target.

        uv honours ``UV_PROJECT_ENVIRONMENT``; inheriting it lets an ambient
        value redirect the mutation to an environment nobody authorized.
        ``UV_VENV_CLEAR`` is dropped because it makes ``uv venv`` delete and
        rebuild the target.
        """
        env = dict(os.environ)
        env["UV_PROJECT_ENVIRONMENT"] = str(decision.venv)
        env.pop("UV_VENV_CLEAR", None)
        return env


def _refusal_message(
    target: Path, operation: VenvOperation, reason: str, remedy: str
) -> str:
    message = f"Refusing to {operation.value} on {target}: {reason}."
    if remedy:
        message += f"\n  To fix: {remedy}"
    return message


def _parse_decision(stdout: str) -> dict[str, str]:
    record: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            record[key.strip()] = value.strip()
    return record


__all__ = [
    "GUARD_RESOURCE",
    "VenvMutationAuthority",
    "VenvMutationDecision",
    "VenvMutationRefused",
    "VenvOperation",
    "VenvOutcome",
]
