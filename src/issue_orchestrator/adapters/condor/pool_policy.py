# pyright: strict
"""Pool-policy self-check for the HTCondor lane backend.

The lane contracts are written against three pool settings. Each is a
silent degradation when it goes missing — the pool keeps accepting
work and keeps reporting lanes as completed:

- ``CONCURRENCY_LIMIT_DEFAULT = 1`` makes every named concurrency limit
  a machine-wide mutex. Lose it and exclusive lane resources (a
  provider account tolerating one concurrent login) stop being
  exclusive; lanes overlap and fail for reasons that look like the
  provider's fault.
- ``PERIODIC_EXPR_INTERVAL = 5`` bounds how far past its deadline a
  lane can run before removal. Lose it and the executor's
  deadline-plus-slack watchdog starts firing as a "backend
  unresponsive" fault.
- ``MOUNT_UNDER_SCRATCH`` empty keeps the job's private ``/tmp`` off.
  Lose it (the shipped default on Linux is ``/tmp,/var/tmp``) and every
  lane whose working directory lives under the real ``/tmp`` holds with
  "Cannot access initial working directory".

Scope: this reads the pool's effective *configuration*, which is what
the daemons read. Daemons still running a configuration older than the
files on disk are out of scope — the pool helper restarts them when it
rewrites policy.
"""

from __future__ import annotations

from pathlib import Path

from ...domain.lane_execution import (
    LaneExecutorError,
    LanePolicyInvariant,
    LanePolicyObservation,
    LanePolicyReport,
)
from .lane_executor import CondorTools

# The three hard invariants, asserted unconditionally. Values are
# compared as the config tool prints them, after the normalization in
# ``_setting`` below.
_REQUIRED_SETTINGS: tuple[tuple[str, str], ...] = (
    ("CONCURRENCY_LIMIT_DEFAULT", "1"),
    ("PERIODIC_EXPR_INTERVAL", "5"),
    ("MOUNT_UNDER_SCRATCH", ""),
)

# Policy files scripts/condor-personal.sh installs only when its
# corresponding opt-in was set at `up` time. Their intended state is
# NOT knowable here: the opt-in is an environment variable read once,
# at install time, by a different process — nothing on the pool records
# whether it was set. Asserting presence would fail every correct pool
# that opted out; asserting absence would bless every pool that opted
# in. So they are REPORTED (present or not, and from which file), which
# is what makes a hand-removed backoff policy visible in the gate log
# without inventing an intent the check cannot verify.
_MANAGED_OPTIONAL_CONFIGS: tuple[str, ...] = (
    "91-io-load-backoff.conf",
    "92-io-pool-capacity.conf",
)

_UNDEFINED_PREFIX = "Not defined:"
_REMEDY = (
    "re-apply the pool policy with `scripts/condor-personal.sh up` "
    "(docs/user/condor_lanes.md), then re-run the gate"
)


class CondorPoolPolicyCheck:
    """Read the pool's effective configuration and report policy drift.

    Cost is one config-tool invocation per required setting plus one
    for the configuration source list — four short-lived local
    processes, measured at well under a second even on the Rosetta
    macOS pool. That is why the gate can afford it unconditionally,
    and why it must run once per gate rather than once per lane.
    """

    def __init__(self, tools: CondorTools) -> None:
        if type(tools) is not CondorTools:
            raise ValueError("CondorPoolPolicyCheck.tools must be CondorTools")
        self._tools = tools

    def inspect(self) -> LanePolicyReport:
        sources = self._configuration_sources()
        return LanePolicyReport(
            source=sources[0],
            remedy=_REMEDY,
            invariants=tuple(
                LanePolicyInvariant(
                    knob=knob, expected=expected, observed=self._setting(knob)
                )
                for knob, expected in _REQUIRED_SETTINGS
            ),
            observations=_optional_policy_files(sources),
        )

    def _setting(self, knob: str) -> str:
        """The value in effect for one knob, or ``""`` when it has none.

        The tool reports an unset knob and one assigned the empty
        string identically ("Not defined"), and so does this: both mean
        no value is in effect, which is exactly what
        ``MOUNT_UNDER_SCRATCH`` must be. Any *other* non-zero exit is a
        backend fault and is raised — a pool that cannot be read must
        never be reported as satisfying its policy.
        """
        completed = self._tools.invoke((str(self._tools.config_query), knob))
        if completed.returncode == 0:
            return completed.stdout.strip()
        diagnostic = (completed.stderr.strip() or completed.stdout.strip()) or (
            "no diagnostic"
        )
        if diagnostic.startswith(_UNDEFINED_PREFIX):
            return ""
        raise LaneExecutorError(
            f"could not read pool setting {knob}: {diagnostic}"
        )

    def _configuration_sources(self) -> tuple[str, ...]:
        """Every configuration file the pool actually reads, in order.

        The truth about which optional policy is live is the file list
        the pool parses, not what happens to sit in a directory: a file
        the pool never reads is not policy.
        """
        completed = self._tools.invoke((str(self._tools.config_query), "-config"))
        if completed.returncode != 0:
            diagnostic = (
                completed.stderr.strip() or completed.stdout.strip()
            ) or "no diagnostic"
            raise LaneExecutorError(
                f"could not read the pool's configuration sources: {diagnostic}"
            )
        # Source paths are the indented entries under the two headings;
        # the headings themselves start at column zero.
        sources = tuple(
            line.strip()
            for line in completed.stdout.splitlines()
            if line[:1].isspace() and line.strip()
        )
        if not sources:
            raise LaneExecutorError(
                "the pool reported no configuration sources: "
                f"{completed.stdout.strip()!r}"
            )
        return sources


def _optional_policy_files(
    sources: tuple[str, ...],
) -> tuple[LanePolicyObservation, ...]:
    by_name = {Path(source).name: source for source in sources}
    return tuple(
        LanePolicyObservation(
            name=managed,
            detail=(
                f"in effect ({by_name[managed]})"
                if managed in by_name
                else "not installed"
            ),
        )
        for managed in _MANAGED_OPTIONAL_CONFIGS
    )
