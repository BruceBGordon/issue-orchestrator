"""Owner for the privileged, test-only Git push authorization used by fixtures.

The orchestrator puts shadow ``git``/``gh`` wrappers on an agent's ``PATH``
(``src/issue_orchestrator/scripts/``). They refuse ``git push`` and the
mutating ``gh`` subcommands unless ``ORCHESTRATOR_GH_AUTH`` carries the
completion-command token — see ``docs/design/guardrails.md``.

Tests that build a throwaway local Git remote under ``tmp_path`` have to push
to it, so they need that same bypass while running inside an orchestrator
session. That bypass is security-sensitive policy, so it lives here rather
than being hand-spelled at each fixture: changing the authorization contract
means editing one module, and a new fixture cannot quietly invent its own
spelling of the token.

Use :func:`authorized_local_fixture_git_env` for the positive path and
:func:`unauthorized_git_env` for the negative control that proves the
wrappers still block.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

# The env var and literal token the wrapper scripts check for. Both are
# asserted against the real wrapper sources in
# ``tests/unit/test_guardrails.py`` so this module cannot drift away from the
# scripts it authorizes against.
GH_AUTH_ENV_VAR = "ORCHESTRATOR_GH_AUTH"
GH_AUTH_TOKEN = "agent-done-authorized"


def _base_env(base: Mapping[str, str] | None, strip: Iterable[str]) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for name in strip:
        env.pop(name, None)
    return env


def authorized_local_fixture_git_env(
    base: Mapping[str, str] | None = None,
    *,
    strip: Iterable[str] = (),
) -> dict[str, str]:
    """Return an environment allowed to push to a local test-fixture remote.

    Args:
        base: Environment to build from. Defaults to a copy of ``os.environ``.
        strip: Variable names to drop first — callers use this to remove the
            ambient ``GIT_*``/``ISSUE_ORCHESTRATOR_*`` session leakage that
            would otherwise point fixture commands at the real repository.

    The returned dict is a fresh copy, so it is safe to mutate further and
    safe under xdist (nothing here touches ``os.environ`` in place).
    """
    env = _base_env(base, strip)
    env[GH_AUTH_ENV_VAR] = GH_AUTH_TOKEN
    return env


def unauthorized_git_env(
    base: Mapping[str, str] | None = None,
    *,
    strip: Iterable[str] = (),
) -> dict[str, str]:
    """Return an environment with the push authorization explicitly removed.

    This is the negative control: the wrappers must still block ``git push``
    and mutating ``gh`` subcommands under this environment.
    """
    env = _base_env(base, strip)
    env.pop(GH_AUTH_ENV_VAR, None)
    return env
