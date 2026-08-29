"""The tool boundary's contract, and its deliberate asymmetry.

``CondorTools`` is the single place this package runs a scheduler
tool, which makes it the single place that decides what environment
those tools see. It answers two different questions and the
environment rule differs by question:

- ``read_configuration`` asks ABOUT the pool, so the pool must be what
  answers: per-process macro overrides are scrubbed, or an ambient
  export could decide what the policy check believes.
- ``invoke`` runs the submit/remove/query tools, and the submit
  description sets ``getenv = true`` — the environment handed to
  ``condor_submit`` is the environment the LANE inherits. Carrying it
  faithfully is the contract, so nothing is scrubbed there.

Both halves are pinned here; the asymmetry is the design, not drift.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor.tools import CondorTools

_DUMP = "#!/bin/sh\nenv\n"


def _dumping_tools(tmp_path: Path, *, pool_config: Path | None = None) -> CondorTools:
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    for name in (
        "condor_submit",
        "condor_rm",
        "condor_q",
        "condor_config_val",
        "condor_status",
    ):
        tool = binaries / name
        tool.write_text(_DUMP)
        tool.chmod(0o755)
    return CondorTools(
        submit=binaries / "condor_submit",
        remove=binaries / "condor_rm",
        query=binaries / "condor_q",
        config_query=binaries / "condor_config_val",
        pool_query=binaries / "condor_status",
        pool_config=pool_config,
    )


def _parse(completed: object) -> dict[str, str]:
    stdout = getattr(completed, "stdout", "")
    seen: dict[str, str] = {}
    for line in str(stdout).splitlines():
        key, separator, value = line.partition("=")
        if separator:
            seen[key] = value
    return seen


def _read_environment(tools: CondorTools) -> dict[str, str]:
    """What the configuration-READ path lets a tool see."""
    completed = tools.read_configuration()
    assert completed.returncode == 0, completed.stderr
    return _parse(completed)


def _submit_environment(tools: CondorTools) -> dict[str, str]:
    """What the SUBMIT path lets a tool see - and, via getenv = true,
    what the lane itself inherits."""
    completed = tools.invoke((str(tools.submit),))
    assert completed.returncode == 0, completed.stderr
    return _parse(completed)


def _environment_seen(tools: CondorTools) -> dict[str, str]:
    return _read_environment(tools)


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

    seen = _read_environment(_dumping_tools(tmp_path))

    leaked = [key for key in seen if key.upper().startswith("_CONDOR_")]
    assert not leaked, f"macro overrides reached a configuration read: {leaked}"


@pytest.mark.parametrize(
    "prefix", ["_CONDOR_", "_condor_", "_CoNdOr_", "_Condor_", "_cOnDoR_"]
)
def test_overrides_are_scrubbed_from_reads_in_any_casing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prefix: str
) -> None:
    """The scheduler matches the override prefix case-INSENSITIVELY
    while POSIX environments are case-SENSITIVE, so `_condor_X` is a
    different variable that the tool honours identically. A
    case-sensitive scrub is one lowercase export away from useless —
    all four casings were proven live to inject (round 4, #7132
    review)."""
    monkeypatch.setenv(f"{prefix}IO_INTENT_LOAD_BACKOFF", "Bogus")

    seen = _read_environment(_dumping_tools(tmp_path))

    leaked = [key for key in seen if key.upper().startswith("_CONDOR_")]
    assert not leaked, f"a {prefix!r} override reached a configuration read: {leaked}"


def test_the_submit_path_carries_the_caller_environment_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other half of the asymmetry, and it is load-bearing: the
    submit description sets `getenv = true`, so what this process hands
    to condor_submit is what the LANE inherits. Scrubbing there would
    silently delete variables from the job's environment — a mutation
    nobody asked for, and not what the read-path property requires."""
    monkeypatch.setenv("_CONDOR_IO_INTENT_LOAD_BACKOFF", "False")
    monkeypatch.setenv("_condor_lowercase_variant", "False")
    monkeypatch.setenv("IO_SENTINEL_FOR_TEST", "kept")

    seen = _submit_environment(_dumping_tools(tmp_path))

    assert seen["_CONDOR_IO_INTENT_LOAD_BACKOFF"] == "False", (
        "the submit path must pass the caller's environment through "
        "unchanged - getenv = true carries it into the lane"
    )
    # The case-insensitive scrub must not leak across the asymmetry
    # either: neither casing is removed from what the lane inherits.
    assert seen["_condor_lowercase_variant"] == "False"
    assert seen["IO_SENTINEL_FOR_TEST"] == "kept"


def test_the_rest_of_the_environment_is_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scrubbing is surgical: the tools still need PATH, HOME and the
    caller's ordinary environment to run at all."""
    monkeypatch.setenv("IO_SENTINEL_FOR_TEST", "kept")
    monkeypatch.setenv("_CONDOR_ANYTHING", "dropped")

    seen = _read_environment(_dumping_tools(tmp_path))

    assert seen["IO_SENTINEL_FOR_TEST"] == "kept"
    assert "PATH" in seen
    assert "_CONDOR_ANYTHING" not in seen


def test_a_variable_merely_containing_the_prefix_is_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the override PREFIX is meaningful to the scheduler; a name
    that happens to contain it elsewhere is an ordinary variable."""
    monkeypatch.setenv("MY_CONDOR_SETTING", "kept")
    monkeypatch.setenv("my_condor_setting_lower", "kept")
    monkeypatch.setenv("CONDOR_CONFIG_LIKE", "kept")

    seen = _environment_seen(_dumping_tools(tmp_path))

    assert seen["MY_CONDOR_SETTING"] == "kept"
    assert seen["my_condor_setting_lower"] == "kept"
    assert seen["CONDOR_CONFIG_LIKE"] == "kept"


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
