"""Executable composition root for the terminal process-group owner child."""

from __future__ import annotations

import argparse

from .bootstrap_executor import build_atomic_record_store_factory
from ..execution.terminal_session_owner import run_terminal_session_owner_child


def main() -> int:
    parser = argparse.ArgumentParser(description="Own one terminal process group")
    parser.add_argument("--owner-request-json", required=True)
    arguments = parser.parse_args()
    return run_terminal_session_owner_child(
        arguments.owner_request_json,
        build_atomic_record_store_factory(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
