"""Real-host contract tests for collision-resistant process identities."""

from __future__ import annotations

import os

from issue_orchestrator.adapters.kernel_process_identity import (
    build_kernel_process_identity_observer,
)
from issue_orchestrator.domain.process_group import ProcessIdentityPresent


def test_system_identity_observer_returns_stable_exact_current_process_fact() -> None:
    observer = build_kernel_process_identity_observer()

    first = observer.observe_process(os.getpid())
    second = observer.observe_process(os.getpid())

    assert type(first) is ProcessIdentityPresent
    assert type(second) is ProcessIdentityPresent
    assert first == second
    assert first.process_group_id == os.getpgrp()
    assert first.birth_identity.kernel_token.startswith(
        ("darwin-timeval:", "linux-boot-ticks:")
    )
