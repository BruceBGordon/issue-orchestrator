"""Behavior-level authority over Python-environment mutation.

One owner receives the actual target and the operation, and returns or executes
a single validated decision. Callers do not interpret exit codes, re-derive
permitted arguments, choose the target, or map errors -- each of those, done
independently at each call site, is what produced the defects this replaces:

* the decision and its permitted arguments were fetched in two executions, so a
  failed second call silently degraded to an unrestricted project install;
* ``UV_PROJECT_ENVIRONMENT`` was inherited, so uv mutated a different
  environment than the one that had been authorized;
* the authority was resolved from the *target* checkout, so preparing an
  arbitrary repository failed merely because it does not carry this project's
  internal script;
* ``Path.exists()`` stood in for "runnable", so a non-executable authority
  raised a raw ``PermissionError`` instead of the declared domain error.

The decision engine itself is the shell script in ``resources/`` rather than
Python, because Control Center must consult it before this package is
importable. This class is the single Python entry point to it.
"""

from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class VenvMutationDecision:
    """One validated decision: what may happen, and with exactly which arguments."""

    outcome: VenvOutcome
    venv: Path
    sync_args: tuple[str, ...]
    reason: str
    remedy: str = ""

    @property
    def may_install_project(self) -> bool:
        return self.outcome is VenvOutcome.OWNED


class VenvMutationAuthority:
    """Resolve, invoke, and enforce the mutation decision for one environment."""

    def __init__(
        self,
        runner: CommandRunner,
        *,
        guard_path: Path | None = None,
    ) -> None:
        self._runner = runner
        # Resolved from THIS installation, never from the target checkout.
        self._guard = guard_path or GUARD_RESOURCE

    def authorize(self, *, checkout: Path, venv: Path | None = None) -> VenvMutationDecision:
        """Return the decision for ``venv``, or raise ``VenvMutationRefused``."""
        target = venv or (checkout / ".venv")
        command = [
            str(self._guard),
            "decide",
            "--quiet",
            "--checkout",
            str(checkout),
            "--venv",
            str(target),
        ]
        try:
            result = self._runner.run(command, cwd=checkout, timeout_seconds=30)
        except OSError as exc:
            # A missing or non-executable authority is not evidence of
            # ownership. Fail closed, in the declared domain error.
            raise VenvMutationRefused(
                f"Cannot consult the venv mutation authority at {self._guard}: {exc}"
            ) from exc

        record = _parse_decision(result.stdout)
        outcome_text = record.get("outcome", "")
        try:
            outcome = VenvOutcome(outcome_text)
        except ValueError:
            raise VenvMutationRefused(
                f"The venv mutation authority returned no usable decision for "
                f"{target} (exit={result.returncode}, output={result.stdout!r})"
            ) from None

        if outcome in (VenvOutcome.BROKEN, VenvOutcome.UNCLAIMED):
            # The remedy travels with the decision. Callers pass --quiet, so a
            # fix written only to the guard's stderr never reaches anyone.
            raise VenvMutationRefused(
                _refusal_message(
                    target,
                    record.get("reason", outcome.value),
                    record.get("remedy", ""),
                )
            )

        sync_args = tuple(record.get("sync_args", "").split())
        if not sync_args:
            # The decision and its arguments arrive together; an authorized
            # outcome with no arguments is a malformed record, not a licence to
            # run an unrestricted sync.
            raise VenvMutationRefused(
                f"The venv mutation authority authorized {target} but supplied "
                f"no arguments; refusing rather than guessing them"
            )
        return VenvMutationDecision(
            outcome=outcome,
            venv=Path(record.get("venv", str(target))),
            sync_args=sync_args,
            reason=record.get("reason", ""),
            remedy=record.get("remedy", ""),
        )

    def sync(
        self,
        *,
        checkout: Path,
        venv: Path | None = None,
        uv: str = "uv",
        extra_args: tuple[str, ...] = (),
    ) -> VenvMutationDecision:
        """Authorize, then run ``uv sync`` bound to the authorized environment."""
        decision = self.authorize(checkout=checkout, venv=venv)
        result = self._runner.run(
            [uv, "sync", *decision.sync_args, *extra_args],
            cwd=checkout,
            env=self.pinned_env(decision),
            timeout_seconds=600,
        )
        if result.returncode != 0:
            raise VenvMutationRefused(
                f"uv sync failed for {decision.venv}: {result.stderr.strip()[:400]}"
            )
        return decision

    @staticmethod
    def pinned_env(decision: VenvMutationDecision) -> dict[str, str]:
        """Environment that binds uv to the authorized target.

        uv honours ``UV_PROJECT_ENVIRONMENT``; inheriting it lets an ambient
        value redirect the mutation to an environment nobody authorized, so the
        authorized path is pinned explicitly on every invocation.
        """
        import os

        env = dict(os.environ)
        env["UV_PROJECT_ENVIRONMENT"] = str(decision.venv)
        # `uv venv` honours this and would delete and rebuild the target.
        env.pop("UV_VENV_CLEAR", None)
        return env


def _refusal_message(target: Path, reason: str, remedy: str) -> str:
    message = f"Refusing to mutate {target}: {reason}."
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
    "VenvOutcome",
]
