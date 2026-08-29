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

Two further policy files are optional, installed only when their
opt-in was set at install time. They are asserted too, against the
intent record the installer writes in the same pass
(``90-io-policy-intent.conf``, read through this same config channel):
each must be present if and only if it was asked for. A pool carrying
no intent record at all is a pool built before the installer recorded
one — it is reported as drift rather than trusted, because on such a
pool "opted out" and "removed by hand" are indistinguishable.

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
# corresponding opt-in was set at `up` time, paired with the intent
# macro that same run wrote to declare whether it was asked for. Each
# pair is asserted present-iff-intended: a file installed without
# intent is stale policy nobody asked for, and intent without the file
# is policy silently missing — the second of which was reproduced as a
# false green (C1, #7132 review) back when these were merely reported.
_MANAGED_POLICY_FILES: tuple[tuple[str, str], ...] = (
    ("91-io-load-backoff.conf", "IO_INTENT_LOAD_BACKOFF"),
    ("92-io-pool-capacity.conf", "IO_INTENT_CAPACITY_PERCENT"),
)

# The macro whose mere presence proves the pool carries an intent
# record at all. The installer writes it in both states (True/False)
# for exactly this reason, so a pool built before intent records
# existed reads as legacy instead of as "opted out of everything".
_INTENT_SENTINEL = "IO_INTENT_LOAD_BACKOFF"
_INTENT_DECLARED = "True or False"
_NEGATED_INTENT = "False"

_INSTALLED = "installed"
_ABSENT = "absent"

_UNDEFINED_PREFIX = "Not defined:"
_REMEDY = (
    "re-run `scripts/condor-personal.sh up` with the "
    "IO_CONDOR_LOAD_BACKOFF / IO_POOL_CAPACITY_PERCENT opt-ins you intend "
    "this pool to carry (docs/user/condor_lanes.md); an IO_INTENT_* knob "
    "reported as '' means the pool predates policy-intent records and must "
    "be rebuilt that way before the gate will dispatch"
)


def _declares_intent(value: str) -> bool:
    """Whether an intent macro asks for its policy to be installed.

    One rule reads both encodings the installer writes — a boolean for
    the backoff policy, a number-or-nothing for the capacity dial — so
    neither file needs its own interpretation here: intent is declared
    when the macro carries a value and that value is not a negation.
    """
    return value not in ("", _NEGATED_INTENT)


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
        required = tuple(
            LanePolicyInvariant(
                knob=knob, expected=expected, observed=self._setting(knob)
            )
            for knob, expected in _REQUIRED_SETTINGS
        )
        return LanePolicyReport(
            source=sources[0],
            remedy=_REMEDY,
            invariants=required + self._intent_invariants(sources),
        )

    def _intent_invariants(
        self, sources: tuple[str, ...]
    ) -> tuple[LanePolicyInvariant, ...]:
        """Assert each managed policy file is present iff it was intended.

        A pool with no intent record cannot be judged this way, and
        "cannot be judged" is itself drift: it is a pool built before
        the installer recorded intent, so nothing here can tell its
        opted-out policy from its silently-removed policy. It is
        reported loudly as one invariant naming the missing record,
        which the remedy tells the operator how to restore.
        """
        declared = {knob: self._setting(knob) for _, knob in _MANAGED_POLICY_FILES}
        if declared[_INTENT_SENTINEL] == "":
            return (
                LanePolicyInvariant(
                    knob=_INTENT_SENTINEL,
                    expected=_INTENT_DECLARED,
                    observed="",
                ),
            )
        installed = {Path(source).name for source in sources}
        return tuple(
            LanePolicyInvariant(
                knob=name,
                expected=(
                    _INSTALLED if _declares_intent(declared[knob]) else _ABSENT
                ),
                observed=_INSTALLED if name in installed else _ABSENT,
            )
            for name, knob in _MANAGED_POLICY_FILES
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
