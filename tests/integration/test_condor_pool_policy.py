"""Pool-policy self-check against a live personal pool.

The unit suite proves the check's judgment against a stubbed config
tool; only this proves it reads the REAL tool correctly — the output
shapes it parses (an empty-valued knob answering "Not defined", the
two-heading source listing) are the tool's, not ours, and a stub can
agree with a wrong assumption forever.

Requires a reachable personal pool (``scripts/condor-personal.sh up``).
Marked ``requires_infra`` for the same reason the executor suite is:
the backend is opt-in, so these run in the dedicated condor CI job and
on developer machines with a pool — never silently skipped inside the
default gate, simply not selected by it.
"""

from __future__ import annotations

import time

import pytest

from issue_orchestrator.adapters.condor import CondorPoolPolicyCheck, CondorTools
from issue_orchestrator.entrypoints.cli_tools.lane_preflight import main
from issue_orchestrator.ports.lane_policy_check import LanePolicyCheck

pytestmark = [
    pytest.mark.timeout(120),
    pytest.mark.requires_infra,
]

_REQUIRED_KNOBS = {
    "CONCURRENCY_LIMIT_DEFAULT",
    "PERIODIC_EXPR_INTERVAL",
    "MOUNT_UNDER_SCRATCH",
}


def _check() -> LanePolicyCheck:
    return CondorPoolPolicyCheck(CondorTools.resolve())


def test_the_live_pool_carries_its_designed_policy() -> None:
    """The acceptance statement: a pool this repo's helper brought up
    satisfies every invariant the lane contracts depend on. A failure
    here is a real finding about the pool, not about the test."""
    report = _check().inspect()

    assert {invariant.knob for invariant in report.invariants} == _REQUIRED_KNOBS
    assert report.drifted == (), (
        "the live pool has drifted from the policy lanes depend on: "
        + "; ".join(invariant.describe() for invariant in report.drifted)
    )


def test_the_real_tool_reports_an_empty_valued_knob_as_no_value() -> None:
    """`MOUNT_UNDER_SCRATCH =` in the pool's config file reads back as
    "Not defined" from the real tool (macOS and Linux alike). The check
    normalizes that to "no value in effect" — if this assumption were
    wrong, every correctly configured pool would read as drifted."""
    report = _check().inspect()

    observed = {
        invariant.knob: invariant.observed for invariant in report.invariants
    }
    assert observed["MOUNT_UNDER_SCRATCH"] == ""


def test_the_report_names_the_pool_it_read_and_its_optional_policy() -> None:
    report = _check().inspect()

    assert report.source.endswith("condor_config"), report.source
    assert {observation.name for observation in report.observations} == {
        "91-io-load-backoff.conf",
        "92-io-pool-capacity.conf",
    }
    for observation in report.observations:
        assert observation.detail == "not installed" or observation.detail.startswith(
            "in effect"
        ), observation


def test_the_check_is_cheap_enough_to_run_at_the_head_of_every_gate() -> None:
    """The whole design rests on this: if the check were expensive, the
    gate would have to cache or conditionalize it, and a stale answer
    is worse than no answer. Measured on the Rosetta macOS pool, four
    config reads land well inside this bound."""
    started = time.monotonic()
    _check().inspect()
    elapsed = time.monotonic() - started

    assert elapsed < 10.0, f"pool-policy check took {elapsed:.1f}s"


def test_the_cli_passes_against_the_live_pool() -> None:
    """End to end through the entrypoint the gate actually invokes."""
    assert main(["--backend", "condor"]) == 0
