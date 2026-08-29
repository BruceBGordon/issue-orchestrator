"""Pool-policy self-check against a live personal pool.

The unit suite proves the check's judgment against a stubbed config
tool; only this proves it reads the REAL tool correctly — the output
shapes it parses (an empty-valued knob answering "Not defined", the
source listing whose heading is singular or plural depending on how
many files there are, a custom ``IO_INTENT_*`` macro round-tripping
verbatim) are the tool's, not ours, and a stub can agree with a wrong
assumption forever.

These assert the pool is HEALTHY, not merely that the check runs.
That is deliberate, and it has a consequence worth stating plainly: a
pool started before this branch carries no policy-intent record, so it
reads as a legacy pool and these fail until the operator re-runs
``scripts/condor-personal.sh up``. The alternative — asserting the
legacy path here — would encode today's stale pool as the expected
state and go green forever on exactly the drift this check exists to
catch. The hermetic legacy case is covered in
``tests/unit/adapters/condor/test_pool_policy_check.py`` instead, where
it costs nobody a live pool.

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
from issue_orchestrator.domain.lane_execution import LanePolicyReport
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
_MANAGED_POLICY_FILES = {
    "91-io-load-backoff.conf",
    "92-io-pool-capacity.conf",
}
_REBUILD = (
    "\n\nIf IO_INTENT_LOAD_BACKOFF is reported as '', this pool predates "
    "policy-intent records: re-run `scripts/condor-personal.sh up` with the "
    "opt-ins it should carry. That restarts the startd, so do it between "
    "gates, never during one."
)


def _check() -> LanePolicyCheck:
    return CondorPoolPolicyCheck(CondorTools.resolve())


def _drift_report(report: LanePolicyReport) -> str:
    return (
        "the live pool has drifted from the policy lanes depend on: "
        + "; ".join(invariant.describe() for invariant in report.drifted)
        + _REBUILD
    )


def test_the_live_pool_carries_its_designed_policy() -> None:
    """The acceptance statement: a pool this repo's helper brought up
    satisfies every invariant the lane contracts depend on. A failure
    here is a real finding about the pool, not about the test."""
    report = _check().inspect()

    assert report.drifted == (), _drift_report(report)


def test_the_live_pool_declares_and_matches_its_optional_policy() -> None:
    """Present-iff-intended, on the real thing: the intent record the
    installer wrote must be readable through the config channel, and
    each managed file must match what it declares. Without the record
    the check falls back to the legacy invariant and these names are
    absent — which is the failure this asserts against."""
    report = _check().inspect()

    checked = {invariant.knob for invariant in report.invariants}
    assert _REQUIRED_KNOBS <= checked
    assert _MANAGED_POLICY_FILES <= checked, (
        "the pool reported no usable policy-intent record, so the optional "
        f"files were not judged at all (checked: {sorted(checked)})" + _REBUILD
    )
    for invariant in report.invariants:
        if invariant.knob in _MANAGED_POLICY_FILES:
            assert invariant.observed in ("installed", "absent")
            assert invariant.satisfied, invariant.describe() + _REBUILD


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


def test_the_report_names_the_pool_it_read() -> None:
    report = _check().inspect()

    assert report.source.endswith("condor_config"), report.source
    assert "condor-personal.sh up" in report.remedy


def test_the_check_is_cheap_enough_to_run_at_the_head_of_every_gate() -> None:
    """The whole design rests on this: if the check were expensive, the
    gate would have to cache or conditionalize it, and a stale answer
    is worse than no answer. Measured on the Rosetta macOS pool, six
    config reads land well inside this bound."""
    started = time.monotonic()
    _check().inspect()
    elapsed = time.monotonic() - started

    assert elapsed < 10.0, f"pool-policy check took {elapsed:.1f}s"


def test_the_cli_passes_against_the_live_pool() -> None:
    """End to end through the entrypoint the gate actually invokes."""
    assert main(["--backend", "condor"]) == 0, (
        "lane-preflight refused the live pool" + _REBUILD
    )
