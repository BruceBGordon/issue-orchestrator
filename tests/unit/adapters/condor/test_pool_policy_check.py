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

_HEALTHY = {
    "CONCURRENCY_LIMIT_DEFAULT": "1",
    "PERIODIC_EXPR_INTERVAL": "5",
}
_DEFAULT_SOURCES = (
    "/pool/etc/condor_config",
    "/pool/local/config.d/00-personal-condor",
    "/pool/local/config.d/90-issue-orchestrator-lanes.conf",
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


def _inspect(tools: CondorTools) -> LanePolicyReport:
    return CondorPoolPolicyCheck(tools).inspect()


def _observed(report: LanePolicyReport, knob: str) -> str:
    for invariant in report.invariants:
        if invariant.knob == knob:
            return invariant.observed
    raise AssertionError(f"{knob} is not among the checked invariants")


def test_healthy_pool_reports_three_invariants_and_no_drift(tmp_path: Path) -> None:
    report = _inspect(_stub_tools(tmp_path))

    assert isinstance(report, LanePolicyReport)
    assert report.drifted == ()
    assert {invariant.knob for invariant in report.invariants} == {
        "CONCURRENCY_LIMIT_DEFAULT",
        "PERIODIC_EXPR_INTERVAL",
        "MOUNT_UNDER_SCRATCH",
    }
    # The three are asserted unconditionally: no opt-in, no environment
    # lookup, no way for a caller to shrink the set.
    assert len(report.invariants) == 3


def test_check_instance_satisfies_the_policy_port(tmp_path: Path) -> None:
    check: LanePolicyCheck = CondorPoolPolicyCheck(_stub_tools(tmp_path))
    assert isinstance(check, LanePolicyCheck)


def test_empty_mount_under_scratch_is_satisfied_when_the_tool_says_undefined(
    tmp_path: Path,
) -> None:
    """`MOUNT_UNDER_SCRATCH =` and an unset knob are indistinguishable
    to the tool (verified live: both answer "Not defined"), and mean
    the same thing — no value in effect. The check must agree, or every
    correctly configured pool reads as drifted."""
    report = _inspect(_stub_tools(tmp_path))

    assert _observed(report, "MOUNT_UNDER_SCRATCH") == ""
    assert report.drifted == ()


def test_non_empty_mount_under_scratch_is_drift(tmp_path: Path) -> None:
    """The shipped Linux default; a lane whose working directory lives
    under the real /tmp holds with "Cannot access initial working
    directory" when this comes back."""
    report = _inspect(
        _stub_tools(tmp_path, values={**_HEALTHY, "MOUNT_UNDER_SCRATCH": "/tmp,/var/tmp"})
    )

    assert [invariant.knob for invariant in report.drifted] == [
        "MOUNT_UNDER_SCRATCH"
    ]
    assert "/tmp,/var/tmp" in report.drifted[0].describe()


def test_lost_concurrency_limit_default_is_drift_naming_the_knob(
    tmp_path: Path,
) -> None:
    report = _inspect(
        _stub_tools(tmp_path, values={"PERIODIC_EXPR_INTERVAL": "5"})
    )

    assert [invariant.knob for invariant in report.drifted] == [
        "CONCURRENCY_LIMIT_DEFAULT"
    ]
    described = report.drifted[0].describe()
    assert "CONCURRENCY_LIMIT_DEFAULT" in described
    assert "'1'" in described


def test_changed_concurrency_limit_default_is_drift(tmp_path: Path) -> None:
    report = _inspect(
        _stub_tools(tmp_path, values={**_HEALTHY, "CONCURRENCY_LIMIT_DEFAULT": "4"})
    )

    assert [invariant.knob for invariant in report.drifted] == [
        "CONCURRENCY_LIMIT_DEFAULT"
    ]
    assert _observed(report, "CONCURRENCY_LIMIT_DEFAULT") == "4"


def test_changed_periodic_expr_interval_is_drift(tmp_path: Path) -> None:
    report = _inspect(
        _stub_tools(tmp_path, values={**_HEALTHY, "PERIODIC_EXPR_INTERVAL": "60"})
    )

    assert [invariant.knob for invariant in report.drifted] == [
        "PERIODIC_EXPR_INTERVAL"
    ]
    assert _observed(report, "PERIODIC_EXPR_INTERVAL") == "60"


def test_every_drifted_knob_is_named_in_one_pass(tmp_path: Path) -> None:
    """Drift is data, not an exception: a wholesale-reverted pool is
    fixed in one round, not one knob per gate attempt."""
    report = _inspect(
        _stub_tools(tmp_path, values={"MOUNT_UNDER_SCRATCH": "/tmp"})
    )

    assert {invariant.knob for invariant in report.drifted} == {
        "CONCURRENCY_LIMIT_DEFAULT",
        "PERIODIC_EXPR_INTERVAL",
        "MOUNT_UNDER_SCRATCH",
    }


def test_managed_optional_files_are_reported_never_asserted(
    tmp_path: Path,
) -> None:
    """Their intended state is not knowable at check time (the opt-in
    is an environment variable read once by the installer), so they are
    reported and never fail the check — in either direction."""
    installed = _inspect(
        _stub_tools(
            tmp_path,
            sources=(
                *_DEFAULT_SOURCES,
                "/pool/local/config.d/91-io-load-backoff.conf",
                "/pool/local/config.d/92-io-pool-capacity.conf",
            ),
        )
    )
    absent = _inspect(_stub_tools(tmp_path))

    assert installed.drifted == ()
    assert absent.drifted == ()
    assert {
        observation.name for observation in installed.observations
    } == {"91-io-load-backoff.conf", "92-io-pool-capacity.conf"}
    assert all(
        observation.detail.startswith("in effect")
        for observation in installed.observations
    )
    assert [observation.detail for observation in absent.observations] == [
        "not installed",
        "not installed",
    ]


def test_optional_file_presence_follows_what_the_pool_actually_reads(
    tmp_path: Path,
) -> None:
    """A file the pool never parses is not policy, so presence is read
    from the effective source list rather than a directory listing."""
    report = _inspect(
        _stub_tools(
            tmp_path,
            sources=(*_DEFAULT_SOURCES, "/pool/local/config.d/91-io-load-backoff.conf"),
        )
    )

    details = {
        observation.name: observation.detail for observation in report.observations
    }
    assert details["91-io-load-backoff.conf"].startswith("in effect")
    assert "/pool/local/config.d/91-io-load-backoff.conf" in (
        details["91-io-load-backoff.conf"]
    )
    assert details["92-io-pool-capacity.conf"] == "not installed"


def test_report_names_the_configuration_it_read(tmp_path: Path) -> None:
    report = _inspect(_stub_tools(tmp_path))

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
    tools = _stub_tools(tmp_path)
    (tmp_path / "bin" / "condor_config_val").unlink()

    with pytest.raises(LaneExecutorError, match="scheduler tool invocation failed"):
        _inspect(tools)


def test_the_check_rejects_anything_but_resolved_tools() -> None:
    with pytest.raises(ValueError, match="must be CondorTools"):
        CondorPoolPolicyCheck("condor_config_val")  # type: ignore[arg-type]


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
    script = Path(__file__).resolve().parents[4] / "scripts" / "condor-personal.sh"
    generated = subprocess.run(
        ["bash", "-c", f'source "{script}" && write_lane_config "$1"', "_", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert generated.returncode == 0, generated.stderr
    written = {
        key.strip(): value.strip()
        for key, _, value in (
            line.partition("=")
            for line in (tmp_path / "90-issue-orchestrator-lanes.conf")
            .read_text()
            .splitlines()
            if "=" in line and not line.startswith("#")
        )
    }

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
    assert set(written) - latency_tuning == set(checked), (
        "the pool helper writes a correctness-bearing setting this check "
        f"does not assert: {set(written) - latency_tuning - set(checked)}"
    )


def test_the_check_costs_a_handful_of_tool_calls(tmp_path: Path) -> None:
    """Cost is the reason this can run at the head of a gate at all —
    and the reason it must not run per lane. One call per invariant
    plus one for the source listing, and no polling loop."""
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
    ]
