# pyright: strict
"""Locating the scheduler's tools, and the one boundary that invokes them.

Every adapter in this package needs the same two things: the absolute
path of a scheduler tool, and a way to run it under the configuration
that lanes are actually submitted to. The executor submits and follows
jobs, the policy check reads the pool's effective configuration, and the
pool inspector reads capacity and queue — so resolution and invocation
belong to the package rather than to whichever adapter happened to need
them first.

The environment rule differs by question, and the asymmetry is the
design: see :meth:`CondorTools.invoke` and
:meth:`CondorTools.read_configuration`. Both take a plain
``timeout_seconds`` rather than a budget object, so the caller that owns
an operation owns how its allowance is divided.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ...domain.lane_execution import (
    LaneExecutorError,
    LaneExecutorUnavailableError,
)

_TOOL_TIMEOUT_SECONDS = 30.0

# Where scripts/condor-personal.sh installs the personal pool. Resolving
# it here means a config-file opt-in works from any process — the
# orchestrator's validation runner, a bare shell, a hook — without every
# caller having to source the pool's environment first.
PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE = "ISSUE_ORCHESTRATOR_CONDOR_HOME"
_DEFAULT_PERSONAL_POOL_HOME = Path.home() / ".local/share/issue-orchestrator/condor"
# The scheduler's per-process macro override prefix: `_CONDOR_<KNOB>`
# in the environment overrides <KNOB> for that process only, never for
# the daemons. Scrubbed on the configuration-READ path (an answer about
# the pool must come from the pool) and deliberately preserved on the
# submit path (`getenv = true` carries it to the lane).
#
# The scheduler matches this prefix CASE-INSENSITIVELY while POSIX
# environments are case-SENSITIVE, so `_condor_X` and `_CoNdOr_X` are
# distinct variables that the tool nonetheless honours identically
# (verified live: all four casings injected). Matching must therefore
# be case-insensitive too, or the scrub is a lowercase bypass away
# from useless (round 4, #7132 review).
_MACRO_OVERRIDE_PREFIX = "_CONDOR_"


def _is_macro_override(name: str) -> bool:
    return name.upper().startswith(_MACRO_OVERRIDE_PREFIX)


_TOOL_EXECUTABLES = (
    ("submit", "condor_submit"),
    ("remove", "condor_rm"),
    ("query", "condor_q"),
    ("config_query", "condor_config_val"),
    ("pool_query", "condor_status"),
)


@dataclass(frozen=True, slots=True)
class CondorTools:
    """Absolute paths to the scheduler's command-line tools, and the
    single boundary through which this package invokes them.

    ``pool_config`` is the configuration file the tools must use; it is
    ``None`` for a system installation whose ambient configuration is
    already correct, and set when the tools come from the personal-pool
    install, whose configuration lives beside its binaries. Because
    every tool invocation must run under that configuration, invocation
    belongs here rather than in each caller: :meth:`invoke` is the only
    way this package runs a scheduler tool, so a caller cannot
    accidentally read a different pool than the one lanes are submitted
    to.

    ``config_query`` reads the pool's effective configuration and is
    what the policy self-check consults; ``pool_query`` reads the
    machines and slots the pool is made of, which is what the
    operator-facing pool snapshot reports as capacity.
    """

    submit: Path
    remove: Path
    query: Path
    config_query: Path
    pool_query: Path
    pool_config: Path | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("submit", self.submit),
            ("remove", self.remove),
            ("query", self.query),
            ("config_query", self.config_query),
            ("pool_query", self.pool_query),
        ):
            if not isinstance(cast(object, value), Path) or not value.is_absolute():
                raise ValueError(f"CondorTools.{field_name} must be an absolute Path")
        if self.pool_config is not None and (
            not isinstance(cast(object, self.pool_config), Path)
            or not self.pool_config.is_absolute()
        ):
            raise ValueError("CondorTools.pool_config must be an absolute Path")

    @classmethod
    def resolve(cls) -> CondorTools:
        """Resolve from PATH first, then the personal-pool install.

        Fails loudly when neither exists: the backend is opt-in and a
        configured-but-missing pool must never degrade silently.
        """
        from_path = cls._resolve_from_path()
        if from_path is not None:
            return from_path
        from_personal = cls._resolve_from_personal_install()
        if from_personal is not None:
            return from_personal
        raise LaneExecutorUnavailableError(
            "no scheduler tools on PATH and no personal pool under "
            f"{cls._personal_pool_home()}: the condor lane backend is opt-in "
            "and requires a running HTCondor pool "
            "(run scripts/condor-personal.sh up, see docs/user/condor_lanes.md)"
        )

    @classmethod
    def _resolve_from_path(cls) -> CondorTools | None:
        located: dict[str, Path] = {}
        for field_name, executable in _TOOL_EXECUTABLES:
            found = shutil.which(executable)
            if found is None:
                return None
            located[field_name] = Path(found).resolve()
        return cls(**located)

    @classmethod
    def _resolve_from_personal_install(cls) -> CondorTools | None:
        home = cls._personal_pool_home()
        for install in sorted(home.glob("condor-*"), reverse=True):
            binaries = install / "bin"
            pool_config = install / "etc" / "condor_config"
            located: dict[str, Path] = {}
            for field_name, executable in _TOOL_EXECUTABLES:
                candidate = binaries / executable
                if not candidate.is_file() or not os.access(candidate, os.X_OK):
                    located.clear()
                    break
                located[field_name] = candidate.resolve()
            if located and pool_config.is_file():
                return cls(pool_config=pool_config.resolve(), **located)
        return None

    @staticmethod
    def _personal_pool_home() -> Path:
        override = os.environ.get(PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE)
        if override:
            return Path(override)
        return _DEFAULT_PERSONAL_POOL_HOME

    def invoke(
        self,
        arguments: tuple[str, ...],
        timeout_seconds: float = _TOOL_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        """Run one scheduler tool against this pool, bounded in time.

        ``timeout_seconds`` defaults to the general bound and is passed
        explicitly by callers running under an allowance of their own —
        a cancelling lane's wind-down spends ONE budget across every
        stage, so a tool invocation inside it cannot quietly take the
        general timeout instead (#7135 round 3).

        The caller's environment is passed through, deliberately. The
        submit description sets ``getenv = true``, so the environment
        this process hands to ``condor_submit`` is the environment the
        LANE ITSELF inherits — carrying it faithfully is the contract,
        not an oversight, and quietly deleting a category of variables
        from it would surprise whoever set them.

        A non-zero return code is the caller's to interpret — tools use
        it for ordinary answers as well as failures. Only an
        invocation that never produced one (missing binary, hung tool)
        is a backend fault.
        """
        return self._run(
            arguments,
            scrub_macro_overrides=False,
            timeout_seconds=timeout_seconds,
        )

    def read_configuration(
        self,
        *query: str,
        timeout_seconds: float = _TOOL_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        """Ask the pool what its own configuration says.

        Deliberately asymmetric with :meth:`invoke`, and the asymmetry
        is the point. ``_CONDOR_<KNOB>`` overrides <KNOB> for one
        process and is invisible to the DAEMONS, so an answer read
        through one describes the caller's environment rather than the
        pool — an ambient export could mask real drift (verified: the
        tool answers "Not defined" for a knob the pool genuinely sets
        wrong) or manufacture fake drift. A question asked ABOUT the
        pool must be answered BY the pool, so overrides are scrubbed
        here and only here (residual on N1, #7132 review).

        Taking the query rather than a full argv is part of the same
        guarantee: this path always runs the configuration tool, and
        no submission can be routed through it by mistake.
        """
        return self._run(
            (str(self.config_query), *query),
            scrub_macro_overrides=True,
            timeout_seconds=timeout_seconds,
        )

    def _run(
        self,
        arguments: tuple[str, ...],
        *,
        scrub_macro_overrides: bool,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        # A non-positive timeout is not clamped away: it means the
        # caller's budget is already spent, and subprocess raises
        # TimeoutExpired promptly, which becomes the LaneExecutorError
        # the caller already reports. Silently granting more time would
        # be the fallback that made the bound a lie.
        environment = dict(os.environ)
        if scrub_macro_overrides:
            environment = {
                key: value
                for key, value in environment.items()
                if not _is_macro_override(key)
            }
        if self.pool_config is not None:
            environment["CONDOR_CONFIG"] = str(self.pool_config)
        try:
            return subprocess.run(
                arguments,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LaneExecutorError(
                f"scheduler tool invocation failed: {arguments[0]}: {error!r}"
            ) from error
