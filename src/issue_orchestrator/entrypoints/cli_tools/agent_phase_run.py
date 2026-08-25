"""Internal executor client for one issue-orchestrator agent lifecycle phase."""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence

from ...domain.executor import (
    ExecutorBoundedDeadline,
    ExecutorCommand,
    ExecutorConcurrencyRange,
    ExecutorDeadlineExceededError,
    ExecutorFairnessGroup,
    ExecutorRunSpecification,
    ExecutorWorkKey,
)
from ..bootstrap import build_executor


def _positive_float(raw: str) -> float:
    try:
        parsed = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-key", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--active-timeout-seconds", type=_positive_float, required=True)
    parser.add_argument(
        "--absolute-timeout-seconds", type=_positive_float, required=True
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(arguments)
    command_arguments = tuple(parsed.command)
    if command_arguments[:1] == ("--",):
        command_arguments = command_arguments[1:]
    try:
        result = build_executor().run(
            ExecutorRunSpecification(
                work_key=ExecutorWorkKey(parsed.work_key),
                fairness_group=ExecutorFairnessGroup(parsed.group),
                concurrency_range=ExecutorConcurrencyRange(1, 1),
                exclusive_resources=(),
            ),
            ExecutorCommand(
                command_arguments,
                ExecutorBoundedDeadline(
                    active_timeout_seconds=parsed.active_timeout_seconds,
                    absolute_timeout_seconds=parsed.absolute_timeout_seconds,
                ),
            ),
        )
    except ExecutorDeadlineExceededError as exc:
        print(f"agent phase deadline exceeded: reason={exc.reason.value} {exc}")
        return 124
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"agent phase execution failed: {exc}")
        return 2
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
