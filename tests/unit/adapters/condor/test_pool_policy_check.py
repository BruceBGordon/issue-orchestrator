"""Pool-policy self-check: every drift case, against a stubbed tool.

Hermetic — the config tool is a shell stub scripted per knob, so no
pool is required and each drift can be produced deliberately. The live
counterpart is ``tests/integration/test_condor_pool_policy.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor.lane_executor import CondorTools
from issue_orchestrator.adapters.condor.pool_policy import (
    _REQUIRED_SETTINGS,
    CondorPoolPolicyCheck,
)
from issue_orchestrator.domain.lane_execution import (
    LaneExecutorError,
    LanePolicyReport,
)
from issue_orchestrator.ports.lane_policy_check import LanePolicyCheck

_BACKOFF_FILE = "91-io-load-backoff.conf"
_CAPACITY_FILE = "92-io-pool-capacity.conf"
_REQUIRED_KNOBS = {
    "CONCURRENCY_LIMIT_DEFAULT",
    "PERIODIC_EXPR_INTERVAL",
    "MOUNT_UNDER_SCRATCH",
}

# A modern pool that opted out of both optional policies: the three
# hard settings in place, an intent record present and declaring
# neither opt-in.
_HEALTHY = {
    "CONCURRENCY_LIMIT_DEFAULT": "1",
    "PERIODIC_EXPR_INTERVAL": "5",
    "IO_INTENT_LOAD_BACKOFF": "False",
}
_DEFAULT_SOURCES = (
    "/pool/etc/condor_config",
    "/pool/local/config.d/00-personal-condor",
    "/pool/local/config.d/90-issue-orchestrator-lanes.conf",
    "/pool/local/config.d/90-io-policy-intent.conf",
)


def _stub_tools(
    tmp_path: Path,
    *,
    values: dict[str, str] | None = None,
    sources: tuple[str, ...] = _DEFAULT_SOURCES,
    config_exit: int = 0,
) -> CondorTools:
    """A config tool that answers from ``values``.

    A knob absent from ``values`` is answered exactly as the real tool
    answers an unset (or empty-valued) knob: "Not defined: KNOB" on
    stderr with exit 1.
    """
    answered = _HEALTHY if values is None else values
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    listing = "Configuration source:\n" + "".join(
        f"\t{source}\n" for source in sources[:1]
    )
    if sources[1:]:
        listing += "Local configuration sources:\n" + "".join(
            f"\t{source}\n" for source in sources[1:]
        )
    branches = "".join(
        f'  {knob}) echo "{value}";;\n' for knob, value in answered.items()
    )
    script = (
        "#!/bin/sh\n"
        'if [ "$1" = "-config" ]; then\n'
        f"  printf '%s' '{listing}'\n"
        f"  exit {config_exit}\n"
        "fi\n"
        'case "$1" in\n'
        f"{branches}"
        '  *) echo "Not defined: $1" >&2; exit 1;;\n'
        "esac\n"
    )
    for name in ("condor_submit", "condor_rm", "condor_q", "condor_config_val"):
        tool = binaries / name
        tool.write_text(script if name == "condor_config_val" else "#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
    return CondorTools(
        submit=binaries / "condor_submit",
        remove=binaries / "condor_rm",
        query=binaries / "condor_q",
        config_query=binaries / "condor_config_val",
    )


def _pool(
    tmp_path: Path,
    *,
    backoff_intent: str = "False",
    capacity_intent: str | None = None,
    installed: tuple[str, ...] = (),
) -> CondorTools:
    """A healthy pool whose optional-policy intent and installed files
    are set independently — which is exactly the axis the four drift
    combinations below explore."""
    values = {**_HEALTHY, "IO_INTENT_LOAD_BACKOFF": backoff_intent}
    if capacity_intent is not None:
        values["IO_INTENT_CAPACITY_PERCENT"] = capacity_intent
    return _stub_tools(
        tmp_path,
        values=values,
        sources=(
            *_DEFAULT_SOURCES,
            *(f"/pool/local/config.d/{name}" for name in installed),
        ),
    )


def _inspect(tools: CondorTools) -> LanePolicyReport:
    return CondorPoolPolicyCheck(tools).inspect()


def _drifted(report: LanePolicyReport) -> set[str]:
    return {invariant.knob for invariant in report.drifted}


def _observed(report: LanePolicyReport, knob: str) -> str:
    for invariant in report.invariants:
        if invariant.knob == knob:
            return invariant.observed
    raise AssertionError(f"{knob} is not among the checked invariants")


def _expected(report: LanePolicyReport, knob: str) -> str:
    for invariant in report.invariants:
        if invariant.knob == knob:
            return invariant.expected
    raise AssertionError(f"{knob} is not among the checked invariants")


def test_healthy_pool_asserts_settings_and_both_policy_files(
    tmp_path: Path,
) -> None:
    report = _inspect(_pool(tmp_path))

    assert isinstance(report, LanePolicyReport)
    assert report.drifted == ()
    assert {invariant.knob for invariant in report.invariants} == {
        "CONCURRENCY_LIMIT_DEFAULT",
        "PERIODIC_EXPR_INTERVAL",
        "MOUNT_UNDER_SCRATCH",
        _BACKOFF_FILE,
        _CAPACITY_FILE,
    }
    # Everything the check has to say is asserted; there is no advisory
    # channel a reader could mistake for a passing check.
    assert len(report.invariants) == 5


def test_check_instance_satisfies_the_policy_port(tmp_path: Path) -> None:
    check: LanePolicyCheck = CondorPoolPolicyCheck(_pool(tmp_path))
    assert isinstance(check, LanePolicyCheck)


def test_empty_mount_under_scratch_is_satisfied_when_the_tool_says_undefined(
    tmp_path: Path,
) -> None:
    """`MOUNT_UNDER_SCRATCH =` and an unset knob are indistinguishable
    to the tool (verified live: both answer "Not defined"), and mean
    the same thing — no value in effect. The check must agree, or every
    correctly configured pool reads as drifted."""
    report = _inspect(_pool(tmp_path))

    assert _observed(report, "MOUNT_UNDER_SCRATCH") == ""
    assert report.drifted == ()


def test_non_empty_mount_under_scratch_is_drift(tmp_path: Path) -> None:
    """The shipped Linux default; a lane whose working directory lives
    under the real /tmp holds with "Cannot access initial working
    directory" when this comes back."""
    report = _inspect(
        _stub_tools(
            tmp_path, values={**_HEALTHY, "MOUNT_UNDER_SCRATCH": "/tmp,/var/tmp"}
        )
    )

    assert _drifted(report) == {"MOUNT_UNDER_SCRATCH"}
    assert "/tmp,/var/tmp" in report.drifted[0].describe()


def test_lost_concurrency_limit_default_is_drift_naming_the_knob(
    tmp_path: Path,
) -> None:
    report = _inspect(
        _stub_tools(
            tmp_path,
            values={
                "PERIODIC_EXPR_INTERVAL": "5",
                "IO_INTENT_LOAD_BACKOFF": "False",
            },
        )
    )

    assert _drifted(report) == {"CONCURRENCY_LIMIT_DEFAULT"}
    described = report.drifted[0].describe()
    assert "CONCURRENCY_LIMIT_DEFAULT" in described
    assert "'1'" in described


def test_changed_concurrency_limit_default_is_drift(tmp_path: Path) -> None:
    report = _inspect(
        _stub_tools(tmp_path, values={**_HEALTHY, "CONCURRENCY_LIMIT_DEFAULT": "4"})
    )

    assert _drifted(report) == {"CONCURRENCY_LIMIT_DEFAULT"}
    assert _observed(report, "CONCURRENCY_LIMIT_DEFAULT") == "4"


def test_changed_periodic_expr_interval_is_drift(tmp_path: Path) -> None:
    report = _inspect(
        _stub_tools(tmp_path, values={**_HEALTHY, "PERIODIC_EXPR_INTERVAL": "60"})
    )

    assert _drifted(report) == {"PERIODIC_EXPR_INTERVAL"}
    assert _observed(report, "PERIODIC_EXPR_INTERVAL") == "60"


def test_every_drifted_knob_is_named_in_one_pass(tmp_path: Path) -> None:
    """Drift is data, not an exception: a wholesale-reverted pool is
    fixed in one round, not one knob per gate attempt. A pool reverted
    this far has also lost its intent record, so that is named too."""
    report = _inspect(
        _stub_tools(tmp_path, values={"MOUNT_UNDER_SCRATCH": "/tmp"})
    )

    assert _drifted(report) == {
        "CONCURRENCY_LIMIT_DEFAULT",
        "PERIODIC_EXPR_INTERVAL",
        "MOUNT_UNDER_SCRATCH",
        "IO_INTENT_LOAD_BACKOFF",
    }


# --- present-iff-intended: the four combinations, per policy file ---


def test_intended_backoff_policy_installed_is_satisfied(tmp_path: Path) -> None:
    report = _inspect(
        _pool(tmp_path, backoff_intent="True", installed=(_BACKOFF_FILE,))
    )

    assert report.drifted == ()
    assert _expected(report, _BACKOFF_FILE) == "installed"


def test_intended_backoff_policy_missing_is_drift(tmp_path: Path) -> None:
    """THE reproduced false green (C1, #7132 review): a pool brought up
    with IO_CONDOR_LOAD_BACKOFF=1 whose 91- file was removed by hand
    used to preflight clean, because nothing recorded that the policy
    had been asked for. The intent record is what makes this a
    failure."""
    report = _inspect(_pool(tmp_path, backoff_intent="True"))

    assert _drifted(report) == {_BACKOFF_FILE}
    described = report.drifted[0].describe()
    assert "'installed'" in described and "'absent'" in described


def test_unintended_backoff_policy_absent_is_satisfied(tmp_path: Path) -> None:
    report = _inspect(_pool(tmp_path, backoff_intent="False"))

    assert report.drifted == ()
    assert _expected(report, _BACKOFF_FILE) == "absent"


def test_unintended_backoff_policy_installed_is_drift(tmp_path: Path) -> None:
    """Stale policy nobody asked for: lanes would be frozen by a
    backoff policy the operator opted out of."""
    report = _inspect(
        _pool(tmp_path, backoff_intent="False", installed=(_BACKOFF_FILE,))
    )

    assert _drifted(report) == {_BACKOFF_FILE}
    assert _observed(report, _BACKOFF_FILE) == "installed"


def test_intended_capacity_dial_installed_is_satisfied(tmp_path: Path) -> None:
    report = _inspect(
        _pool(tmp_path, capacity_intent="150", installed=(_CAPACITY_FILE,))
    )

    assert report.drifted == ()
    assert _expected(report, _CAPACITY_FILE) == "installed"


def test_intended_capacity_dial_missing_is_drift(tmp_path: Path) -> None:
    report = _inspect(_pool(tmp_path, capacity_intent="150"))

    assert _drifted(report) == {_CAPACITY_FILE}


def test_unintended_capacity_dial_absent_is_satisfied(tmp_path: Path) -> None:
    """Capacity intent encodes "not asked for" as an UNDEFINED macro,
    not as a negation — the check reads both encodings under one rule
    so neither file needs its own interpretation."""
    report = _inspect(_pool(tmp_path, capacity_intent=None))

    assert report.drifted == ()
    assert _expected(report, _CAPACITY_FILE) == "absent"


def test_unintended_capacity_dial_installed_is_drift(tmp_path: Path) -> None:
    report = _inspect(_pool(tmp_path, installed=(_CAPACITY_FILE,)))

    assert _drifted(report) == {_CAPACITY_FILE}


def test_both_policy_files_drift_independently(tmp_path: Path) -> None:
    report = _inspect(
        _pool(tmp_path, backoff_intent="True", installed=(_CAPACITY_FILE,))
    )

    assert _drifted(report) == {_BACKOFF_FILE, _CAPACITY_FILE}


def test_a_pool_with_no_intent_record_is_loud_drift(tmp_path: Path) -> None:
    """A legacy pool — built before the installer recorded intent —
    cannot be judged present-iff-intended at all, and "cannot be
    judged" is itself the finding. Trusting it is what the false green
    was. The three hard settings still pass; only the missing record
    fails, and the remedy says how to restore it.
    """
    values = {key: value for key, value in _HEALTHY.items() if key.startswith(("C", "P"))}
    report = _inspect(
        _stub_tools(
            tmp_path,
            values=values,
            sources=_DEFAULT_SOURCES[:3],
        )
    )

    assert _drifted(report) == {"IO_INTENT_LOAD_BACKOFF"}
    assert _expected(report, "IO_INTENT_LOAD_BACKOFF") == "True or False"
    assert _observed(report, "IO_INTENT_LOAD_BACKOFF") == ""
    # A legacy pool is not judged on the optional files: the check says
    # what it actually knows and no more.
    assert _BACKOFF_FILE not in {
        invariant.knob for invariant in report.invariants
    }
    assert "predates policy-intent records" in report.remedy


# --- malformed intent is drift, never "declared" (N1, #7132 review) ---


def test_the_reviewers_reproduction_bogus_intent_with_the_file_present(
    tmp_path: Path,
) -> None:
    """N1 verbatim: IO_INTENT_LOAD_BACKOFF=Bogus with the 91 file
    installed used to exit 0 ("5 required settings hold"), because any
    value except exactly "False" was read as declared intent. A value
    the installer never writes is not intent — it is a hand-edited or
    corrupt record, and guessing at it either invents an opt-in nobody
    asked for or silently drops one that was."""
    report = _inspect(
        _pool(tmp_path, backoff_intent="Bogus", installed=(_BACKOFF_FILE,))
    )

    assert _drifted(report) == {"IO_INTENT_LOAD_BACKOFF"}
    described = report.drifted[0].describe()
    assert "IO_INTENT_LOAD_BACKOFF" in described
    assert "'Bogus'" in described, "the message must name the malformed value"
    assert "'True or False'" in described


@pytest.mark.parametrize(
    "value",
    [
        "Bogus",
        "yes",
        "1",
        "0",
        # The config tool canonicalizes NOTHING (verified live), so a
        # casing the installer never writes reached the check as-is.
        # Tolerating it would be tolerating a hand-edit.
        "true",
        "TRUE",
        "false",
        "FALSE",
        # Internal whitespace, which condor does NOT strip (it only
        # trims around the value) - so this one is reachable.
        "True False",
    ],
)
def test_any_sentinel_value_outside_the_schema_is_drift(
    tmp_path: Path, value: str
) -> None:
    report = _inspect(
        _pool(tmp_path, backoff_intent=value, installed=(_BACKOFF_FILE,))
    )

    assert _drifted(report) == {"IO_INTENT_LOAD_BACKOFF"}
    assert _observed(report, "IO_INTENT_LOAD_BACKOFF") == value


def test_an_emptied_sentinel_is_indistinguishable_from_a_missing_one(
    tmp_path: Path,
) -> None:
    """`IO_INTENT_LOAD_BACKOFF =` reads back as "Not defined" from the
    real tool — an emptied macro and an absent one are the same
    observation. Both land on the same loud path, so neither can be
    used to quietly disable the check."""
    report = _inspect(_stub_tools(tmp_path, values={**_HEALTHY, "IO_INTENT_LOAD_BACKOFF": ""}))

    assert _drifted(report) == {"IO_INTENT_LOAD_BACKOFF"}
    assert _observed(report, "IO_INTENT_LOAD_BACKOFF") == ""


@pytest.mark.parametrize("value", ["0", "-5", "abc", "007", "1.5", "100%", "5 0"])
def test_any_capacity_value_outside_the_schema_is_drift(
    tmp_path: Path, value: str
) -> None:
    """The installer writes a base-10-normalized positive integer, so
    anything else is a hand-edit. "007" matters most: it is a value
    whose meaning depends on who parses it (7 or 8), and the pool was
    never sized with it."""
    report = _inspect(
        _pool(tmp_path, capacity_intent=value, installed=(_CAPACITY_FILE,))
    )

    assert _drifted(report) == {"IO_INTENT_CAPACITY_PERCENT"}
    described = report.drifted[0].describe()
    assert "IO_INTENT_CAPACITY_PERCENT" in described
    assert repr(value) in described


def test_malformed_intent_suppresses_the_file_judgements_it_governs(
    tmp_path: Path,
) -> None:
    """A record that cannot be trusted for one macro is not trusted
    for the rest: the check reports what it knows (the record is bad)
    and refuses to pronounce on files whose intent it cannot read."""
    report = _inspect(
        _pool(tmp_path, backoff_intent="Bogus", installed=(_BACKOFF_FILE,))
    )

    judged = {invariant.knob for invariant in report.invariants}
    assert _BACKOFF_FILE not in judged
    assert _CAPACITY_FILE not in judged
    # The hard settings are still asserted - they do not depend on intent.
    assert _REQUIRED_KNOBS <= judged


def test_every_malformed_intent_macro_is_named_in_one_pass(
    tmp_path: Path,
) -> None:
    report = _inspect(
        _pool(tmp_path, backoff_intent="maybe", capacity_intent="lots")
    )

    assert _drifted(report) == {
        "IO_INTENT_LOAD_BACKOFF",
        "IO_INTENT_CAPACITY_PERCENT",
    }


def test_a_well_formed_record_still_passes(tmp_path: Path) -> None:
    """The strictness must not reject what the installer writes: both
    boolean states and a canonical dial are accepted."""
    for backoff, capacity, installed in (
        ("True", None, (_BACKOFF_FILE,)),
        ("False", None, ()),
        ("False", "150", (_CAPACITY_FILE,)),
        ("True", "1", (_BACKOFF_FILE, _CAPACITY_FILE)),
    ):
        report = _inspect(
            _pool(
                tmp_path,
                backoff_intent=backoff,
                capacity_intent=capacity,
                installed=installed,
            )
        )
        assert report.drifted == (), (backoff, capacity, installed)


def test_policy_file_presence_follows_what_the_pool_actually_reads(
    tmp_path: Path,
) -> None:
    """A file the pool never parses is not policy, so presence is read
    from the effective source list rather than a directory listing."""
    report = _inspect(
        _pool(tmp_path, backoff_intent="True", installed=(_BACKOFF_FILE,))
    )

    assert _observed(report, _BACKOFF_FILE) == "installed"
    assert _observed(report, _CAPACITY_FILE) == "absent"


def test_report_names_the_configuration_it_read(tmp_path: Path) -> None:
    report = _inspect(_pool(tmp_path))

    assert report.source == "/pool/etc/condor_config"
    assert "condor-personal.sh up" in report.remedy


def test_unreadable_configuration_is_a_backend_fault_not_satisfied_policy(
    tmp_path: Path,
) -> None:
    with pytest.raises(LaneExecutorError, match="configuration sources"):
        _inspect(_stub_tools(tmp_path, config_exit=3))


def test_unparseable_source_listing_fails_loudly(tmp_path: Path) -> None:
    """No indented source paths means the tool's output is not what
    this check parses; guessing "no optional policy" from that would
    silently report a fiction."""
    with pytest.raises(LaneExecutorError, match="no configuration sources"):
        _inspect(_stub_tools(tmp_path, sources=()))


def test_a_failing_setting_read_is_a_backend_fault(tmp_path: Path) -> None:
    """Only "Not defined" means "no value in effect". Any other
    failure — an unreadable config, a tool that cannot start — must
    raise rather than be normalized into an empty observation, which
    would silently satisfy MOUNT_UNDER_SCRATCH."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    script = (
        "#!/bin/sh\n"
        'if [ "$1" = "-config" ]; then\n'
        "  printf 'Configuration source:\\n\\t/pool/etc/condor_config\\n'\n"
        "  exit 0\n"
        "fi\n"
        'echo "Can\'t read config source /pool/etc/condor_config" >&2\n'
        "exit 1\n"
    )
    for name in ("condor_submit", "condor_rm", "condor_q", "condor_config_val"):
        tool = binaries / name
        tool.write_text(script if name == "condor_config_val" else "#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
    tools = CondorTools(
        submit=binaries / "condor_submit",
        remove=binaries / "condor_rm",
        query=binaries / "condor_q",
        config_query=binaries / "condor_config_val",
    )

    with pytest.raises(LaneExecutorError, match="could not read pool setting"):
        _inspect(tools)


def test_a_missing_config_tool_is_a_backend_fault(tmp_path: Path) -> None:
    tools = _pool(tmp_path)
    (tmp_path / "bin" / "condor_config_val").unlink()

    with pytest.raises(LaneExecutorError, match="scheduler tool invocation failed"):
        _inspect(tools)


def test_the_check_rejects_anything_but_resolved_tools() -> None:
    with pytest.raises(ValueError, match="must be CondorTools"):
        CondorPoolPolicyCheck("condor_config_val")  # type: ignore[arg-type]


def _generate_pool_config(tmp_path: Path, **environment: str) -> dict[str, str]:
    """Run the installer's real config writer and read back what it
    wrote across every file it produced."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve().parents[4] / "scripts" / "condor-personal.sh"
    exports = "".join(f"export {key}={value}; " for key, value in environment.items())
    generated = subprocess.run(
        [
            "bash",
            "-c",
            f'{exports}source "{script}" && write_lane_config "$1"',
            "_",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert generated.returncode == 0, generated.stderr
    written: dict[str, str] = {}
    for path in sorted(tmp_path.glob("*.conf")):
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                written[key.strip()] = value.strip()
    return written


def test_the_checked_invariants_are_exactly_what_the_helper_installs(
    tmp_path: Path,
) -> None:
    """Bidirectional drift enforcement, the way the lane declarations
    get it: the settings this check asserts and the settings
    scripts/condor-personal.sh writes are two statements of one
    contract. A check asserting a value the helper never writes would
    fail every pool it built; a helper knob the check ignores is
    unguarded policy. Both directions are held here, from the
    helper's real output rather than a hand-copied list.
    """
    written = _generate_pool_config(tmp_path)
    checked = dict(_REQUIRED_SETTINGS)
    for knob, expected in checked.items():
        assert knob in written, (
            f"the check asserts {knob} but the pool helper never writes it"
        )
        assert written[knob] == expected, (
            f"the check expects {knob}={expected!r} but the helper writes "
            f"{written[knob]!r}"
        )
    # The reverse direction: a lane-compatibility setting the helper
    # writes and this check ignores is policy nothing guards. Settings
    # that are pure latency tuning are named here as a deliberate,
    # reviewed exclusion — losing one makes lanes slower, never wrong.
    latency_tuning = {
        "NEGOTIATOR_INTERVAL",
        "NEGOTIATOR_CYCLE_DELAY",
        "NEGOTIATOR_MIN_INTERVAL",
        "SCHEDD_MIN_INTERVAL",
        "JOB_START_DELAY",
        "JOB_START_COUNT",
        "CLAIM_WORKLIFE",
    }
    intent = {"IO_INTENT_LOAD_BACKOFF", "IO_INTENT_CAPACITY_PERCENT"}
    unguarded = set(written) - latency_tuning - intent - set(checked)
    assert not unguarded, (
        "the pool helper writes a correctness-bearing setting this check "
        f"does not assert: {unguarded}"
    )


def test_the_helper_records_intent_the_check_can_read(tmp_path: Path) -> None:
    """The other half of the same contract: the sentinel the check
    keys on must be written in BOTH opt-in states, and the capacity
    intent must be present exactly when the dial was set. If the
    installer stopped writing the sentinel, every pool it built would
    read as legacy."""
    opted_out = _generate_pool_config(tmp_path)
    assert opted_out["IO_INTENT_LOAD_BACKOFF"] == "False"
    assert "IO_INTENT_CAPACITY_PERCENT" not in opted_out

    opted_in = _generate_pool_config(
        tmp_path / "opted-in",
        IO_CONDOR_LOAD_BACKOFF="1",
        IO_POOL_CAPACITY_PERCENT="150",
    )
    assert opted_in["IO_INTENT_LOAD_BACKOFF"] == "True"
    assert opted_in["IO_INTENT_CAPACITY_PERCENT"] == "150"


def test_the_check_costs_a_handful_of_tool_calls(tmp_path: Path) -> None:
    """Cost is the reason this can run at the head of a gate at all —
    and the reason it must not run per lane. One call per required
    setting, one per intent macro, one for the source listing, and no
    polling loop."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    ledger = tmp_path / "calls.txt"
    script = (
        "#!/bin/sh\n"
        f'echo "$1" >> "{ledger}"\n'
        'if [ "$1" = "-config" ]; then\n'
        "  printf 'Configuration source:\\n\\t/pool/etc/condor_config\\n'\n"
        "  exit 0\n"
        "fi\n"
        'case "$1" in\n'
        '  CONCURRENCY_LIMIT_DEFAULT) echo 1;;\n'
        '  PERIODIC_EXPR_INTERVAL) echo 5;;\n'
        '  IO_INTENT_LOAD_BACKOFF) echo False;;\n'
        '  *) echo "Not defined: $1" >&2; exit 1;;\n'
        "esac\n"
    )
    for name in ("condor_submit", "condor_rm", "condor_q", "condor_config_val"):
        tool = binaries / name
        tool.write_text(script if name == "condor_config_val" else "#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
    tools = CondorTools(
        submit=binaries / "condor_submit",
        remove=binaries / "condor_rm",
        query=binaries / "condor_q",
        config_query=binaries / "condor_config_val",
    )

    _inspect(tools)

    assert ledger.read_text().split() == [
        "-config",
        "CONCURRENCY_LIMIT_DEFAULT",
        "PERIODIC_EXPR_INTERVAL",
        "MOUNT_UNDER_SCRATCH",
        "IO_INTENT_LOAD_BACKOFF",
        "IO_INTENT_CAPACITY_PERCENT",
    ]
