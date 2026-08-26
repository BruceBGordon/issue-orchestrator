"""Public fault proofs for PTY agent startup resource ownership."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from issue_orchestrator.execution.agent_runner import AgentRunner, AgentSpec
from issue_orchestrator.entrypoints.bootstrap_executor import (
    build_process_group_supervisor,
)
from issue_orchestrator.execution.session_interactions import (
    SessionInteractionHandler,
)
from tests.process_completion_fixture import PROCESS_COMPLETION_WATCHDOG
from tests.process_tree_fixture import ProcessTreeMember


def _agent_spec(
    tmp_path: Path,
    command: tuple[str, ...],
) -> AgentSpec:
    return AgentSpec(
        command=list(command),
        working_dir=tmp_path,
        timeout_seconds=30,
        output_dir=tmp_path,
        log_path=(tmp_path / "terminal-recording.jsonl").resolve(),
    )


def _recording_descriptors(recording_path: Path) -> tuple[int, ...]:
    descriptors: list[int] = []
    for raw_descriptor in os.listdir("/dev/fd"):
        try:
            descriptor = int(raw_descriptor)
            if os.path.samefile(f"/dev/fd/{descriptor}", recording_path):
                descriptors.append(descriptor)
        except (FileNotFoundError, OSError, ValueError):
            continue
    return tuple(sorted(descriptors))


class _BindFailureAfterProcessTreeReadiness(SessionInteractionHandler):
    """Fail session construction only after the real child tree is observable."""

    def __init__(self, process_record: Path) -> None:
        super().__init__(session_name="fault-proof", rules=())
        self._process_record = process_record

    def bind_sender(self, sender: Callable[[str], bool]) -> None:
        del sender
        PROCESS_COMPLETION_WATCHDOG.wait_for_path(
            self._process_record,
            operation="agent startup process-tree readiness",
        )
        raise RuntimeError("injected interaction binding failure")


def test_spawn_failure_closes_the_already_open_recording(tmp_path: Path) -> None:
    spec = _agent_spec(
        tmp_path,
        (str((tmp_path / "missing-agent-executable").resolve()),),
    )

    with pytest.raises(Exception, match="missing-agent-executable"):
        AgentRunner(build_process_group_supervisor()).start_direct(spec)

    assert spec.log_path is not None
    assert _recording_descriptors(spec.log_path) == ()


def test_session_binding_failure_contains_child_tree_and_closes_recording(
    tmp_path: Path,
) -> None:
    process_record = (tmp_path / "agent-processes").resolve()
    descendant_source = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(300)\n"
    )
    agent_source = (
        "import os, pathlib, subprocess, sys, time\n"
        f"child = subprocess.Popen([{sys.executable!r}, '-c', "
        f"{descendant_source!r}], stdout=subprocess.PIPE, text=True)\n"
        "assert child.stdout is not None\n"
        "assert child.stdout.readline().strip() == 'ready'\n"
        f"pathlib.Path({str(process_record)!r}).write_text("
        "f'{os.getpid()} {child.pid}', encoding='utf-8')\n"
        "time.sleep(300)\n"
    )
    spec = _agent_spec(
        tmp_path,
        (sys.executable, "-c", agent_source),
    )

    with pytest.raises(
        RuntimeError,
        match="injected interaction binding failure",
    ):
        AgentRunner(build_process_group_supervisor()).start_direct(
            spec,
            interaction_handler=_BindFailureAfterProcessTreeReadiness(
                process_record
            ),
        )

    process_ids = tuple(
        int(value) for value in process_record.read_text(encoding="utf-8").split()
    )
    assert len(process_ids) == 2
    for process_id in process_ids:
        ProcessTreeMember(process_id).assert_contained()
    assert spec.log_path is not None
    assert _recording_descriptors(spec.log_path) == ()


def test_binding_failure_contains_descendant_after_group_leader_exits(
    tmp_path: Path,
) -> None:
    process_record = (tmp_path / "agent-processes").resolve()
    leader_exited = (tmp_path / "agent-leader-exited").resolve()
    descendant_source = (
        "import os, pathlib, signal, sys, time\n"
        "leader = int(sys.argv[1])\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "while os.getppid() == leader:\n"
        "    time.sleep(0.01)\n"
        "pathlib.Path(sys.argv[2]).write_text('exited', encoding='utf-8')\n"
        "time.sleep(300)\n"
    )
    agent_source = (
        "import os, pathlib, subprocess, sys\n"
        f"child = subprocess.Popen([{sys.executable!r}, '-c', "
        f"{descendant_source!r}, str(os.getpid()), {str(leader_exited)!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL)\n"
        f"pathlib.Path({str(process_record)!r}).write_text("
        "f'{os.getpid()} {child.pid}', encoding='utf-8')\n"
    )
    spec = _agent_spec(
        tmp_path,
        (sys.executable, "-c", agent_source),
    )

    with pytest.raises(
        RuntimeError,
        match="injected interaction binding failure",
    ):
        AgentRunner(build_process_group_supervisor()).start_direct(
            spec,
            interaction_handler=_BindFailureAfterProcessTreeReadiness(
                leader_exited
            ),
        )

    process_ids = tuple(
        int(value) for value in process_record.read_text(encoding="utf-8").split()
    )
    assert len(process_ids) == 2
    for process_id in process_ids:
        ProcessTreeMember(process_id).assert_contained()
    assert spec.log_path is not None
    assert _recording_descriptors(spec.log_path) == ()
