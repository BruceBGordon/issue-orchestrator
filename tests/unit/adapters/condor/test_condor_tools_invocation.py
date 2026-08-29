"""The tool boundary's contract: every invocation reads the POOL.

``CondorTools.invoke`` is the single place this package runs a
scheduler tool, which makes it the single place that decides what
environment those tools see. The rule it enforces is that the answer a
tool gives describes the pool's own configuration — not whatever the
calling shell happened to export.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor.lane_executor import CondorTools

_DUMP = "#!/bin/sh\nenv\n"


def _dumping_tools(tmp_path: Path, *, pool_config: Path | None = None) -> CondorTools:
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    for name in ("condor_submit", "condor_rm", "condor_q", "condor_config_val"):
        tool = binaries / name
        tool.write_text(_DUMP)
        tool.chmod(0o755)
    return CondorTools(
        submit=binaries / "condor_submit",
        remove=binaries / "condor_rm",
        query=binaries / "condor_q",
        config_query=binaries / "condor_config_val",
        pool_config=pool_config,
    )


def _environment_seen(tools: CondorTools) -> dict[str, str]:
    completed = tools.invoke((str(tools.config_query),))
    assert completed.returncode == 0, completed.stderr
    seen: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            seen[key] = value
    return seen


def test_per_process_macro_overrides_never_reach_the_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_CONDOR_<KNOB>` overrides <KNOB> for one process and is invisible
    to the daemons. Read through one and the check reports on a
    configuration the pool is not running — masking real drift, or
    manufacturing fake drift (residual on N1, #7132 review). On the
    submit path the same export would quietly change how lanes run."""
    monkeypatch.setenv("_CONDOR_IO_INTENT_LOAD_BACKOFF", " False ")
    monkeypatch.setenv("_CONDOR_MOUNT_UNDER_SCRATCH", "")
    monkeypatch.setenv("_CONDOR_CONCURRENCY_LIMIT_DEFAULT", "1")

    seen = _environment_seen(_dumping_tools(tmp_path))

    leaked = [key for key in seen if key.startswith("_CONDOR_")]
    assert not leaked, f"macro overrides reached a scheduler tool: {leaked}"


def test_the_rest_of_the_environment_is_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scrubbing is surgical: the tools still need PATH, HOME and the
    caller's ordinary environment to run at all."""
    monkeypatch.setenv("IO_SENTINEL_FOR_TEST", "kept")
    monkeypatch.setenv("_CONDOR_ANYTHING", "dropped")

    seen = _environment_seen(_dumping_tools(tmp_path))

    assert seen["IO_SENTINEL_FOR_TEST"] == "kept"
    assert "PATH" in seen
    assert "_CONDOR_ANYTHING" not in seen


def test_a_variable_merely_containing_the_prefix_is_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the override PREFIX is meaningful to the scheduler; a name
    that happens to contain it elsewhere is an ordinary variable."""
    monkeypatch.setenv("MY_CONDOR_SETTING", "kept")

    seen = _environment_seen(_dumping_tools(tmp_path))

    assert seen["MY_CONDOR_SETTING"] == "kept"


def test_the_pool_configuration_is_still_pinned(tmp_path: Path) -> None:
    """The one variable this boundary SETS, unaffected by the scrub:
    without it a personal-pool invocation would read the ambient
    system configuration instead."""
    pool_config = tmp_path / "etc" / "condor_config"
    pool_config.parent.mkdir(parents=True)
    pool_config.write_text("# pool config\n")

    seen = _environment_seen(_dumping_tools(tmp_path, pool_config=pool_config))

    assert seen["CONDOR_CONFIG"] == str(pool_config)


def test_the_caller_process_environment_is_not_mutated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scrubbing builds the child's environment; it must not reach back
    into this process, where it would silently change unrelated work."""
    monkeypatch.setenv("_CONDOR_IO_INTENT_LOAD_BACKOFF", "False")

    _environment_seen(_dumping_tools(tmp_path))

    assert os.environ["_CONDOR_IO_INTENT_LOAD_BACKOFF"] == "False"
