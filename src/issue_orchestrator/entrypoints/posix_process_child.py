"""Child entrypoint for retained POSIX process activation."""

from __future__ import annotations

import argparse

from ..execution.posix_process import run_posix_process_child


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-json", required=True)
    arguments = parser.parse_args()
    return run_posix_process_child(arguments.request_json)


if __name__ == "__main__":
    raise SystemExit(main())
