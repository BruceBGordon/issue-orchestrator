"""Monotonic clock helper for portable Make validation markers."""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Sequence


class ValidationMarkerClock:
    """Own monotonic target timestamps and fail-fast elapsed arithmetic."""

    def __init__(self, monotonic_nanoseconds: Callable[[], int]) -> None:
        if not callable(monotonic_nanoseconds):
            raise ValueError(
                "ValidationMarkerClock.monotonic_nanoseconds must be callable"
            )
        self._monotonic_nanoseconds = monotonic_nanoseconds

    def now_nanoseconds(self) -> int:
        observed = self._monotonic_nanoseconds()
        if type(observed) is not int or observed < 1:
            raise RuntimeError("monotonic clock must return positive nanoseconds")
        return observed

    @staticmethod
    def elapsed_seconds(started_nanoseconds: int, ended_nanoseconds: int) -> int:
        for name, value in (
            ("started_nanoseconds", started_nanoseconds),
            ("ended_nanoseconds", ended_nanoseconds),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        elapsed_nanoseconds = ended_nanoseconds - started_nanoseconds
        if elapsed_nanoseconds < 0:
            raise RuntimeError("monotonic validation clock moved backwards")
        return elapsed_nanoseconds // 1_000_000_000


def _canonical_positive_integer(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if value < 1 or str(value) != raw:
        raise argparse.ArgumentTypeError(
            "must be a positive base-ten integer without padding"
        )
    return value


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("now")
    elapsed = subparsers.add_parser("elapsed")
    elapsed.add_argument("started_nanoseconds", type=_canonical_positive_integer)
    elapsed.add_argument("ended_nanoseconds", type=_canonical_positive_integer)
    parsed = parser.parse_args(arguments)
    clock = ValidationMarkerClock(time.monotonic_ns)
    if parsed.command == "now":
        print(clock.now_nanoseconds())
        return 0
    if parsed.command == "elapsed":
        print(
            clock.elapsed_seconds(
                parsed.started_nanoseconds,
                parsed.ended_nanoseconds,
            )
        )
        return 0
    raise AssertionError(f"unsupported validation marker clock command: {parsed.command}")


if __name__ == "__main__":
    raise SystemExit(main())
